"""跑在慢一档的那一家（mimo）时，她的回答意愿跟着降下来（2026-10-12 用户拍板）。

用户原话（2026-10-12）："用mimo的时候下降模型回答意愿，给模型提示词改成云茹不舒服、
困倦、心情不好不太想回答这样，更符合模型速度。"

为什么：余额耗尽之后整条切到兜底那一家，实测那一家的判定中位 36.8 秒、回复 10–15 秒，
比主家慢一个数量级。要的不是"慢，还硬撑着聊"，而是让她的状态跟速度对上——今天不
舒服、困、提不起兴致，于是话少、也想早点收场。

这一份钉住：

1. **主家一个字都不许变**：默认（`tired=False`）下 system 段仍逐字是"人格 + 三条常量"，
   新增那条一个字都不进去；
2. **只在跑慢的那一家时拼**：`tired=True` → `TIRED_MOOD_RULE` 接在三条常量**之后**、
   只拼一次（幂等）；面板整段覆盖的人格也照样拼得上；
3. **判定那侧只在没直接叫到她的时候出现**：被叫到时判定连 route 都不输出，
   给她"能不出声就别出声"只会白占位置——而且判定输入**除了那一行不许有别的差别**；
4. **接线在源码里**：回复那个调用点与判定那一行都得真的去问 `on_slow_model`，
   不然上面几组测的都是没人用的函数；
5. **措辞合规**：两条新常量都是交谈视角、零机制词（`prompt_guard` 两把扫描器都干净）。

`on_slow_model` 本身的取值**另外单独测**（第 2 组）：它有一半来自"这个用途本来就配的那
家是谁"，那是配置相关的，所以判定那几组用打桩把闸门按住，只测"闸门开了会怎样"。

突变（各自还原）：回复调用点去掉 `tired=on_slow_model("reply")` → 第 4 组红、第 2 组
（接线部分）红；判定那行去掉 `not must_reply` → 第 3 组红；`TIRED_MOOD_RULE` 改拼在三条
**之前** → 第 2 组"接在三条之后"红。
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from unittest import mock

from qq_roleplay_bot import dialogue_judge, model_config, provider_failover, stage3_runtime
from qq_roleplay_bot.dialogue_judge import build_judge_messages
from qq_roleplay_bot.prompt_guard import (
    PERSONA_MECHANISM_WORDS,
    hits,
    scan_agent_text,
    scan_persona_text,
)
from qq_roleplay_bot.stage3_runtime import (
    DATA_BOUNDARY_RULE,
    SLOW_PROVIDERS,
    STAY_IN_CHARACTER_RULE,
    TIRED_MOOD_HINT,
    TIRED_MOOD_RULE,
    TIME_IS_GIVEN_RULE,
    compose_system_prompt,
    on_slow_model,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
PERSONA = "（测试用的极简人格，本身不含任何一条代码层的规矩）"
THREE_RULES = STAY_IN_CHARACTER_RULE + DATA_BOUNDARY_RULE + TIME_IS_GIVEN_RULE


@contextlib.contextmanager
def _fallback():
    """把全局兜底开关按下去；出块还原（只动 `_mode`，不碰任何真端点）。"""

    before = provider_failover._mode
    provider_failover._mode = provider_failover.FALLBACK
    try:
        yield
    finally:
        provider_failover._mode = before


@contextlib.contextmanager
def _gate(value: bool):
    """把判定那侧读到的那道闸按住，只测"闸门开了会怎样"。"""

    with mock.patch.object(dialogue_judge, "on_slow_model", return_value=value):
        yield


def _messages(*, addressed: bool, must_reply: bool = False) -> list[dict[str, str]]:
    current = IncomingMessage("r1", f"group:{GROUP}", "100", "在吗", TARGET,
                              sender_name="某人", is_bot_mentioned=addressed)
    return build_judge_messages(
        [current], current=current,
        trigger="mention" if addressed else "threshold",
        addressed=addressed, must_reply=must_reply)


def _flat(messages: list[dict[str, str]]) -> str:
    return "".join(message["content"] for message in messages)


# --- 1. 主家：一个字都不许变 --------------------------------------------------


def test_the_system_prompt_is_byte_identical_by_default() -> None:
    assert compose_system_prompt(PERSONA) == PERSONA + THREE_RULES
    assert compose_system_prompt(PERSONA, tired=False) == PERSONA + THREE_RULES
    assert TIRED_MOOD_RULE not in compose_system_prompt(PERSONA)


# --- 2. 慢的那一家：接在三条之后、只拼一次、覆盖版人格也拼得上 ------------------


def test_the_tired_rule_lands_after_the_three_and_only_once() -> None:
    composed = compose_system_prompt(PERSONA, tired=True)
    assert composed.startswith(PERSONA + THREE_RULES), "新那条该接在三条常量之后，不许插队"
    assert composed.endswith(TIRED_MOOD_RULE)
    assert composed.count(TIRED_MOOD_RULE) == 1
    assert compose_system_prompt(composed, tired=True) == composed, "拼过的再拼一次不该变"


def test_a_replaced_persona_still_gets_every_rule() -> None:
    replaced = "面板上整段替换过的人格。"
    composed = compose_system_prompt(replaced, tired=True)
    for rule in (STAY_IN_CHARACTER_RULE, DATA_BOUNDARY_RULE, TIME_IS_GIVEN_RULE,
                 TIRED_MOOD_RULE):
        assert rule in composed, rule[:12]


def test_on_slow_model_follows_the_fallback_switch() -> None:
    # 主家上：这个开关只反映"这个用途本来就配的那家是不是慢的那家"（配置相关，所以
    # 这里跟配置对照着断言，而不是写死 False）。
    expected = model_config.resolve("reply").provider in SLOW_PROVIDERS
    assert on_slow_model("reply") is expected
    with _fallback():
        assert on_slow_model("reply") is True, "切到兜底那家之后她一定进这个状态"
    assert on_slow_model("reply") is expected


# --- 3. 判定那侧：只在没直接叫到她时出现，且只多那一行 ------------------------


def test_the_judge_hears_it_only_when_she_was_not_called() -> None:
    with _gate(True):
        assert TIRED_MOOD_HINT not in _flat(_messages(addressed=True))
        assert TIRED_MOOD_HINT in _flat(_messages(addressed=False))
    with _gate(False):
        assert TIRED_MOOD_HINT not in _flat(_messages(addressed=False))


def test_being_called_directly_never_carries_it() -> None:
    with _gate(True):
        assert TIRED_MOOD_HINT not in _flat(_messages(addressed=True, must_reply=True))


def test_the_only_difference_is_that_one_line() -> None:
    with _gate(False):
        quiet = _messages(addressed=False)[1]["content"]
    with _gate(True):
        tired = _messages(addressed=False)[1]["content"]
    assert tired != quiet
    assert tired.replace(TIRED_MOOD_HINT, "") == quiet, "除了那一行，判定的输入不该有差别"


# --- 4. 接线在源码里（两处调用点都得真去问那道闸） ----------------------------


def test_the_reply_call_site_asks_the_gate() -> None:
    source = Path(stage3_runtime.__file__).read_text(encoding="utf-8")
    assert 'tired=on_slow_model("reply")' in source, "回复那个调用点没把闸门接上"


def test_the_judge_call_site_asks_the_gate() -> None:
    source = Path(dialogue_judge.__file__).read_text(encoding="utf-8")
    assert 'not addressed and not must_reply and on_slow_model("judge")' in source, \
        "判定那行没把闸门接上，或者忘了「被叫到就不拼」"


# --- 5. 措辞合规：零机制词 ----------------------------------------------------


def test_the_new_wording_is_clean() -> None:
    for text in (TIRED_MOOD_RULE, TIRED_MOOD_HINT):
        assert scan_persona_text(text) == []
        assert scan_agent_text(text) == []
        assert hits(text) == []
        for word in PERSONA_MECHANISM_WORDS:
            assert word not in text, word
