"""提醒的**邮箱那一步**：核心收、核心记、核心查好递给插件（2026-10-08 拍板）。

形状：**核心听懂 + 插件做事 + 核心记录 + 核心发送**。用户还说过一句更硬的：
**"插件绝不碰记忆"**。所以流程是：

1. 判定认到"到点提醒我"，但核心手上**还没有他的邮箱** → 记一条待办，
   这一轮由核心在易变段加**一句**话让她顺口问一句（`clarify_note` 口子，
   **不是**插件提示词）；**先不交给插件**（不递半条）；
2. 他在聊天里把邮箱说给她 → **核心**从这句话里认出邮箱（保守正则），
   存进核心那份按人的值，交给**既有那条记忆捕获**（`memory_service.capture`，按人入队），
   再把那条待办**装好邮箱**交给插件；
3. 之后再有人让他提醒 → 核心直接用手上那份值，交过去。

这一份钉住：

1. 没有邮箱时不交给插件、并且**那一轮 prompt 里多一句核心写的话**（有邮箱的那一轮**没有**）；
2. 他给了邮箱之后：`memory_service.capture` 收到这一条（**邮箱进记忆**）、
   插件拿到 `email=` 那条完整请求、待办清掉；
3. 认不出邮箱的那句话：什么都不做，继续等（不猜、不报错）；
4. 有邮箱之后再来的提醒：直接用、不重复问；
5. **插件侧仍然拿不到记忆**（`RemindRequest` 字段白名单，见 `test_remind_seam.py`）。

突变（各自还原）：去掉"认出邮箱就记进记忆"那一步（只存核心值）→ 第 2 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.remind import RemindEmailBook, RemindRequest
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}
ASK_LINE = "可你手上还没有他的邮箱"


class _ScriptedJudge:
    """按调用次数回不同的输出：第一轮认到提醒，之后普通回复。"""

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


class _MemoryStub:
    """最小记忆服务：记下每一句被捕获的消息（按 message_id 数次数）。

    数次数是有意的：**核心那条既有捕获**（`handle()` 里对每条消息都会叫一次）
    与本流程里"认出邮箱之后再明确交一次"是**两次**——后者正是"邮箱进记忆"这一步
    （它管的是"那一句在别的路上被跳过/被延迟"的情况，例如焦点队列里那一条）。
    """

    def __init__(self) -> None:
        self.captured: list[IncomingMessage] = []

    def capture(self, message) -> None:
        self.captured.append(message)

    def captured_ids(self) -> list[str]:
        return [item.message_id for item in self.captured]

    async def retrieve(self, message, topic, mentioned=()):
        from qq_roleplay_bot.memory_model import MemoryMaterial

        return MemoryMaterial()

    async def identity_for(self, message):
        from qq_roleplay_bot.memory_model import MemoryMaterial

        return MemoryMaterial()

    async def care_note(self, message, *, topic_shifted, current_terms=()):
        return None

    async def profile_for(self, message) -> str:
        return ""

    async def mark_care_noted(self, record_id):
        return None

    def snapshot(self) -> dict:
        return {"queued_events": 0}


class _ScriptedNotifier:
    """顶替 `prompt_sources.notify`：核心每一轮都会叫它一次，这里只要不炸。"""

    async def collect(self, context, **kwargs):
        from qq_roleplay_bot.stage3_runtime import PromptMaterial

        return PromptMaterial()

    async def notify(self, context, decision):
        return None


def _message(index: int, text: str) -> IncomingMessage:
    return IncomingMessage(f"m{index}", SESSION, "100", text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


def _engine(remind) -> tuple[DialogueEngine, _ScriptedJudge, _ScriptedReply, _MemoryStub]:
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = _ScriptedJudge([
        "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>",
        "<route>REPLY</route><topic>邮箱</topic>",
        "<route>REPLY</route><topic>提醒</topic><remind>后天9点|开会</remind>",
    ])
    reply = _ScriptedReply()
    memory = _MemoryStub()
    engine = DialogueEngine(reply, judge_client=judge, enabled_group_ids=frozenset({GROUP}),
                            remind=remind, wall_clock=lambda: NOW)
    engine.memory_service = memory
    engine.prompt_sources = _ScriptedNotifier()
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, judge, reply, memory


# --- 1. 没有邮箱：先问、不交 -------------------------------------------------


def test_without_the_email_the_core_asks_and_does_not_hand_over() -> None:
    handed: list[RemindRequest] = []
    engine, _, reply, _ = _engine(handed.append)
    with patch.dict(os.environ, TZ):
        result = asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))

    assert result is not None
    assert handed == [], "还没有邮箱就把请求递出去了"
    assert "100" in engine._remind_asks, "没记待办"
    volatile = reply.requests[0][-1]["content"]
    assert ASK_LINE in volatile, "核心没有让她问一句邮箱"
    assert "邮箱" in volatile


def test_the_ask_line_is_gone_once_the_email_is_known() -> None:
    handed: list[RemindRequest] = []
    engine, _, reply, _ = _engine(handed.append)
    engine._remind_emails = RemindEmailBook(persist=False)   # 内存版：这一条测的是流程
    engine._remind_emails.remember("100", "lele@example.com")
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))

    assert len(handed) == 1
    assert ASK_LINE not in reply.requests[0][-1]["content"], "已经有邮箱了还在问"


# --- 2. 他给了邮箱：进记忆 + 交出去 -----------------------------------------


def test_the_email_is_captured_into_memory_and_then_handed_over() -> None:
    handed: list[RemindRequest] = []
    engine, _, _, memory = _engine(handed.append)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
        asyncio.run(engine.handle(_message(2, "我的邮箱是 lele@example.com")))

    # 两次：`handle()` 那条既有捕获（每条消息一次）+ 本流程认出邮箱后**明确**交的那一次。
    assert memory.captured_ids().count("m2") == 2, memory.captured_ids()
    assert len(handed) == 1
    assert handed[0].email == "lele@example.com"
    assert handed[0].at == datetime(2026, 10, 9, 8, tzinfo=timezone(timedelta(hours=8))).timestamp()
    assert handed[0].what == "交作业"
    assert "100" not in engine._remind_asks, "待办没清掉"
    assert engine._remind_emails.get("100") == "lele@example.com"


def test_a_later_remind_reuses_the_remembered_email() -> None:
    handed: list[RemindRequest] = []
    engine, _, _, _ = _engine(handed.append)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
        asyncio.run(engine.handle(_message(2, "lele@example.com")))
        asyncio.run(engine.handle(_message(3, "@YunRu 后天9点提醒我开会")))

    assert [item.what for item in handed] == ["交作业", "开会"]
    assert handed[1].email == "lele@example.com", "第二次没有用记下来的那个值"
    assert handed[1].at == datetime(2026, 10, 10, 9, tzinfo=timezone(timedelta(hours=8))).timestamp()


# --- 3. 认不出邮箱：继续等 ---------------------------------------------------


def test_a_message_without_an_email_keeps_waiting() -> None:
    handed: list[RemindRequest] = []
    engine, _, _, memory = _engine(handed.append)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
        asyncio.run(engine.handle(_message(2, "好，你说得对")))

    assert len(engine._remind_emails) == 0, "认不出邮箱却把它存下来了"
    assert handed == []
    assert "100" in engine._remind_asks, "还没给邮箱就把待办清了"


# --- 4. 没有待办时不许乱收 ---------------------------------------------------


def test_an_email_without_a_pending_remind_is_ignored() -> None:
    """没在等邮箱时，一句邮箱就是普通聊天：不捕获、不留值。"""

    handed: list[RemindRequest] = []
    engine, judge, _, memory = _engine(handed.append)
    # 这一轮判定**没有**认提醒（就是一个普通话题）。
    judge.outputs = ["<route>REPLY</route><topic>闲聊</topic>"]
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(2, "lele@example.com")))

    assert len(engine._remind_emails) == 0, "没在等邮箱也把它记下来了"
    assert handed == []
    assert engine._remind_asks == {}


if __name__ == "__main__":
    import unittest

    unittest.main()
