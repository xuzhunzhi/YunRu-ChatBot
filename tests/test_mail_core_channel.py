"""**M1：发信能力进核心**（`dev/data/plans/02-邮件重构.md` §三 M1）。

判据（用户 2026-10-22 交办的原话）：**删掉 `plugins/mail/` 之后，`outage_notice` /
`timeout_alert` 仍能装、仍能发信。**

## 这条文件测什么、不测什么

| 测 | 不测 |
| --- | --- |
| 核心 `qq_roleplay_bot.mail` 的 provide 点、两条窄口的形状、假 `MailClient` 下真发一封 | 真实 SMTP/CLI（子进程不起，`MailClient` 全换假对象） |
| `runtime.build_engine()` 在 **attach 插件之前**把能力放上注册表 | 插件树里那两个插件的源码（它们在 `yunru-stage4`，由那边的测试覆盖） |
| `REQUIRES = ("mail",)` 在"只有核心能力、没有 mail 插件"时**放行**；两边都没有时**跳过自己** | 真实删目录（用临时加载根模拟，别动工作区） |

## 为什么"删插件"用临时加载根模拟

`dev` 这棵树的 `plugins/` 下本来就只有 `__init__.py`（插件源码在 `yunru-stage4`）。
要验"没有 mail 插件目录"这件事，不需要真删任何东西：往 `plugins.__path__` 上挂一个
临时根，里面只放两个**形状与真实告警插件一致**的 `plugin.py`（声明
`REQUIRES = ("mail",)`、在 `register()` 里登记一个接收者），`mail` 目录**故意不建**。
发现机制的"找不到前置就跳过自己"走的就是 `ModuleNotFoundError` 那条路，
与真删目录同一条分支。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
from contextlib import contextmanager
from unittest import mock

from plugin_support import plugin_registry
from qq_roleplay_bot import dev_config
from qq_roleplay_bot import plugins as plugins_module
from qq_roleplay_bot import runtime
from qq_roleplay_bot.mail import MailError, provide_senders

RECIPIENT_ENV = "QQBOT_MAIL_REPORT_TO"
#: `.invalid` 是 RFC 2606 留的"永远不存在"的顶级域，不会被误当成真地址。
FAKE_RECIPIENT = "someone@example.invalid"


class _FakeMailClient:
    """替身 `MailClient`：只记下 `send(...)` 的关键字，不起子进程、不连网。"""

    instances: list["_FakeMailClient"] = []

    def __init__(self, executable: str = "agently-cli", *, workdir: object = None,
                 **kwargs) -> None:
        self.executable = executable
        self.workdir = workdir
        self.sends: list[dict] = []
        _FakeMailClient.instances.append(self)

    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> dict:
        self.sends.append({"to": to, "subject": subject, "body": body})
        return {"message_id": "fake"}


class _FailingMailClient:
    """发信永远失败：用来钉住"失败是一个带类别的 `MailError`，不是裸异常"。"""

    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> dict:
        raise MailError("邮箱工具超时", kind="timeout")


@contextmanager
def _recipient_configured():
    """把"这个部署配了收件人"这个前提显式摆好，退出时按原样还回去。

    源码里没有默认收件人（用户 2026-10-16："必须由 `.env` 给"），所以"配了之后
    那条路对不对"必须由用例自己配这个前提，不能指望本机环境。
    """

    saved = os.environ.get(RECIPIENT_ENV)
    os.environ[RECIPIENT_ENV] = FAKE_RECIPIENT
    try:
        yield FAKE_RECIPIENT
    finally:
        os.environ.pop(RECIPIENT_ENV, None)
        if saved is not None:
            os.environ[RECIPIENT_ENV] = saved


@contextmanager
def _no_recipient():
    """"没配收件人"的前提：环境与回落项都清空（`.env` 可能填了回落项）。"""

    saved = os.environ.get(RECIPIENT_ENV)
    os.environ.pop(RECIPIENT_ENV, None)
    with mock.patch.object(dev_config, "MAIL_OWNER_FROM", ""):
        try:
            yield
        finally:
            if saved is not None:
                os.environ[RECIPIENT_ENV] = saved


@contextmanager
def _tmp_plugin_root(dirs: dict[str, str]):
    """挂一个临时加载根（`plugins.__path__`），退出时模块、路径、发现状态全部还原。

    `dirs` 是 `{目录名: plugin.py 的源码}`；**故意允许里面没有 `mail`**——
    那正是 M1 要验的"删掉邮件插件"。
    """

    previous_path = list(plugins_module.__path__)
    previous_state = (plugins_module._REGISTRY, plugins_module._LAST_LOADED)
    saved_modules = set(sys.modules)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "plugins"
        root.mkdir(parents=True)
        for name, body in dirs.items():
            directory = root / name
            directory.mkdir()
            (directory / "plugin.py").write_text(body, encoding="utf-8")
        plugins_module.__path__.append(str(root))
        try:
            yield root
        finally:
            plugins_module.__path__[:] = previous_path
            plugins_module._REGISTRY, plugins_module._LAST_LOADED = previous_state
            for name in list(sys.modules):
                if name not in saved_modules and name.startswith(plugins_module.__name__):
                    sys.modules.pop(name, None)


#: 形状与 `plugins/outage_notice/plugin.py` 一致的最小版：只声明 REQUIRES、
#: 在 register() 里登记一个接收者。**不 import mail**（依赖只经由接缝）。
OUTAGE_PROBE = '''
REQUIRES = ("mail",)
TITLE = "掉线探针"


def register(registry):
    async def receiver():
        return None

    registry.link.on_disconnect(receiver)
'''

TIMEOUT_PROBE = '''
REQUIRES = ("mail",)
TITLE = "超时探针"


def register(registry):
    registry.provide_owner_channel("probe-timeout")
'''


# --- ① 核心 provide 的接缝：两条窄口都在，形状对 -------------------------------


def test_core_provides_both_senders_and_both_narrow_ports_work() -> None:
    """核心放上 `operator_sender` / `address_sender`；两条窄口都能真的发出信。

    这是 M1 的"接缝存在且形状对"：`MailSeams.notify`（主题, 正文）与
    `MailSeams.send_to`（地址, 主题, 正文）——**函数，不是客户端**。
    """

    fake = _FakeMailClient()
    registry = plugin_registry()

    with _recipient_configured() as recipient:
        operator, address = provide_senders(registry.mail, client=fake)

        assert registry.mail.knows() is True, "核心该提供「给操作者发一封」"
        assert registry.mail.knows_address_sender() is True, "核心该提供「寄给指定地址」"
        assert registry.mail.operator_sender is operator
        assert registry.mail.address_sender is address
        assert asyncio.run(registry.mail.notify("主题一", "正文一")) == {"message_id": "fake"}
        assert asyncio.run(registry.mail.send_to("a@example.invalid", "主题二", "正文二")) \
            == {"message_id": "fake"}

    assert [item["to"] for item in fake.sends] == [recipient, "a@example.invalid"]
    assert [item["subject"] for item in fake.sends] == ["主题一", "主题二"]
    assert [item["body"] for item in fake.sends] == ["正文一", "正文二"]


def test_core_never_overwrites_a_sender_somebody_else_already_provided() -> None:
    """已经有人提供过就不覆盖——测试/别的装配先塞的那条线不该被核心换掉。

    这也是插件侧 `wire.build_*` 的同一条纪律：两边同时在场（M1 到 M4 之间）
    不该出现"谁后跑谁赢"。
    """

    calls: list[tuple[str, str]] = []

    async def existing(subject: str, body: str) -> dict:
        calls.append((subject, body))
        return {"message_id": "existing"}

    registry = plugin_registry()
    registry.mail.provide(existing)
    fake = _FakeMailClient()

    operator, _address = provide_senders(registry.mail, client=fake)

    assert operator is existing
    assert registry.mail.operator_sender is existing
    assert asyncio.run(registry.mail.notify("别覆盖", "这条")) == {"message_id": "existing"}
    assert calls == [("别覆盖", "这条")]
    assert fake.sends == [], "覆盖之后这一封会去起真客户端——这正是要挡的"


# --- ② 假 MailClient 下"发一封"：成功走核心路径、失败是带类别的 MailError --------


def test_a_send_through_the_core_path_succeeds_with_a_fake_client() -> None:
    """核心路径发一封：`MailSeams.notify` → 核心 sender → 假 client，一路走通。"""

    fake = _FakeMailClient()
    registry = plugin_registry()

    with _recipient_configured() as recipient:
        provide_senders(registry.mail, client=fake)
        result = asyncio.run(registry.mail.notify("云茹断线了", "我这边断了。"))

    assert result == {"message_id": "fake"}
    assert len(fake.sends) == 1, fake.sends
    assert fake.sends[0]["to"] == recipient
    assert fake.sends[0]["subject"] == "云茹断线了"
    assert fake.sends[0]["body"] == "我这边断了。"


def test_a_failed_send_surfaces_as_a_categorised_mailerror() -> None:
    """失败**不变成静默成功**、也不把裸 `OSError`/子进程异常漏给调用方。

    `MailSeams.notify` 的契约是"照原样抛"（使用者据此决定记不记、退不退出），
    所以这里钉两件：异常是 `MailError`，而且 `kind` 还在——调用方分得清
    "钥匙串锁着"和"没装 CLI"。**吞掉异常才是错的**（那会让告警永远显示成功）。
    """

    registry = plugin_registry()
    with _recipient_configured():
        provide_senders(registry.mail, client=_FailingMailClient())
        try:
            asyncio.run(registry.mail.notify("主题", "正文"))
        except MailError as exc:
            assert exc.kind == "timeout", exc.kind
        else:  # pragma: no cover - 走到这里就是失败形状变了
            raise AssertionError("发信失败被静默吞掉了")


def test_without_a_recipient_the_operator_port_is_absent_and_provide_does_not_raise() -> None:
    """没配收件人：**只缺操作者那一条**，不猜地址、不抛，核心照常起。

    fail-closed 的正确形状是"这次没有这条路"（`knows()` 为假，使用者据此不登记），
    不是"发个空的试试"，也不是装配阶段直接抛。
    """

    registry = plugin_registry()

    with _no_recipient():
        operator, address = provide_senders(registry.mail, client=_FakeMailClient())

    assert operator is None
    assert registry.mail.knows() is False, "没配收件人时不该装作能发给操作者"
    assert callable(address), "「寄给指定地址」不需要操作者是谁，应照常提供"


# --- ③ 删掉 `plugins/mail/`：有核心能力 → 两个告警插件仍 loaded；都没有 → 跳过 ---


def test_requirers_still_load_when_only_the_core_mail_capability_exists() -> None:
    """**M1 判据**：没有 `mail` 插件目录，但有核心能力 → `REQUIRES` 满足、插件照装。

    `discover()` 现在先问 `registry.has_capability("mail")`；为真就**不再**去
    `_load("mail")`。所以加载结果里**没有 `"mail"`**、两个插件都在——"删掉邮件插件
    之后告警仍能装"就是这一条。
    """

    registry = plugin_registry()
    registry.mail.provide(_fake_sender())

    with _tmp_plugin_root({"probe_outage": OUTAGE_PROBE, "probe_timeout": TIMEOUT_PROBE}):
        loaded = plugins_module.discover(
            registry, only=("probe_outage", "probe_timeout"))

    assert "mail" not in loaded, f"有核心能力就不该再装同名插件：{loaded}"
    assert "probe_outage" in loaded, loaded
    assert "probe_timeout" in loaded, loaded
    assert registry.disconnect_receivers() != (), "掉线接收者该登记上"

    # 反向：没有核心能力、也没有 mail 插件 → 两个都跳过自己（fail-closed），不抛。
    bare = plugin_registry()
    with _tmp_plugin_root({"probe_outage": OUTAGE_PROBE, "probe_timeout": TIMEOUT_PROBE}):
        skipped = plugins_module.discover(
            bare, only=("probe_outage", "probe_timeout"))

    assert "probe_outage" not in skipped, skipped
    assert "probe_timeout" not in skipped, skipped
    assert bare.disconnect_receivers() == ()
    assert bare.knows_owner_channel("probe-timeout") is False


def _fake_sender():
    """一个假发信函数（`MailSeams` 上那个形状），只为让 `has_capability` 为真。"""

    async def sender(subject: str, body: str) -> dict:
        return {"message_id": "fake"}

    return sender


# --- ④ 装配点：能力在**插件 attach 之前**放上注册表 ----------------------------


class _FakeTransport:
    """够 `build_engine()` 起来的最小传输层（同 `test_plugin_install_root.py`）。"""

    async def call_api(self, action, params=None):
        return {"status": "ok", "retcode": 0,
                "data": {"role": "owner", "user_id": "9", "nickname": "n"}}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


def test_build_engine_provides_mail_before_attaching_plugins() -> None:
    """`runtime.build_engine()` 里 provide 点在 `attach_plugins()` **之前**。

    为什么顺序是判据：插件侧 `wire.build_*` 是"已有人提供就不覆盖"，所以核心先放，
    插件再装时才能确定性地 no-op；反过来的话"谁提供的那一条"会取决于发现顺序。
    """

    order: list[str] = []
    seen: dict[str, object] = {}

    def spy_provide(seams):
        order.append("provide")
        return provide_senders(seams, client=_FakeMailClient())

    def spy_attach(registry, commands=None):
        order.append("attach")
        seen["knows"] = registry.mail.knows()
        registry.loaded = ()
        return commands

    with _recipient_configured(), \
            mock.patch.object(runtime, "provide_mail_senders", spy_provide), \
            mock.patch.object(runtime, "attach_plugins", spy_attach):
        runtime.build_engine(_FakeTransport())

    assert order == ["provide", "attach"], order
    assert seen["knows"] is True, "attach 那一刻能力必须已经在注册表上"
