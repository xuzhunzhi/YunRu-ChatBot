"""「你那边 / 群里这边」两个时刻，以及那条"时间以给出的为准"的规矩（2026-10-08）。

用户口径：**她那边 = 北京 + 3 小时**。所以每一轮报给她的时间是两句：

    你那边现在是 2026-10-09 周五 01:12；群里这边是 2026-10-08 周四 22:12

"群里这边"是服务器本地（北京）的真实时间，"你那边"= 它 + 偏移。跨天、跨周由
`datetime` + `timedelta` 算，不许手写。偏移是配置项
（`dev_config.persona_offset_hours()`，`QQBOT_PERSONA_OFFSET_HOURS`，默认 3）。

"喂给她的时间必须是墙上时间"那一半（wall_clock、1970 回归）在
`tests/test_wall_clock.py`；这一份只管**两个时刻**与 system 里那条规矩。
"""
import os
from datetime import datetime
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.prompt_guard import scan_persona_text
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import (
    TIME_IS_GIVEN_RULE,
    ContextState,
    ConversationMode,
    _now_text,
    build_dialogue_messages,
    compose_system_prompt,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
TARGET = MessageTarget(group_id=GROUP)

#: 2026-10-08 周四 22:12（北京）。`timestamp()` 与 `fromtimestamp()` 是一对逆运算，
#: 所以这台机器在哪个时区都不影响下面的断言。
BEIJING = datetime(2026, 10, 8, 22, 12).timestamp()
#: 她那边那句（+3 小时，跨到次日）。
HER_SIDE = "你那边现在是 2026-10-09 周五 01:12"
#: 群里那句（服务器本地）。
HERE = "群里这边是 2026-10-08 周四 22:12"
STAMP = f"{HER_SIDE}；{HERE}"


def msg(index, text="在吗", *, mentioned=True):
    return IncomingMessage(
        message_id=f"t{index}", session_id=f"group:{GROUP}", user_id="100",
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


def _request(now=None):
    current = msg(1)
    return build_dialogue_messages(
        [current], current=current, mode=ConversationMode.ACTIVE,
        trigger="mention", context=ContextState(topic="闲聊"), now=now,
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
        enabled_group_ids=frozenset({GROUP}), **kwargs)
    engine._default_flags = RuntimeFlags(review_enabled=False)
    return engine


# --- 1. 两个时刻与偏移 -------------------------------------------------------


def test_both_clocks_are_printed_with_the_offset_applied() -> None:
    """"你那边"= 北京 + 3 小时，"群里这边"= 北京本地；两句都在同一行里。"""

    assert _now_text(BEIJING) == STAMP
    assert f"（{STAMP}）" in _request(now=BEIJING)[-1]["content"]


def test_a_cross_day_boundary_is_computed_not_hand_written() -> None:
    """北京 23:30 → 她那边**次日** 02:30（跨天要真的跨过去）。"""

    late = datetime(2026, 10, 8, 23, 30).timestamp()
    assert _now_text(late) == (
        "你那边现在是 2026-10-09 周五 02:30；群里这边是 2026-10-08 周四 23:30")


def test_a_cross_week_boundary_is_computed_not_hand_written() -> None:
    """北京周日 23:30 → 她那边**周一** 02:30（跨周，星期几要跟着变）。"""

    sunday = datetime(2026, 10, 11, 23, 30).timestamp()
    assert _now_text(sunday) == (
        "你那边现在是 2026-10-12 周一 02:30；群里这边是 2026-10-11 周日 23:30")


def test_the_offset_is_the_configured_one_and_defaults_to_three() -> None:
    """偏移是配置项（`QQBOT_PERSONA_OFFSET_HOURS`），默认 3 小时；改了就跟着变。"""

    with patch.dict(os.environ):
        os.environ.pop("QQBOT_PERSONA_OFFSET_HOURS", None)
        assert dev_config.persona_offset_hours() == 3.0
    with patch.dict(os.environ, {"QQBOT_PERSONA_OFFSET_HOURS": "5"}):
        assert dev_config.persona_offset_hours() == 5.0
        assert _now_text(BEIJING) == (
            "你那边现在是 2026-10-09 周五 03:12；群里这边是 2026-10-08 周四 22:12")


def test_the_two_clocks_stay_in_the_volatile_segment() -> None:
    """它只能待在**易变段**：进了稳定段前缀缓存每轮都断（那是另一条已锁的判据）。"""

    request = _request(now=BEIJING)
    assert "你那边现在是" not in request[1]["content"], "时间进了稳定段"
    assert "你那边现在是" in request[2]["content"]


# --- 2. 判定那处同源 ---------------------------------------------------------


def test_the_reply_and_the_judge_read_the_same_wall_clock() -> None:
    """判定与回复读**同一个**墙钟：两边印出来的时刻逐字相同。"""

    import asyncio

    reply, judge = ScriptedReply(), ScriptedJudge()
    engine = _engine(reply, judge, wall_clock=lambda: BEIJING)
    asyncio.run(engine.handle(msg(1, "@YunRu 在吗")))

    assert STAMP in reply.requests[0][-1]["content"]
    assert f"现在是什么时候：{STAMP}" in judge.requests[0][-1]["content"], \
        "判定那处没跟上同一个墙钟（`dialogue_judge` 那条路）"


# --- 3. system 里那条规矩 ----------------------------------------------------


def test_the_time_rule_is_in_the_trusted_prefix_and_has_no_mechanism_words() -> None:
    """**安全/约定类可以写在前缀**：这条规矩进 system，且零机制词。

    扫的比 `prompt_guard` 那张表更宽一点：时区 / UTC / token 这类词也不许出现——
    她要躲的不只是那八个词，而是"开始讲自己的机制"这件事。
    """

    assert scan_persona_text(TIME_IS_GIVEN_RULE) == [], scan_persona_text(TIME_IS_GIVEN_RULE)
    for word in ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage",
                 "时区", "UTC", "token", "系统", "模型", "服务器", "程序"):
        assert word not in TIME_IS_GIVEN_RULE, word
    # 它谈的是"时间怎么算"，不是她该怎么说话。
    assert "为准" in TIME_IS_GIVEN_RULE
    persona = "你是一个住在群聊里的角色。这一份是测试用的极简人格。"
    composed = compose_system_prompt(persona)
    assert composed.startswith(persona), "人格本身不能被改动"
    assert composed.endswith(TIME_IS_GIVEN_RULE)
    assert TIME_IS_GIVEN_RULE in _request(now=BEIJING)[0]["content"], "规矩没进 system"


if __name__ == "__main__":
    import unittest

    unittest.main()
