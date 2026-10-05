"""回复风格审核（可选 agent）：**只判断，不改写**；打回之后由回复 agent 重写一次。

2026-10-05 用户定的口径：*"审核只负责打回，不负责修改"*。在这之前 `review()` 返回
**改完的文本**（`REVIEW_SYSTEM_PROMPT` 自称"校对"、末尾写着"只改这四类"），
也就是说**风格审核在直接改写她的话**——这是用户报的"她的回复风格变得很奇怪、很 ai"
里被定位到的第二个原因（第一个是常驻 system 里那段「不懂就问」的规矩，
见 `tests/test_ask_when_unsure.py`）。

所以这个文件现在钉四类东西：

1. `review()` 的形状：`(是否通过, 理由)`，**第二项永远不是她的台词**；
2. 打回之后：**同一轮里重写一次**（最多一次，不许循环）；
   重写后仍被拒 → **发重写的那一版**（不许丢回复、不许卡住），并记一笔；
3. 任何不对劲（超时 / 报错 / 输出认不出 / 模型又想改写）→ **按通过**处理（fail-open）；
4. prompt 只授权它判断四类（事实错误 / 越权 / 出戏 / 伤人），
   **不许它去磨她的脾气与情绪**，也不许它产出改写后的句子。

历史（"窄职责改写"那套 10 条改 5 条、数字机械兜底）已经作废：审核不再产出文本，
所以"改坏事实"由**结构**排除，`keeps_the_facts` / `within_growth_limit` 两条
机械规则随改写路径一起删掉了。
"""
import asyncio
import unittest

from qq_roleplay_bot.runtime_flags import RuntimeFlags
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.style_reviewer import (
    MAX_REASON_CHARS,
    REVIEW_SYSTEM_PROMPT,
    StyleReviewer,
    parse_review_output,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

# 本机配置在不在的判定（干净 clone 只有 `.env.example`）——见 `tests/config_support.py`
from config_support import skip_unless_review_enabled

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def message(mid="m1", text="在吗", user="100", mentioned=True) -> IncomingMessage:
    return IncomingMessage(mid, f"group:{GROUP}", user, text, TARGET, mentioned, sender_name="某人")


class _Client:
    """审核 agent 的替身：按脚本返回；也可以抛错（模拟超时/挂了）。

    可以给一串输出（按调用顺序取），也可以给一个字符串（每次都返回它）。
    """

    def __init__(self, output="", *, error=None):
        self.outputs = list(output) if isinstance(output, list) else [output]
        self.error = error
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class _Reply:
    """回复 agent 的替身：可以按顺序给多版正文；也可以让某一次抛错。"""

    def __init__(self, *texts, error_on=None):
        self.texts = list(texts) or ["我接一句。"]
        self.error_on = set(error_on or ())
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        index = len(self.requests)
        if index in self.error_on:
            raise RuntimeError("回复这一趟挂了")
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.texts[min(index - 1, len(self.texts) - 1)]}</reply>")


def _engine(reply, reviewer=None, *, review=True) -> DialogueEngine:
    """装一台引擎，并把 `review_enabled` 钉死。

    用**实例级替身**而不是 `runtime_flags.install`：这条测试不该跟着进程里
    别的测试装的全局开关走（否则跑单条与跑全量结果不同）。
    """

    engine = DialogueEngine(reply, style_reviewer=reviewer)
    flags = RuntimeFlags(review_enabled=review)
    engine._flags = lambda: flags
    return engine


def _last_note(reply: _Reply) -> str:
    """回复 agent 收到的**最后一条 user 消息**（重写那一次的现场提示）。"""

    return reply.requests[-1][-1]["content"]


