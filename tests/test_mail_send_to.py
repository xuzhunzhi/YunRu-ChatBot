"""邮件接缝上**第二条窄口**（`MailSeams.send_to`）的离线覆盖。

由来（2026-10-08）：到点提醒的邮箱那一路要寄给**那个人**，而原来开放出去的那一条
（`notify`）收件人写死是操作者。两条能力分开登记、各自独立——这一份钉住三件事：

1. **新的那条能用，而且是窄的**：地址必须是**一个**地址的形状——空、多收件人
   （逗号 / 分号 / 空格）、带换行（头注入）、不像地址的东西**一律拒**，一个都不许漏进
   mail 那一侧；
2. **旧的那条一个字没变**：`provide` / `knows` / `notify`（发给操作者）照旧，
   新口子**不碰** `operator_sender`，两条口子互不替换；
3. **没有提供者就抛，不静默**（与 `notify` 同一纪律："以为发了其实没发"最贵）。

还有一条走真装配的：`plugins/mail/plugin.py` 的 `register()` 会把**两条**都放上去
（`build_address_mail_sender` 用的是 mail 自己的 `MailClient`，这里用替身把
"确实交上去了"这件事与"真的连邮箱"分开测）。

突变（各自还原）：把 `_one_address` 的校验去掉 → 第 1 条红；让 `send_to` 去写
`operator_sender` → 第 2 条红；没有提供者时改成静默返回 `None` → 第 3 条红。
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import os
import tempfile
from unittest.mock import patch

from qq_roleplay_bot.plugins import MailSeams, PluginRegistry, discover

MAIL_WIRE = pathlib.Path(__file__).resolve().parents[1] / "src" / "qq_roleplay_bot" / "plugins" / "mail" / "wire.py"


async def _no_action(action, params=None):
    return None


class _Recorder:
    """一个"寄信"的替身：把每一次调用记下来。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def __call__(self, address: str, subject: str, body: str) -> dict:
        self.sent.append((address, subject, body))
        return {"status": "ok"}


def _seams_with(sender) -> MailSeams:
    seams = MailSeams()
    seams.provide_address_sender(sender)
    return seams


# --- 1. 新那条能用，而且窄 ----------------------------------------------------


def test_a_provided_sender_is_used_with_the_address_it_was_given() -> None:
    recorder = _Recorder()
    seams = _seams_with(recorder)

    assert seams.knows_address_sender() is True
    asyncio.run(seams.send_to("lele@example.com", "到点提醒：交作业", "正文"))

    assert recorder.sent == [("lele@example.com", "到点提醒：交作业", "正文")]


def test_only_one_well_formed_address_gets_through() -> None:
    """**窄就窄在这里**：多收件人、头注入、空、不像地址的一律拒。"""

    recorder = _Recorder()
    seams = _seams_with(recorder)
    refused = ("", "   ", "a@b.com, c@d.com", "a@b.com; c@d.com", "a@b.com c@d.com",
               "a@b.com\nBcc: eve@example.com", "a@b.com\r\nX: y", "没有邮箱",
               "lele@", "@example.com", "lele@example")
    for address in refused:
        try:
            asyncio.run(seams.send_to(address, "s", "b"))
        except ValueError:
            continue
        raise AssertionError(f"这个地址被放进去了：{address!r}")
    assert recorder.sent == [], "被拒的地址还是寄出去了"

    # 一个好地址照常过（上一条已经验过内容，这里只验"没被误杀"）。
    asyncio.run(seams.send_to("a.b+c@sub.example.co", "s", "b"))
    assert recorder.sent[0][0] == "a.b+c@sub.example.co"


def test_without_a_provider_it_raises_rather_than_pretending() -> None:
    seams = MailSeams()
    assert seams.knows_address_sender() is False
    try:
        asyncio.run(seams.send_to("lele@example.com", "s", "b"))
    except RuntimeError as exc:
        assert "寄给某个地址" in str(exc) or "registry.mail" in str(exc), str(exc)
    else:
        raise AssertionError("没有提供者时静默成功了")


def test_a_non_callable_is_refused_at_registration() -> None:
    seams = MailSeams()
    seams.provide_address_sender("这不是函数")
    assert seams.knows_address_sender() is False


