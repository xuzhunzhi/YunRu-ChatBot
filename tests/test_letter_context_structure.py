# -*- coding: utf-8 -*-
"""**核心那一半**的测试：`letter_context.py` 的"结构"（时间/会话/引用链/图）。

它落在哪里：`data/letter_context_structure.patch` 是给 **本体线**（`dev`）的补丁，
应用之后把本文件拷成 `dev/tests/test_letter_context_structure.py` 一起提交。

为什么它不跟插件那一半在一起：`check_plugin_branch_scope.py` 只允许插件线改
`src/qq_roleplay_bot/plugins/**`，而 `letter_context.py` 是核心（本体线）的模块——
那道门槛这次真的拦住了，拦得对（这条改动在本体树上也要有）。
所以：插件侧只留"核心递过来的字段我怎么渲染"，本文件管"那些字段从日志里怎么取"。

本文件只在**本体树/核心树**上跑得起来（它 import `qq_roleplay_bot.letter_context`，
插件树上那个文件也有，但这份测试的归属在本体线——见上一段）。
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from qq_roleplay_bot import letter_context


def _row(at: float, body: str, *, user_id: str = "10001", direction: str = "in",
         kind: str = "text", **extra) -> dict:
    row = {"at": at, "body": body, "user_id": user_id, "direction": direction, "kind": kind}
    row.update(extra)
    return row


class StructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "chat.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def collect(self, rows, *, since: float = 0.0, owner_ids=("10001",), now=None):
        self.log.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8")
        return letter_context.collect_letter_context(
            self.log, since=since, owner_ids=owner_ids, now=now)

    # --- 1. 结构与正文同序等长、正文一个字不改 -------------------------------

    def test_details_line_up_with_the_lines_and_change_nothing(self) -> None:
        got = self.collect([
            _row(100.0, "在吗", message_id="m1", session_id="group:1", group_id="1"),
            _row(101.0, "别人说的", user_id="20002", message_id="m2",
                 session_id="group:1", group_id="1"),
            _row(102.0, "别太晚睡", message_id="m3", session_id="private:10001",
                 reply_to="m2", has_media=True),
        ], now=103.0)

        assert got is not None
        assert got.his_lines == ("在吗", "别太晚睡")
        assert [item.body for item in got.his_details] == list(got.his_lines), "结构不许改字"
        assert len(got.his_details) == len(got.his_lines) == 2

    def test_without_details_the_old_shape_still_works(self) -> None:
        """老形状（没有结构）不该因此少一句话。"""

        got = self.collect([_row(100.0, "在吗")], now=100.0)

        assert got is not None and got.his_lines == ("在吗",)
        assert len(got.his_details) == 1, "新字段在老日志上也要成立"

    # --- 2. 时间说人话（按日历日，不按秒数除）--------------------------------

    def test_the_when_text_says_today_yesterday(self) -> None:
        now = datetime.datetime(2026, 10, 16, 9, 0).timestamp()

        assert letter_context._when_text(now - 3600, now) == "今天早上"
        assert letter_context._when_text(now - 23.5 * 3600, now) == "昨天上午", "跨午夜就是昨天"
        assert letter_context._when_text(now - 47 * 3600, now) == "前天上午"
        assert letter_context._when_text(now - 5 * 86400, now) == "5 天前"

    def test_the_time_never_prints_a_clock(self) -> None:
        """不给 `14:07` 这种钟点：素材里给钟点，她会想把它抄进信里。"""

        now = datetime.datetime(2026, 10, 16, 12, 30).timestamp()
        text = letter_context._when_text(now - 60, now)

        assert text == "今天中午", text
        assert ":" not in text and "：" not in text

    # --- 3. 会话标号：本封信内稳定、不是群名也不是群号 ------------------------

    def test_sessions_get_short_stable_labels(self) -> None:
        got = self.collect([
            _row(100.0, "甲", session_id="group:717151356", group_id="717151356"),
            _row(101.0, "乙", session_id="group:800000001", group_id="800000001"),
            _row(102.0, "丙", session_id="group:717151356", group_id="717151356"),
            _row(103.0, "丁", session_id="private:10001", group_id=""),
        ], now=104.0)

        labels = [item.session for item in got.his_details]
        assert labels == ["群1", "群2", "群1", "私聊1"], labels
        assert not any("717151356" in label for label in labels), "标号不是群号"

    # --- 4. 引用链只给结构，不给被引用的正文 ---------------------------------

    def test_the_reply_hint_says_who_was_answered_not_what(self) -> None:
        got = self.collect([
            _row(100.0, "你说的那句", direction="out", user_id="900000002",
                 message_id="mine", session_id="group:1", group_id="1"),
            _row(101.0, "别人说的那句", user_id="20002", message_id="theirs",
                 session_id="group:1", group_id="1"),
            _row(102.0, "接你的话", message_id="a", reply_to="mine",
                 session_id="group:1", group_id="1"),
            _row(103.0, "接别人的话", message_id="b", reply_to="theirs",
                 session_id="group:1", group_id="1"),
            _row(104.0, "引了一条翻不到的", message_id="c", reply_to="看不到的",
                 session_id="group:1", group_id="1"),
            _row(105.0, "没引谁", message_id="d", session_id="group:1", group_id="1"),
        ], now=106.0)

        hints = [item.reply for item in got.his_details]
        # 她自己那条（`direction="out"`）不是"他说的话"，所以不在这里；
        # 剩下四条按时间：接你的话 / 接别人的话 / 翻不到 / 没引谁。
        assert hints == ["接你的话", "接别人的话", "引了一条翻不到的话", ""], hints
        assert "别人说的那句" not in "".join(hints), "不许把被引用的正文带出来"

    def test_media_is_marked(self) -> None:
        got = self.collect([
            _row(100.0, "（发送了一条媒体消息）[表情包]", message_id="m1", has_media=True),
            _row(101.0, "纯文字", message_id="m2"),
        ], now=102.0)

        assert [item.media for item in got.his_details] == [True, False]

    # --- 5. 边界不许松（这一半是老规矩，别被新字段带坏）----------------------

    def test_only_the_recipients_own_words_and_nothing_without_a_recipient(self) -> None:
        rows = [
            _row(100.0, "他说的", message_id="m1"),
            _row(101.0, "别人说的", user_id="20002", message_id="m2"),
        ]

        got = self.collect(rows, now=102.0)
        assert got.his_lines == ("他说的",) and len(got.his_details) == 1

        got2 = self.collect(rows, owner_ids=(), now=102.0)
        assert got2.his_lines == () and got2.his_details == ()


if __name__ == "__main__":
    unittest.main()
