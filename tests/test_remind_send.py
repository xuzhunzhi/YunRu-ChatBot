"""提醒的**投递口**：`registry.remind_send`（核心拥有出站，插件只交意图）。

用户 2026-10-08 拍板 (a)：插件那一侧**没有任何一条路**能把提醒送到人手上——
`call_action` 的 `send_*` 属 `purpose="send"`、插件过的是 `"read"`（被 `ActionDenied`）；
`notify` 的收件人是操作者；`registry.mail` 只有 `operator_sender`（寄给操作者）；
`registry.chat.deliver` 只回文本、它自己不发。所以核心给一条**只做这一件事**的窄口：

    async (RemindDelivery) -> RemindSendResult

`RemindDelivery` 里**只有**"发给谁 / 发到哪 / 说什么"；没有 transport、没有 action 名、
没有权限名单（出站是核心的事，AGENTS §2.3）。

这一份钉住：

1. 群里 → **@ 那个人**（传输层支持 `send_at` 就用它；不支持退回纯文本，消息照发）；
2. 私聊 → 直接发给他（`group_id=""`）；
3. 三种失败分类都**如实回结果**、不抛异常；认不出的投递什么都不发也不报错；
4. `RemindDelivery` 是一张白名单（塞不进 transport/权限名单）；
5. 接缝的缺省值是 `None`（核心没接上时插件必须容忍），`build_engine` 会把它装上。

突变（各自还原）：往 `RemindDelivery` 里塞 transport/权限名单 → 第 4 条红；
认不出的投递改成抛异常 → 第 3 条红。
"""
import asyncio

from qq_roleplay_bot.plugins import PluginRegistry
from qq_roleplay_bot.remind import RemindDelivery
from qq_roleplay_bot.runtime import _SeamBinder
from qq_roleplay_bot.transport import (
    DeliveryRejected,
    DeliveryUncertain,
    MessageNotDelivered,
    MessageTarget,
)

GROUP = "717151356"
PERSON = "100"
ALLOWED_FIELDS = frozenset({"person_id", "text", "group_id", "channel"})


class _FakeTransport:
    """够接缝用就行：`send` / 可选 `send_at`，可以按需抛分类异常。"""

    connected = True

    def __init__(self, *, with_at: bool = True, fail: Exception | None = None) -> None:
        self.sent: list[tuple[MessageTarget, str]] = []
        self.mentions: list[tuple[MessageTarget, str, str]] = []
        self.fail = fail
        if with_at:
            self.send_at = self._send_at

    async def send(self, target, text, *, reply_to=""):
        if self.fail is not None:
            raise self.fail
        self.sent.append((target, text))

    async def _send_at(self, target, user_id, text, *, reply_to=""):
        if self.fail is not None:
            raise self.fail
        self.mentions.append((target, user_id, text))


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("投递这条路不该调用模型")


def _seam(transport) -> _SeamBinder:
    from qq_roleplay_bot.stage3_main import DialogueEngine

    return _SeamBinder(DialogueEngine(_NeverCalled()), transport)


# --- 1 / 2. 群里 @ 他，私聊直接发 ---------------------------------------------


def test_a_group_delivery_mentions_the_person() -> None:
    transport = _FakeTransport()
    result = asyncio.run(_seam(transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP)))

    assert result.sent is True and result.reason == "ok"
    assert transport.sent == []
    assert len(transport.mentions) == 1
    target, user_id, text = transport.mentions[0]
    assert target.group_id == GROUP and user_id == PERSON and text == "交作业"


def test_a_group_delivery_falls_back_to_plain_text_without_send_at() -> None:
    """传输层没有 `send_at` 这个可选能力时，消息**照发**（少一个 @ 可以接受）。"""

    transport = _FakeTransport(with_at=False)
    result = asyncio.run(_seam(transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP)))

    assert result.sent is True
    assert transport.mentions == []
    assert transport.sent == [(MessageTarget(group_id=GROUP), "交作业")]


