from qq_roleplay_bot.transport import IncomingMessage, MessageTarget
from qq_roleplay_bot_legacy.v1_main import MessageDeduplicator, V1Trigger


def message(message_id: str, mentioned: bool = False) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        session_id="group:717151356",
        user_id="1",
        text="测试",
        target=MessageTarget(group_id="717151356"),
        is_bot_mentioned=mentioned,
    )


def test_deduplicator_rejects_duplicate_ids() -> None:
    deduplicator = MessageDeduplicator()
    assert deduplicator.accept("1")
    assert not deduplicator.accept("1")


def test_trigger_reaches_batch_threshold() -> None:
    trigger = V1Trigger(batch_size=2, interval_seconds=60, cooldown_seconds=60)
    assert trigger.add(message("1")) is None
    batch = trigger.add(message("2"))
    assert batch is not None
    assert [item.message_id for item in batch] == ["1", "2"]


def test_mention_forces_trigger_when_not_in_cooldown() -> None:
    trigger = V1Trigger(batch_size=20, interval_seconds=60, cooldown_seconds=60)
    batch = trigger.add(message("1", mentioned=True), force=True)
    assert batch is not None
    assert batch[0].message_id == "1"


def test_mention_does_not_bypass_cooldown() -> None:
    trigger = V1Trigger(batch_size=1, interval_seconds=60, cooldown_seconds=60)
    assert trigger.add(message("1")) is not None
    assert trigger.add(message("2", mentioned=True), force=True) is None