class StyleReviewerTests(unittest.TestCase):
    def test_pass_lets_the_draft_through(self) -> None:
        reviewer = StyleReviewer(_Client("PASS"))
        passed, reason = asyncio.run(reviewer.review("她今天不来了。"))
        self.assertTrue(passed)
        self.assertEqual(reason, "")
        self.assertEqual(reviewer.stats["calls"], 1)
        self.assertEqual(reviewer.stats["blocked"], 0)

    def test_block_returns_only_a_verdict_and_a_reason(self) -> None:
        """打回的返回值里**只有判定与理由**——一个字的台词都不许夹带。"""

        draft = "你要的是小数比大小，一开始说清楚，三秒钟的事。"
        reviewer = StyleReviewer(_Client("BLOCK: 越权——她站到讲台上替对方定了该怎么比。"))
        result = asyncio.run(reviewer.review(draft))
        self.assertEqual(result, (False, "越权——她站到讲台上替对方定了该怎么比。"))
        passed, reason = result
        self.assertFalse(passed)
        self.assertNotIn(draft, reason)
        self.assertLessEqual(len(reason), MAX_REASON_CHARS)
        self.assertEqual(reviewer.stats["blocked"], 1)

    def test_reason_can_be_a_chinese_verdict_line(self) -> None:
        reviewer = StyleReviewer(_Client("不通过：伤人了——拿他的私事开刀。"))
        passed, reason = asyncio.run(reviewer.review("原稿。"))
        self.assertFalse(passed)
        self.assertEqual(reason, "伤人了——拿他的私事开刀。")

    def test_a_reviewer_that_rewrites_instead_of_judging_is_not_obeyed(self) -> None:
        """**它想改写也不算数**：输出不是判定行 → 按通过处理，并记一笔退化。

        这是这次改动最要紧的一条：审核的文本永远不成为她的话。
        """

        draft = "是负的。前面那个字你没看。"
        reviewer = StyleReviewer(_Client("是负的。"))
        passed, reason = asyncio.run(reviewer.review(draft))
        self.assertEqual((passed, reason), (True, ""))
        self.assertEqual(reviewer.stats["unrecognized"], 1)
        self.assertEqual(reviewer.stats["blocked"], 0)

    def test_reviewer_failure_passes_the_draft(self) -> None:
        """审核挂了 = 少一次打回，绝不等于"她的话被挡住"。"""

        reviewer = StyleReviewer(_Client(error=RuntimeError("timeout")))
        self.assertEqual(asyncio.run(reviewer.review("原稿。")), (True, ""))
        self.assertEqual(reviewer.stats["failed"], 1)

    def test_empty_review_passes_the_draft(self) -> None:
        reviewer = StyleReviewer(_Client("   "))
        self.assertEqual(asyncio.run(reviewer.review("原稿。")), (True, ""))
        self.assertEqual(reviewer.stats["unrecognized"], 1)

    def test_no_client_means_pass_through(self) -> None:
        reviewer = StyleReviewer(None)
        self.assertEqual(asyncio.run(reviewer.review("原稿。")), (True, ""))
        self.assertFalse(reviewer.enabled)
        self.assertEqual(reviewer.stats["calls"], 0)

    def test_blank_draft_is_not_reviewed(self) -> None:
        client = _Client("BLOCK: 什么都不是")
        reviewer = StyleReviewer(client)
        self.assertEqual(asyncio.run(reviewer.review("   ")), (True, ""))
        self.assertEqual(client.requests, [], "空草稿不该花一次调用")

    def test_the_reason_is_passed_the_context_but_never_her_draft_back(self) -> None:
        """交给审核的是现场 + 草稿；它**只回判定行**，不回台词。"""

        client = _Client("PASS")
        reviewer = StyleReviewer(client)
        asyncio.run(reviewer.review("原稿。", context="上一句：在吗"))
        body = client.requests[0][1]["content"]
        self.assertIn("原稿。", body)
        self.assertIn("上一句：在吗", body)

    def test_prompt_forbids_rewriting_and_forbids_explaining(self) -> None:
        """prompt 必须明写"只判断、不改写"——否则模型会又去替她写一句。"""

        self.assertIn("只判断，不改写", REVIEW_SYSTEM_PROMPT)
        self.assertIn("由写这句话的人自己重写", REVIEW_SYSTEM_PROMPT)
        self.assertIn("不要给出改好的句子", REVIEW_SYSTEM_PROMPT)
        self.assertIn("PASS", REVIEW_SYSTEM_PROMPT)
        self.assertIn("BLOCK", REVIEW_SYSTEM_PROMPT)
        # 自称"校对"、"只改这四类"是改写时代的说法，不许回来。
        self.assertNotIn("只改这四类", REVIEW_SYSTEM_PROMPT)
        self.assertNotIn("你是一段台词的**校对**", REVIEW_SYSTEM_PROMPT)

    def test_prompt_covers_the_preaching_criterion(self) -> None:
        """说教/上课（2026-09-30 用户："增加对说教语气的审核"）。

        样本是她自己的真实回复（`data/style_preach_ab.py` 里那批），
        所以这条判据必须点名"给建议""讲道理""评价对方该怎么想""科普讲解""大道理"，
        并且给出**照着判**的真例子。2026-10-04 判据收成四类（事实/越权/出戏/伤人），
        说教并进第 2 类"越权"，例子逐字保留；2026-10-05 它从"照着删"改成"照着判"，
        所以这里的锚点是"整句都在教对方"那一条自检。
        四类的授权范围另有守卫：`tests/test_persona_shape.py` 里
        `test_review_prompt_keeps_emotion_and_only_blocks_four_kinds`。
        """

        self.assertIn("越权", REVIEW_SYSTEM_PROMPT)
        self.assertIn("站到讲台上了", REVIEW_SYSTEM_PROMPT)
        for marker in ("我建议你", "其实", "你想多了", "早晚会", "别想太多"):
            self.assertIn(marker, REVIEW_SYSTEM_PROMPT, marker)
        self.assertIn("教对方", REVIEW_SYSTEM_PROMPT, "自检那句被拿掉了")
        self.assertIn("有人连名字都没认全就得投票", REVIEW_SYSTEM_PROMPT)


