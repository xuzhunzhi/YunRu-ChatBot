"""**掉线通知插件**（用户那半边：*"掉线可以调用 mail 插件给我发消息通知我"*）。

由来（2026-10-06 用户原话，四句都要在这份测试里有对应的断言）：

> *"掉线可以调用 mail 插件给我发消息通知我，具体怎么触发，yunru 怎么说话就你自己看着办了"*
> · *"断一次只发一次，不要反复调用"* · *"记住这个也是插件"*
> · *"这个作为 mail 插件的后置插件，也就是说 mail 插件是这个插件的依赖，不要搞错了"*

## 分工：哪些是这里的、哪些不是

| 断言 | 在哪 |
| --- | --- |
| "一段掉线只广播一次 / 重连后重新武装 / 恢复不发"（**状态机本体**） | `test_disconnect_notice.py`（本体侧） |
| "**接收者做出来的事**：发一封信、且只发一封" | **这里** |

所以这里的每条用例都是**端到端**的：用真实的 `discover()` 把插件装上、用真实的
`runtime._DisconnectNotifier` 喂事件、**断言信真的被发了**——不是"我认为它会发"。
核心那一份状态机不是本插件的代码，这里**不曾**在插件里再实现一遍（那正是要避免的：
两处各记一份必然分叉）。其中一条用例（在 `_feed_twice_as_a_mutation` 的注释里）
用的是"绕过核心一次性"的驱动方式——那是**突变验证**的手法，见文件末尾的说明。

## 全是离线、绝不真发信

mail 那侧一律塞**假对象**：
`_fake_sender()` 只管记下 `(主题, 正文)`；真实装配那条（`_real_mail_plugin`）
把 `MailClient` 换成假的，所以连子进程都不会起。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import pathlib
import re
import sys
import types
from contextlib import contextmanager
from unittest import mock

from plugin_support import plugin_registry
from qq_roleplay_bot import dev_config
from qq_roleplay_bot import plugins as plugins_module
from qq_roleplay_bot import runtime as runtime_module
from qq_roleplay_bot.plugins import PluginRegistry, discover
from qq_roleplay_bot.plugins.mail import plugin as mail_plugin
from qq_roleplay_bot.plugins.outage_notice import outage_notice
from qq_roleplay_bot.plugins.outage_notice import plugin as outage_plugin
from qq_roleplay_bot.prompt_guard import scan_persona_text

#: 插件自己的那一份：`REQUIRES` 必须**只**声明 mail，方向一个字都不能反。
OUTAGE_PLUGIN = "qq_roleplay_bot.plugins.outage_notice.plugin"
MAIL_PLUGIN = "qq_roleplay_bot.plugins.mail.plugin"


class _Transport:
    """看门狗用得到的全部东西：一个 `connected` 字段（与 `onebot_ws` 同名同义）。"""

    def __init__(self, connected: bool) -> None:
        self.connected = connected


def _fake_sender():
    """一个假发信函数：记下每次 `(主题, 正文)`，别的什么都不做。

    这就是 `plugins.MailSeams` 上那个函数的形状（`(subject, body) -> await`）——
    本插件**只**认这个形状，不知道背后是哪个模块。
    """

    sends: list[tuple[str, str]] = []

    async def sender(subject: str, body: str) -> object:
        sends.append((subject, body))
        return {"message_id": "fake"}

    return sender, sends


def _wired(*, sender=None, only=("outage_notice",)):
    """把插件装到一个注册表上（走真实的 `discover()`），回 `(registry, sends, wired)`。

    **`only` 缺省是 `("outage_notice",)`——刻意不把前置 `mail` 一起装上**。
    这不是绕开 `REQUIRES`：发现机制对 `only` 命中的那个名字照样先把它的 `REQUIRES`
    装一遍（`discover()` 的 `_load` 就是这么写的），而真装上 mail 会把它**自己的**发信
    函数 `provide` 上来，**盖掉**这里塞的假对象——于是用例会去起真的 CLI（实测踩到：
    假发信函数一次没被调，日志里是 `MailError: credential_lock_unavailable`）。
    所以"用假对象"这条路上，前置由用例自己扮演；**真装配那一半由
    `test_the_mail_plugin_provides_the_sender_and_the_outage_uses_it_end_to_end` 覆盖**，
    `REQUIRES` 的先后顺序由
    `test_the_requirement_points_at_mail_and_mail_is_installed_first` 覆盖。

    第三个返回值 `wired` 是**传进 `discover()` 的那一个 `plugin.py` 模块对象**：
    突变验证要临时翻它上面的 `MUTATION_SEND_EVERY_EVENT`，而 `discover()` 是按名字
    重新 import 的——直接打 `tests` 里 import 的那一份有可能不是同一个对象
    （测试文件是被 `spec_from_file_location` 加载的）。拿到哪一个，就翻哪一个。

    **`discover()` 会改进程级的 `_REGISTRY` / `_LAST_LOADED`**（那是它的正常行为，
    `prompt_library` 与面板清单读的就是它们）。别的测试文件也会读那两个值，
    所以这里用 `_discovery_state()` 把发现之前的那一份**还回去**——
    本插件与那两个值本来没有任何关系，测试不该顺手改变全进程的状态。
    """

    sender, sends = _fake_sender() if sender is None else (sender, [])
    registry = plugin_registry()
    registry.mail.provide(sender)
    with _discovery_state():
        loaded = discover(registry, only=only)
        wired = sys.modules[OUTAGE_PLUGIN]
    assert "outage_notice" in loaded, loaded
    return registry, sends, wired


def _notifier(registry):
    """按生产路径从注册表造广播对象（`_disconnect_notifier_for` 读的就是这份名单）。"""

    engine = types.SimpleNamespace(plugin_registry=registry)
    return runtime_module._disconnect_notifier_for(engine)


def _feed(states: list[bool], notifier) -> list[bool]:
    async def run() -> list[bool]:
        return [await notifier.observe(state) for state in states]

    return asyncio.run(run())


@contextmanager
def _flag(module, name: str, value):
    """临时改一个模块级配置值（`dev_config` 与 `runtime` 都是普通模块）。"""

    previous = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, previous)


@contextmanager
def _discovery_state():
    """发现机制的进程级记录：进这块就**原样还回去**（见 `_wired` 的说明）。

    两个值都属于"插件发现"本身（`_REGISTRY` 给核心按名字读 prompt 原稿用，
    `_LAST_LOADED` 给面板清单用），本插件与它们无关，测试不该顺手改掉。
    """

    previous = (plugins_module._REGISTRY, plugins_module._LAST_LOADED)
    try:
        yield
    finally:
        plugins_module._REGISTRY, plugins_module._LAST_LOADED = previous


@contextmanager
def _logs(name: str):
    """抓某个 logger 的日志行（与 `test_disconnect_notice._runtime_logs` 同法）。"""

    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# --- ① 掉线事件来了 → 发信被调用**正好一次**，内容就是那个模板 ------------------


def test_one_outage_sends_exactly_one_mail_with_the_fixed_template() -> None:
    """断一次 → **一封**；主题是固定的那一句，正文含【时间】【症状】【怎么办】。"""

    registry, sends, _ = _wired()

    fired = _feed([False, False, False], _notifier(registry))

    assert fired == [True, False, False], fired
    assert len(sends) == 1, f"断一次该只发一封，实际 {len(sends)} 封：{sends}"
    subject, body = sends[0]
    assert subject == outage_notice.SUBJECT == "云茹断线了", subject
    # 三段小标题一个都不能少（不必逐字，但这三样是"读得懂"的最低要求）
    assert "【时间】" in body and "【症状】" in body and "【怎么办】" in body, body
    # 时间：形如 2026-10-06 21:37:12（+08:00）——操作者不必自己换算时区
    assert re.search(r"【时间】\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}（[+-]\d{4}）", body), body
    # 症状：说的是"她收不到也发不出"，不是"协议断了"这类机制话
    assert "收不到" in body and "发不出" in body, body
    # 怎么办：给的是**动作**（把 NapCat 重开一次），不是"请检查配置"
    assert "NapCat" in body and "重开" in body, body


def test_the_body_is_built_without_any_model_call() -> None:
    """正文是拼出来的：**同一时刻**两次构造逐字相同，且不含任何模型调用的痕迹。

    用户要的是"告警要确定、要便宜、不能依赖模型可用性"——所以这条钉住"没有模型"：
    本用例不给任何 client / 网络，构造照样成功（真调模型的话这里会红）。
    """

    when = 1_800_000_000.0
    first = outage_notice.build_notice(when)
    second = outage_notice.build_notice(when)

    assert first == second
    subject, body = first
    assert subject == outage_notice.SUBJECT
    assert body == outage_notice.NOTICE_TEMPLATE.format(
        moment=outage_notice.format_moment(when))


# --- ② 同一段掉线里再来几次事件 → **不再发** ----------------------------------


def test_more_events_within_the_same_outage_send_nothing_more() -> None:
    """喂 5 次"没连上" → **仍然只有第一封**（用户："断一次只发一次，不要反复调用"）。"""

    registry, sends, _ = _wired()

    fired = _feed([False] * 5, _notifier(registry))

    assert fired == [True, False, False, False, False], fired
    assert len(sends) == 1, f"同一段掉线里又发了：{sends}"


# --- ③ 重连后再断 → **再发一封** ----------------------------------------------


def test_reconnect_and_a_second_outage_send_again() -> None:
    """`断 → 连上 → 再断` = 两段 → 两封；**恢复本身不发**（用户："不用额外通知"）。"""

    registry, sends, _ = _wired()
    notifier = _notifier(registry)

    fired = _feed([False, False, True, True, False, False], notifier)

    assert fired == [True, False, False, False, True, False], fired
    assert len(sends) == 2, f"第二段掉线该再发一封：{sends}"
    assert sends[0] == sends[1], "两封信只有时间可能不同，模板是同一份"


def test_recovery_alone_never_sends() -> None:
    """一直连着 → **一封都不发**。"""

    registry, sends, _ = _wired()

    fired = _feed([True, True, True], _notifier(registry))

    assert fired == [False, False, False]
    assert sends == []


# --- ④ mail 插件不在 → 本插件不装配，核心照跑 ---------------------------------


def test_without_the_mail_plugin_this_one_is_not_assembled() -> None:
    """`REQUIRES = ("mail",)` 生效：mail 装不上 → 本插件**不登记任何接收者**。

    手法：本插件的插件模块**真的在磁盘上**，只让前置 `mail` 的 import 失败
    （`discover()` 里那条 `ModuleNotFoundError` → `failed` 分支），
    再看 `outage_notice` 有没有被装上。**只发现 `outage_notice` 这一个名字**——
    于是"本插件被列进 `only` 都没装上"这件事只剩一个原因：它的前置装不上。
    """

    registry = plugin_registry()
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name == "qq_roleplay_bot.plugins.mail.plugin":
            raise ModuleNotFoundError("No module named 'qq_roleplay_bot.plugins.mail'",
                                      name="qq_roleplay_bot.plugins.mail")
        return real_import(name, package)

    with mock.patch.object(plugins_module.importlib, "import_module", fake_import):
        with _discovery_state():
            loaded = discover(registry, only=("outage_notice",))

    assert "outage_notice" not in loaded, loaded
    assert "mail" not in loaded, loaded
    assert registry.disconnect_receivers() == (), "没装上的插件不该留下接收者"
    assert registry.mail.knows() is False


def test_the_requirement_points_at_mail_and_mail_is_installed_first() -> None:
    """判据本身：`REQUIRES` 里是 `mail`，且真装配里 **mail 先装、本插件后装**。

    方向反了（写进 mail、或让 mail 依赖本插件）这条会红：
    它同时钉住"本插件只声明需要 mail"与"装配顺序按依赖走"。
    """

    assert tuple(outage_plugin.REQUIRES) == ("mail",), outage_plugin.REQUIRES

    registered: list[str] = []
    real_register = outage_plugin.register

    def spy(registry):
        registered.append("outage_notice")
        return real_register(registry)

    with mock.patch.object(outage_plugin, "register", spy):
        with _discovery_state():
            loaded = discover(PluginRegistry())

    assert "mail" in loaded and "outage_notice" in loaded, loaded
    assert loaded.index("mail") < loaded.index("outage_notice"), loaded
    assert registered == ["outage_notice"], "本插件的 register 该**正好**被调一次"


def test_a_deployment_without_the_mail_plugin_still_links_nothing_and_core_runs() -> None:
    """**把 `mail` 的插件模块藏起来**：`outage_notice` 不装，核心照跑。

    这条与 `check_module_removal.py`（在**全新解释器**里拦掉整个包）是同一个判据的
    两个尺度：那边验"没有本插件时核心起得起"，这边验"没有它的前置时它安静地不装"，
    而且**不抛异常**（`discover()` 把它记成 `failed` 就完事）。
    **只发现 `outage_notice`**：这样"mail 装不上"是这条路上唯一的变数，
    不会顺带把别的插件的成败算进来。
    """

    registry = plugin_registry()
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.startswith("qq_roleplay_bot.plugins.mail"):
            raise ModuleNotFoundError("No module named 'qq_roleplay_bot.plugins.mail'",
                                      name="qq_roleplay_bot.plugins.mail")
        return real_import(name, package)

    with mock.patch.object(plugins_module.importlib, "import_module", fake_import):
        with _discovery_state():
            loaded = discover(registry, only=("outage_notice",))

    for name in ("outage_notice", "mail"):
        assert name not in loaded, loaded
    # 核心那边（不带插件的注册表）照样是最小可用状态
    assert registry.disconnect_receivers() == ()
    assert registry.mail.knows() is False


# --- ⑤ 发信抛异常 → 不崩、只记一笔 --------------------------------------------


def test_a_failing_send_is_logged_and_never_propagates() -> None:
    """mail 的发信抛异常 → 本插件的接收者**不把异常放出去**，只记一行。

    为什么必须是这样：那行日志的调用方是核心的广播侧，它兜住异常但**区分不了**
    "发信失败"与"这个插件有 bug"。就地记下具体是哪件事失败，排障时才有用；
    而"不崩"这条判据**不依赖调用方**——本函数自己就不抛。
    """

    async def angry_sender(subject: str, body: str) -> object:
        raise RuntimeError("发不出去（合成故障：凭据过期）")

    registry, _, _ = _wired(sender=angry_sender)
    notifier = _notifier(registry)

    with _logs("qq_roleplay_bot.plugins.outage_notice.outage_notice") as records:
        fired = _feed([False, False], notifier)

    assert fired == [True, False], "发信失败不许改变核心的状态机（这一段仍然只播一次）"
    assert any("outage_notice_send_failed" in line for line in records), records
    # 核心那一侧也不该出现"接收者炸了"——异常在插件里就被接住了
    with _logs("qq_roleplay_bot.runtime") as core_records:
        _feed([False, False], _notifier(registry))
    assert not any("disconnect_receiver_failed" in line for line in core_records), core_records


def test_the_plugin_survives_a_failing_send_and_still_notices_the_next_outage() -> None:
    """一封信没发出去**不影响下一段掉线**：仍然是每段一封（不是"失败一次就再也不发"）。

    ## 喂的那串状态为什么是这两个（第一版写错过，记下来）

    喂进去的是**段与段之间的迁移**，不是随便几个布尔值。注意
    `在线 → 掉线 → 在线 → 掉线` 里有**两段**掉线，而第一版我写成
    `[False, False, True, False]` —— 那个尾巴上的 `False` 已经开了**第二段**，
    于是"两段"变成三段、断言数错。现在第一段是 `[False, False]`（一段，第一枪发信），
    第二段是 `[True, True, False]`（先重连 = 重新武装，再断 = 新的一段）。
    """

    attempts = {"count": 0, "fail": True}
    sends: list[tuple[str, str]] = []

    async def flaky_sender(subject: str, body: str) -> object:
        attempts["count"] += 1
        if attempts["fail"]:
            raise RuntimeError("这一封发不出去")
        sends.append((subject, body))
        return {}

    registry, _, _ = _wired(sender=flaky_sender)
    notifier = _notifier(registry)

    with _logs("qq_roleplay_bot.plugins.outage_notice.outage_notice"):
        fired1 = _feed([False, False], notifier)     # 第一段掉线：这一封发失败
    attempts["fail"] = False
    fired2 = _feed([True, True, False], notifier)    # 重连后又断：第二段，这封发得出去

    assert fired1 == [True, False], fired1
    assert fired2 == [False, False, True], fired2
    assert attempts["count"] == 2, f"两段掉线该各试一次（实际 {attempts['count']} 次）"
    assert len(sends) == 1, sends


# --- ⑥ 删掉 `plugins/outage_notice/` → 核心与 mail 都照跑（判据）--------------


def test_removing_the_whole_plugin_folder_keeps_core_and_mail_running() -> None:
    """**判据**：把这个插件文件夹整个拿掉，核心起得来，mail 的装配一点没变。

    用**插件的名字**（`outage_notice`）拦掉它的模块——等价于那个文件夹不在
    （`discover()` 的 `ModuleNotFoundError` 分支，与真删掉走的是同一条路）。
    同时钉住另一件容易搞反的事：**mail 不依赖本插件**（它得照常装上、照常提供能力）。
    """

    registry = plugin_registry()
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.startswith("qq_roleplay_bot.plugins.outage_notice"):
            raise ModuleNotFoundError(
                "No module named 'qq_roleplay_bot.plugins.outage_notice'",
                name="qq_roleplay_bot.plugins.outage_notice")
        return real_import(name, package)

    with mock.patch.object(plugins_module.importlib, "import_module", fake_import):
        with _discovery_state():
            loaded = discover(registry)

    assert "outage_notice" not in loaded, loaded
    assert "mail" in loaded, f"mail 不该依赖本插件：{loaded}"
    assert registry.mail.knows() is True, "拿掉本插件之后 mail 的能力照旧提供"
    assert registry.disconnect_receivers() == (), "没有它就没有接收者，核心照跑"


# --- ⑦ 邮件正文里不许出现机制词（AGENTS §2.2 那张表）--------------------------


def test_the_mail_body_carries_no_persona_mechanism_words() -> None:
    """正文**一个机制词都没有**——词表就是 `prompt_guard.PERSONA_MECHANISM_WORDS`。

    两种尺度都扫：**模板本身**（有人改了措辞立刻红）与**真发出去的那一封**。
    """

    subject, body = outage_notice.build_notice(1_800_000_000.0)
    assert scan_persona_text(body) == [], scan_persona_text(body)
    assert scan_persona_text(subject) == []

    _registry, sends, _ = _wired()
    _feed([False], _notifier(_registry))
    assert len(sends) == 1
    sent_subject, sent_body = sends[0]
    assert scan_persona_text(sent_body) == [], scan_persona_text(sent_body)
    assert scan_persona_text(sent_subject) == [], scan_persona_text(sent_subject)


def test_the_mail_does_not_speak_in_her_voice() -> None:
    """**不扮演**：正文不提她的人设、不用第一人称讲自己的状况。

    用户把措辞交给我们时说的是"写清楚、别扮演"：这封信的收件人是操作者，
    它该像一封运维告警（谁、什么时候、做什么），不像"云茹写给主人的信"。
    """

    _subject, body = outage_notice.build_notice(1_800_000_000.0)
    for awkward in ("我觉得", "呜", "人家", "云茹我", "主人"):
        assert awkward not in body, f"这封告警不该有 {awkward!r}：{body}"
    assert "我" not in body, f"正文不该用她的口吻讲自己的状况：{body}"
    assert "自动发的告警" in body, "要说明这是自动告警（免得被当成她本人写的）"


# --- 装配的其余分支：开关与"能力没提供" ---------------------------------------


def test_the_switch_off_never_registers_a_receiver() -> None:
    """`QQBOT_MAIL_OUTAGE_NOTICE=0` → 这个插件**不接那条事件**（核心照跑）。"""

    registry = plugin_registry()
    registry.mail.provide(lambda subject, body: None)
    with _flag(dev_config, "MAIL_OUTAGE_NOTICE_ENABLED", False):
        outage_plugin.register(registry)

    assert registry.disconnect_receivers() == ()
    # 总闸（核心那侧）关掉时也一个接收者都不叫——两层开关各管各的
    with _flag(runtime_module.dev_config, "DISCONNECT_NOTICE_ENABLED", False):
        assert _feed([False], _notifier(registry)) == [False]


def test_a_mail_plugin_without_a_recipient_registers_nothing_and_says_so() -> None:
    """mail 在，但**没配收件人**（能力是空的）→ 本插件明确不接，并记一行。

    这一条防的是"接了但永远失败"：那种形状每次掉线都往日志里灌一行错，
    而且操作者永远收不到信却以为功能开着。**明确地不接**比那样诚实。
    """

    registry = plugin_registry()          # `registry.mail` 没被人 `provide`

    with _logs("qq_roleplay_bot.plugins.outage_notice.plugin") as records:
        outage_plugin.register(registry)

    assert registry.disconnect_receivers() == ()
    assert any("掉线通知这次没接上" in line for line in records), records


def test_the_config_flag_defaults_to_on() -> None:
    """**默认开**（用户要的是"断了通知我"，不是"要先配置才通知"）。

    读的是 `dev_config` 的**实际值**：只有这台机器显式写了
    `QQBOT_MAIL_OUTAGE_NOTICE=0` 才会红——那正是"默认"被改掉的信号。
    """

    assert dev_config.MAIL_OUTAGE_NOTICE_ENABLED is True


# --- 真实装配：mail 插件那侧真的把能力提供出来了 ------------------------------


class _FakeMailClient:
    """替身 `MailClient`：只记下 `send(...)` 的关键字，不起子进程、不连网。"""

    instances: list["_FakeMailClient"] = []

    def __init__(self, executable: str, *, workdir: object, **kwargs) -> None:
        self.executable = executable
        self.workdir = workdir
        self.sends: list[dict] = []
        _FakeMailClient.instances.append(self)

    async def send(self, *, to: str, subject: str, body: str, **kwargs) -> dict:
        self.sends.append({"to": to, "subject": subject, "body": body})
        return {"message_id": "fake"}


@contextmanager
def _real_mail_plugin():
    """走**真实**的 mail 插件装配，只把 `MailClient` 换成假对象。

    为什么要这条：上面那些用例是"我在注册表上放了个假发信函数"——它们证明不了
    **mail 插件真的会把它提供出来**。这一条把那一半也钉住：
    `plugins/mail/plugin.py::register()` → `wire.build_operator_mail_sender` →
    `registry.mail`，收件人来自 mail 自己的配置（**没有第二份配置**）。
    """

    _FakeMailClient.instances = []
    with mock.patch("qq_roleplay_bot.plugins.mail.mail_client.MailClient",
                    _FakeMailClient):
        yield _FakeMailClient.instances


def test_the_mail_plugin_provides_the_sender_and_the_outage_uses_it_end_to_end() -> None:
    """端到端：装上**真实 mail 插件** → 发现本插件 → 喂一次掉线 → 假 `MailClient` 收到**一封**。

    这条是"假对象"那些用例缺的另一半：它们自己在注册表上放了个假发信函数，
    证明不了 **mail 插件真的会把能力提供出来**。这里走
    `plugins/mail/plugin.py::register()` → `wire.build_operator_mail_sender` →
    `registry.mail` → 本插件的接收者，一条真链路。
    收件人也一并钉住：它就是 mail 配置里那一个（`MAIL_REPORT_TO`），**没有新写一份**。
    """

    with _real_mail_plugin() as clients:
        registry = PluginRegistry()
        mail_plugin.register(registry)
        assert registry.mail.knows() is True, "mail 插件该把发信能力提供出来"
        assert clients, "mail 插件该真的造了一个 MailClient"

        with _discovery_state():
            loaded = discover(registry, only=("outage_notice",))
        assert "outage_notice" in loaded, loaded
        assert registry.disconnect_receivers() != (), "本插件该登记了接收者"

        _feed([False], _notifier(registry))

    sends = [item for client in clients for item in client.sends]
    assert len(sends) == 1, f"该正好发一封，实际 {len(sends)}：{sends}"
    assert sends[0]["to"] == dev_config.MAIL_REPORT_TO, sends[0]
    assert sends[0]["subject"] == "云茹断线了"
    assert "【症状】" in sends[0]["body"]


# --- 这个文件自己的守卫 -------------------------------------------------------


def test_the_plugin_module_is_where_the_discovery_mechanism_looks_for_it() -> None:
    """`plugins/<名字>/plugin.py` 就在约定位置（发现机制只认这个形状）。"""

    path = pathlib.Path(outage_plugin.__file__)
    assert path.name == "plugin.py"
    assert path.parent.name == "outage_notice"
    assert callable(outage_plugin.register)


def test_the_outage_notice_module_does_not_know_the_mail_modules() -> None:
    """**依赖方向**的机械检查：这个模块不 import mail 的任何东西。

    它只认 `plugins.MailSeams` 上那个函数。有人把
    `from ..mail.mail_client import MailClient` 写进来，这条立刻红——
    那正是用户点名"不要搞错"的那个方向。
    """

    source = pathlib.Path(outage_notice.__file__).read_text(encoding="utf-8")
    assert "mail_client" not in source, "不许 import mail 的实现模块"
    assert "MailClient" not in source, "不许 import mail 的类"
    # 模块命名空间里也不许出现 mail 的东西（同一条判据的运行时尺度）
    namespace = vars(outage_notice)
    assert "MailClient" not in namespace
    assert not any(isinstance(value, types.ModuleType) and "mail" in name.lower()
                   for name, value in namespace.items())


# --- 突变验证（这一节是给"改弱了会不会红"用的，不是常规用例）------------------


def _call_the_receiver_twice(registry) -> None:
    """**突变手法**：把同一个接收者直接叫两次（绕过核心的一次性）。

    为什么手法是这样：核心的 `_DisconnectNotifier` 只在一段掉线的**第一枪**才叫接收者，
    所以"每来一次事件就发一封"这个弱化在**正常喂事件**时根本看不出来
    （喂 5 次 `False` 也只叫一次）。能把它变红的驱动方式有两种：
    直接叫两次接收者（本函数），或者翻插件里那个突变开关（见下面那条用例）。
    """

    receivers = registry.disconnect_receivers()
    assert len(receivers) == 1, receivers
    for receiver in receivers:
        # **同一个接收者叫两次**（不是"把名单走一遍"——名单只有一个元素，
        # 走一遍就是一次。这里要的是两次）。
        asyncio.run(receiver())
        asyncio.run(receiver())


def test_the_receiver_sends_one_mail_per_call_and_keeps_no_state() -> None:
    """接收者被叫两次 = 两封（它**不**记"这一段发过了"——那是核心的活）。

    这条钉住插件这一侧的契约：它只做"被叫一次发一封"，**不在自己这里再记一份状态**
    （两处各记一份正是用户最强调的那条会分叉的地方）。
    """

    registry, sends, _ = _wired()
    _call_the_receiver_twice(registry)

    assert len(sends) == 2, sends


def test_the_mutation_hook_defaults_to_off_and_making_it_send_twice_turns_the_count_red() -> None:
    """**突变验证**：把"断一次只发一封"改弱成"被叫一次发两封"，钉住它的用例立刻红。

    改弱点就是 `plugin.MUTATION_SEND_EVERY_EVENT`（生产路径永远是 `False`，
    没有任何生产代码会打开它）。这条同时说清三件事：

    1. 开关缺省是关的（生产语义不受那个开关影响）；
    2. 打开它之后**同一段掉线会发出两封**——正是用户不许的形状；
    3. 文件上半部分那几条（`test_one_outage_sends_exactly_one_mail_with_the_fixed_template`、
       `test_more_events_within_the_same_outage_send_nothing_more`、
       `test_the_mail_plugin_provides_the_sender_and_the_outage_uses_it_end_to_end`）
       断的正是"信的数量"，它们在那种改弱下必然红。
       下面这个 `assert len(sends) == 2` 与那几条里的 `len(sends) == 1`
       是**同一件事的两种取值**——所以"会红"不必靠人相信。
    """

    assert outage_plugin.MUTATION_SEND_EVERY_EVENT is False, "生产路径不该默认打开突变"

    registry, sends, wired = _wired()
    assert wired is not None and hasattr(wired, "MUTATION_SEND_EVERY_EVENT"), wired

    wired.MUTATION_SEND_EVERY_EVENT = True
    try:
        _feed([False, False], _notifier(registry))
    finally:
        wired.MUTATION_SEND_EVERY_EVENT = False

    assert len(sends) == 2, f"改弱之后该变成两封，实际 {len(sends)}：{sends}"
    assert sends[0] == sends[1]
    assert outage_plugin.MUTATION_SEND_EVERY_EVENT is False, "跑完必须还原"
