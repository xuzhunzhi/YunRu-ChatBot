"""提醒的**窄接缝**：`registry.remind`（形状同 `registry.vision`，但更窄）。

用户 2026-10-08 拍板："**核心听懂 + 插件做事 + 核心记录 + 核心发送**"。核心那一侧
（判定里认出来 + 算绝对时刻，见 `tests/test_remind_intent.py`）把这一条交给插件；
**插件没装就什么都不发生**——不报错、不回复、不留半条状态。

这一份钉住：

1. 没有插件（`engine.remind is None`）：认到提醒也**什么都不发生**，这一轮照常回复；
2. 有插件：交过去的 `RemindRequest` 带的是**绝对时刻**、谁、提醒什么、发到哪个会话；
3. 时间认不清 → **不交**（当普通聊天）；
4. 插件抛错：只记日志，**吃不掉这一轮回复**；
5. **插件拿不到记忆**：`RemindRequest` 的字段是一张白名单（没有引擎、没有记忆服务、
   没有回调）——"插件绝不碰记忆"这条靠它成立；
6. `registry.remind` 缺省是 None（插件只在 `register()` 里放上来）。

突变（各自还原）：插件缺失时改成报错/回复 → 第 1 条红；让核心把记忆交给插件
（往 `RemindRequest` 里加字段）→ 第 5 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.plugins import PluginRegistry
from qq_roleplay_bot.remind import RemindEmailBook, RemindRequest
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}

#: 插件那一侧只允许看到这几个值（白名单，第 5 条判据）。
ALLOWED_FIELDS = frozenset({"person_id", "session_id", "group_id", "at", "what", "email",
                            "key", "channel"})


class _RemindJudge:
    def __init__(self, remind: str) -> None:
        self.output = f"<route>REPLY</route><topic>提醒</topic>{remind}"
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class _ScriptedReply:
    def __init__(self) -> None:
        self.text = "行，到点我叫你。"
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.text}</reply>")


def _message(text: str = "@YunRu 明天8点提醒我交作业") -> IncomingMessage:
    return IncomingMessage("r1", SESSION, "100", text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


def _engine(remind, *, remind_tag: str = "<remind>明天8点|交作业|2</remind>"):
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = _RemindJudge(remind_tag)
    reply = _ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge, enabled_group_ids=frozenset({GROUP}),
                            remind=remind, wall_clock=lambda: NOW)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, judge, reply


# --- 1 / 6. 没插件就什么都不发生 ---------------------------------------------


def test_the_registry_seam_is_none_until_a_plugin_puts_something_there() -> None:
    registry = PluginRegistry()
    assert registry.remind is None
    marker = object()
    registry.remind = marker
    assert registry.remind is marker


def test_without_the_plugin_nothing_happens_but_the_turn_still_replies() -> None:
    engine, judge, reply = _engine(None)
    with patch.dict(os.environ, TZ):
        result = asyncio.run(engine.handle(_message()))

    assert engine.remind is None
    assert result is not None and result.text == reply.text, "没插件就不回了？"
    assert len(judge.requests) == 1 and len(reply.requests) == 1
    # 那一轮的消息照旧进历史（"什么都没发生"不等于"这条被吞掉"）。
    history_ids = [item.message_id for item in engine.sessions.state(SESSION).history]
    assert "r1" in history_ids, history_ids


# --- 2 / 3. 有插件：交过去的是绝对时刻 ---------------------------------------


def test_the_plugin_gets_the_request_with_an_absolute_time() -> None:
    """核心算好绝对时刻、问清怎么收、查出邮箱之后才交过去。"""

    handed: list[RemindRequest] = []
    engine, _, _ = _engine(handed.append)
    # 核心手里已经有他的邮箱（内存版的书：这一条只测投递，不碰磁盘）。
    engine._remind_emails = RemindEmailBook(persist=False)
    engine._remind_emails.remember("100", "lele@example.com")
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message()))

    assert len(handed) == 1
    request = handed[0]
    assert request.at == datetime(2026, 10, 9, 8, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert request.what == "交作业"
    assert request.person_id == "100"
    assert request.session_id == SESSION
    assert request.group_id == GROUP
    assert request.channel == "private", "判定给的 `2` 要翻成核心的说法"
    assert request.email == "lele@example.com", "核心查出来的值要原样递过去"


def test_without_an_email_the_request_is_not_handed_over_yet() -> None:
    """他选的是邮箱那条路、而邮箱还没有 → 不交（让她先问，见 `test_remind_email.py`）。"""

    handed: list[RemindRequest] = []
    engine, _, reply = _engine(handed.append, remind_tag="<remind>明天8点|交作业|3</remind>")
    with patch.dict(os.environ, TZ):
        result = asyncio.run(engine.handle(_message()))

    assert handed == [], "还没有邮箱就把请求交给插件了"
    assert engine._remind_asks["100"].channel == "email", "待办里要记着他想收邮箱"
    assert result is not None and result.text == reply.text


def test_an_unclear_time_is_not_handed_over() -> None:
    handed: list[RemindRequest] = []
    engine, _, reply = _engine(handed.append,
                               remind_tag="<remind>有空提醒我|交作业|2</remind>")
    with patch.dict(os.environ, TZ):
        result = asyncio.run(engine.handle(_message()))

    assert handed == [], "时间说不清也交出去了（猜了）"
    assert result is not None and result.text == reply.text


def test_no_remind_intent_means_no_call() -> None:
    handed: list[RemindRequest] = []
    engine, _, _ = _engine(handed.append, remind_tag="")
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message()))
    assert handed == []


# --- 4. 插件坏了也不吃掉这一轮 -----------------------------------------------


def test_a_plugin_error_does_not_eat_the_turn() -> None:
    def boom(request):
        raise RuntimeError("插件炸了")

    engine, _, reply = _engine(boom)
    engine._remind_emails = RemindEmailBook(persist=False)
    engine._remind_emails.remember("100", "lele@example.com")
    with patch.dict(os.environ, TZ):
        result = asyncio.run(engine.handle(_message()))
    assert result is not None and result.text == reply.text


# --- 5. 插件拿不到记忆 -------------------------------------------------------


def test_the_request_is_a_whitelist_with_no_memory_or_engine() -> None:
    fields = set(RemindRequest.__dataclass_fields__)
    assert fields == ALLOWED_FIELDS, f"接缝上多了字段：{fields - ALLOWED_FIELDS}"
    # 那几个字段里一个都不能是"能反查回核心"的东西。
    request = RemindRequest(person_id="100", session_id=SESSION, group_id=GROUP,
                            at=NOW, what="交作业")
    for forbidden in ("store", "memory", "memory_service", "engine", "transport",
                      "callback", "on_due", "send"):
        assert not hasattr(request, forbidden), forbidden


if __name__ == "__main__":
    import unittest

    unittest.main()