# --- 2. 旧的那条一个字没变 ---------------------------------------------------


def test_the_operator_path_is_untouched_by_the_new_one() -> None:
    """两条口子**互不替换**：新口子不许动 `operator_sender`，旧口子也不许动新的。"""

    operator_calls: list[tuple[str, str]] = []
    address_calls: list[tuple[str, str, str]] = []

    async def operator(subject: str, body: str) -> dict:
        operator_calls.append((subject, body))
        return {"status": "ok"}

    async def address(to: str, subject: str, body: str) -> dict:
        address_calls.append((to, subject, body))
        return {"status": "ok"}

    seams = MailSeams()
    seams.provide(operator)
    seams.provide_address_sender(address)

    assert seams.knows() is True and seams.knows_address_sender() is True
    asyncio.run(seams.notify("断了", "正文"))
    asyncio.run(seams.send_to("lele@example.com", "到点提醒", "正文"))

    assert operator_calls == [("断了", "正文")]
    assert address_calls == [("lele@example.com", "到点提醒", "正文")]
    # 反过来也不行：只有旧口子时，新口子照样是"没有"。
    only_operator = MailSeams()
    only_operator.provide(operator)
    assert only_operator.knows() is True and only_operator.knows_address_sender() is False


def test_the_old_notify_still_raises_when_nobody_provided_it() -> None:
    """旧那条的 fail-closed 行为没被这一笔动过（掉线通知靠它）。"""

    seams = MailSeams()
    try:
        asyncio.run(seams.notify("s", "b"))
    except RuntimeError as exc:
        assert "发信能力" in str(exc)
    else:
        raise AssertionError("旧的 notify 被改成静默了")


def test_build_address_mail_sender_refuses_to_replace_an_existing_one() -> None:
    """已经有人提供过就不覆盖（与 `build_operator_mail_sender` 同一条纪律）。"""

    from qq_roleplay_bot.plugins.mail import wire

    recorder = _Recorder()
    seams = _seams_with(recorder)
    with patch("qq_roleplay_bot.plugins.mail.mail_client.MailClient") as client:
        returned = wire.build_address_mail_sender(seams)
    assert client.call_count == 0, "已经有人提供过还去造了一个 MailClient"
    assert returned is recorder and seams.address_sender is recorder


def test_the_mail_plugin_registers_both_capabilities() -> None:
    """走真装配：mail 的 `register()` 把**两条**都放上来了。"""

    from qq_roleplay_bot.plugins.mail import wire

    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QQBOT_DATA_DIR": tmp}):
        registry = PluginRegistry(call_action=_no_action)
        with patch.object(wire, "build_operator_mail_sender") as operator_builder, \
                patch.object(wire, "build_address_mail_sender") as address_builder:
            discover(registry, only=("mail",))
        assert operator_builder.call_count == 1, "旧那条没被放上去"
        assert address_builder.call_count == 1, "新那条没被放上去"


def test_wire_passes_the_address_through_to_its_own_mail_client() -> None:
    """`build_address_mail_sender` 用的是 mail **自己那套** `MailClient`（没有第二套）。"""

    from qq_roleplay_bot.plugins.mail import wire

    calls: list[dict] = []

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args

        async def send(self, **kwargs):
            calls.append(kwargs)
            return {"status": "ok"}

    seams = MailSeams()
    with patch("qq_roleplay_bot.plugins.mail.mail_client.MailClient", _Client):
        sender = wire.build_address_mail_sender(seams)
    assert sender is not None and seams.knows_address_sender() is True
    asyncio.run(seams.send_to("lele@example.com", "主题", "正文"))
    assert calls == [{"to": "lele@example.com", "subject": "主题", "body": "正文"}]


def test_the_address_helper_is_synchronous_and_total() -> None:
    """收件人校验是**同步纯函数**（在接缝那一侧），不是协程——省得有人等着它 await。"""

    from qq_roleplay_bot.plugins import _one_address

    assert not inspect.iscoroutinefunction(_one_address)
    assert _one_address("  lele@example.com  ") == "lele@example.com"
