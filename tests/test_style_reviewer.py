"""回复风格审核（可选 agent）：窄职责、机械兜底、永不挡回复。

由来（2026-09-29）：用户报"说话很冲、喜欢怼、很人机"。离线实测（data/style_reviewer_ab.py）
显示光靠 prompt 压不住，而一次窄职责校对能把冲的尾巴删掉；同时又实测到"重写型"审核
会改坏事实（3.11 → 0.11）。这个文件锁的就是这两条结论：能删尾巴，但不能动事实、不能挡住回复。
"""
import asyncio
import unittest

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.style_reviewer import (REVIEW_SYSTEM_PROMPT, StyleReviewer, keeps_the_facts,
                                            within_growth_limit)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

# 本机配置在不在的判定（干净 clone 只有 `.env.example`）——见 `tests/config_support.py`
from config_support import skip_unless_review_enabled

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


def message(mid="m1", text="在吗", user="100", mentioned=True) -> IncomingMessage:
    return IncomingMessage(mid, f"group:{GROUP}", user, text, TARGET, mentioned, sender_name="某人")


class _Client:
    """按脚本返回；也可以抛错（模拟审核超时/挂了）。"""

    def __init__(self, output="", *, error=None):
        self.output = output
        self.error = error
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.output


class _Reply:
    def __init__(self, text):
        self.text = text

    async def complete(self, request):
        return f"<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>{self.text}</reply>"


