"""信里"他"那一份怎么取（`letter_context.py`）。

要治的病（2026-10-13 用户原话）：

    加，不是说了允许读取记忆吗，你是不是不懂什么叫不是非要一次输入就得到一封信

在那之前素材里**一句人话都没有**（`chat_window` 只数千条、只给号码），她手上唯一有
内容的东西是那份固定的《已确定的事》——实测同一份素材三封信里，有一封把事实表里的
旧事写成了"今天"发生的新事，用户读到的那封"根本不像是人写的信"就是这么来的。

这里钉住四条规矩（每一条都是这一整个模块的边界，不是防御性写法）：

1. **只有收信人自己说过的话**进得来：别人的话一句都不给——信只寄给他一个人；
2. **认不出收信人就一句话都不给**（`owner_ids` 为空 = fail-closed）：宁可这封没素材；
3. 只取窗口内、有正文、非表情回应、也不是斜杠命令的那几条，按时间从早到晚留最近 8 条；
4. 读不出日志就是 `None`（调用方按"他这几天没说什么"处理），**不编**。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qq_roleplay_bot import letter_context


def _row(at: float, body: str, *, user_id: str = "10001", direction: str = "in",
         kind: str = "text") -> dict:
    return {"at": at, "body": body, "user_id": user_id, "direction": direction, "kind": kind}


class LetterContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "chat.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rows: list[dict]) -> Path:
        self.log.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return self.log

    def collect(self, rows: list[dict], *, since: float = 0.0, owner_ids=("10001",),
                last_letter=None):
        self.write(rows)
        return letter_context.collect_letter_context(
            self.log, since=since, owner_ids=owner_ids, last_letter=last_letter)

    # --- 1. 只有他一个人的话 ---------------------------------------------------

    def test_only_the_recipients_own_lines_come_through(self) -> None:
        got = self.collect([
            _row(100.0, "他说的第一句"),
            _row(101.0, "别人说的，不该出现", user_id="20002"),
            _row(102.0, "她说的也不该出现", user_id="10001", direction="out"),
            _row(103.0, "他说的第二句"),
        ])

        assert got is not None
        assert got.his_lines == ("他说的第一句", "他说的第二句"), got.his_lines

    def test_without_a_recipient_nothing_comes_through(self) -> None:
        """认不出收信人就一句都不给——这一条是模块的边界，不是兜底。"""

        got = self.collect([_row(100.0, "谁说的话都算的话，这封信就寄错了")],
                           owner_ids=())

        assert got is not None
        assert got.his_lines == ()

    def test_several_recipients_are_allowed(self) -> None:
        """部署级管理员 ∪ 超管可能不止一个（`_letter_owner_ids()` 就是这么给的）。"""

        got = self.collect([
            _row(100.0, "甲", user_id="10001"),
            _row(101.0, "乙", user_id="10002"),
            _row(102.0, "丙", user_id="30003"),
        ], owner_ids=("10001", "10002"))

        assert got.his_lines == ("甲", "乙")

    # --- 2. 窗口 / 形状 --------------------------------------------------------

    def test_lines_before_the_window_are_dropped(self) -> None:
        got = self.collect([_row(100.0, "太早了"), _row(500.0, "窗口里的")], since=200.0)

        assert got.his_lines == ("窗口里的",)
        assert got.since == 200.0

    def test_reactions_and_commands_are_not_his_words(self) -> None:
        got = self.collect([
            _row(100.0, "正经话"),
            _row(101.0, "点了个赞", kind="reaction"),
            _row(102.0, "/super mail send"),
            _row(103.0, "／全角斜杠也是命令"),
            _row(104.0, "!bang"),
            _row(105.0, "  "),
        ])

        assert got.his_lines == ("正经话",), got.his_lines

    def test_newlines_collapse_and_long_lines_are_clipped(self) -> None:
        got = self.collect([
            _row(100.0, "第一行\n第二行"),
            _row(101.0, "长" * (letter_context.MAX_LINE_CHARS + 40)),
        ])

        assert got.his_lines[0] == "第一行 第二行"
        clipped = got.his_lines[1]
        assert len(clipped) == letter_context.MAX_LINE_CHARS + 1, len(clipped)
        assert clipped.endswith("…")

    def test_only_the_last_lines_are_kept(self) -> None:
        rows = [_row(100.0 + i, f"第{i}句") for i in range(12)]

        got = self.collect(rows)

        assert len(got.his_lines) == letter_context.MAX_LINES == 8
        assert got.his_lines[-1] == "第11句", "留最近说的那几句"

    def test_consecutive_duplicates_collapse(self) -> None:
        got = self.collect([_row(100.0, "在吗"), _row(101.0, "在吗"), _row(102.0, "在？")])

        assert got.his_lines == ("在吗", "在？")

    # --- 3. 上一封信 ------------------------------------------------------------

    def test_the_last_letter_comes_through(self) -> None:
        got = self.collect([_row(100.0, "在吗")],
                           last_letter={"at": 99.0, "subject": "没别的，说件事"})

        assert got.last_letter_at == 99.0
        assert got.last_letter_subject == "没别的，说件事"

    def test_a_missing_or_broken_last_letter_is_empty(self) -> None:
        got = self.collect([_row(100.0, "在吗")], last_letter={"at": "坏值"})
        assert got.last_letter_at == 0.0 and got.last_letter_subject == ""

        got2 = self.collect([_row(100.0, "在吗")], last_letter=None)
        assert got2.last_letter_at == 0.0 and got2.last_letter_subject == ""

    def test_the_subject_is_bounded(self) -> None:
        got = self.collect([_row(100.0, "在吗")],
                           last_letter={"at": 1.0, "subject": "主" * 400})

        assert len(got.last_letter_subject) == letter_context.MAX_SUBJECT_CHARS

    # --- 4. 读不出来就是没有 ----------------------------------------------------

    def test_a_missing_log_reads_as_nothing(self) -> None:
        got = letter_context.collect_letter_context(
            self.tmp / "没有这个文件.jsonl", since=0.0, owner_ids=("10001",))

        assert got is None

    def test_a_broken_line_is_skipped_not_fatal(self) -> None:
        self.log.write_text(
            json.dumps(_row(100.0, "好行"), ensure_ascii=False) + "\n"
            "{不是 json\n" + "\n",
            encoding="utf-8",
        )
        got = letter_context.collect_letter_context(
            self.log, since=0.0, owner_ids=("10001",))

        assert got is not None and got.his_lines == ("好行",)


class SeamBinderTest(unittest.TestCase):
    """装配那一层（`runtime._SeamBinder.report_letter_context`）只做三件事。

    读法是 `letter_context.collect_letter_context`，这里钉的是**它怎么被喂**：
    收信人来自引擎自己那份名单，上一封信取最新一条，日志路径拿不到就什么都不给。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "chat.jsonl"
        self.log.write_text(
            json.dumps(_row(600.0, "窗口里他说的"), ensure_ascii=False) + "\n"
            + json.dumps(_row(10.0, "窗口之前的"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _binder(self, engine):
        from qq_roleplay_bot import runtime

        return runtime._SeamBinder(engine)

    def test_the_recipient_list_and_the_last_letter_come_from_the_engine(self) -> None:
        from unittest import mock

        from qq_roleplay_bot import runtime

        class _Engine:
            letter_history = [{"at": 999.0, "subject": "上一封的主题"}]

            def _letter_owner_ids(self):
                return {"10001"}

        with mock.patch.object(runtime, "_chat_log_path", return_value=self.log):
            got = self._binder(_Engine()).report_letter_context(500.0)

        assert got is not None
        assert got.his_lines == ("窗口里他说的",), "窗口起点要透下去"
        assert got.last_letter_at == 999.0 and got.last_letter_subject == "上一封的主题"

    def test_an_engine_that_cannot_name_the_recipient_gives_nothing(self) -> None:
        """认不出收信人 = 一句都不给（不是"那就都给"）。"""

        from unittest import mock

        from qq_roleplay_bot import runtime

        class _Engine:
            letter_history = []

        with mock.patch.object(runtime, "_chat_log_path", return_value=self.log):
            got = self._binder(_Engine()).report_letter_context(500.0)

        assert got is not None and got.his_lines == ()

    def test_without_a_chat_log_there_is_nothing(self) -> None:
        from unittest import mock

        from qq_roleplay_bot import runtime

        class _Engine:
            letter_history = []

            def _letter_owner_ids(self):
                return {"10001"}

        with mock.patch.object(runtime, "_chat_log_path", return_value=None):
            got = self._binder(_Engine()).report_letter_context(500.0)

        assert got is None


if __name__ == "__main__":
    unittest.main()
