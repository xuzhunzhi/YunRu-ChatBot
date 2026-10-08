"""核心那份按人的邮箱**落盘**：重启后仍在（独立审查 2026-10-09 第 10 条）。

用户 2026-10-08 的要求是"**邮箱只第一次要，之后记住**"。而 `_remind_emails` 原来只是
一个内存 `dict`：重启就空，于是同一个人的邮箱会被重新问一遍——违背他要的行为。
现在它是 `remind.RemindEmailBook`：标准库 JSON、**原子写入**、任何读写失败降级、
上限 `MAX_REMIND_EMAILS`，落在**核心自己的状态文件**（`data/remind_emails.json`，
**不是** `data/memory/`——那是记忆库）。

这一份钉住：

1. 记下来的邮箱**换一个实例还在**（模拟重启）；
2. **引擎级重启**：新引擎（同一个状态文件）认得那个值，直接交给插件、不再问；
3. 路径：默认在 `data/` 下、**不在 `data/memory/`** 下；`QQBOT_REMIND_EMAILS_FILE` 可覆盖；
   `QQBOT_STATE_PERSIST=0`（离线测试入口就是它）时**一个字节都不写**；
4. 坏文件 / 版本不对 → 按空的重来（不炸）；
5. 上限：超了淘汰最旧的（与核心别处那些"按人小表"同一个纪律）。

突变（各自还原）：把 `remember()` 里的 `_save()` 去掉（只写内存）→ 第 1、2 条红。
"""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.remind import (
    MAX_REMIND_EMAILS,
    REMIND_EMAILS_VERSION,
    RemindEmailBook,
    remind_emails_path,
)
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}


class _ScriptedJudge:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.outputs) - 1)
        return self.outputs[index]


class _ScriptedReply:
    def __init__(self) -> None:
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                "<reply>好，记下了。</reply>")


def _message(index: int, text: str) -> IncomingMessage:
    return IncomingMessage(f"m{index}", SESSION, "100", text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


# --- 1 / 2. 重启后仍在 --------------------------------------------------------


def test_the_book_survives_a_new_instance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"
        first = RemindEmailBook(path=path, persist=True)
        first.remember("100", "lele@example.com")
        assert path.is_file(), "没有落盘"

        # 模拟重启：换一个实例，从同一个文件读。
        second = RemindEmailBook(path=path, persist=True)
        assert second.get("100") == "lele@example.com"
        assert "100" in second and len(second) == 1


def test_the_saved_shape_is_versioned_and_plain() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"
        book = RemindEmailBook(path=path, persist=True)
        book.remember("100", "lele@example.com")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"version": REMIND_EMAILS_VERSION,
                        "emails": {"100": "lele@example.com"}}


def test_a_restarted_engine_does_not_ask_again() -> None:
    """引擎级重启：新引擎认得上一轮记下的邮箱，直接交给插件、不再问。"""

    handed: list = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"

        def _engine(outputs: list[str]) -> DialogueEngine:
            from qq_roleplay_bot.runtime_flags import RuntimeFlags

            judge = _ScriptedJudge(outputs)
            engine = DialogueEngine(_ScriptedReply(), judge_client=judge,
                                    enabled_group_ids=frozenset({GROUP}),
                                    remind=handed.append, wall_clock=lambda: NOW)
            engine._default_flags = RuntimeFlags(review_enabled=False)
            engine._remind_emails = RemindEmailBook(path=path, persist=True)
            return engine

        with patch.dict(os.environ, TZ):
            first = _engine([
                "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>",
                "<route>REPLY</route><topic>邮箱</topic>",
            ])
            asyncio.run(first.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
            assert handed == [], "还没有邮箱就交了"
            asyncio.run(first.handle(_message(2, "我的邮箱是 lele@example.com")))
            assert [item.email for item in handed] == ["lele@example.com"]

            # 重启：新进程、新引擎、新书，同一个文件。
            second = _engine([
                "<route>REPLY</route><topic>提醒</topic><remind>后天9点|开会</remind>",
            ])
            assert second._remind_emails.get("100") == "lele@example.com"
            asyncio.run(second.handle(_message(3, "@YunRu 后天9点提醒我开会")))

    assert [item.what for item in handed] == ["交作业", "开会"]
    assert handed[1].email == "lele@example.com", "重启之后又问了一遍 / 值丢了"


# --- 3. 路径与开关 -----------------------------------------------------------


def test_the_default_path_is_core_state_not_the_memory_library() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QQBOT_REMIND_EMAILS_FILE", None)
        os.environ.pop("QQBOT_DATA_DIR", None)
        path = remind_emails_path()
    assert path.name == "remind_emails.json"
    assert "memory" not in path.parts, path
    assert path.parent.name == "data", path


def test_the_path_can_be_overridden() -> None:
    with patch.dict(os.environ, {"QQBOT_REMIND_EMAILS_FILE": r"C:\tmp\x.json"}):
        assert str(remind_emails_path()).endswith("x.json")
    with patch.dict(os.environ, {"QQBOT_STATE_PERSIST": "0", "QQBOT_DATA_DIR": r"C:\tmp\dd"}):
        assert str(remind_emails_path()).endswith("dd") is False  # 名字仍是文件
        assert Path(str(remind_emails_path())).name == "remind_emails.json"


def test_persistence_off_writes_nothing() -> None:
    """离线测试入口把 `QQBOT_STATE_PERSIST` 设成 0——那种情况下一个字节都不写。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"
        with patch.dict(os.environ, {"QQBOT_STATE_PERSIST": "0"}):
            book = RemindEmailBook(path=path)          # persist 现读开关
            assert book.persist is False
            book.remember("100", "lele@example.com")
        assert book.get("100") == "lele@example.com", "内存里也要有"
        assert not path.exists(), "开关关掉还写盘了"


# --- 4. 坏文件降级 -----------------------------------------------------------


def test_a_broken_file_degrades_to_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"
        for payload in ("{不是 JSON", json.dumps({"version": 999, "emails": {"1": "a@b.com"}}),
                        json.dumps({"version": REMIND_EMAILS_VERSION, "emails": ["不是字典"]})):
            path.write_text(payload, encoding="utf-8")
            book = RemindEmailBook(path=path, persist=True)
            assert len(book) == 0, payload
            assert book.last_error, payload


def test_non_string_entries_are_dropped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"
        path.write_text(json.dumps({"version": REMIND_EMAILS_VERSION,
                                    "emails": {"100": "a@b.com", "": "x@y.com", "200": 5}}),
                        encoding="utf-8")
        book = RemindEmailBook(path=path, persist=True)
        assert book.as_dict() == {"100": "a@b.com"}


# --- 5. 上限 -----------------------------------------------------------------


def test_the_book_is_bounded_and_drops_the_oldest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "remind_emails.json"
        book = RemindEmailBook(path=path, persist=True, limit=3)
        for index in range(5):
            book.remember(f"u{index}", f"u{index}@example.com")
        assert len(book) == 3
        assert list(book.as_dict()) == ["u2", "u3", "u4"], "淘汰的不是最旧的"
        # 落盘的也是修剪过的那三份
        assert len(RemindEmailBook(path=path, persist=True).as_dict()) == 3


def test_the_default_limit_is_the_documented_one() -> None:
    assert MAX_REMIND_EMAILS == 256


if __name__ == "__main__":
    import unittest

    unittest.main()
