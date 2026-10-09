"""提醒被闸拦下时**要让她知道**（2026-10-09 实测：她承诺了一件没排上的事）。

日志：`13:29:09 remind: 这一条里没有时间（判定是从老话里翻出来的），不算` ✓ 规矩对 ✓
但 `13:29:26` 她回：*"行，一点四十叫你，414是吧。"* ✗✗ —— 13:40 什么都不会发生，
而用户会以为安排好了。**闸拦对了，可没把"没排上"告诉她。**

这一份钉住：

1. 被闸拦下的那一轮，她的易变段里多一句"问一句几点、**别自己先许一个时间**"；
   那句话**自己不带任何时间**（不许在 prompt 里给她一个可以照说的时刻）；
2. 接了这句之后，**真的没有新提醒**（没有交给插件、也没有待办）——回归用例；
3. 没有触发（普通聊天）时那句话不出现（别每轮都念）。

突变（各自还原）：去掉那句提示 → 第 1 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.remind import parse_remind_time
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8", "QQBOT_REMIND_MIN_INTERVAL_SECONDS": "0"}


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
                "<reply>行。</reply>")


def _engine(outputs: list[str], handed: list):
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    reply = _ScriptedReply()
    engine = DialogueEngine(reply, judge_client=_ScriptedJudge(outputs),
                            enabled_group_ids=frozenset({GROUP}),
                            remind=handed.append, wall_clock=lambda: NOW)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, reply


def _msg(index: int, text: str, *, user_id: str = "100"):
    return IncomingMessage(f"m{index}", SESSION, user_id, text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


#: 实测那条的形状：判定从窗口的老话里报了一个提醒，可他这一条里**没有时间**、
#: 也没提收法（所以走的是"被闸拦下"那一支，而不是"改这次"那一支）。
OLD_TALK = "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>"


def test_a_blocked_reminder_makes_her_ask_instead_of_promising() -> None:
    handed: list = []
    engine, reply = _engine([OLD_TALK], handed)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "414是吧")))

    volatile = reply.requests[-1][-1]["content"]
    assert "问一句几点" in volatile, "没告诉她'没排上'，她会照旧答应一个时间"
    assert "别自己先许一个时间" in volatile
    # 那句话**自己不许带时间**：prompt 里给她一个时刻，她就会照着说。
    note = volatile[volatile.index("（他这句没说清什么时候"):]
    assert parse_remind_time(note.split("）")[0], now=NOW) is None, note


def test_the_blocked_turn_creates_no_reminder() -> None:
    """回归：那一轮之后**真的没有**新提醒（现在就是没有，把它钉住）。"""

    handed: list = []
    engine, _ = _engine([OLD_TALK], handed)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "414是吧")))

    assert handed == [], "被闸拦下的不该排提醒"
    assert engine._remind_asks == {}, "也不该留待办"


def test_a_normal_turn_has_no_such_note() -> None:
    handed: list = []
    engine, reply = _engine(["<route>REPLY</route><topic>闲聊</topic>"], handed)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 在吗")))

    assert "没说清什么时候" not in reply.requests[-1][-1]["content"]


if __name__ == "__main__":
    import unittest

    unittest.main()
