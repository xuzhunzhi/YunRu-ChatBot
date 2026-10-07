"""喂给她的"现在"必须是**墙上时间**，不是开机时长（2026-10-08 用户报的 bug）。

生产实测（`run/data/logs/reply.jsonl`）：她看到的"现在"是
`现在是你那边的 1970-01-02 周五 23:50`——那是 `time.monotonic()`（开机时长）被
`time.localtime()` 当成 epoch 印出来的。她的原话与那个假小时逐分钟对得上
（喂 `1970-01-07 周三 07:57` → 她说"我这边才早上。睡吧。"）。

根因有两截：`stage3_main` 里那个 `now = self.clock()`（单调）**同时喂着焦点值班的
数学**，所以不能整体替换；而它又被顺手递给了"显示/提示"那三处。修法是给引擎加一个
独立的 `wall_clock`（默认 `time.time`），**只**在显示那几处用它。
（"她那边 / 群里这边"两句话与偏移见 `tests/test_two_clocks.py`。）

这一份测三件事：

1. **年份是墙上那一年**，不是 1970；
2. **单调时钟一个小数值（模拟开机 20 分钟）流不到显示那条路**——这条就是这次 bug 的
   回归：修之前它必然红；
3. **引擎用的是注入的那个墙钟**（不是自己现取时间）。

之所以要单开一份：原来那个套件抓不到这个 bug——`test_data_boundary` 只断言前缀
"（现在是你那边的 "，**一个字都没提年份或小时**。
"""
import asyncio
from datetime import datetime, timedelta, timezone

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import (
    ContextState,
    ConversationMode,
    _now_text,
    build_dialogue_messages,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
TARGET = MessageTarget(group_id=GROUP)

#: 2026-10-08 周四 22:12（北京）。**显式按 +8 构造**（不是"本机时区"）：
#: "群里这边"由 `QQBOT_LOCAL_TZ_OFFSET_HOURS`（默认 8）决定，与宿主时区无关，
#: 所以这一份的期望值在任何机器上都成立（2026-10-08 安全与时间口径审计 B-01）。
BEIJING = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
#: 北京本地那一句（两个渲染口径下都该出现"群里这边"这个时刻）。
BEIJING_TEXT = "2026-10-08 周四 22:12"


def msg(index, text="在吗", *, mentioned=True):
    return IncomingMessage(
        message_id=f"w{index}", session_id=f"group:{GROUP}", user_id="100",
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


class ScriptedJudge:
    def __init__(self, output="<route>REPLY</route><topic>话题</topic>") -> None:
        self.output = output
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class ScriptedReply:
    def __init__(self, text="我接一句。") -> None:
        self.text = text
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.text}</reply>")


def _engine(reply, judge, **kwargs) -> DialogueEngine:
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    engine = DialogueEngine(
        reply, judge_client=judge,
        # 群必须在启停名单里，否则 `handle` 在"群未启用"那一步就静默落下。
        enabled_group_ids=frozenset({GROUP}), **kwargs)
    # 实例级替身：不 install 到进程上，免得污染同进程里别的测试。
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine


def test_the_year_is_the_wall_year_not_1970() -> None:
    """喂一个已知的墙上时间戳 → 印出来的是那一年。"""

    text = _now_text(BEIJING)
    assert "2026" in text, text
    assert "1970" not in text, text
    # 请求里那两处（回复那条路）也不许冒出 1970。
    request = build_dialogue_messages(
        [msg(1)], current=msg(1), mode=ConversationMode.ACTIVE,
        trigger="mention", context=ContextState(topic="闲聊"), now=BEIJING,
    )
    assert "1970" not in request[-1]["content"], request[-1]["content"]


def test_a_monotonic_value_never_reaches_the_display() -> None:
    """**关键的回归**：单调时钟就是个小数值（模拟开机 20 分钟）。

    修之前，这个数被当成 epoch 印成 1970；现在它一个都不许出现在她看到的时间上。
    这条同时验"引擎默认的墙钟是 `time.time`"，因为这里没有注入 `wall_clock`。
    """

    tick = [1234.5]

    def monotonic() -> float:
        tick[0] += 0.01
        return tick[0]

    reply, judge = ScriptedReply(), ScriptedJudge()
    engine = _engine(reply, judge, clock=monotonic)
    asyncio.run(engine.handle(msg(1, "@YunRu 在吗")))

    shown = reply.requests[0][-1]["content"]
    assert "1970" not in shown, shown
    assert str(datetime.now().year) in shown, shown


def test_the_engine_uses_the_injected_wall_clock() -> None:
    """注入的墙钟就是印出来的那个（引擎不许自己另取时间）。"""

    reply, judge = ScriptedReply(), ScriptedJudge()
    engine = _engine(reply, judge, wall_clock=lambda: BEIJING)
    asyncio.run(engine.handle(msg(1, "@YunRu 在吗")))

    shown = reply.requests[0][-1]["content"]
    assert BEIJING_TEXT in shown, shown
    assert "1970" not in shown, shown


if __name__ == "__main__":
    import unittest

    unittest.main()
