"""出站补发：没送出去的消息，等连接回来再发（用户 2026-09-28 要的）。

要守住的性质：

1. **只补"确定没送出去"的那一类**（`MessageNotDelivered`）。"发出去了但没回执"
   （`DeliveryUncertain`）与"对面拒收"（`DeliveryRejected`）一律不补——前者补了
   会让同一句话在群里出现两遍，后者补了只是反复撞墙；
2. **顺序不能乱**，第 N 条没成功就不要把第 N+1 条先发出去；
3. **三道上限**：条数、年龄、尝试次数。没有年龄上限时，断线一小时回来会把一小时的
   回复连着喷出来，那比丢消息更像事故；
4. **连接没回来时不去撞那 15 秒的连接等待**（`flush` 先看 `transport.connected`）。
"""
import asyncio

from qq_roleplay_bot.outbox import OUTBOX_MAX_AGE_SECONDS, Outbox
from qq_roleplay_bot.stage3_main import _deliver_reply
from qq_roleplay_bot.transport import (
    DeliveryRejected,
    DeliveryUncertain,
    IncomingMessage,
    MessageNotDelivered,
    MessageTarget,
    OutgoingMessage,
)

TARGET = MessageTarget(group_id="717151356")


class _Transport:
    """可控的假传输层：`send` 按脚本抛异常，"连接"用 `connected` 表示。"""

    def __init__(self, *, failures: int = 0, error: Exception | None = None) -> None:
        self.sent: list[tuple[MessageTarget, str, str]] = []
        self.connected = True
        self.failures = failures
        self.error = error or MessageNotDelivered("尚未连接")
        self.typing: list[str] = []

    async def send(self, target, text, *, reply_to: str = "") -> None:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        self.sent.append((target, text, reply_to))

    async def send_typing(self, target, notice="typing") -> None:
        self.typing.append(notice)


def _message(text: str = "在吗") -> IncomingMessage:
    return IncomingMessage(message_id="m1", session_id="group:717151356", user_id="100",
                           text=text, target=TARGET)


def _engine(**kwargs):
    from qq_roleplay_bot.stage3_main import DialogueEngine

    engine = DialogueEngine(object(), typing_sim=False, **kwargs)
    engine.record_sent_reply = lambda *args, **kw: None  # 语境不是这条测试要管的
    return engine


# --- 队列本身 ---------------------------------------------------------------


def test_enqueue_keeps_order() -> None:
    outbox = Outbox(clock=lambda: 100.0)
    assert outbox.enqueue(TARGET, "第一句")
    assert outbox.enqueue(TARGET, "第二句", reply_to="m9")
    items = outbox.pending()
    assert [item.text for item in items] == ["第一句", "第二句"]
    assert items[1].reply_to_message_id == "m9"


def test_full_queue_drops_the_oldest() -> None:
    outbox = Outbox(clock=lambda: 100.0, max_items=2)
    for text in ("第一句", "第二句", "第三句"):
        outbox.enqueue(TARGET, text)
    assert [item.text for item in outbox.pending()] == ["第二句", "第三句"]
    assert outbox.stats()["dropped"] == 1


def test_flush_resends_in_order_once_connected() -> None:
    outbox = Outbox(clock=lambda: 100.0)
    outbox.enqueue(TARGET, "第一句")
    outbox.enqueue(TARGET, "第二句")
    transport = _Transport()

    resent, dropped = asyncio.run(outbox.flush(transport))
    assert (resent, dropped) == (2, 0)
    assert [text for _, text, _ in transport.sent] == ["第一句", "第二句"]
    assert len(outbox) == 0


def test_flush_does_nothing_while_disconnected() -> None:
    """连接没回来时**不试着发**：`send` 会等 15 秒连接超时，把这个时钟顶住。"""

    outbox = Outbox(clock=lambda: 100.0)
    outbox.enqueue(TARGET, "等一下再发")
    transport = _Transport()
    transport.connected = False

    assert asyncio.run(outbox.flush(transport)) == (0, 0)
    assert transport.sent == []
    assert len(outbox) == 1


