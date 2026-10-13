"""回复风格审核（可选 agent）：**只判断，不改写**；打回之后由回复 agent 重写一次。

2026-10-05 用户定的口径：*"审核只负责打回，不负责修改"*。在这之前 `review()` 返回
**改完的文本**（`REVIEW_SYSTEM_PROMPT` 自称"校对"、末尾写着"只改这四类"），
也就是说**风格审核在直接改写她的话**——这是用户报的"她的回复风格变得很奇怪、很 ai"
里被定位到的第二个原因（第一个是常驻 system 里那段「不懂就问」的规矩，
见 `tests/test_ask_when_unsure.py`）。

同一天用户又问"审核是不是管太松了"——**是**，所以判据那一轮一起收紧了：宽松偏置去掉、
四类扩到六类（加了**出戏自指 / 不成句 / 空回复**的写实判据）、输出认不出来要**重试一次**、
拦截率进 `EngineSnapshot` 与 `/super status`。收紧的账记在 `style_reviewer` 的模块头。

所以这个文件现在钉五类东西：

1. `review()` 的形状：`(是否通过, 理由)`，**第二项永远不是她的台词**；
2. 打回之后：**同一轮里重写一次**（最多一次，不许循环）；
   重写后仍被拒 → **发重写的那一版**（不许丢回复、不许卡住），并记一笔；
3. 审核自己出问题（超时 / 报错 / **重试后仍读不出判定**）→ **按通过**处理（fail-open）；
4. prompt 只授权它判断**六类**（事实错误 / 越权 / 出戏自指 / 伤人 / 不成句 / 空回复），
   **不许它去磨她的脾气与情绪**，也不许它评价"好不好听、像不像她、够不够有人味"；
5. 两条**生产故障**当用例：`再加一个插件，那条线还得挪。`（出戏）与
   `哦，看啊，云茹，今天的大大的哈基米。`（不成句 / 第三人称）→ 判不过；
   而**原样**接住一句通顺的话（`这么说好奇怪。`）→ 通过。

> **这份测试测不到什么**（如实说）：判"出戏/不成句"是**模型**按判据做的语义判断，
> 离线测试不跑真实模型。所以这里用**脚本化的审核替身**验"照判据判不过之后整条路怎么走"，
> 用**判据原文的锚点**验"这一类真的写进去了"。真正的语义判断只能上线后看
> `/super status` 里的 `风格审核：判断 N 次 打回 M 次`。

历史（"窄职责改写"那套 10 条改 5 条、数字机械兜底）已经作废：审核不再产出文本，
所以"改坏事实"由**结构**排除，`keeps_the_facts` / `within_growth_limit` 两条
机械规则随改写路径一起删掉了。
"""
import asyncio
import unittest

from qq_roleplay_bot.runtime_flags import RuntimeFlags
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.style_reviewer import (
    EMPTY_REPLY_REASON,
    MAX_NOTE_CHARS,
    MAX_REASON_CHARS,
    REVIEW_SYSTEM_PROMPT,
    REVIEW_TIMEOUT_SECONDS,
    StyleReviewer,
    is_blank,
    parse_review,
    parse_review_output,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

# 本机配置在不在的判定（干净 clone 只有 `.env.example`）——见 `tests/config_support.py`
from config_support import skip_unless_review_enabled

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)

#: 两条**生产故障**的原话（群 908696203 与 22:06 那条）——判据要覆盖它们。
FAULT_OUT_OF_CHARACTER = "分得清是今天的事。再加一个插件，那条线还得挪。"
FAULT_MANGLED = "哦，看啊，云茹，今天的大大的哈基米。"
#: 用户明确说"很有意思"的那种：**原样**接住一句通顺的话（不是搅碎的）。
ECHOED_FLUENT = "这么说好奇怪。"


def message(mid="m1", text="在吗", user="100", mentioned=True, name="某人") -> IncomingMessage:
    return IncomingMessage(mid, f"group:{GROUP}", user, text, TARGET, mentioned, sender_name=name)


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


