"""人格文件读出来的**形状**（2026-10-11）。

由来：线上那份 system 前缀的第一个字符曾经是一个**反斜杠**，后面还跟了一个空行。
原因是人格文件把正文写成「赋值号 + 三个引号 + 一个反斜杠 + 换行」的行续行写法（为了文件里
好看），而 `base_prompt._load_base_prompt()` 是用正则抓**原始文本**的——反斜杠和那个换行
于是原样进了她的 system 开头。这里钉住三件事：

1. 开头的续行反斜杠不留；
2. 首尾空白不留（正文以换行结尾也不留）；
3. **正文内部的空行不许动**——那是她说话时分段用的。
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import base_prompt


def _load(text: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "persona.py"
        path.write_text(text, encoding="utf-8")
        with patch.dict(os.environ, {"QQBOT_BASE_PROMPT_FILE": str(path)}):
            return base_prompt._load_base_prompt()


def test_line_continuation_backslash_is_stripped():
    """`BASE_PROMPT = \"\"\"\\` 这种写法不许把 `\\` 带进正文。"""

    persona = _load('BASE_PROMPT = """\\\n第一行。\n\n第二行。\n"""\n')
    assert not persona.startswith("\\"), persona[:20]
    assert persona.startswith("第一行。"), persona[:20]


def test_surrounding_whitespace_is_trimmed():
    persona = _load('BASE_PROMPT = """\n\n   \n正文。\n\n\n"""\n')
    assert persona.startswith("正文。"), persona[:20]
    assert not persona.endswith("\n"), repr(persona[-10:])


def test_inner_blank_lines_survive():
    persona = _load('BASE_PROMPT = """A\n\n\nB"""\n')
    assert persona == "A\n\n\nB", repr(persona)