class ParseReviewOutputTests(unittest.TestCase):
    def test_readable_verdicts(self) -> None:
        self.assertEqual(parse_review_output("PASS"), (True, ""))
        self.assertEqual(parse_review_output("通过"), (True, ""))
        self.assertEqual(parse_review_output("BLOCK: 出戏了。"), (False, "出戏了。"))
        self.assertEqual(parse_review_output("不通过：事实错了。"), (False, "事实错了。"))

    def test_unreadable_output_is_a_pass(self) -> None:
        """认不出来一律按通过：审核自己出问题，代价只能是"少一次重写"。"""

        for raw in ("", "   ", "我改好了：她今天不来。", "这句不太好吧？"):
            self.assertEqual(parse_review_output(raw), (True, ""), raw)

    def test_wrappers_are_stripped(self) -> None:
        self.assertEqual(parse_review_output("```\nBLOCK: 越权。\n```"), (False, "越权。"))


class EngineReviewWiringTests(unittest.TestCase):
    def test_a_rejected_reply_is_rewritten_once_in_the_same_turn(self) -> None:
        reviewer = StyleReviewer(_Client(["BLOCK: 越权——替对方定了对错。", "PASS"]))
        reply = _Reply("你要的是小数比大小，三秒钟的事。", "0.8 大。")
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 3.11 和 0.8 哪个大")))

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "0.8 大。", "打回之后要发的是**重写**的那一版")
        self.assertEqual(len(reply.requests), 2, "同一轮里重写一次")
        # 重写那一次的现场提示：既有理由，也有她刚写的那一版
        note = _last_note(reply)
        self.assertIn("越权——替对方定了对错。", note)
        self.assertIn("你要的是小数比大小，三秒钟的事。", note)
        # 重写走的是**同一份请求 + 一条追加消息**：system 一个字没动
        self.assertEqual(reply.requests[0][0]["content"], reply.requests[1][0]["content"])
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.reply_reviewed, 1)
        self.assertEqual(snapshot.reply_review_rejected_final, 0)

    def test_a_rewrite_that_is_rejected_again_is_still_sent(self) -> None:
        """重写后仍被拒 → **发重写的那一版**，最多一次、不许循环、不许丢掉。"""

        reviewer = StyleReviewer(_Client("BLOCK: 还是越权。"))
        reply = _Reply("原稿。", "重写版。")
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "重写版。")
        self.assertEqual(len(reply.requests), 2, "不许循环重写")
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.reply_reviewed, 1)
        self.assertEqual(snapshot.reply_review_rejected_final, 1)

    def test_the_reviewers_text_never_becomes_her_line(self) -> None:
        """审核返回一句"改写好的台词"时，发出去的仍然是她自己写的那一版。"""

        reviewer = StyleReviewer(_Client("审核替她写好的一句。"))
        reply = _Reply("她自己写的一句。")
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))

        self.assertEqual(outgoing.text, "她自己写的一句。")
        self.assertEqual(len(reply.requests), 1, "没打回就不该重写")
        self.assertEqual(engine.snapshot().reply_reviewed, 0)

    def test_review_failure_still_sends_the_draft(self) -> None:
        reviewer = StyleReviewer(_Client(error=RuntimeError("审核挂了")))
        engine = _engine(_Reply("原稿就在这里。"), reviewer)
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))
        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "原稿就在这里。")
        self.assertEqual(engine.snapshot().reply_reviewed, 0)

    def test_a_failed_rewrite_falls_back_to_the_first_draft(self) -> None:
        """重写那一趟挂了 → 发第一版。**不许因为重写失败把一条回复弄丢。**"""

        reviewer = StyleReviewer(_Client("BLOCK: 越权。"))
        reply = _Reply("原稿。", "重写版。", error_on={2})
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "原稿。")
        self.assertEqual(len(reply.requests), 2)
        self.assertEqual(engine.snapshot().reply_reviewed, 1)

    def test_without_reviewer_nothing_changes(self) -> None:
        engine = _engine(_Reply("原稿就在这里。"))
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))
        self.assertEqual(outgoing.text, "原稿就在这里。")
        self.assertEqual(engine.snapshot().reply_reviewed, 0)

    def test_turning_the_switch_off_skips_the_review_entirely(self) -> None:
        """面板关掉 `review_enabled` → 连那一次判断都不发生（原稿直发）。"""

        client = _Client("BLOCK: 越权。")
        reply = _Reply("原稿。")
        engine = _engine(reply, StyleReviewer(client), review=False)
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))
        self.assertEqual(outgoing.text, "原稿。")
        self.assertEqual(client.requests, [])
        self.assertEqual(len(reply.requests), 1)
        self.assertEqual(engine.snapshot().reply_reviewed, 0)

    def test_a_machine_with_review_configured_still_sends_a_reply(self) -> None:
        """本机把审核配起来时，整条路仍然出得来一句话（这条走真实开关初值）。"""

        skip_unless_review_enabled()
        engine = DialogueEngine(_Reply("原稿就在这里。"),
                                style_reviewer=StyleReviewer(_Client("PASS")))
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))
        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "原稿就在这里。")
        self.assertEqual(engine.snapshot().reply_reviewed, 0)


if __name__ == "__main__":
    unittest.main()
