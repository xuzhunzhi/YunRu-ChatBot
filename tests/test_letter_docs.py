"""信人格与《已确定的事》怎么读（`letter_docs.py`）。

要治的病（2026-10-12 用户原话）：

    我不理解为什么到处都是重启就出问题的。写信的人格prompt做单独适配。注意写信agent
    本身是插件里的，所以凭啥这玩意能直接复用人格prompt啊。

配套改动的两侧：核心这边新增 `letter_docs.py`（信专用人格 + 事实表，**每次现读**、
读不到就 `None`），插件那边删掉 `from ...base_prompt import BASE_PROMPT`。

这里钉住四条规矩：

1. 两种写法都认（模块形状 `LETTER_PERSONA = \"\"\"…\"\"\"` 与裸文本）——与 `base_prompt`
   同一个正则口径，别再各写一套；
2. **每次调用都重新读文件**（以前 `BASE_PROMPT` 是模块级常量，文件换了进程不知道）；
3. 读不到返回 `None`，**不回落模板**——理由写在 `letter_docs` 的模块开头
   （拿模板顶上去等于用不是她的字迹写信），这里的断言把那条话钉在测试里；
4. 机制词只提醒、不拦截（与 `base_prompt` 2026-10-11 的修正一致）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qq_roleplay_bot import letter_docs


def _write(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


class LetterPersonaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def load(self, text: str, *, name: str = "letter_persona.REAL.py"):
        path = _write(self.tmp, name, text)
        with mock.patch.dict(os.environ, {"QQBOT_LETTER_PERSONA_FILE": str(path)}):
            return letter_docs.load_letter_persona()

    def test_module_shape_is_read_from_the_assignment(self) -> None:
        body = self.load('LETTER_PERSONA = """\n夜里写的信，不赶时间。\n"""\n')

        assert body == "夜里写的信，不赶时间。", body

    def test_a_bare_text_file_is_used_as_is(self) -> None:
        body = self.load("夜里写的信，不赶时间。\n", name="letter_persona.REAL.txt")

        assert body == "夜里写的信，不赶时间。", body

    def test_the_line_continuation_backslash_is_stripped(self) -> None:
        """`LETTER_PERSONA = \"\"\"\\` 换行这种写法，那个 `\\` 不能进 system 前缀。"""

        body = self.load('LETTER_PERSONA = """\\\n夜里写的信。\n"""\n')

        assert body == "夜里写的信。", body

    def test_the_file_is_read_again_every_time(self) -> None:
        """同一进程里第二次调用要读到**新的**文件（这条是这次改动的要害）。"""

        path = _write(self.tmp, "letter_persona.REAL.py", 'LETTER_PERSONA = """第一版"""\n')
        with mock.patch.dict(os.environ, {"QQBOT_LETTER_PERSONA_FILE": str(path)}):
            first = letter_docs.load_letter_persona()
            path.write_text('LETTER_PERSONA = """第二版"""\n', encoding="utf-8")
            second = letter_docs.load_letter_persona()

        assert (first, second) == ("第一版", "第二版"), (first, second)

    def test_missing_file_is_none_and_we_do_not_write_that_night(self) -> None:
        with mock.patch.object(letter_docs, "persona_candidates",
                               lambda: (self.tmp / "nope.py",)):
            with self.assertLogs("qq_roleplay_bot.letter_docs", level="WARNING") as logs:
                body = letter_docs.load_letter_persona()

        assert body is None
        blob = "\n".join(logs.output)
        assert "今晚这封信不写" in blob, blob
        assert "不回落到模板" in blob, blob

    def test_an_empty_file_counts_as_missing(self) -> None:
        empty = _write(self.tmp, "empty.py", 'LETTER_PERSONA = """\n"""\n')
        with mock.patch.object(letter_docs, "persona_candidates", lambda: (empty,)):
            with self.assertLogs("qq_roleplay_bot.letter_docs", level="WARNING"):
                assert letter_docs.load_letter_persona() is None

    def test_mechanism_words_are_reported_but_do_not_block(self) -> None:
        """2016-10-11 的修正：命中机制词只提醒。禁句里出现这些词是正常的。"""

        text = 'LETTER_PERSONA = """\n不许提记忆库和协议。\n"""\n'
        with self.assertLogs("qq_roleplay_bot.letter_docs", level="WARNING") as logs:
            body = self.load(text)

        assert body == "不许提记忆库和协议。"
        assert "机制词" in "\n".join(logs.output)

    def test_too_long_is_truncated_and_said_out_loud(self) -> None:
        long_text = "字" * (letter_docs.MAX_DOC_CHARS + 50)
        with self.assertLogs("qq_roleplay_bot.letter_docs", level="WARNING") as logs:
            body = self.load(f'LETTER_PERSONA = """{long_text}"""\n')

        assert body is not None and len(body) == letter_docs.MAX_DOC_CHARS
        assert "上限" in "\n".join(logs.output)


class LetterFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_facts_use_the_same_shape_and_rules(self) -> None:
        path = _write(self.tmp, "letter_facts.REAL.txt",
                      'LETTER_FACTS = """\n【已确定的事】百夫长没保住。\n"""\n')
        with mock.patch.dict(os.environ, {"QQBOT_LETTER_FACTS_FILE": str(path)}):
            facts = letter_docs.load_letter_facts()

        assert facts == "【已确定的事】百夫长没保住。", facts

    def test_missing_facts_are_none_too(self) -> None:
        """事实表读不到同样不写：对着模板"校对"等于没校对，却装成校过了。"""

        with mock.patch.object(letter_docs, "facts_candidates",
                               lambda: (self.tmp / "nope.txt",)):
            with self.assertLogs("qq_roleplay_bot.letter_docs", level="WARNING") as logs:
                facts = letter_docs.load_letter_facts()

        assert facts is None
        assert "今晚这封信不写" in "\n".join(logs.output)

    def test_the_env_var_wins_over_the_default_location(self) -> None:
        assert str(letter_docs.facts_candidates()[0]).endswith("nope.txt") is False
        with mock.patch.dict(os.environ, {"QQBOT_LETTER_FACTS_FILE": "C:/tmp/x.txt"}):
            first = letter_docs.facts_candidates()[0]

        assert str(first).replace("\\", "/") == "C:/tmp/x.txt", first