def test_flush_stops_at_the_first_failure_to_keep_order() -> None:
    outbox = Outbox(clock=lambda: 100.0)
    outbox.enqueue(TARGET, "第一句")
    outbox.enqueue(TARGET, "第二句")
    transport = _Transport(failures=1)  # 第一条失败，第二条不该被先发出去

    resent, dropped = asyncio.run(outbox.flush(transport))
    assert (resent, dropped) == (0, 0)
    assert transport.sent == []
    assert [item.text for item in outbox.pending()] == ["第一句", "第二句"]
    assert outbox.pending()[0].attempts == 1

    # 下次连接好了：两条按顺序补上。
    assert asyncio.run(outbox.flush(transport)) == (2, 0)
    assert [text for _, text, _ in transport.sent] == ["第一句", "第二句"]


def test_expired_messages_are_not_resent() -> None:
    now = {"value": 100.0}
    outbox = Outbox(clock=lambda: now["value"])
    outbox.enqueue(TARGET, "太久了")
    now["value"] += OUTBOX_MAX_AGE_SECONDS + 1
    transport = _Transport()

    assert asyncio.run(outbox.flush(transport)) == (0, 1)
    assert transport.sent == []
    assert outbox.stats()["dropped"] == 1


def test_message_is_dropped_after_too_many_attempts() -> None:
    outbox = Outbox(clock=lambda: 100.0, max_attempts=2)
    outbox.enqueue(TARGET, "总是发不出去")
    transport = _Transport(failures=99)

    for _ in range(2):
        asyncio.run(outbox.flush(transport))
    assert outbox.pending()[0].attempts == 2
    # 第三次直接放弃，不再去发。
    assert asyncio.run(outbox.flush(transport)) == (0, 1)
    assert outbox.pending() == ()


# --- 投递路径：哪些失败该排队、哪些不该 ---------------------------------------


def test_not_delivered_reply_is_queued_and_resent() -> None:
    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=1)
    engine = _engine()

    asyncio.run(_deliver_reply(transport, engine, _message(),
                               [OutgoingMessage(TARGET, "我在。", paced=True)], outbox=outbox))
    assert transport.sent == []
    assert [item.text for item in outbox.pending()] == ["我在。"]

    # 连接回来之后自动补上。
    assert asyncio.run(outbox.flush(transport)) == (1, 0)
    assert [text for _, text, _ in transport.sent] == ["我在。"]


def test_uncertain_delivery_is_never_retried() -> None:
    """发出去了但没回执：补发可能让同一句话出现两遍，所以宁可少一句。"""

    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=1, error=DeliveryUncertain("已发出但未收到回执"))
    engine = _engine()

    asyncio.run(_deliver_reply(transport, engine, _message(),
                               [OutgoingMessage(TARGET, "我在。", paced=True)], outbox=outbox))
    assert outbox.pending() == ()


def test_rejected_delivery_is_never_retried() -> None:
    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=1, error=DeliveryRejected("被禁言"))
    engine = _engine()

    asyncio.run(_deliver_reply(transport, engine, _message(),
                               [OutgoingMessage(TARGET, "我在。", paced=True)], outbox=outbox))
    assert outbox.pending() == ()


def test_multisegment_reply_queues_the_rest_without_retrying_each() -> None:
    """第一段确定发不出去之后，剩下的段直接排队——不再逐条去等 15 秒连接超时。"""

    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=1)  # 只给一次失败：如果后面还去发，就会成功
    engine = _engine()
    outgoing = [
        OutgoingMessage(TARGET, "第一段", paced=True),
        OutgoingMessage(TARGET, "第二段", paced=True),
    ]

    asyncio.run(_deliver_reply(transport, engine, _message(), outgoing, outbox=outbox))
    assert transport.sent == []
    assert [item.text for item in outbox.pending()] == ["第一段", "第二段"]


def test_send_failures_count_attempts_not_segments() -> None:
    """指标记的是**真的试过几次**：第一段失败后剩下的段只排队，不再计一次失败。"""

    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=9)
    engine = _engine()
    outgoing = [OutgoingMessage(TARGET, "第一段"), OutgoingMessage(TARGET, "第二段")]

    asyncio.run(_deliver_reply(transport, engine, _message(), outgoing, outbox=outbox))
    assert engine.metrics.snapshot()["send_failures"] == 1
    assert [item.text for item in outbox.pending()] == ["第一段", "第二段"]


