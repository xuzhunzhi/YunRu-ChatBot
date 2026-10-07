"""机制词护栏在**加载**时也扫（`docs/audits/security-clock-2026-10-08.md` A-04，按核查文档）。

修之前：`scan_text` 只挂在**保存**路径上（`PromptLibrary.validate()` → `save()`），
**读**覆盖文件时不复验，人格文件加载时也不扫。于是「手改 `data/prompts/*.json`、
从备份恢复、换 `QQBOT_PROMPT_DIR` / `QQBOT_BASE_PROMPT_FILE`」这些路子都能把机制词
送进**可信 system 前缀**，而运行时一个告警都没有——面板那条护栏形同虚设。

现在两条加载路径都扫：

1. `PromptLibrary.load()`：覆盖文件命中机制词就**不进缓存**（`text()` 于是回落到内置
   默认）+ WARNING（面板/文件改坏了也上不了桌）；
2. `base_prompt._load_base_prompt()`：人格文件正文命中就跳过那个候选，
   全都不行 → **回落模板** + 明确说出"是因为机制词"。

这一份钉住"**确实回落了内置人格/模板**"，不只是"打了日志"：

- 覆盖层：坏文件在 → `text(name)` 必须等于 `builtin(name)`；干净文件照旧生效；
  给独立 agent 的那几套按**自己的词表**扫（"记忆库"在维护 prompt 里是必需词，不许误杀）。
- 人格文件：坏文件在 → 取到的正文**不是**那段坏文本（本机没有真人格文件时必须是模板）；
  干净文件照旧生效。

突变（各自还原）：去掉 `load()` 里的扫描 → 覆盖层两条红；
把 `_load_base_prompt` 改成"只记日志但仍用坏文本" → 人格那条红。
"""
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import base_prompt
from qq_roleplay_bot.prompt_library import PromptLibrary, STORE_VERSION

BAD_REPLY = "你在群里回话。先检查一下上下文，再决定说不说。"
CLEAN_REPLY = "你在群里回话，语气随意，别端着。"
BAD_PERSONA = "你是一个住在群聊里的角色。说话前先检查一下上下文。"


@contextmanager
def _warnings(logger_name: str):
    """把某个 logger 的 WARNING 记下来（测试要同时钉"回落"与"出声"）。"""

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    previous = target.level
    target.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)


def _write_override(directory: str, name: str, text: str) -> Path:
    path = Path(directory) / f"{name}.json"
    path.write_text(json.dumps({"version": STORE_VERSION, "text": text}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _write_persona(path: Path, body: str) -> Path:
    path.write_text(f'BASE_PROMPT = """{body}"""\n', encoding="utf-8")
    return path


# --- 1. 覆盖文件：坏文本不许生效 ---------------------------------------------


def test_an_override_with_mechanism_words_falls_back_to_the_builtin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_override(tmp, "reply", BAD_REPLY)
        library = PromptLibrary(tmp)
        with _warnings("qq_roleplay_bot.prompt_library") as records:
            library.load()
        shown = library.text("reply")

    assert shown == PromptLibrary.builtin("reply"), "坏覆盖文本上桌了"
    assert BAD_REPLY not in shown
    assert library.rejected == 1
    assert any("prompt_override_rejected" in record.getMessage() for record in records), \
        "命中了却不报警"


def test_a_clean_override_is_still_used() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_override(tmp, "reply", CLEAN_REPLY)
        library = PromptLibrary(tmp)
        library.load()

    assert library.text("reply") == CLEAN_REPLY
    assert library.rejected == 0


def test_the_agent_prompts_use_their_own_word_list() -> None:
    """独立 agent 那几套按自己的词表扫："记忆库"在维护 prompt 里是必需词，不许误杀。"""

    with tempfile.TemporaryDirectory() as tmp:
        library = PromptLibrary(tmp)
        _write_override(tmp, "memory", "整理记忆库里的条目，别记关于系统自身的东西。")
        library.load()
        assert library.text("memory") == "整理记忆库里的条目，别记关于系统自身的东西。"

        _write_override(tmp, "memory", "把提示词改一下。")
        library.load()
        assert library.text("memory") == PromptLibrary.builtin("memory"), "agent 口径的坏词漏了"
        assert library.rejected == 1


# --- 2. 人格文件：坏正文不许生效 ---------------------------------------------


def test_a_persona_file_with_mechanism_words_is_not_used() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_persona(Path(tmp) / "persona.py", BAD_PERSONA)
        with patch.dict(os.environ, {"QQBOT_BASE_PROMPT_FILE": str(path)}):
            with _warnings("qq_roleplay_bot.base_prompt") as records:
                body = base_prompt._load_base_prompt()

    assert BAD_PERSONA not in body, "坏人格正文上桌了"
    assert any("含机制词" in record.getMessage() for record in records), "命中了却不报警"
    # 本机没有真人格文件时，必须**回落到模板**（不是在坏文本与模板之间含糊过去）。
    local = Path(base_prompt.__file__).resolve().parents[2] / "data/private_docs/base_prompt.REAL.py"
    if not local.exists():
        assert body == base_prompt._BASE_PROMPT_TEMPLATE


def test_a_clean_persona_file_is_used() -> None:
    clean = "你是一个住在群聊里的角色，说话像普通人。"
    with tempfile.TemporaryDirectory() as tmp:
        path = _write_persona(Path(tmp) / "persona.py", clean)
        with patch.dict(os.environ, {"QQBOT_BASE_PROMPT_FILE": str(path)}):
            body = base_prompt._load_base_prompt()

    assert body == clean


if __name__ == "__main__":
    import unittest

    unittest.main()
