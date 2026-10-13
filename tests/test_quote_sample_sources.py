"""金句样本的第二条来源（2026-10-13 用户："学的太少了"）。

实情：她 1442 条发言里**只有 97 条**被点过表情 —— 只学那 97 条，池子早就干了
（档案 `seen` 107 条、日志里天天 `quote_learning_idle`）。所以补一条弱信号来源：
**没人点、但说完之后很快有人接着说话**。

这条来源必须守住的边界：表情才是"说得好不好"的线索，"有人接"只是"有人在听"。
所以这类样本**不许判方向**——提示词里写死，`as_data` 也要把"没有表情"显式说出来。
"""
from __future__ import annotations

import unittest

from qq_roleplay_bot.quote_learning import (
    LEARNING_SYSTEM_PROMPT,
    MAX_UNREACTIVE_SAMPLES_PER_RUN,
)
from qq_roleplay_bot.quote_samples import build_samples


def _her(second: float = 0, *, mid: str = "a", text: str = "在的。") -> dict:
    return {"direction": "out", "kind": "text", "group_id": "1", "session_id": "group:1",
            "message_id": mid, "body": text, "at": 1000.0 + second}


def _incoming(second: float, *, text: str = "接着说") -> dict:
    return {"direction": "in", "kind": "text", "group_id": "1", "session_id": "group:1",
            "message_id": f"i{second}", "body": text, "at": 1000.0 + second}


def _reaction(target: str, *, emoji: str = "12951") -> dict:
    return {"direction": "in", "kind": "reaction", "group_id": "1", "session_id": "group:1",
            "message_id": target, "emoji_id": emoji, "user_id": "7", "count": 1, "at": 1001.0}


class UnreactiveSampleTests(unittest.TestCase):
    def test_by_default_nothing_changes(self) -> None:
        """不传 `unreactive_max` 时，行为与改动前逐字相同（老判据没被放宽）。"""

        rows = [_her(mid="a"), _incoming(5)]
        self.assertEqual(build_samples(rows).samples, ())

    def test_a_replied_to_message_becomes_a_sample(self) -> None:
        rows = [_her(mid="a"), _incoming(5)]
        selection = build_samples(rows, unreactive_max=3)
        self.assertEqual([s.message_id for s in selection.samples], ["a"])
        self.assertEqual(selection.samples[0].reactions, (), "补位样本没有表情")
        data = selection.samples[0].as_data(for_learning=True)
        self.assertEqual(data["被点了"], "（无）", "没有表情这件事必须显式写在样本里")

    def test_silence_is_not_a_signal(self) -> None:
        """窗口里没人接就不收——弱信号也得有个信号。"""

        rows = [_her(mid="a"), _incoming(999)]
        self.assertEqual(build_samples(rows, unreactive_max=3).samples, ())

    def test_the_cap_keeps_the_newest(self) -> None:
        rows = []
        for index in range(5):
            rows.append(_her(index * 10, mid=f"m{index}"))
            rows.append(_incoming(index * 10 + 1))
        selection = build_samples(rows, unreactive_max=2)
        self.assertEqual([s.message_id for s in selection.samples], ["m3", "m4"])

    def test_reactive_samples_come_first_and_are_not_duplicated(self) -> None:
        rows = [_her(mid="a"), _incoming(5), _reaction("a"),
                _her(30, mid="b"), _incoming(35)]
        selection = build_samples(rows, unreactive_max=3)
        self.assertEqual([s.message_id for s in selection.samples], ["a", "b"])
        self.assertTrue(selection.samples[0].reactions, "有表情的排在前面")

    def test_the_prompt_spells_out_the_no_reaction_rule(self) -> None:
        self.assertIn("（无）", LEARNING_SYSTEM_PROMPT)
        self.assertIn("方向只能由表情给", LEARNING_SYSTEM_PROMPT)
        self.assertIn("reaction_meanings", LEARNING_SYSTEM_PROMPT)
        self.assertGreater(MAX_UNREACTIVE_SAMPLES_PER_RUN, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
