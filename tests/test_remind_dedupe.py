"""提醒的**幂等**：同一件事只记一次待办、只生成一条提醒（2026-10-09 实测那条）。

用户实测（富哥之家）：同一句提醒请求被**每一轮**重新认出来——核心连着 **5 次**记
"还没有邮箱，先记待办"，插件那边**生成了两条提醒**（`a8c18f77…` 时间不对、
`3044b5ba…` 是 +5 分钟）。重复的来源是"那句话还留在判定读得到的窗口里"。

去重键取 **`(人, 事情)`**（不是 `(人, 事情, 绝对时刻)`）：那两条提醒正是**同一个 what、
不同的 at**——带绝对时刻的键拦不住它；带 `(人, 事情)` 才拦得住"同一句老话被反复认出来"。
窗口 `REMIND_DEDUPE_WINDOW_SECONDS`（10 分钟）只盖一轮对话里的反复识别：过一阵子再提
同一件事是**真新请求**，照收。

这一份钉住：

1. 同一件事连着来三遍 → **只交给插件一条**；
2. 同一件事、**时间说法变了** → 还是同一条（不新增第二条）；
3. 换一件事 / 换一个人 → 就是新的一条；
4. 窗口过了 → 同一件事可以再办；
5. 待办只记一次（同一件事的第二遍不改动它、也不重复记日志）。

突变（各自还原）：去掉去重那一段 → 第 1、2 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot import stage3_main
from qq_roleplay_bot.remind import (
    MAX_REMIND_SEEN,
    REMIND_DEDUPE_WINDOW_SECONDS,
    RemindRequest,
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
    async def complete(self, request):
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                "<reply>行，记着了。</reply>")


def _message(index: int, *, user_id: str = "100") -> IncomingMessage:
    return IncomingMessage(f"m{index}", SESSION, user_id, "@YunRu 明天8点提醒我交作业", TARGET,
                           sender_name=f"某人{user_id}", is_bot_mentioned=True)


class _Clock:
    """可以拨的墙上钟（默认停在一个固定时刻）。"""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(outputs: list[str], handed: list, clock: _Clock):
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = _ScriptedJudge(outputs)
    engine = DialogueEngine(_ScriptedReply(), judge_client=judge,
                            enabled_group_ids=frozenset({GROUP}),
                            remind=handed.append, wall_clock=clock)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine, judge


def _tag(time_text: str, what: str = "交作业", channel: str = "1") -> str:
    return f"<route>REPLY</route><topic>提醒</topic><remind>{time_text}|{what}|{channel}</remind>"


# --- 1 / 2. 同一件事只办一次 --------------------------------------------------


def test_the_same_request_three_times_hands_over_once() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, judge = _engine([_tag("明天8点")] * 3, handed, clock)
    with patch.dict(os.environ, TZ):
        for index in (1, 2, 3):
            asyncio.run(engine.handle(_message(index)))

    assert len(judge.requests) == 3, "三轮都真的过了判定（否则这条测试是空转）"
    assert len(handed) == 1, [item.what for item in handed]
    assert len(engine._remind_seen) == 1, "同一件事只该在表里留一行"


def test_the_same_thing_with_a_different_time_is_still_one_request() -> None:
    """实测那两条提醒就是这种：同一个 what、两个 at（一个时间还不对）。"""

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点"), _tag("明天9点")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))
        asyncio.run(engine.handle(_message(2)))

    assert len(handed) == 1, [item.at for item in handed]
    assert handed[0].at == datetime(2026, 10, 9, 8,
                                    tzinfo=timezone(timedelta(hours=8))).timestamp(), \
        "第一条算出来的时刻"


def test_the_pending_is_written_once() -> None:
    """同一件事的第二遍**不动**那条待办、也不重复记日志（实测是连记 5 次）。"""

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点", channel="")] * 3, handed, clock)   # 没定频道 → 留待办
    with patch.dict(os.environ, TZ), patch.object(stage3_main.logger, "info") as logged:
        for index in (1, 2, 3):
            asyncio.run(engine.handle(_message(index)))

    assert handed == []
    assert len(engine._remind_asks) == 1
    told = [call for call in logged.call_args_list if "记下这一条" in str(call)]
    assert len(told) == 1, [str(call) for call in told]


# --- 3. 换事 / 换人就是新的一条 -----------------------------------------------


def test_a_different_thing_is_a_new_request() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点"), _tag("明天9点", what="开会")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))
        asyncio.run(engine.handle(_message(2)))

    assert [item.what for item in handed] == ["交作业", "开会"]


def test_a_different_person_is_a_new_request() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点"), _tag("明天8点")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1, user_id="100")))
        asyncio.run(engine.handle(_message(2, user_id="200")))

    assert [item.person_id for item in handed] == ["100", "200"]


# --- 4. 窗口过了就照收 -------------------------------------------------------


def test_the_dedupe_window_expires() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点"), _tag("明天8点")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_message(1)))
        clock.now += REMIND_DEDUPE_WINDOW_SECONDS + 1
        asyncio.run(engine.handle(_message(2)))

    assert len(handed) == 2, "过了一阵子又提同一件事——那是真新请求，该照收"


# --- 5. 那张表有上限 ---------------------------------------------------------


def test_the_seen_table_is_bounded() -> None:
    """表有上限（超出淘汰最旧），过期的也顺手清掉。"""

    handed: list = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点")], handed, clock)
    for index in range(300):
        engine._remember_remind_seen((f"u{index}", f"w{index}"), NOW + index)
    assert len(engine._remind_seen) == MAX_REMIND_SEEN

    # 跳到窗口之后：旧的都过期了，清完只剩刚记的这条。
    engine._remember_remind_seen(("u-late", "w-late"), NOW + REMIND_DEDUPE_WINDOW_SECONDS + 10_000)
    assert len(engine._remind_seen) == 1, list(engine._remind_seen)


if __name__ == "__main__":
    import unittest

    unittest.main()
