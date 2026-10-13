"""笔记按 `note_id` 合并（2026-10-13，用户："学的太少了"）。

原来 `apply_run` 是"**整份替换**"：一轮只看得见 36 条样本，档案就永远只有那一轮的量；
而且**已经学到的"避免"会被下一轮冲掉**（当天实测：笔记 9→19，`avoid` 却 2→0）。

含义表早就是按群合并的，`_merge_meanings` 的注释写得很清楚：整份替换会让上一次学过的
东西凭空消失——同一条道理对笔记一样成立。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qq_roleplay_bot.quote_learning import MAX_AVOID_NOTES, MAX_NOTES, QuoteStore


def _note(when: str, note: str, *, avoid: bool = False, kind: str = "style") -> dict:
    item = {"kind": kind, "when": when, "note": note}
    if avoid:
        item["avoid"] = True
    return item


class NoteMergeTests(unittest.TestCase):
    def test_notes_merge_instead_of_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteStore(Path(tmp) / "profile.json")
            store.apply_run(notes=[_note("被夸的时候", "少来")], meanings={}, seen=[])
            store.apply_run(notes=[_note("被问事实的时候", "先给结论")], meanings={}, seen=[])
            self.assertEqual({item["note"] for item in store.notes}, {"少来", "先给结论"})

    def test_the_same_note_is_not_duplicated_and_the_new_one_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteStore(Path(tmp) / "profile.json")
            store.apply_run(notes=[_note("被夸的时候", "少来", kind="style")],
                            meanings={}, seen=[])
            store.apply_run(notes=[_note("被夸的时候", "少来", kind="context")],
                            meanings={}, seen=[])
            self.assertEqual(len(store.notes), 1, "同一条（when + note 相同）只留一份")
            self.assertEqual(store.notes[0]["kind"], "context", "以这一轮写的为准")

    def test_avoid_notes_survive_and_sort_first(self) -> None:
        """一轮里没有 `㊗` 样本时，先前学到的"踩过的坑"不许消失。"""

        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteStore(Path(tmp) / "profile.json")
            store.apply_run(notes=[_note("被逗就顺着往下损", "这么接的那次被嫌了", avoid=True)],
                            meanings={}, seen=[])
            store.apply_run(notes=[_note("聊吃的", "接一句有画面的话")], meanings={}, seen=[])
            self.assertIn("这么接的那次被嫌了", [item["note"] for item in store.notes])
            self.assertTrue(store.notes[0].get("avoid"), "坑排最前，到上限时最后被丢")

    def test_the_cap_keeps_the_newest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteStore(Path(tmp) / "profile.json")
            for index in range(MAX_NOTES + 5):
                store.apply_run(notes=[_note(f"场合{index}", f"笔记{index}")],
                                meanings={}, seen=[])
            notes = [item["note"] for item in store.notes]
            self.assertEqual(len(notes), MAX_NOTES)
            self.assertNotIn("笔记0", notes, "最老的先被丢")
            self.assertIn(f"笔记{MAX_NOTES + 4}", notes, "最新的留着")

    def test_avoid_notes_cannot_crowd_out_the_positive_ones(self) -> None:
        """"踩过的坑"单独限额——一轮补学一大票 `㊗` 样本之后，正面笔记不许被挤光。

        2026-10-13 实测踩到：补学 55 条 `㊗` 样本产出 65 条 avoid，把 60 条上限填满，
        19 条正面笔记全丢，档案变成清一色"别这么做"。
        """

        with tempfile.TemporaryDirectory() as tmp:
            store = QuoteStore(Path(tmp) / "profile.json")
            store.apply_run(notes=[_note(f"场合{index}", f"正面笔记{index}")
                                   for index in range(20)], meanings={}, seen=[])
            store.apply_run(notes=[_note(f"坑{index}", f"踩过的坑{index}", avoid=True)
                                   for index in range(65)], meanings={}, seen=[])
            avoid = [item for item in store.notes if item.get("avoid")]
            positive = [item for item in store.notes if not item.get("avoid")]
            self.assertEqual(len(avoid), MAX_AVOID_NOTES, "坑走自己的限额")
            self.assertEqual(len(positive), 20, "正面笔记一条都不许被挤掉")
            self.assertLessEqual(len(store.notes), MAX_NOTES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