def test_a_private_delivery_goes_to_the_person() -> None:
    transport = _FakeTransport()
    result = asyncio.run(_seam(transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业")))

    assert result.sent is True
    assert transport.mentions == [], "私聊不该走 @"
    assert transport.sent == [(MessageTarget(user_id=PERSON), "交作业")]


def test_an_explicit_channel_is_honoured() -> None:
    """明说 `"group"` / `"private"` 时按它发（不看群号推）。"""

    group_transport = _FakeTransport()
    group = asyncio.run(_seam(group_transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP, channel="group")))
    assert group.sent is True and len(group_transport.mentions) == 1

    private_transport = _FakeTransport()
    private = asyncio.run(_seam(private_transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", channel="private")))
    assert private.sent is True
    assert private_transport.sent == [(MessageTarget(user_id=PERSON), "交作业")]


# --- 1b. 说了发到哪、却缺那个号：不合法，什么都不发 ---------------------------


def test_a_group_delivery_without_a_group_id_is_refused() -> None:
    """独立审查 2026-10-09 第 9 条：从前这里会**悄悄按私聊发**。

    "说的是发到群里、回复也说'我在群里 @ 你'，可没给群号" —— 那等于把群里该说的
    话塞进私聊。现在是不合法：什么都不发、如实回 `no_target`。
    """

    transport = _FakeTransport()
    result = asyncio.run(_seam(transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", group_id="", channel="group")))

    assert result.sent is False and result.reason == "no_target", result
    assert transport.sent == [] and transport.mentions == [], "不许改投私聊"


def test_a_private_delivery_with_a_group_id_is_refused() -> None:
    transport = _FakeTransport()
    result = asyncio.run(_seam(transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP, channel="private")))

    assert result.sent is False and result.reason == "no_target", result
    assert transport.sent == [] and transport.mentions == []


def test_an_unrecognised_channel_is_refused() -> None:
    transport = _FakeTransport()
    result = asyncio.run(_seam(transport).remind_send(
        RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP, channel="telepathy")))

    assert result.sent is False and result.reason == "no_target", result
    assert transport.sent == [] and transport.mentions == []


# --- 3. 失败如实回结果；认不出的什么都不发 -----------------------------------


def test_send_failures_come_back_as_results_not_exceptions() -> None:
    for error, reason in ((MessageNotDelivered("断了"), "not_delivered"),
                          (DeliveryUncertain("不知道"), "uncertain"),
                          (DeliveryRejected("对方还不是好友"), "rejected"),
                          (RuntimeError("传输层没分类的异常"), "error")):
        transport = _FakeTransport(fail=error)
        result = asyncio.run(_seam(transport).remind_send(
            RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP)))
        assert result.sent is False and result.reason == reason, (error, result)
        assert result.detail, "失败要说清是哪一类"


def test_a_delivery_without_a_target_or_text_sends_nothing_and_does_not_raise() -> None:
    for delivery in (RemindDelivery(person_id="", text="交作业", group_id=GROUP),
                     RemindDelivery(person_id=PERSON, text="", group_id=GROUP)):
        transport = _FakeTransport()
        result = asyncio.run(_seam(transport).remind_send(delivery))
        assert result.sent is False and result.reason == "no_target"
        assert transport.sent == [] and transport.mentions == []


# --- 4. 投递意图是一张白名单 -------------------------------------------------


def test_the_delivery_is_a_whitelist() -> None:
    assert set(RemindDelivery.__dataclass_fields__) == ALLOWED_FIELDS
    delivery = RemindDelivery(person_id=PERSON, text="交作业", group_id=GROUP)
    for forbidden in ("transport", "engine", "call_action", "action", "admin_user_ids",
                      "super_admin_user_ids", "allowed_groups", "outbox", "send"):
        assert not hasattr(delivery, forbidden), forbidden


# --- 5. 缺省与装配 -----------------------------------------------------------


def test_the_registry_seam_defaults_to_none() -> None:
    """核心没接上时插件必须容忍（那就什么都不发）；这里钉的是缺省值本身。"""

    assert PluginRegistry().remind_send is None


def test_build_engine_wires_the_delivery_seam() -> None:
    from qq_roleplay_bot import runtime

    class _Transport:
        async def call_api(self, action, params=None):
            return {"status": "ok", "retcode": 0, "data": {}}

        async def send(self, target, text, **kwargs):
            return None

        async def start(self):
            return None

    engine = runtime.build_engine(_Transport())
    assert callable(engine.plugin_registry.remind_send), "核心没有把投递口装上"
    result = asyncio.run(engine.plugin_registry.remind_send(
        RemindDelivery(person_id=PERSON, text="交作业")))
    assert result.sent is True


if __name__ == "__main__":
    import unittest

    unittest.main()
