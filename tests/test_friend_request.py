"""好友申请：名单内的自动通过，名单外的通知操作者（2026-10-26 用户定）。

用户原话两句，顺序就是这里的两个判据：

1. 「加好友的白名单跟 private 进行同步，这两个作为一份白名单」——名单只有一份，
   就是 `private_allow.json`（引擎里的 `private_debug_user_ids`）；
2. 「申请被拒建议给我发，通过邮件或者私聊」——**没通过的要通知**，
   私聊优先，私聊不通再用邮件。

钉住的性质（每条去掉对应实现都会红）：

* 名单内 → 调 `set_friend_add_request(flag, approve=True)`，**不打扰操作者**；
* 名单外 → **一个 API 都不调**，只通知（fail-closed：不给陌生人回话、不批）；
* 没有 `flag` → 批不了，按失败通知（**不拿 QQ 号猜 flag**）；
* 自动通过失败 → 也要通知（申请会一直挂着）；
* 同一条申请重复推送 → **只通知一次**；
* 通知：私聊发不出去才走邮件，两条都不通也不抛（只记日志）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from qq_roleplay_bot.runtime import FriendRequestGate, notify_operator
from qq_roleplay_bot.transport import DeliveryRejected, MessageNotDelivered, MessageTarget


class _FakeTransport:
    """只记下"发了什么 / 调了什么"，不发任何东西。"""

    def __init__(self, *, api_error: Exception | None = None,
                 send_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.sent: list[tuple[MessageTarget, str]] = []
        self.api_error = api_error
        self.send_error = send_error

    async def call_api(self, action: str, params: dict[str, object] | None = None):
        self.calls.append((action, dict(params or {})))
        if self.api_error is not None:
            raise self.api_error
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send(self, target: MessageTarget, text: str, *, reply_to: str = "") -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((target, text))


def _engine(allowed=("900",), *, mail=None):
    registry = SimpleNamespace(mail=SimpleNamespace(operator_sender=mail)) if mail else None
    return SimpleNamespace(
        private_debug_user_ids={str(item) for item in allowed},
        outbox=None,
        plugin_registry=registry,
    )


def _request(user_id="901", flag="flag-1", comment="我是群友"):
    return {
        "post_type": "request",
        "request_type": "friend",
        "user_id": int(user_id),
        "comment": comment,
        "flag": flag,
    }


def test_a_whitelisted_request_is_approved_without_bothering_the_operator() -> None:
    transport = _FakeTransport()
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    gate = FriendRequestGate(transport, _engine({"900"}), notify=notify)
    asyncio.run(gate.handle(_request(user_id="900", flag="f-900")))

    assert len(transport.calls) == 1, transport.calls
    action, params = transport.calls[0]
    assert action == "set_friend_add_request"
    assert params == {"flag": "f-900", "approve": True}
    assert notes == [], notes


def test_a_stranger_request_is_not_approved_but_the_operator_is_told() -> None:
    transport = _FakeTransport()
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    gate = FriendRequestGate(transport, _engine({"900"}), notify=notify)
    asyncio.run(gate.handle(_request(user_id="901", flag="f-901", comment="加一下")))

    assert transport.calls == [], "名单外不许调任何 API（fail-closed）"
    assert len(notes) == 1, notes
    assert "901" in notes[0]
    assert "不在私聊白名单里" in notes[0]
    assert "加一下" in notes[0], "留言要带上，方便判断是谁"
    assert "/yunru private" in notes[0]


def test_the_same_request_is_announced_only_once() -> None:
    transport = _FakeTransport()
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    gate = FriendRequestGate(transport, _engine({"900"}), notify=notify)
    asyncio.run(gate.handle(_request(user_id="901", flag="same-flag")))
    asyncio.run(gate.handle(_request(user_id="901", flag="same-flag")))

    assert len(notes) == 1, notes


def test_a_request_without_flag_is_not_approved_and_says_why() -> None:
    transport = _FakeTransport()
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    gate = FriendRequestGate(transport, _engine({"900"}), notify=notify)
    asyncio.run(gate.handle(_request(user_id="900", flag="")))

    assert transport.calls == [], "没有 flag 就批不了，不许拿 QQ 号猜"
    assert len(notes) == 1 and "没有可用的申请凭证" in notes[0], notes


def test_a_failed_approval_is_reported_to_the_operator() -> None:
    transport = _FakeTransport(api_error=DeliveryRejected("被拒"))
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    gate = FriendRequestGate(transport, _engine({"900"}), notify=notify)
    asyncio.run(gate.handle(_request(user_id="900", flag="f-900")))

    assert len(notes) == 1, notes
    assert "自动通过好友申请失败" in notes[0]
    assert "900" in notes[0]


def test_other_events_are_ignored() -> None:
    transport = _FakeTransport()
    notes: list[str] = []

    async def notify(text):
        notes.append(text)

    gate = FriendRequestGate(transport, _engine({"900"}), notify=notify)
    asyncio.run(gate.handle({"post_type": "message", "message_type": "private"}))
    asyncio.run(gate.handle({"post_type": "request", "request_type": "group"}))

    assert transport.calls == [] and notes == []


def test_the_notice_goes_by_private_message_first() -> None:
    transport = _FakeTransport()
    mailed: list[tuple[str, str]] = []

    async def mail(subject, body):
        mailed.append((subject, body))

    asyncio.run(notify_operator(transport, _engine({"900"}, mail=mail), "有人申请加好友"))

    assert transport.sent, "私聊这条路在的时候要先走私聊"
    assert transport.sent[0][0] == MessageTarget(user_id="900000001"), transport.sent
    assert mailed == [], "私聊发成功了就不该再发邮件"


def test_the_notice_falls_back_to_mail_when_private_fails() -> None:
    transport = _FakeTransport(send_error=MessageNotDelivered("连接不在"))
    mailed: list[tuple[str, str]] = []

    async def mail(subject, body):
        mailed.append((subject, body))

    status = asyncio.run(
        notify_operator(transport, _engine({"900"}, mail=mail), "有人申请加好友")
    )

    assert status == "mailed", status
    assert len(mailed) == 1 and mailed[0][1] == "有人申请加好友"


def test_a_notice_that_cannot_be_delivered_does_not_raise() -> None:
    """两条路都不通：只记日志，绝不把申请处理带崩（这里连邮件都没配）。"""

    transport = _FakeTransport(send_error=MessageNotDelivered("连接不在"))

    status = asyncio.run(notify_operator(transport, _engine({"900"}, mail=None), "x"))

    assert status == "failed"


def test_the_transport_hands_a_friend_request_to_the_core() -> None:
    """传输层只负责"交出去"：判定一个都不做（判定在核心）。"""

    import json

    from qq_roleplay_bot.chat_log import ChatLog
    from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
    from qq_roleplay_bot.raw_events import RawEventLog

    seen: list[dict[str, object]] = []

    async def handler(payload: dict[str, object]) -> None:
        seen.append(payload)

    async def run() -> None:
        transport = OneBotWebSocketTransport(
            chat_log=ChatLog(enabled=False), raw_events=RawEventLog(enabled=False)
        )
        transport.on_friend_request = handler
        await transport._handle_payload(json.dumps(_request(user_id="901", flag="f-901")))
        # 处理是 create_task 丢出去的，给它一次调度机会
        await asyncio.sleep(0.01)

    asyncio.run(run())

    assert len(seen) == 1, seen
    assert seen[0]["user_id"] == 901
