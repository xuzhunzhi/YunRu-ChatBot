"""「到点提醒我」：**判定那一次调用**就把它认出来 + 核心把时间算成绝对时刻。

用户 2026-10-08 拍板形状：**核心听懂 + 插件做事 + 核心记录 + 核心发送**，而且是
**对话调起**（群里说"云茹，8 点提醒我交作业"），不是斜杠命令。所以：

* "这是提醒请求 + 时间 + 事情"挤在**现有那次判定**的返回里（`<remind>时间|事情</remind>`）
  ——**零额外模型调用**；
* 时间怎么算成绝对时刻由**核心**做（只有核心知道"群里这边"是哪个时区）；
  **认不清就什么都不做**（当普通聊天，不猜、不追问）；
* 插件那一侧只收一个 `RemindRequest`（见 `remind.py`），它自己不做时间推算。

这一份钉住：

1. 判定输出里的 `<remind>` 被解析出来；给不全（只给时间 / 只给事情 / 空）一律当没给；
2. 判定提示词里只多了**最小**的字段说明，而且**零机制词**（人格口径与 agent 口径都扫）；
3. 批次 7 的分节边界与大小约束**没退化**（那句规矩照旧在、发出去的那份仍"远小于人设"）；
4. 相对 / 绝对 / 只有钟点的时间说法都算得对，认不清的一律 None；
5. 认到提醒**不会多一次模型调用**（判定仍然 1 次、回复仍然 1 次）。

突变（各自还原）：把 `<remind>` 字段从判定返回里去掉（解析不认）→ 第 1、5 条红。
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from qq_roleplay_bot.base_prompt import BASE_PROMPT
from qq_roleplay_bot.dialogue_judge import (
    JUDGE_SYSTEM_PROMPT,
    build_judge_messages,
    compose_judge_system_prompt,
    parse_judge_output,
)
from qq_roleplay_bot.prompt_guard import scan_agent_text, scan_persona_text
from qq_roleplay_bot.remind import RemindIntent, find_email, parse_remind_time
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.stage3_runtime import SECTION_IS_DATA_RULE, SYSTEM_PROMPT
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
#: 2026-10-08 周四 22:12（+8）。显式带时区构造，所以与部署机器的时区无关。
NOW = datetime(2026, 10, 8, 22, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()

TZ = {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}


def _at(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(year, month, day, hour, minute,
                    tzinfo=timezone(timedelta(hours=8))).timestamp()


# --- 1. 判定那一次调用里就认出来 ---------------------------------------------


def test_the_judge_output_carries_the_remind_intent() -> None:
    verdict = parse_judge_output(
        "<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>")
    assert verdict.remind == RemindIntent(time_text="明天8点", what="交作业")


def test_a_half_filled_remind_is_ignored() -> None:
    for raw in ("", "明天8点", "|交作业", "  |  ", "|", "明天8点|"):
        verdict = parse_judge_output(f"<route>REPLY</route><remind>{raw}</remind>")
        assert verdict.remind is None, raw


def test_only_the_first_pipe_splits() -> None:
    verdict = parse_judge_output("<route>REPLY</route><remind>明天8点|交作业|顺手带伞</remind>")
    assert verdict.remind == RemindIntent(time_text="明天8点", what="交作业|顺手带伞")


def test_no_remind_field_means_no_intent() -> None:
    assert parse_judge_output("<route>REPLY</route><topic>闲聊</topic>").remind is None


# --- 2. 提示词只多了最小的一段，且零机制词 -----------------------------------


def test_the_prompt_explains_the_field_and_only_that() -> None:
    assert "<remind>时间|事情" in JUDGE_SYSTEM_PROMPT
    assert "到点提醒他" in JUDGE_SYSTEM_PROMPT
    assert "宁可不填，也别猜" in JUDGE_SYSTEM_PROMPT


def test_the_prompt_stays_free_of_mechanism_words() -> None:
    assert scan_agent_text(JUDGE_SYSTEM_PROMPT) == []
    assert scan_persona_text(JUDGE_SYSTEM_PROMPT) == []
    added = JUDGE_SYSTEM_PROMPT[JUDGE_SYSTEM_PROMPT.index("<remind>"):]
    for word in ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage"):
        assert word not in added, word


# --- 3. 批次 7 的边界与大小没退化 --------------------------------------------


def test_the_boundary_rule_and_size_are_not_degraded() -> None:
    current = IncomingMessage("r1", f"group:{GROUP}", "100", "8点提醒我交作业", TARGET,
                              sender_name="某人", is_bot_mentioned=True)
    request = build_judge_messages([current], current=current, trigger="mention",
                                   addressed=True)
    assert SECTION_IS_DATA_RULE in request[0]["content"], "批次 7 那句规矩被挤掉了"
    assembled = compose_judge_system_prompt(JUDGE_SYSTEM_PROMPT)
    assert len(assembled) < len(BASE_PROMPT)
    assert len(assembled) < len(SYSTEM_PROMPT) * 0.4, len(assembled)


# --- 4. 核心算时间 -----------------------------------------------------------


def test_relative_times() -> None:
    with patch.dict(os.environ, TZ):
        assert parse_remind_time("20分钟后提醒我", now=NOW) == NOW + 1200
        assert parse_remind_time("3小时后", now=NOW) == NOW + 10800
        assert parse_remind_time("半小时后", now=NOW) == NOW + 1800
        assert parse_remind_time("2天后", now=NOW) == NOW + 172800


def test_absolute_times_with_a_day_word() -> None:
    with patch.dict(os.environ, TZ):
        assert parse_remind_time("明天8点", now=NOW) == _at(2026, 10, 9, 8)
        assert parse_remind_time("今天23:30", now=NOW) == _at(2026, 10, 8, 23, 30)
        assert parse_remind_time("后天下午3点", now=NOW) == _at(2026, 10, 10, 15)
        assert parse_remind_time("明晚8点", now=NOW) == _at(2026, 10, 9, 20)


def test_a_day_word_in_the_past_is_not_guessed() -> None:
    """"今天20:30"在 22:12 说出口 = 说不清，不是"改天"。"""

    with patch.dict(os.environ, TZ):
        assert parse_remind_time("今天20:30", now=NOW) is None


def test_a_bare_clock_takes_the_next_occurrence() -> None:
    with patch.dict(os.environ, TZ):
        assert parse_remind_time("8点", now=NOW) == _at(2026, 10, 9, 8)          # 今天 8 点已过
        assert parse_remind_time("20:30", now=NOW) == _at(2026, 10, 9, 20, 30)  # 今天 20:30 已过
        assert parse_remind_time("下午3点", now=NOW) == _at(2026, 10, 9, 15)
        assert parse_remind_time("23点", now=NOW) == _at(2026, 10, 8, 23)         # 还没到 → 今天
        assert parse_remind_time("晚上11点", now=NOW) == _at(2026, 10, 8, 23)


def test_unclear_times_are_not_reminders() -> None:
    with patch.dict(os.environ, TZ):
        for text in ("明天提醒我", "有空提醒我一下", "晚点吧", "25点", "一会儿",
                     "后天的这个点", "", "   "):
            assert parse_remind_time(text, now=NOW) is None, text


def test_the_timezone_is_the_configured_one_not_the_host() -> None:
    """同一句话、同一时刻，换个配置就换个绝对时刻（证明走的是显式时区）。"""

    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "0"}):
        utc_eight = parse_remind_time("明天8点", now=NOW)
    with patch.dict(os.environ, {"QQBOT_LOCAL_TZ_OFFSET_HOURS": "8"}):
        plus_eight = parse_remind_time("明天8点", now=NOW)
    # 两者相差**正好 8 小时**：同一个"明天8点"在两个时区里是不同的绝对时刻。
    # 方向取决于哪个时区在前（UTC 的"明天8点"在绝对时间上更早），所以比绝对值。
    assert abs(utc_eight - plus_eight) == 8 * 3600


def test_find_email_is_conservative() -> None:
    assert find_email("我的邮箱是 lele@example.com，谢了") == "lele@example.com"
    assert find_email("a.b+c@sub.example.co") == "a.b+c@sub.example.co"
    for text in ("没有邮箱", "lele@", "@example.com", "lele@example"):
        assert find_email(text) is None, text


# --- 5. 不多一次模型调用 -----------------------------------------------------


class _ScriptedJudge:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class _ScriptedReply:
    def __init__(self) -> None:
        self.text = "行，八点我叫你。"
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        return (f"<decision>REPLY</decision><dialogue>KEEP</dialogue>"
                f"<reply>{self.text}</reply>")


def test_recognizing_a_remind_adds_no_model_call() -> None:
    from qq_roleplay_bot.runtime_flags import RuntimeFlags

    judge = _ScriptedJudge("<route>REPLY</route><topic>提醒</topic><remind>明天8点|交作业</remind>")
    reply = _ScriptedReply()
    engine = DialogueEngine(reply, judge_client=judge, enabled_group_ids=frozenset({GROUP}))
    engine._default_flags = RuntimeFlags(review_enabled=False)
    message = IncomingMessage("r1", f"group:{GROUP}", "100", "@YunRu 明天8点提醒我交作业",
                              TARGET, sender_name="某人", is_bot_mentioned=True)

    result = asyncio.run(engine.handle(message))

    assert result is not None
    assert len(judge.requests) == 1, "判定多跑了一次"
    assert len(reply.requests) == 1, "回复多跑了一次"


if __name__ == "__main__":
    import unittest

    unittest.main()
