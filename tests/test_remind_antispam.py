"""提醒的**防刷屏**三道闸（2026-10-09 用户实测：她在富哥之家刷屏）。

日志铁证：她发出 `12:54:06 / 12:55:06 / 13:00:07 / 13:01:08 / 13:01:38` 五条
"@2639157559 到点了：给dsh做插件使用反馈"（写法每次略不同），`remind_sent` 五次。

三道闸（都在核心，判定模型那侧一个字没改）：

1. **归一化去重**：键取 `(人, 归一化之后的事情)`——比**原文**比不出来（她每次多一个空格、
   多一个标点、全角半角混着）；窗口 **60 分钟**。
2. **只认"这一轮真正的用户消息"**：她自己发出去的话（`is_bot_message`）不算；
   而且**当前这条消息自己必须带一个认得出的时间**——判定读整段，她"到点了：…"那句
   也在里面，于是"有人让你提醒"在**老话**上又成立了一次，那不算。
3. **频次兜底**：同一个人两次交给插件之间至少隔
   `dev_config.remind_min_interval_seconds()`（默认 **300** 秒，调用时现读，0 = 关）。
   前两道万一漏了也不刷屏。

这一份钉住：

1. 归一化：同一件事**换写法/加空格/全角**只办一条；不同的事仍是两条；
2. 真实形状的回归：**她发出"到点了：X"之后不产生新提醒**；
   老话里翻出来的提醒（这一条没给时间）也不算；
3. 频次闸：默认 300 秒内第二条不交、过了就交、`0` 时关掉；配置**调用时现读**、带夹取；
4. `RemindRequest.key` 就是那把归一化的键（插件按它"更新那一条"，别新增）。

突变（各自还原）：① 去掉归一化（键用原文）→ 第 1 条红；
② 让她自己的消息/老话也能触发（去掉那两道判断）→ 第 2 条红；
③ 去掉频次兜底 → 第 3 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.dialogue_judge import parse_judge_output as _parse_judge_output
from qq_roleplay_bot.remind import RemindRequest, remind_key
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}
#: 关掉频次闸：测别的两道时不想让它掺进来（它有自己的用例）。
NO_FLOOR = {**TZ, "QQBOT_REMIND_MIN_INTERVAL_SECONDS": "0"}


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


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _engine(outputs: list[str], handed: list, clock: _Clock):
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    engine = DialogueEngine(_ScriptedReply(), judge_client=_ScriptedJudge(outputs),
                            enabled_group_ids=frozenset({GROUP}),
                            remind=handed.append, wall_clock=clock)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine


def _tag(time_text: str, what: str, channel: str = "1") -> str:
    return f"<route>REPLY</route><topic>提醒</topic><remind>{time_text}|{what}|{channel}</remind>"


def _message(index: int, text: str, *, user_id: str = "100", bot: bool = False):
    return IncomingMessage(f"m{index}", SESSION, user_id, text, TARGET,
                           sender_name="某人", is_bot_mentioned=True, is_bot_message=bot)


# --- 1. 归一化去重 -----------------------------------------------------------


def test_remind_key_normalises_shape_not_meaning() -> None:
    assert remind_key("给dsh做插件使用反馈") == remind_key(" 给 dsh 做插件使用反馈 ")
    assert remind_key("给DSH做插件使用反馈。") == remind_key("给ｄｓｈ做插件使用反馈")
    assert remind_key("交作业") != remind_key("交作业了"), "多一个字仍是两把键（不装作懂语义）"


def test_the_same_thing_in_different_writings_lands_once() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine = _engine([
        _tag("明天8点", "给dsh做插件使用反馈"),
        _tag("明天8点", " 给 dsh 做插件使用反馈 "),
        _tag("明天8点", "给ＤＳＨ做插件使用反馈！"),
    ], handed, clock)
    with patch.dict(os.environ, NO_FLOOR):
        for index in (1, 2, 3):
            asyncio.run(engine.handle(_message(index, "@YunRu 明天8点提醒我给dsh做插件使用反馈")))

    assert len(handed) == 1, [item.what for item in handed]
    assert handed[0].key == remind_key("给dsh做插件使用反馈")


# --- 2. 她自己的发言 / 老话都不算 --------------------------------------------


def test_her_own_message_does_not_create_a_reminder() -> None:
    """**实测那条的形状**：她发出"@某人 到点了：X"之后不该再排一条。

    （这条是**端到端**的：`handle()` 本来就会把她自己的消息挡在 `_note_remind` 之前，
    所以它单靠"挡在更前面那一步"也是绿的。下面那条**直接调** `_note_remind`
    才是钉住这道判断本身的——两者都要。）
    """

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine = _engine([
        _tag("明天8点", "给dsh做插件使用反馈"),      # 第一轮：真人提出
        _tag("明天8点", "给dsh做插件使用反馈"),      # 她自己那句"到点了：…"被回显
        _tag("明天8点", "给dsh做插件使用反馈"),      # 她**回话里带时间**的那句（更像主因）
    ], handed, clock)
    with patch.dict(os.environ, NO_FLOOR):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我给dsh做插件使用反馈")))
        assert len(handed) == 1
        # 实测的形状：她发出"@某人 到点了：X"。
        asyncio.run(engine.handle(_message(
            2, "@2639157559 到点了：给dsh做插件使用反馈", user_id="9", bot=True)))
        # 更危险的形状：她自己的回话里**带着时间**（"好，明天8点我叫你"）。
        asyncio.run(engine.handle(_message(3, "好，明天8点我叫你", user_id="9", bot=True)))

    assert len(handed) == 1, "她自己的话又排了一条"


def test_the_guard_itself_refuses_a_bot_message() -> None:
    """直接调 `_note_remind`：她自己的**带时间**的回话也不许排提醒。

    用"好，明天8点我叫你"这种形状（她自己的话里**有时间**）——不然这道判断会被
    另一道（"这一条里没有时间"）挡住，测不出它自己。
    """

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine = _engine([_tag("明天8点", "给dsh做插件使用反馈")], handed, clock)
    verdict = _parse_judge_output("<route>REPLY</route><topic>提醒</topic>"
                                 "<remind>明天8点|给dsh做插件使用反馈|1</remind>")
    bot_message = _message(2, "好，明天8点我叫你", user_id="9", bot=True)

    with patch.dict(os.environ, NO_FLOOR):
        engine._note_remind(bot_message, verdict)

    assert handed == []
    assert engine._remind_asks == {}


def test_a_remind_pulled_from_old_talk_is_not_a_request() -> None:
    """判定从窗口里的**老话**翻出来一个提醒、而这一条没给时间 → 不算。"""

    handed: list[RemindRequest] = []
    clock = _Clock()
    engine = _engine([
        _tag("明天8点", "交作业"),                     # 第一轮：真人提出（时间在消息里）
        _tag("明天8点", "交作业"),                     # 第二轮：判定又报了一次
    ], handed, clock)
    with patch.dict(os.environ, NO_FLOOR):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
        # 第二轮是别的真人、跟提醒无关的一句（消息里没有时间）。
        asyncio.run(engine.handle(_message(2, "@YunRu 你刚刚说什么", user_id="200")))

    assert len(handed) == 1
    assert "200" not in engine._remind_asks, "不该给说话的人排一条"


# --- 3. 频次兜底 -------------------------------------------------------------


def test_the_default_interval_is_conservative() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QQBOT_REMIND_MIN_INTERVAL_SECONDS", None)
        assert dev_config.remind_min_interval_seconds() == 300
    with patch.dict(os.environ, {"QQBOT_REMIND_MIN_INTERVAL_SECONDS": "99999"}):
        assert dev_config.remind_min_interval_seconds() == 3600, "越界要夹住"
    with patch.dict(os.environ, {"QQBOT_REMIND_MIN_INTERVAL_SECONDS": "不是数字"}):
        assert dev_config.remind_min_interval_seconds() == 300, "坏值回落默认"


def test_two_different_things_too_soon_only_the_first_is_handed_over() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine = _engine([
        _tag("明天8点", "交作业"),
        _tag("明天9点", "开会"),
    ], handed, clock)
    with patch.dict(os.environ, TZ):        # 用**默认** 300 秒的闸
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
        clock.now += 60                     # 一分钟之后又说一件新事
        asyncio.run(engine.handle(_message(2, "@YunRu 明天9点提醒我开会")))

    assert [item.what for item in handed] == ["交作业"], "一分钟里两件不同的事也只办第一条"

    clock.now += 300                        # 过了闸
    asyncio.run(engine.handle(_message(3, "@YunRu 明天9点提醒我开会")))
    assert [item.what for item in handed] == ["交作业", "开会"]


def test_the_interval_can_be_switched_off() -> None:
    handed: list[RemindRequest] = []
    clock = _Clock()
    engine = _engine([
        _tag("明天8点", "交作业"),
        _tag("明天9点", "开会"),
    ], handed, clock)
    with patch.dict(os.environ, NO_FLOOR):
        asyncio.run(engine.handle(_message(1, "@YunRu 明天8点提醒我交作业")))
        asyncio.run(engine.handle(_message(2, "@YunRu 明天9点提醒我开会")))

    assert [item.what for item in handed] == ["交作业", "开会"]


if __name__ == "__main__":
    import unittest

    unittest.main()