class _DraftJudge:
    """**按草稿内容判**的审核替身：草稿里出现某几个片段就判不过，否则判通过。

    它模拟的是"模型照判据判"——真实语义判断离线测不了（见文件头那段"测不到什么"）。
    用它测的是**判不过之后整条路怎么走**，以及判据要覆盖的那两条真故障确实会被打回。
    """

    def __init__(self, blocks: dict[str, str] | None = None) -> None:
        self.blocks = dict(blocks or {})
        self.requests: list[list[dict[str, str]]] = []

    async def complete(self, request):
        self.requests.append(request)
        body = request[-1]["content"]
        draft = body.rpartition("待判断的草稿：")[2].strip()
        for key, reason in self.blocks.items():
            if key in draft:
                return f"BLOCK: {reason}"
        return "PASS"


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
        """**它想改写也不算数**：输出不是判定行 → 重试一次，仍不成 → 按通过处理。

        这是这次改动最要紧的一条：审核的文本永远不成为她的话。
        """

        draft = "是负的。前面那个字你没看。"
        client = _Client("是负的。")
        reviewer = StyleReviewer(client)
        passed, reason = asyncio.run(reviewer.review(draft))
        self.assertEqual((passed, reason), (True, ""))
        self.assertEqual(reviewer.stats["unrecognized"], 1)
        self.assertEqual(reviewer.stats["blocked"], 0)
        self.assertEqual(len(client.requests), 2, "认不出要先重试一次")

    def test_a_retry_that_comes_back_readable_is_obeyed(self) -> None:
        """第一次输出不合格式、**重试那一趟**写出了判定 → 按判定办（不再算认不出）。"""

        client = _Client(["这句我说不好，改成：她今天不来了。", "BLOCK: 出戏自指——她提到自己的构造。"])
        reviewer = StyleReviewer(client)
        passed, reason = asyncio.run(reviewer.review("原稿。"))
        self.assertFalse(passed)
        self.assertIn("出戏", reason)
        self.assertEqual(reviewer.stats["retried"], 1)
        self.assertEqual(reviewer.stats["unrecognized"], 0)
        self.assertEqual(reviewer.stats["blocked"], 1)
        self.assertEqual(reviewer.stats["calls"], 1, "重试仍算同一份草稿")

    def test_unreadable_output_is_retried_once_then_passed(self) -> None:
        """重试后仍读不出判定 → **放行**，但必须记一笔（这一条要看得见）。"""

        client = _Client(["又是台词。", "还是台词。"])
        reviewer = StyleReviewer(client)
        self.assertEqual(asyncio.run(reviewer.review("原稿。")), (True, ""))
        self.assertEqual(len(client.requests), 2, "只重试一次，不许循环")
        self.assertEqual(reviewer.stats["unrecognized"], 1)
        self.assertEqual(reviewer.stats["blocked"], 0)
        self.assertEqual(reviewer.stats["calls"], 1)

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

    def test_contentless_drafts_are_blocked(self) -> None:
        """空回复那一类：**只有空白**→ **不通过**（机器判，不花调用）。

        2026-10-11 收窄：原来 `。。。`/`……`/`，。！？ ` 也被机器判空回复，现在它们
        **照常走模型**——用户要的就是"单个标点符号、一个语气词"这种句子偶尔能发出去
        （"比如'？'或者'。'或者'啥'或者'哦哦'"），这件事机器判不了。
        """

        for draft in ("", "   ", "\n", " \t "):
            self.assertTrue(is_blank(draft), repr(draft))
        client = _Client("BLOCK: 什么都不是")
        reviewer = StyleReviewer(client)
        self.assertEqual(asyncio.run(reviewer.review("   ")), (False, EMPTY_REPLY_REASON))
        self.assertEqual(client.requests, [], "空草稿不该花一次调用")
        self.assertEqual(reviewer.stats["calls"], 1)
        self.assertEqual(reviewer.stats["blocked"], 1)

    def test_single_punctuation_and_particles_reach_the_model(self) -> None:
        """单标点 / 单语气词**不再被机器拦**，而是照常交给模型判（2026-10-11）。"""

        for draft in ("。", "？", "……", "哦哦", "嗯。", "啥"):
            self.assertFalse(is_blank(draft), repr(draft))
        client = _Client("PASS")
        reviewer = StyleReviewer(client)
        self.assertEqual(asyncio.run(reviewer.review("？")), (True, ""))
        self.assertEqual(len(client.requests), 1, "单标点要真的问一次模型，不能机器判死")
        self.assertEqual(client.requests[0][1]["content"].splitlines()[-1], "待判断的草稿：？")

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
        `test_review_prompt_keeps_emotion_and_only_blocks_six_kinds`。
        """

        self.assertIn("越权", REVIEW_SYSTEM_PROMPT)
        self.assertIn("站到讲台上了", REVIEW_SYSTEM_PROMPT)
        for marker in ("我建议你", "其实", "你想多了", "早晚会", "别想太多"):
            self.assertIn(marker, REVIEW_SYSTEM_PROMPT, marker)
        self.assertIn("教对方", REVIEW_SYSTEM_PROMPT, "自检那句被拿掉了")
        self.assertIn("有人连名字都没认全就得投票", REVIEW_SYSTEM_PROMPT)

    def test_prompt_has_no_lenient_bias(self) -> None:
        """**去掉宽松偏置**（2026-10-05 用户："审核是不是管太松了"）。

        "拿不准就判过""一律判过"是"审核会改写"时代的产物——那时候拦错一句的代价是
        **她的话被改掉**。现在它只判不改，那个代价没有了，所以偏置也不许留。
        """

        for lenient in ("一律判过", "拿不准就判过", "通常不用管",
                        "绝大多数时候你什么都不用做"):
            self.assertNotIn(lenient, REVIEW_SYSTEM_PROMPT, lenient)
        self.assertIn("**逐条**对照", REVIEW_SYSTEM_PROMPT)
        self.assertIn("一类都没命中才判**过**", REVIEW_SYSTEM_PROMPT)

    def test_prompt_names_the_out_of_character_criterion(self) -> None:
        """**出戏自指**那一类要写实（真故障 A：`再加一个插件，那条线还得挪。`）。

        判据必须点名"她提到自己的构造/实现/机制"，并把生产里真实出现过的说法写进去，
        否则模型只会拦"我是 AI"这种最直白的写法，漏掉"那条线""这个 bot"这类。
        """

        self.assertIn("出戏自指", REVIEW_SYSTEM_PROMPT)
        for marker in ("插件", "架构", "agent", "模型", "记忆（库）", "这条线", "我这个 bot",
                       "是程序", "是 AI", "写好的说明"):
            self.assertIn(marker, REVIEW_SYSTEM_PROMPT, marker)
        self.assertIn("再加一个插件，那条线还得挪。", REVIEW_SYSTEM_PROMPT)

    def test_prompt_covers_the_broken_sentence_criterion(self) -> None:
        """**不成句 / 胡话**那一类（真故障 B：`哦，看啊，云茹，今天的大大的哈基米。`）。

        判据要写清四样：读不通、词序错乱、**第三人称说自己**、把来信搅碎成残句；
        同时**写清边界**——原样接住一句通顺的话是允许的（用户说这样有意思）。
        """

        for marker in ("读不通", "词序错乱", "第三人称", "搅碎", "残句"):
            self.assertIn(marker, REVIEW_SYSTEM_PROMPT, marker)
        self.assertIn("云茹今天", REVIEW_SYSTEM_PROMPT)
        self.assertIn("宝宝你是一个大大的哈基米", REVIEW_SYSTEM_PROMPT)
        self.assertIn("今天的大大的哈基米", REVIEW_SYSTEM_PROMPT)
        # 边界：复读通顺的一句 = 通过
        self.assertIn("复读不算第 5 类", REVIEW_SYSTEM_PROMPT)
        self.assertIn("**原样**接住对方一句通顺的话", REVIEW_SYSTEM_PROMPT)
        self.assertIn("是**通过**的", REVIEW_SYSTEM_PROMPT)

    def test_prompt_covers_the_empty_reply_criterion(self) -> None:
        """第 6 类只管**真的什么都没写**；单标点 / 单语气词另有明文许可（2026-10-11）。"""

        self.assertIn("空回复", REVIEW_SYSTEM_PROMPT)
        self.assertIn("一个字都没有", REVIEW_SYSTEM_PROMPT)
        self.assertIn("单发一个标点或一个语气词是允许的", REVIEW_SYSTEM_PROMPT)
        # 边界：**对方在正面问她**时，一个没有信息量的语气词仍算第 6 类。
        self.assertIn("对方在正面问她、求她办事", REVIEW_SYSTEM_PROMPT)
        self.assertIn("那不算回应，判不过", REVIEW_SYSTEM_PROMPT)

    def test_prompt_lets_casual_texture_through(self) -> None:
        """**口语毛边放行**（2026-10-11，用户："说话太书面化了有感觉吗"）。

        人设那边松了口（允许半句、句末语气词、整句不打句号），审核这边必须同步——
        否则她写得越随手，越容易被判成第 5 类打回去。守两条边界：
        毛边放行，但"**只有**语气词"在对她正面提问时仍归第 6 类。
        """

        self.assertIn("口语的省略不算这一类", REVIEW_SYSTEM_PROMPT)
        self.assertIn("把她打成书面语才是改坏了", REVIEW_SYSTEM_PROMPT)
        self.assertIn("口语的毛边", REVIEW_SYSTEM_PROMPT)
        self.assertIn("句末带一个语气词的正常句子**不**算这一类", REVIEW_SYSTEM_PROMPT)

    def test_prompt_forbids_judging_her_taste(self) -> None:
        """**不许审核评价"好不好听 / 像不像她 / 够不够有人味"**（防它又把她磨平）。"""

        self.assertIn('"好不好听""像不像她""够不够有人味"不归你管', REVIEW_SYSTEM_PROMPT)
        self.assertIn("不去评价她的味道", REVIEW_SYSTEM_PROMPT)

    def test_prompt_covers_hitting_back_and_targeting_people(self) -> None:
        """2026-10-13：第 4 类要能落地"回敬"与"拿人开涮"这两类冲着人的话。

        由来是用户那句"说话冲、怼人的问题又起来了"——之前这一趟只拦越权/事实，
        回复日志里打回理由里"伤人"一条都没有。
        """

        self.assertIn("伤人", REVIEW_SYSTEM_PROMPT)
        self.assertIn("回敬、赌气、把人顶回去", REVIEW_SYSTEM_PROMPT)
        self.assertIn("拿具体的人开涮", REVIEW_SYSTEM_PROMPT)
        self.assertIn("冲在人上就是第 4 类", REVIEW_SYSTEM_PROMPT)
        # 反例得是当天真草稿——规矩靠它们落地，不能只剩一句抽象的话。
        for sample in ("怎么，我说话还得端着。", "2.7 岁也能算出来，挺细致的。",
                       "少年感 100，这是拿年龄换的吧。"):
            self.assertIn(sample, REVIEW_SYSTEM_PROMPT, sample)
        # 而"冲在事上"照旧放行，不许把她的脾气一起拦掉。
        self.assertIn("冲在事上", REVIEW_SYSTEM_PROMPT)
        self.assertIn("写得冲也不算越界", REVIEW_SYSTEM_PROMPT)

    def test_prompt_spells_out_the_two_overreach_mistakes(self) -> None:
        """这两条真例子是**这一腿换成 `reasoning_effort=none` 的前提**。

        2026-10-13 实测：没有它们时 `none` 在 6 条真越权里只拦 3 条（`low` 拦 5 条），
        写进第 2 类之后 `none` 也拦 5 条，而速度是 0.72s vs 4.92s（最大 1.15s vs 12.55s）。
        删掉它们＝把这条腿的判据悄悄削薄，所以钉住。
        """

        self.assertIn("把一句感受拔成一般规律", REVIEW_SYSTEM_PROMPT)
        self.assertIn("想象里的价，一般都比实际低一截", REVIEW_SYSTEM_PROMPT)
        self.assertIn("顺手教他怎么做、怎么选", REVIEW_SYSTEM_PROMPT)
        self.assertIn("9950 好办，倒是改卡那台的电源别省", REVIEW_SYSTEM_PROMPT)

    def test_the_review_budget_covers_the_slow_drafts(self) -> None:
        """超时的后果是**原稿直发**，可这一趟在发送路径上：预算换的是对方等的秒数。

        2026-10-13 实测（部署后的 prompt、24 条真草稿）：中位 3.46s、p75 8.27s、
        p90 11.49s、最长 24.48s；跳过率 4.5s→41%、7s→29%、9s→20%。用户当天第二次问
        "为什么还是有明显延迟"，所以停在 6 秒：盖住中位与快的那一档，砍掉长尾（代价是
        慢的两三成按原稿直发）。**不许再往下**——4.5s 那版跳过 41%，等于四成不做判断。
        """

        self.assertGreaterEqual(REVIEW_TIMEOUT_SECONDS, 6.0)
        self.assertLessEqual(REVIEW_TIMEOUT_SECONDS, 9.0)


class RealFaultReviewTests(unittest.TestCase):
    """两条**生产故障**当用例：判据覆盖它们，判不过时**同一轮重写一次**。

    真实语义判断在模型那侧（离线测不了，见文件头）；这里用 `_DraftJudge` 模拟"照判据判"，
    测的是判不过之后整条路：重写一次 → 发重写的那一版 → 计数对得上。
    """

    def test_a_the_plugin_line_is_out_of_character_and_gets_rewritten(self) -> None:
        reviewer = StyleReviewer(_DraftJudge(
            {"插件": "出戏自指——她替自己那一套出主意了。"}))
        reply = _Reply(FAULT_OUT_OF_CHARACTER, "那事你们聊，我听着。")
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 给 bot 加个社交欲望？")))

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "那事你们聊，我听着。", "打回之后发重写的那一版")
        self.assertEqual(len(reply.requests), 2, "同一轮里重写一次，不许循环")
        self.assertIn("出戏自指", _last_note(reply), "理由要交回给写这句话的人")
        self.assertEqual(engine.snapshot().reply_reviewed, 1)

    def test_b_the_third_person_mangled_line_gets_rewritten(self) -> None:
        reviewer = StyleReviewer(_DraftJudge(
            {"大大的": "不成句——词序乱了，还用第三人称叫自己。"}))
        reply = _Reply(FAULT_MANGLED, "你才是哈基米。")
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 宝宝你是一个大大的哈基米")))

        self.assertEqual(outgoing.text, "你才是哈基米。")
        self.assertEqual(len(reply.requests), 2)
        self.assertIn("不成句", _last_note(reply))
        self.assertEqual(engine.snapshot().reply_reviewed, 1)

    def test_c_echoing_a_fluent_sentence_verbatim_passes_untouched(self) -> None:
        """**原样**接住一句通顺的话 → 通过：不打回、不重写、原稿直发。"""

        reviewer = StyleReviewer(_DraftJudge())
        reply = _Reply(ECHOED_FLUENT)
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 宝宝你是一个大大的哈基米")))

        self.assertEqual(outgoing.text, ECHOED_FLUENT)
        self.assertEqual(len(reply.requests), 1, "没打回就不该重写")
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.reply_reviewed, 0)
        self.assertEqual(snapshot.review_calls, 1)
        self.assertEqual(snapshot.review_blocked, 0)

    def test_the_context_of_the_review_call_is_who_she_is_answering(self) -> None:
        """审核那次调用拿得到现场：**对方那句原话** + 草稿（同一份请求的 user 段）。"""

        client = _DraftJudge()
        engine = _engine(_Reply("在的。"), StyleReviewer(client))

        asyncio.run(engine.handle(message(text="@YunRu 在吗", name="阿星")))

        body = client.requests[0][-1]["content"]
        self.assertIn("阿星说：@YunRu 在吗", body, "对方那句要进审核请求")
        self.assertIn("待判断的草稿：在的。", body)
        # 现场只进 user 段：审核的 system 里没有它
        self.assertNotIn("阿星说", client.requests[0][0]["content"])

    def test_the_review_counts_are_visible_in_the_snapshot_and_status(self) -> None:
        """**拦截率要看得见**：长期 0 拦截 = 审核没在工作；拦截率很高 = 可能过紧。"""

        reviewer = StyleReviewer(_Client(["BLOCK: 出戏自指。", "PASS"]))
        engine = _engine(_Reply("原稿。", "改一版。"), reviewer)
        asyncio.run(engine.handle(message(text="@YunRu 在吗")))

        snapshot = engine.snapshot()
        # 2 次判断 = 原稿那一次 + 重写之后那一次（只有原稿那次被打回）
        self.assertEqual((snapshot.review_calls, snapshot.review_blocked), (2, 1))
        self.assertIn("风格审核：判断 2 次   打回 1 次（50%）", engine._super_status())


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


class ReviewVerdictNoteTests(unittest.TestCase):
    """审核顺手带走的那半句（2026-10-11「惦记事」）。

    审核是**唯一在她发出去之前看到成品**的环节，所以她"应下了一件以后要跟的小事"
    由它顺手抓（用户口径："惦记的事由审核agent负责"）。抓的是**照抄的原话**，
    它接着会进记忆收件箱、下一拍被写成 `intention`，所以取错了就是"她记着的事
    被人改过一遍"。这里逐条钉住 `parse_review` / `review_verdict` 对 `NOTE:` 的取法。

    > 测不到什么：真正会不会写 `NOTE:` 是**模型**按 prompt 判的（判据原文在
    > `REVIEW_SYSTEM_PROMPT` 末尾那一段）；这里只测**它写了之后这边怎么读**。
    """

    def test_a_pass_with_a_note_keeps_her_own_half_sentence(self) -> None:
        """`PASS` + 一行 `NOTE:` → `note` 就是那半句原话，理由仍是空的。"""

        verdict = parse_review("PASS\nNOTE: 回头问他那个接口好没")

        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.reason, "")
        self.assertEqual(verdict.note, "回头问他那个接口好没")

    def test_a_pass_without_a_note_keeps_the_note_empty(self) -> None:
        """没应下什么就只写 `PASS`：`note` 必须是空串（不是空白、也不是那行判定本身）。"""

        for raw in ("PASS", "PASS\n", "通过", "OK\n", "```\nPASS\n```"):
            verdict = parse_review(raw)
            self.assertEqual(verdict.note, "", raw)

    def test_the_note_is_flattened_to_one_line_and_capped(self) -> None:
        """`NOTE:` 那一行内的空白压成一个空格；超长截到 `MAX_NOTE_CHARS`。

        上限的由来（判据原文）：它是**照抄的原话**，长了就说明模型在替她总结。
        """

        self.assertEqual(parse_review("PASS\nNOTE: 回头  问他  那个接口").note,
                         "回头 问他 那个接口")
        long_note = "怎" * (MAX_NOTE_CHARS + 40)
        self.assertEqual(parse_review(f"PASS\nNOTE: {long_note}").note,
                         long_note[:MAX_NOTE_CHARS])

    def test_only_the_first_note_line_is_kept_and_extra_lines_are_dropped(self) -> None:
        """只取**第一条** `NOTE:`；第二行（换行接着写）不算进去——prompt 里写明"不要写第二条"。"""

        self.assertEqual(parse_review("PASS\nNOTE: 回头问他那个接口\nNOTE: 明天再看一遍").note,
                         "回头问他那个接口")
        self.assertEqual(parse_review("PASS\nNOTE: 回头问他\n那个接口").note, "回头问他")

    def test_a_blocked_draft_never_carries_a_note(self) -> None:
        """判**不过**时即使模型多写了 `NOTE:` 也不取——那一版不是她要发出去的话。"""

        verdict = parse_review("BLOCK: 越权——在教对方怎么做。\nNOTE: 回头问他那个接口")

        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.note, "")
        self.assertEqual(verdict.reason, "越权——在教对方怎么做。")

    def test_a_note_is_copied_verbatim_with_no_filtering(self) -> None:
        """**照抄就是照抄**：这一行不做任何过滤或清洗（连短码形状也照抄）。

        所以"进去的这一串安全不安全"不归这里管——`MemoryService.note_own_words`
        那一侧自己过 `safe_memory_text`（凭据 / 本机路径那类在那里被拦）。
        这条钉的是"这里没有偷偷改写或替她抹掉什么"。
        """

        raw = "PASS\nNOTE: 行，回头把 ‹A1B2C3D› 那句给他看"

        self.assertEqual(parse_review(raw).note, "行，回头把 ‹A1B2C3D› 那句给他看")

    def test_review_verdict_reads_the_note_through_the_real_call_path(self) -> None:
        """走 `StyleReviewer.review_verdict`（不只测解析函数）：`NOTE:` 到得了调用方。"""

        reviewer = StyleReviewer(_Client("PASS\nNOTE: 明天问他那个接口好了没"))

        verdict = asyncio.run(reviewer.review_verdict("行，明天我问他那个接口好了没。"))

        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.note, "明天问他那个接口好了没")
        self.assertEqual(reviewer.stats["calls"], 1)
        self.assertEqual(reviewer.stats["blocked"], 0)

    def test_a_retry_that_comes_back_readable_still_carries_the_note(self) -> None:
        """第一次输出认不出来、重试后才 `PASS` + `NOTE:` → 那半句照样到得了。"""

        reviewer = StyleReviewer(_Client(["我改好了：行，明天问他。",
                                          "PASS\nNOTE: 明天问他那个接口"]))

        verdict = asyncio.run(reviewer.review_verdict("行，明天我问他那个接口。"))

        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.note, "明天问他那个接口")
        self.assertEqual(reviewer.stats["retried"], 1)

    def test_the_prompt_tells_her_judge_when_to_write_a_note(self) -> None:
        """判据原文里那一段必须在（判过才写、照抄原话、不要第二条、判不过不写）。"""

        self.assertIn("顺手记一笔", REVIEW_SYSTEM_PROMPT)
        self.assertIn("`NOTE: `", REVIEW_SYSTEM_PROMPT)
        self.assertIn("照抄她那半句原话", REVIEW_SYSTEM_PROMPT)
        self.assertIn("不要写第二条", REVIEW_SYSTEM_PROMPT)
        self.assertIn("判**不过**的时候不写 `NOTE`", REVIEW_SYSTEM_PROMPT)


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

    def test_she_can_refuse_the_rewrite_by_writing_the_same_line_back(self) -> None:
        """她说"这句我不改"的方式：把原来那句**原样写回来**。

        2026-10-11 用户："审核agent打回后回复agent可以拒绝重写原样发出"。
        风格这一趟判的是**审美**（措辞、分寸、像不像人），不是"这句话有事实错误"——
        所以最后由**她自己**决定改不改：逐字相同 = 拒绝，照**第一版**发出去
        （不是重写的那一版），并记一笔"她拒绝了"。
        """

        reviewer = StyleReviewer(_Client("BLOCK: 太书面了，像在念稿。"))
        reply = _Reply("她自己的原话。", "她自己的原话。")   # 第二版：原样写回
        engine = _engine(reply, reviewer)

        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))

        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "她自己的原话。", "拒绝之后发的是她原来那一句")
        self.assertEqual(len(reply.requests), 2, "还是只重写一次，不循环")
        # 拒绝的**办法**要写在现场提示里（她不会用一句话说出来，说了就变成台词）
        self.assertIn("原样再写一遍", _last_note(reply))
        # 只给风格这一趟：另两趟（没把握就别断言 / 别抄短码）必须照改
        self.assertIn("不合适的地方", _last_note(reply))
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.reply_reviewed, 1)
        self.assertEqual(snapshot.reply_review_rewrite_declined, 1)
        self.assertEqual(snapshot.reply_review_rejected_final, 0,
                         "拒绝不是「重写后仍被拒」——那一条计的是照重写版发")

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