def test_reply_without_outbox_still_sends() -> None:
    """不传队列时行为不变（既有调用点与测试都是这么用的）。"""

    transport = _Transport()
    engine = _engine()
    asyncio.run(_deliver_reply(transport, engine, _message(),
                               [OutgoingMessage(TARGET, "嗯。", paced=True)]))
    assert [text for _, text, _ in transport.sent] == ["嗯。"]


# --- 转告与诊断回执也接进来了（用户 2026-09-28："接入"） ----------------------

ADMIN_TARGET = MessageTarget(group_id="999999999")


def test_relay_that_cannot_be_delivered_is_queued_with_a_note() -> None:
    """转告是管理员确认过的动作：断线时不能静默消失，补发成功还要回他一声。"""

    from qq_roleplay_bot.stage3_main import RelayDelivery, _deliver_relay

    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=1)  # 转告本身发不出去，之后通知能发出去
    delivery = RelayDelivery(
        target=TARGET, text="转告内容", admin_target=ADMIN_TARGET,
    )

    asyncio.run(_deliver_relay(transport, delivery, outbox=outbox))
    # 转告本身没送出去；管理员收到的是"排队中"那条通知（通知发出去了）。
    assert [text for _, text, _ in transport.sent] == [
        "转告这次没送出去（对方连接不在），我已经记下了，接上就自动补发；发出去会再告诉你。"
    ]
    assert [item.text for item in outbox.pending()] == ["转告内容"]
    item = outbox.pending()[0]
    assert item.note_target == ADMIN_TARGET and item.note_text == "转告已发送：转告内容"

    # 连接回来：转告补发，便条把"已发送"告诉管理员。
    assert asyncio.run(outbox.flush(transport)) == (1, 0)
    assert [text for _, text, _ in transport.sent][-2:] == ["转告内容", "转告已发送：转告内容"]


def test_relay_queued_notice_reaches_the_admin_when_connection_is_back() -> None:
    """连通知都发不出去时，通知也进队列（不让管理员永远等不到回执）。"""

    from qq_roleplay_bot.stage3_main import RelayDelivery, _deliver_relay

    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=9)
    delivery = RelayDelivery(target=TARGET, text="转告内容", admin_target=ADMIN_TARGET)

    asyncio.run(_deliver_relay(transport, delivery, outbox=outbox))
    texts = [item.text for item in outbox.pending()]
    assert texts[0] == "转告内容"
    assert any("自动补发" in text for text in texts), texts

    transport.failures = 0
    resent, dropped = asyncio.run(outbox.flush(transport))
    assert (resent, dropped) == (2, 0)
    assert [text for _, text, _ in transport.sent][0] == "转告内容"


def test_diagnostics_followup_uses_the_outbox() -> None:
    """`/super processes cpu` 的结果是超管等了 5 秒的东西：连接断了也得补上。"""

    class Diagnostics:
        async def processes(self, mode="default"):
            return f"超管诊断：CPU 结果（{mode}）"

    outbox = Outbox(clock=lambda: 100.0)
    transport = _Transport(failures=1)
    sent: list[tuple[object, str]] = []

    async def sender(target, text):
        # 与 runtime 里那条通道同样的接法。
        from qq_roleplay_bot.stage3_main import _send_or_queue

        status = await _send_or_queue(transport, outbox, target, text)
        sent.append((target, status))

    engine = _engine(super_admin_user_ids=frozenset({"100"}), runtime_diagnostics=Diagnostics())
    engine.async_sender = sender

    async def run() -> None:
        result = await engine.handle(_message("/super processes cpu"))
        assert result is not None and "正在采样" in result.text
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert sent and sent[0][1] == "queued"
    assert [item.text for item in outbox.pending()] == ["超管诊断：CPU 结果（cpu）"]

    transport.failures = 0
    assert asyncio.run(outbox.flush(transport)) == (1, 0)
    assert "CPU 结果" in transport.sent[-1][1]