class StyleReviewerTests(unittest.TestCase):
    def test_reviewed_line_replaces_the_draft(self) -> None:
        reviewer = StyleReviewer(_Client("是负的。"))
        text, changed = asyncio.run(reviewer.review("是负的。前面那个字你没看。", context="3.8-3.11 是多少"))
        self.assertEqual(text, "是负的。")
        self.assertTrue(changed)
        self.assertEqual(reviewer.stats["changed"], 1)

    def test_reviewer_failure_keeps_the_draft(self) -> None:
        reviewer = StyleReviewer(_Client(error=RuntimeError("timeout")))
        text, changed = asyncio.run(reviewer.review("原稿。"))
        self.assertEqual(text, "原稿。")
        self.assertFalse(changed)
        self.assertEqual(reviewer.stats["failed"], 1)

    def test_numbers_must_survive_review(self) -> None:
        """实测教训：重写型审核把 3.11 改成了 0.11，一句话的意思就变了。"""

        reviewer = StyleReviewer(_Client("0.11 大。"))
        text, changed = asyncio.run(reviewer.review("3.11 大。"))
        self.assertEqual(text, "3.11 大。", "改掉数字的定稿要作废")
        self.assertFalse(changed)
        self.assertEqual(reviewer.stats["reverted"], 1)
        self.assertFalse(keeps_the_facts("3.11 大。", "0.11 大。"))
        self.assertTrue(keeps_the_facts("负 0.69，不是 0.69", "负 0.69"))

    def test_deleting_a_clause_with_a_number_is_allowed(self) -> None:
        """审核的活就是删掉冲的尾巴，而那些句子常带数字——不能因此把合法改写也毙掉。

        （第一版兜底要求"原稿每个数字都要出现在定稿里"，实测把
        `0.8 大。你要的是小数比大小，3.11 那个是版本号。` → `0.8 大。` 也判成了不合格。）
        """

        self.assertTrue(keeps_the_facts("0.8 大。你要的是小数比大小，3.11 那个是版本号。", "0.8 大。"))
        reviewer = StyleReviewer(_Client("0.8 大。"))
        text, changed = asyncio.run(reviewer.review("0.8 大。你要的是小数比大小，3.11 那个是版本号。"))
        self.assertEqual(text, "0.8 大。")
        self.assertTrue(changed)

    def test_wiping_every_number_is_rejected(self) -> None:
        """但"把带答案的整句删光"不行：原稿有数字，定稿一个数字都不剩就退回原稿。"""

        self.assertFalse(keeps_the_facts("3.11 大。", "大。"))
        reviewer = StyleReviewer(_Client("大。"))
        text, changed = asyncio.run(reviewer.review("3.11 大。"))
        self.assertEqual(text, "3.11 大。")
        self.assertFalse(changed)

    def test_reviewer_cannot_grow_the_line(self) -> None:
        reviewer = StyleReviewer(_Client("短。" * 60))
        text, changed = asyncio.run(reviewer.review("短。"))
        self.assertEqual(text, "短。")
        self.assertFalse(changed)
        self.assertFalse(within_growth_limit("短。", "短。" * 60))

    def test_wrappers_are_stripped(self) -> None:
        reviewer = StyleReviewer(_Client('修改后："不是。"'))
        text, _changed = asyncio.run(reviewer.review("不是。纱布吸水，我不吸。"))
        self.assertEqual(text, "不是。")

    def test_empty_review_keeps_the_draft(self) -> None:
        reviewer = StyleReviewer(_Client("   "))
        text, changed = asyncio.run(reviewer.review("原稿。"))
        self.assertEqual(text, "原稿。")
        self.assertFalse(changed)

    def test_no_client_means_passthrough(self) -> None:
        reviewer = StyleReviewer(None)
        text, changed = asyncio.run(reviewer.review("原稿。"))
        self.assertEqual(text, "原稿。")
        self.assertFalse(changed)
        self.assertFalse(reviewer.enabled)

    def test_prompt_forbids_touching_facts_and_forbids_explaining(self) -> None:
        assert "所有数字、称呼、事实一个字都不许动" in REVIEW_SYSTEM_PROMPT
        assert "把原稿一字不差地输出" in REVIEW_SYSTEM_PROMPT
        assert "不要解释" in REVIEW_SYSTEM_PROMPT

    def test_prompt_covers_the_preaching_criterion(self) -> None:
        """第 6 条：说教/上课（2026-09-30 用户："增加对说教语气的审核"）。

        样本是她自己的真实回复（`data/style_preach_ab.py` 里那批），
        所以这条判据必须点名"给建议""讲道理""评价对方该怎么想""科普讲解""大道理"，
        并且给出**照着删**的改写例子 + 一条自检——只写"不要说教"实测不够
        （第一版 8 条里只改掉 5 条，加了例子才 8/8）。
        """

        assert "说教/上课" in REVIEW_SYSTEM_PROMPT
        assert "第 6 条怎么改" in REVIEW_SYSTEM_PROMPT
        assert "站在讲台上" in REVIEW_SYSTEM_PROMPT
        for marker in ("我建议你", "其实", "你想多了", "早晚会", "别想太多"):
            assert marker in REVIEW_SYSTEM_PROMPT, marker
        assert "改完再自检一遍" in REVIEW_SYSTEM_PROMPT

    def test_deleting_the_preaching_but_keeping_the_number_is_accepted(self) -> None:
        """删掉说教、留下事实（含数字）——这正是新判据想要的结果，不能被兜底毙掉。"""

        draft = "0.8 大。你要的是小数比大小，你以后慢慢就懂了。"
        reviewer = StyleReviewer(_Client("0.8 大。"))
        text, changed = asyncio.run(reviewer.review(draft))
        self.assertEqual(text, "0.8 大。")
        self.assertTrue(changed)
        self.assertEqual(reviewer.stats["reverted"], 0)


class EngineReviewWiringTests(unittest.TestCase):
    def test_engine_sends_the_reviewed_line(self) -> None:
        # 前提是本机的风格审核配置在场（干净 clone 只有 .env.example）。
        skip_unless_review_enabled()
        reviewer = StyleReviewer(_Client("不是。"))
        engine = DialogueEngine(_Reply("不是。纱布吸水，我不吸。"), style_reviewer=reviewer)
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 你是纱布")))
        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "不是。")
        self.assertEqual(engine.snapshot().reply_reviewed, 1)

    def test_review_failure_still_sends_the_draft(self) -> None:
        reviewer = StyleReviewer(_Client(error=RuntimeError("审核挂了")))
        engine = DialogueEngine(_Reply("原稿就在这里。"), style_reviewer=reviewer)
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))
        self.assertIsNotNone(outgoing)
        self.assertEqual(outgoing.text, "原稿就在这里。")
        self.assertEqual(engine.snapshot().reply_reviewed, 0)

    def test_without_reviewer_nothing_changes(self) -> None:
        engine = DialogueEngine(_Reply("原稿就在这里。"))
        outgoing = asyncio.run(engine.handle(message(text="@YunRu 在吗")))
        self.assertEqual(outgoing.text, "原稿就在这里。")
        self.assertEqual(engine.snapshot().reply_reviewed, 0)


if __name__ == "__main__":
    unittest.main()
