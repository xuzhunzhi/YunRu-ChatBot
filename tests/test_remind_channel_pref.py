"""提醒的收法：**每次都问**，除非他说"以后都这么干"；"这次…"只改这一条。

用户 2026-10-09 改的规矩（比前一条更根本）：

> *"我要的是**每一次都问**，除非说**以后都这么干**"*
> *"然后要可以**改**，比如说叫云茹**这次群里叫我**"*

于是：

1. **每次都问**（群里 @ / 私聊 / 邮箱）：他以前选过一次**不算数**；
2. 只有他明说"**以后都这么干**"这类意思，才记成长期偏好、之后不再问
   （保守正则在核心做，见 `remind.forever_channel`——**判定 prompt 一个字没动**）；
3. **邮箱不受影响**：地址仍是"没有就问一次、有就用"（那是**一次性**给的地址，
   不是"提醒方式"）；
4. 他说"**这次**群里叫我" → 改掉**本会话里最近那条还没发的**提醒的收法，
   **只改这一次**（不动长期偏好；这条发完即止）；
5. 没有待发的提醒可改 → 什么都不做（不报错、不新建）；
6. **老数据**：插件那份 `people.jsonl` 里按老规矩写下的 `channel`（他只选过一次）
   **没有** `forever` 标记 → 一律视为"不粘"（核心不去动用户的数据文件，用标记区分）。

这一份钉住上面每一条，突变（各自还原）：
① 让"有渠道就不再问"回来（把每次选择都记成偏好）→ 第 1 条红；
② 去掉"以后都"的识别 → 第 2 条红；
③ 把旧记录/普通选择当成 forever（`forever=True` 一律发）→ 第 3 条红；
④ 让"这次"去改长期偏好 → 第 4 条红；
⑤ 把"这次"当成"以后都"（`forever_channel` 认"这次"）→ 第 4.b 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.remind import RemindRequest, forever_channel, remind_key
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
                "<reply>行，记着了。</reply>")


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


def _tag(time_text: str, what: str = "交作业", channel: str = "") -> str:
    tail = f"|{channel}" if channel else ""
    return f"<route>REPLY</route><topic>提醒</topic><remind>{time_text}|{what}{tail}</remind>"


def _msg(index: int, text: str, *, user_id: str = "100"):
    return IncomingMessage(f"m{index}", SESSION, user_id, text, TARGET,
                           sender_name="某人", is_bot_mentioned=True)


# --- 1 / 3. 选过一次不算数：下次照问；普通选择不带 forever 标记 -----------------


def test_a_past_choice_does_not_stop_the_question() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, reply = _engine([
        _tag("明天8点", channel="2"),        # 第一件：他这次说私聊
        _tag("明天9点", what="开会"),         # 第二件：他没说收法
    ], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 明天8点提醒我交作业，私聊我")))
        assert len(handed) == 1 and handed[0].forever is False, "普通选择不许带长期标记"
        clock.now += 3600
        asyncio.run(engine.handle(_msg(2, "@YunRu 明天9点提醒我开会")))

    assert len(handed) == 1, "没说收法就不该交出去"
    assert engine._remind_channel_pref == {}, "普通选择不许写进长期偏好"
    assert ASK in reply.requests[-1][-1]["content"], "每次都问：这一轮还得问她收法"


def test_an_ordinary_choice_is_not_marked_forever() -> None:
    """他早先那种"我 1"式的普通选择，不许被当成"以后都"。"""

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点", channel="1")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 明天8点提醒我交作业，我 1")))

    assert len(handed) == 1
    assert handed[0].forever is False
    assert engine._remind_channel_pref == {}


# --- 2. "以后都这么干" → 记成长期偏好、以后不再问 ----------------------------


def test_saying_forever_stops_the_asking() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, reply = _engine([
        _tag("明天8点", channel="1"),        # 他这次说：以后都群里@
        _tag("明天9点", what="开会"),         # 下一件：他没说收法
    ], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 以后都群里@我就行，明天8点提醒我交作业")))
        assert engine._remind_channel_pref == {"100": "group"}
        assert handed[0].forever is True, "明说过以后都，就要带标记"
        assert "以后都这么办" in reply.requests[-1][-1]["content"], "她那句确认要说清"
        clock.now += 3600
        asyncio.run(engine.handle(_msg(2, "@YunRu 明天9点提醒我开会")))

    assert len(handed) == 2, "有'以后都'的约定就不该再拦着问"
    assert handed[1].channel == "group" and handed[1].forever is True
    assert ASK not in reply.requests[-1][-1]["content"], "有约定了就不许再问三选一"
    assert "还是照老规矩" in reply.requests[-1][-1]["content"], "有约定时让她复述一句，别再问"


def test_the_forever_recogniser_is_conservative() -> None:
    # 认：长期说法 **且** 明确收法（或者判定那轮给了收法）。
    assert forever_channel("以后都群里@我就行") == "group"
    assert forever_channel("默认就私聊吧") == "private"
    assert forever_channel("往后都发邮件") == "email"
    assert forever_channel("以后都这么干", "1") == "group"
    # 不认：只有长期说法（没说要哪种）／只有收法／普通选择／反话。
    for text, chosen in (("以后都这么干", ""), ("群里@我", ""), ("我 1", ""),
                         ("以后都别在群里@我", ""), ("算了以后都别提醒我", "")):
        assert forever_channel(text, chosen) == "", (text, chosen)


# --- 4. "这次群里叫我"：只改这一条 ------------------------------------------


def test_this_time_changes_only_that_one_reminder() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, reply = _engine([
        _tag("明天8点", channel="2"),        # 第一件：私聊
        _tag("明天8点", channel="1"),        # 他说"这次群里叫我"（没有新时间，第三段是 1）
        _tag("明天9点", what="开会"),         # 下一件：又没说收法
    ], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 明天8点提醒我交作业，私聊我")))
        assert handed[0].channel == "private"
        asyncio.run(engine.handle(_msg(2, "这次群里叫我")))
        clock.now += 3600
        asyncio.run(engine.handle(_msg(3, "@YunRu 明天9点提醒我开会")))

    # 那一条的收法改了（同一把 key → 插件按它更新，不新增）。
    assert len(handed) == 2, [item.channel for item in handed]
    assert handed[1].channel == "group"
    assert handed[1].key == remind_key("交作业")
    assert handed[1].forever is False, "改这一次不带长期标记"
    # 长期偏好没被改动，后面的仍然要问。
    assert engine._remind_channel_pref == {}
    assert ASK in reply.requests[-1][-1]["content"]
    # 那句确认要说清"只有这一次"。
    assert "这次" in reply.requests[1][-1]["content"]


def test_this_time_with_a_new_request_is_still_one_off() -> None:
    """"这次群里叫我，明天8点提醒我交作业"：收法**只管这一次**，确认也要说清。"""

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, reply = _engine([_tag("明天8点", channel="1")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "@YunRu 这次群里叫我，明天8点提醒我交作业")))

    assert len(handed) == 1
    assert handed[0].channel == "group"
    assert handed[0].forever is False, "不许当成'以后都'"
    assert engine._remind_channel_pref == {}, "一次性说法不许写进长期偏好"
    assert "只有这一次" in reply.requests[-1][-1]["content"]


def test_this_time_with_no_pending_reminder_does_nothing() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine, _ = _engine([_tag("明天8点", channel="1")], handed, clock)
    with patch.dict(os.environ, TZ):
        asyncio.run(engine.handle(_msg(1, "这次群里叫我")))

    assert handed == [], "没有待发的提醒可改，就不该新建"
    assert engine._remind_last_request == {}
    assert engine._remind_channel_pref == {}


if __name__ == "__main__":
    import unittest

    unittest.main()
