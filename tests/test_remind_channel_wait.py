"""「等他说怎么收」那条链：**每一轮都问、改得到、不会永远不响**（2026-10-09 实测）。

日志铁证：

    13:40:34  他说："五分钟后提醒我去导员办公室" ✓（时间清楚）
    13:40:36  remind: 记下这一条（怎么收=还没说）
    13:40:36  remind: **还没问清他想怎么收，先不交给插件**   ← 卡在这
    13:40:57  她回："那改五分钟后？一点四十五叫你。"          ← 该问的没问、自己默认了群里
    13:48/13:49（测试群）：同样"先不交给插件"，然后"他要改收法，**可没有待发的提醒**"

用户一句话：**"还是没叫我"**。

三件都在核心：

1. **每一轮都问**：只要他还欠一个"怎么收"，之后**每一轮**的易变段都要出那一句
   （措辞也改成"必须问出来、问清之前先别答应时间、也别自己挑一种"）；
2. **"这次群里叫我"要改得到核心手里那条**：`_remind_change` 先看**待办**（还没交给
   插件的那条），再看已经交出去的那条；
3. **不许永远不响**：`_expire_remind_channel_wait`——等过
   `dev_config.remind_channel_wait_seconds()`（默认 **600** 秒，夹 60..86400），
   **或者提醒时刻已经到了**，就先按**他上次真用过的收法**办、没有就**群里 @**；
   **不写进**长期偏好（下一次照旧每次都问）。

这一份钉住：真实序列（请求没带收法 → 她没问 → **下一轮仍然问** → 他回一个渠道 →
**真的排给插件**）· 每一轮都问 · "这次"改得到待办那条 · 超时兜底（上次用过的优先、
没有就群里）· 兜底不改长期偏好 · 配置默认与夹取。

突变（各自还原）：① 只问一轮就放弃 → 第 1 条红；② "这次"只找插件队列那条 → 第 2 条红；
③ 去掉超时兜底 → 第 3 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.remind import RemindRequest
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8", "QQBOT_REMIND_MIN_INTERVAL_SECONDS": "0"}
ASK = "在群里 @ 他、私聊他、还是寄到他邮箱"


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


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(outputs: list[str], handed: list, clock: _Clock):
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    reply = _ScriptedReply()
    engine = DialogueEngine(reply, judge_client=_ScriptedJudge(outputs),
                            enabled_group_ids=frozenset({GROUP}),
                            remind=handed.append, wall_clock=clock)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, reply


def _msg(index: int, text: str, *, user_id: str = "100"):
    return IncomingMessage(f"m{index}", SESSION, user_id, text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


#: 他说提醒、但没有说怎么收（实测那条）。
ASK_LESS = "<route>REPLY</route><topic>提醒</topic><remind>五分钟后|去导员办公室</remind>"
#: 普通聊天轮（判定没报提醒）。
PLAIN = "<route>REPLY</route><topic>闲聊</topic>"


# --- 1 / 真实序列：每一轮都问，直到他给一个渠道 -------------------------------


def test_the_real_sequence_ends_with_a_scheduled_reminder() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, reply = _engine([ASK_LESS, PLAIN, "<route>REPLY</route><topic>闲聊</topic>"
                             "<remind>五分钟后|去导员办公室|1</remind>"], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 五分钟后提醒我去导员办公室")))
        assert handed == [], "还没问清收法就不该排"
        assert ASK in reply.requests[-1][-1]["content"], "第一轮就要问"
        # 她没问、他也不答——**下一轮仍然要问**（实测就断在这里：只问一轮就烂在那儿）。
        asyncio.run(engine.handle(_msg(2, "@YunRu 那改五分钟后？")))
        assert ASK in reply.requests[-1][-1]["content"], "下一轮还得问"
        assert "别自己挑一种" in reply.requests[-1][-1]["content"], "措辞要拦得住她自己定"
        # 他这回说了收法（没给新时间）→ 交出去。
        asyncio.run(engine.handle(_msg(3, "群里叫我吧")))

    assert len(handed) == 1, [item.what for item in handed]
    assert handed[0].channel == "group"
    assert handed[0].what == "去导员办公室"


def test_the_ask_appears_on_every_turn_while_it_is_open() -> None:
    handed: list = []
    clock = _Clock()
    engine, reply = _engine([ASK_LESS] + [PLAIN] * 3, handed, clock)
    with patch.dict(os.environ, TZ):
        # 第一句就是那条请求（带着时间），后面几句是别的闲聊——每一轮都该出"问怎么收"。
        asyncio.run(engine.handle(_msg(1, "@YunRu 五分钟后提醒我去导员办公室")))
        for index in (2, 3, 4):
            clock.now += 30
            asyncio.run(engine.handle(_msg(index, f"@YunRu 第{index}句闲聊")))

    assert handed == []
    assert len(reply.requests) == 4
    for request in reply.requests:
        assert ASK in request[-1]["content"], "每一轮都要出那句"


# --- 2. "这次群里叫我" 改得到核心手里那条 -------------------------------------


def test_this_time_changes_the_pending_one() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([ASK_LESS,
                         "<route>REPLY</route><topic>闲聊</topic><remind>五分钟后|去导员办公室|2</remind>"],
                        handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 五分钟后提醒我去导员办公室")))
        assert handed == []
        # 实测 13:49:10 那句"他要改收法，可没有待发的提醒"就是这一条：待办也算"待发"。
        asyncio.run(engine.handle(_msg(2, "这次私聊我吧")))

    assert len(handed) == 1, [item.what for item in handed]
    assert handed[0].channel == "private"
    assert handed[0].forever is False, "改这一次不带长期标记"


# --- 3. 不许永远不响：超时兜底 -------------------------------------------------


def test_the_wait_expires_and_uses_the_last_channel() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([
        "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业|2</remind>",   # 先来一条私聊的
        ASK_LESS,                                                                   # 这条没说怎么收
        PLAIN,                                                                      # 等超时之后的那一轮
    ], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 明天8点提醒我交作业，私聊我")))
        assert handed[0].channel == "private"
        clock.now += 3600
        asyncio.run(engine.handle(_msg(2, "@YunRu 五分钟后提醒我去导员办公室")))
        assert len(handed) == 1, "还在等收法"
        # 等过他该等的时间（默认 600 秒）→ 按**他上次用过的**那种办。
        clock.now += dev_config.remind_channel_wait_seconds() + 1
        asyncio.run(engine.handle(_msg(3, "@YunRu 在吗")))

    assert len(handed) == 2, [item.what for item in handed]
    assert handed[1].channel == "private", "超时兜底优先用他上次真用过的收法"
    assert handed[1].forever is False, "兜底不算长期偏好"
    assert engine._remind_channel_pref == {}, "兜底不许写进长期偏好"


def test_the_wait_expires_to_the_group_when_there_is_no_history() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([ASK_LESS, PLAIN], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 五分钟后提醒我去导员办公室")))
        clock.now += dev_config.remind_channel_wait_seconds() + 1
        asyncio.run(engine.handle(_msg(2, "@YunRu 在吗")))

    assert len(handed) == 1
    assert handed[0].channel == "group", "没得挑就群里 @（他提问的地方，最不容易漏）"


def test_the_reminder_time_arriving_also_releases_it() -> None:
    """提醒时刻到了就别再等收法了——迟到总比不到强。"""

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([ASK_LESS, PLAIN], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 五分钟后提醒我去导员办公室")))
        assert handed == []
        clock.now += 301                      # 五分钟到了（但还没到 600 秒那道线）
        asyncio.run(engine.handle(_msg(2, "@YunRu 在吗")))

    assert len(handed) == 1, "提醒时刻已经到了，不该继续等收法"


def test_the_wait_config_is_clamped() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QQBOT_REMIND_CHANNEL_WAIT_SECONDS", None)
        assert dev_config.remind_channel_wait_seconds() == 600
    with patch.dict(os.environ, {"QQBOT_REMIND_CHANNEL_WAIT_SECONDS": "1"}):
        assert dev_config.remind_channel_wait_seconds() == 60, "下限 60 秒（不许永远不等）"
    with patch.dict(os.environ, {"QQBOT_REMIND_CHANNEL_WAIT_SECONDS": "999999"}):
        assert dev_config.remind_channel_wait_seconds() == 86400


if __name__ == "__main__":
    import unittest

    unittest.main()
