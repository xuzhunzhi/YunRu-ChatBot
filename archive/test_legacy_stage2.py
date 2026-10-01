from qq_roleplay_bot_legacy.stage2_runtime import (
    ConversationMode,
    DecisionKind,
    IdleTrigger,
    build_stage2_messages,
    parse_stage2_output,
)


def test_control_outputs_are_local_only() -> None:
    assert parse_stage2_output("NO_REPLY").kind is DecisionKind.NO_REPLY
    assert parse_stage2_output("[EXIT_DIALOGUE]").kind is DecisionKind.EXIT
    assert parse_stage2_output("回复：你好").text == "你好"


def test_idle_trigger_uses_batch_or_mention() -> None:
    trigger = IdleTrigger(batch_size=2, interval_seconds=60, cooldown_seconds=60)
    assert trigger.add(_message("1")) is None
    assert [m.message_id for m in trigger.add(_message("2"))] == ["1", "2"]


def test_forced_trigger_bypasses_cooldown() -> None:
    """被 @ 的第二次呼叫不能被上一轮触发的冷却静默挡住。"""

    trigger = IdleTrigger(batch_size=20, interval_seconds=60, cooldown_seconds=60)
    assert trigger.add(_message("1"), force=True) is not None
    assert trigger.add(_message("2"), force=True) is not None


def test_cooldown_still_limits_threshold_trigger() -> None:
    """冷却仍然约束非强制的阈值触发。"""

    trigger = IdleTrigger(batch_size=1, interval_seconds=0, cooldown_seconds=60)
    assert trigger.add(_message("1")) is not None
    assert trigger.add(_message("2")) is None


def test_reset_clears_cooldown() -> None:
    """显式退出对话或关群后不应残留冷却。"""

    trigger = IdleTrigger(batch_size=20, interval_seconds=60, cooldown_seconds=60)
    assert trigger.add(_message("1"), force=True) is not None
    trigger.reset()
    assert trigger.add(_message("2"), force=True) is not None


def test_stage2_request_has_one_system_and_one_user_message() -> None:
    messages = build_stage2_messages([], mode=ConversationMode.ACTIVE, trigger="active_message")
    assert [item["role"] for item in messages] == ["system", "user"]
    assert "对话中" in messages[1]["content"]


def _message(message_id: str):
    from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

    return IncomingMessage(
        message_id=message_id,
        session_id="group:717151356",
        user_id="1",
        text="测试",
        target=MessageTarget(group_id="717151356"),
    )
