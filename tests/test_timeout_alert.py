"""**超时通知插件**（用户 2026-10-12：*"还有就是短时间内频繁超时则发送邮件通知我"*）。

这句话里有三个必须落成断言的东西：

1. **"频繁"**——窗口内够几次才写（缺省 5 分钟内 5 次）。这里量的是"什么时候会响、
   什么时候不会响"，包括两种最容易搞错的情形：**窗口滑走之后旧账不算**
   与**机器睡一觉起来不许把几小时前的超时算进"最近五分钟"**。
2. **"超时"**——只数超时这一种失败（`model_errors` 里键名带 `timeout` 的那些）。
   401 / 余额不足 / 格式错都不是超时，混进来这封信说的就不是真事。
3. **"发邮件通知我"**——真装配下走的是 `registry.mail` 那个发信口，
   断言的是**信真的被发了**，不是"我认为它会发"。

## 哪些不是这里的

| 断言 | 在哪 |
| --- | --- |
| "超时次数"从哪来（`metrics.record_model_error` / `EngineSnapshot.metrics`） | 核心（`metrics.py` / `snapshots.py`），本文件用**真的** `RuntimeMetrics` 喂它 |
| 发信本身（收件人、CLI、凭据） | `plugins/mail/`（`tests/test_mail_channel.py` 等） |
| 后台节拍（多久跑一轮、异常怎么办） | `background_plugins.py`（`tests/test_background_plugins.py`） |

## 全是离线、绝不真发信

`_wired()` 里的发信口一律是**假对象**（只管记下 `(主题, 正文)`）；真装配那条
（`test_the_real_assembly_hands_over_the_mails_own_sender`）**只看接上了哪个口、不发信**。
本文件没有任何网络调用与子进程。

## 突变验证

文件末尾 `_mutation_notes` 记着交付时真跑过的两处改动与各自变红的用例——
"这几条断言守的就是用户那句话"，不是"我认为它会红"。
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib
import logging
import os
import re
import types
from contextlib import contextmanager
from unittest import mock

from plugin_support import plugin_registry
from qq_roleplay_bot import plugins as plugins_module
from qq_roleplay_bot.metrics import RuntimeMetrics
from qq_roleplay_bot.plugins import PluginRegistry, ReportSeams, discover
from qq_roleplay_bot.plugins.outage_notice import outage_notice
from qq_roleplay_bot.plugins.timeout_alert import alert as alert_module
from qq_roleplay_bot.plugins.timeout_alert import plugin as timeout_plugin
from qq_roleplay_bot.plugins.timeout_alert.background import TimeoutAlertPlugin
from qq_roleplay_bot.prompt_guard import scan_persona_text
from qq_roleplay_bot.snapshots import EngineSnapshot

#: 缺省口径（`alert.py` 顶部那张表）：窗口 5 分钟、够 5 次、降温 30 分钟、每 60 秒看一次。
WINDOW = 300.0
THRESHOLD = 5
COOLDOWN = 1800.0
POLL = 60.0

#: 采样间隔（用例里"往前走一步"就是这么多秒）。
STEP = 60.0

ALERT_LOGGER = "qq_roleplay_bot.plugins.timeout_alert.alert"
PLUGIN_LOGGER = "qq_roleplay_bot.plugins.timeout_alert.plugin"
BACKGROUND_LOGGER = "qq_roleplay_bot.plugins.timeout_alert.background"

PACKAGE_FILES = ("plugin.py", "alert.py", "background.py", "__init__.py")


def _snapshot_of(counts: dict[str, int]) -> object:
    """造一份只读快照，形状与 `EngineSnapshot` 一致：`.metrics["model_errors"]`。"""

    return types.SimpleNamespace(metrics={"model_errors": dict(counts)})


class _Snapshots:
    """可以换内容的只读快照口（`plugin.py` 要的那个 `snap` 就是这个形状）。

    `boom` 用来验"快照读不出来时这一轮安静地跳过"，`calls` 用来看它被读了几次。
    """

    def __init__(self, total: int = 0) -> None:
        self.total = total
        self.extra: dict[str, int] = {}
        self.calls = 0
        self.boom: Exception | None = None

    def __call__(self) -> object:
        self.calls += 1
        if self.boom is not None:
            raise self.boom
        counts = {"transport_error: TimeoutError": int(self.total)}
        counts.update(self.extra)
        return _snapshot_of(counts)


class _Clock:
    """可以拨的钟：`poll_once()` 每次读它，`_feed` 每轮往前拨一步。"""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _fake_sender():
    """假发信函数：记下每次 `(主题, 正文)`，别的什么都不做（`MailSeams` 上的形状）。"""

    sends: list[tuple[str, str]] = []

    async def sender(subject: str, body: str) -> object:
        sends.append((subject, body))
        return {"message_id": "fake"}

    return sender, sends


def _failing_sender(attempts: list[tuple[str, str]]):
    """发不出去的发送口：**记下尝试**再抛（用来验"失败只记一行、不崩"）。"""

    async def sender(subject: str, body: str) -> object:
        attempts.append((subject, body))
        raise RuntimeError("smtp 挂了")

    return sender


@contextmanager
def _env(name: str, value: str):
    """临时改一个环境变量（`dev_config.get` 读的就是 `os.environ` + 已注入的 `.env`）。"""

    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


#: 收件人那个键名 / 假地址。`.invalid` 是 RFC 2606 留的"永远不存在"的顶级域。
RECIPIENT_ENV = "QQBOT_MAIL_REPORT_TO"
FAKE_RECIPIENT = "someone@example.invalid"


@contextmanager
def _recipient_configured():
    """把"这个部署配了收件人"这个前提显式摆好，跑完原样（含环境变量）还回去。

    与 `tests/test_outage_notice.py::_recipient_configured` 同法——为什么前提必须由
    测试自己配、为什么**只设环境变量**（`build_operator_mail_sender` 从 2026-10-16 起
    改成现读 `dev_config.mail_report_to()`，`MAIL_REPORT_TO` 常量不再参与）、为什么不
    改回源码默认值也不 reload，那份 docstring 写了完整理由（用户原话："改成必须由
    `.env` 给"，于是源码里没有默认收件人了；本机 `.env` 也没填）。
    """

    saved_env = os.environ.get(RECIPIENT_ENV)
    os.environ[RECIPIENT_ENV] = FAKE_RECIPIENT
    try:
        yield FAKE_RECIPIENT
    finally:
        os.environ.pop(RECIPIENT_ENV, None)
        if saved_env is not None:
            os.environ[RECIPIENT_ENV] = saved_env


@contextmanager
def _discovery_state():
    """发现机制的进程级记录：进这块就**原样还回去**（别的测试文件也读它们）。"""

    previous = (plugins_module._REGISTRY, plugins_module._LAST_LOADED)
    try:
        yield
    finally:
        plugins_module._REGISTRY, plugins_module._LAST_LOADED = previous


@contextmanager
def _logs(name: str):
    """抓某个 logger 的日志行（与 `test_outage_notice._logs` 同法）。"""

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


def _wired(*, sender=None, snapshots=None, only=("timeout_alert",)):
    """把插件装到一个注册表上（走真实的 `discover()`），回 `(registry, sends, snapshots)`。

    `discover()` 会先把 `REQUIRES` 里的 `mail` 装一遍（`_load` 的规矩，实测
    `loaded == ("mail", "timeout_alert")`），而**这里塞的假发信口不会被它盖掉**：
    `mail/wire.py:122-143` 只在"还没有发信函数"时才 `provide`——先到者胜，
    与 `PluginRegistry.provide_roles` 的"谁放的就用谁的"是同一条道理。
    所以"用假对象数发了什么"这条路是通的，也不会去起真的邮件 CLI。

    **真装配那一路**（真 mail 插件放上来的那个口）由
    `test_the_real_assembly_hands_over_the_mails_own_sender` 覆盖；
    `REQUIRES` 的先后顺序由
    `test_the_requirement_points_at_mail_and_register_is_called_once` 覆盖。
    """

    sender, sends = _fake_sender() if sender is None else (sender, [])
    snapshots = _Snapshots() if snapshots is None else snapshots
    registry = plugin_registry()
    registry.mail.provide(sender)
    # `ReportSeams` 是 frozen 的（字段改不了，只能换一份）——这里换的正是插件要看的那一项。
    registry.report = ReportSeams(snapshot=snapshots)
    with _discovery_state():
        loaded = discover(registry, only=only)
    assert "timeout_alert" in loaded, loaded
    return registry, sends, snapshots


def _background(registry) -> TimeoutAlertPlugin:
    """取本插件登记上去的那个后台节拍对象。"""

    found = [item for item in registry.backgrounds
             if getattr(item, "name", "") == "timeout-alert"]
    assert len(found) == 1, f"该正好登记一个，实际 {registry.backgrounds}"
    return found[0]


def _armed(registry, snapshots) -> TimeoutAlertPlugin:
    """取到插件，并把**测试的**钟与快照口装上去。

    装配时 `plugin.py` 拿走的是 `ReportSeams.snap` 那个绑定方法（它每次才去问核心），
    这里直接换成测试自己的可调用对象：`_feed` 就能一轮一轮地摆布
    "累计超时多少"与"现在几点"，而不用真等 5 分钟。
    """

    plugin = _background(registry)
    plugin.clock = _Clock()
    plugin.snap = snapshots
    return plugin


def _every(totals, *, step: float = STEP) -> list[tuple[float, int]]:
    """把一串"累计超时数"排成每 `step` 秒一轮。"""

    return [(step, total) for total in totals]


def _feed(plugin, rounds) -> list[object]:
    """按 `(往前走多少秒, 这时的累计超时数)` 逐轮问 `poll_once()`。

    `plugin.clock` / `plugin.snap` 都是本文件建的对象，所以"什么时候会响"是确定的：
    不睡觉、不真等，一轮就是一步。回的是每一轮的返回（发出去的那封或 `None`）。
    """

    async def run() -> list[object]:
        results: list[object] = []
        for advance, total in rounds:
            plugin.clock.now += advance
            plugin.snap.total = total
            results.append(await plugin.poll_once())
        return results

    return asyncio.run(run())


def _alerts(results: list[object]) -> list[object]:
    return [item for item in results if item is not None]


# --- ① "频繁"落成的数：什么时候会响 -------------------------------------------


def test_four_timeouts_in_the_window_stay_quiet() -> None:
    """5 分钟里 4 次 → **不写**（"频繁"是有门槛的，抖一下不算）。"""

    registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)

    results = _feed(plugin, _every([0, 1, 2, 3, 4]))

    assert results == [None] * 5, results
    assert sends == [], sends


def test_five_timeouts_in_the_window_write_exactly_one_mail() -> None:
    """5 分钟里 5 次 → 正好一封，而且是**那一封**：主题固定、正文是她的口气。"""

    registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)

    results = _feed(plugin, _every([0, 1, 2, 3, 4, 5]))

    assert results[:5] == [None] * 5, results
    assert results[5] is not None, results
    assert results[5].count == 5, results[5]
    assert len(sends) == 1, f"该只写一封，实际 {len(sends)} 封：{sends}"
    subject, body = sends[0]
    assert subject == alert_module.SUBJECT, subject
    assert body == results[5].body, body


def test_timeouts_that_fell_out_of_the_window_are_not_counted() -> None:
    """"窗口滑走就不算"：先 3 次，隔一段之后再 5 次 → 信里说 5 次，不是 8 次。

    这是"短时间"三个字的落点：一个上午零零散散超时 8 次的人，不该收到这封信。
    """

    registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)

    # 前 3 次在三分钟里；然后停 400 秒（超过 5 分钟窗口），再来 5 次。
    rounds = _every([1, 2, 3]) + [(400.0, 3)] + _every([4, 5, 6, 7, 8])
    results = _feed(plugin, rounds)

    alerts = _alerts(results)
    assert len(alerts) == 1, results
    assert alerts[0].count == 5, f"旧账被算进来了：{alerts[0].count}（该是 5）"
    assert len(sends) == 1, sends


def test_a_sleep_does_not_turn_old_timeouts_into_a_false_alarm() -> None:
    """机器睡了一觉起来（采样断档 3 小时）→ **不许响**。

    这是这个判定最容易出的错：累计值是单调增的，断档期间涨的那些**不属于**最近五分钟。
    做法是"窗口外的采样点一律丢掉"，代价是这一轮判不出来（`None`），
    而不是拿三小时前的底去减——那会变成一封假警报。
    """

    registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)

    # 一轮正常采样，然后断档 3 小时（累计从 0 跳到 40，都是断档里的）。
    results = _feed(plugin, [(STEP, 0), (3 * 3600.0, 40), (STEP, 40)])

    assert results == [None, None, None], results
    assert sends == [], sends


def test_a_single_sample_is_never_decided_on() -> None:
    """窗口里只有一个采样点 → 这一轮不判（"这一段的底"还不知道）。"""

    registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)

    results = _feed(plugin, [(STEP, 99)])

    assert results == [None], results
    assert sends == [], sends


def test_the_cooldown_keeps_the_second_mail_quiet_until_it_expires() -> None:
    """写过一封之后 30 分钟内不再写；过了降温期还能再写（窗口重新计数）。

    门槛在这里调成 2（`QQBOT_MAIL_TIMEOUT_ALERT_TIMES=2`），好让这条用例短——
    顺带钉住"门槛是从配置读的"。
    """

    with _env(alert_module.ENV_THRESHOLD, "2"):
        registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)
    assert plugin.watch.threshold == 2, plugin.watch.threshold

    results = _feed(plugin, _every([0, 1, 2]))
    assert results[2] is not None and results[2].count == 2, results

    # 还在降温期：又超了（窗口内累计 3 次），但**不再写**。
    quiet = _feed(plugin, _every([3]))
    assert quiet == [None], quiet
    assert len(sends) == 1, sends

    # 断档一整段（窗口清空）之后重新计数：窗口里又够 2 次 → 再写一封。
    after = _feed(plugin, [(COOLDOWN, 3)] + _every([4, 5]))
    assert after[-1] is not None and after[-1].count == 2, after
    assert len(sends) == 2, f"降温期过了该能再写：{sends}"


def test_a_counter_restart_re_baselines_instead_of_firing() -> None:
    """累计值回退（换过指标对象 / 进程重启）→ 重新起算，**不许拿两份计数器相减**。

    这里的形状：先看到累计 100，然后掉到 2——那一瞬间不该响；随后在新计数器的
    时间线上够 5 次才响，而且信里说的次数是**新线上的**（6 次，不是 100 与 8 之差）。
    """

    registry, sends, snapshots = _wired()
    plugin = _armed(registry, snapshots)

    results = _feed(plugin, _every([100]) + _every([2, 4, 6, 8, 10]))

    assert results[1] is None, f"计数器回退的那一轮不该响：{results}"
    alerts = _alerts(results)
    assert len(alerts) == 1, results
    assert alerts[0].count == 6, f"该只算新线上的：{alerts[0].count}（该是 6）"
    assert len(sends) == 1, sends


def test_the_configured_window_and_cooldown_reach_the_watch() -> None:
    """窗口 / 降温也从配置读（用户要改口径时只动 `.env`，不改代码）。"""

    with _env(alert_module.ENV_WINDOW_SECONDS, "120"), \
            _env(alert_module.ENV_COOLDOWN_SECONDS, "90"):
        registry, _sends, _ = _wired()

    watch = _background(registry).watch
    assert watch.window_seconds == 120.0, watch.window_seconds
    assert watch.cooldown_seconds == 90.0, watch.cooldown_seconds


def test_the_default_sampling_interval_is_far_inside_the_window() -> None:
    """缺省采样间隔要明显小于窗口，否则窗口里凑不到采样点、什么都判不出来。"""

    assert alert_module.poll_seconds() * 2 <= alert_module.window_seconds(), (
        alert_module.poll_seconds(), alert_module.window_seconds())


# --- ② "超时"落成的数：数的是不是只有超时 -------------------------------------


def test_only_timeout_kinds_are_counted() -> None:
    """只数键名带 `timeout` 的：别的失败（401、余额不足、别的传输错）不算超时。

    那晚的现场值得记住：`transport_error: TimeoutError` 是判定那条路被闸掐掉的形状，
    而同一天还有 210 条**余额不足**——它们该走各自的信号，不该变成"超时通知"。
    """

    counts = {
        "transport_error: TimeoutError": 3,
        "transport_error: ReadTimeout": 2,
        "timeout: timed out": 1,
        "http_error: status=401": 50,
        "transport_error: ConnectionResetError": 7,
    }

    assert alert_module.timeout_count(_snapshot_of(counts)) == 6


def test_a_snapshot_without_timeouts_reads_as_zero_not_as_unknown() -> None:
    """看见了、没有超时 = `0`（与"看不见"的 `None` 是两件事）。"""

    assert alert_module.timeout_count(_snapshot_of({"http_error: status=500": 4})) == 0


def test_an_unreadable_snapshot_counts_nothing() -> None:
    """快照形状不对 → `None`（"我看不见"），调用方据此安静跳过，绝不当成 0。"""

    assert alert_module.timeout_count(None) is None
    assert alert_module.timeout_count(types.SimpleNamespace()) is None
    assert alert_module.timeout_count(types.SimpleNamespace(metrics=None)) is None
    assert alert_module.timeout_count(
        types.SimpleNamespace(metrics={"model_errors": "nope"})) is None
    assert alert_module.timeout_count(
        types.SimpleNamespace(metrics={})) is None


def test_a_real_metrics_snapshot_is_read_the_same_way() -> None:
    """数的是**真的** `RuntimeMetrics.snapshot()`——键名不是这里猜的，是核心记的。

    两条钉子：①核心自己 `record_model_error()` 出来的字典能被正确数出来；
    ②那份字典在真快照里真的有位置（`EngineSnapshot` 带 `metrics` 字段）。
    """

    metrics = RuntimeMetrics()
    for key in ("transport_error: TimeoutError", "ReadTimeout: timed out",
                "http_error: status=401"):
        metrics.record_model_error(key)

    assert alert_module.timeout_count(
        types.SimpleNamespace(metrics=metrics.snapshot())) == 2
    assert "metrics" in {field.name for field in dataclasses.fields(EngineSnapshot)}


# --- ③ 措辞：她那口气、固定模板、不调模型 -------------------------------------


def test_the_mail_is_built_without_any_model_call() -> None:
    """同一输入两次构造逐字相同，且不需要任何 client / 网络（要真调模型这里会红）。"""

    when = 1_800_000_000.0
    first = alert_module.build_alert(count=5, window_seconds=WINDOW, at=when)
    second = alert_module.build_alert(count=5, window_seconds=WINDOW, at=when)

    assert first == second
    assert first.subject == alert_module.SUBJECT
    assert first.body == alert_module.BODY_TEMPLATE.format(
        moment=alert_module.format_moment(when), window="5 分钟", count=5)


def test_the_mail_says_how_often_and_when() -> None:
    """两个锚：**几次**（用户要的是"频繁"这件事被说出来）+ **原样的时刻**。"""

    alert = alert_module.build_alert(count=7, window_seconds=WINDOW, at=1_800_000_000.0)

    assert alert.body.startswith("我最近老是卡住。"), alert.body
    assert "7 次" in alert.body, alert.body
    assert "5 分钟" in alert.body, alert.body
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}（[+-]\d{4}）", alert.body), alert.body


def test_the_mail_carries_no_persona_mechanism_words() -> None:
    """`AGENTS.md` §2.2 那张表（检查 / 触发 / 调用 / …）**一个都不许出现**。

    这封信不进她的 prompt，但同一条道理在这里更硬：收件人读了要马上知道做什么，
    用她的口吻讲机制词只会让告警看不懂。
    """

    alert = alert_module.build_alert(count=5, window_seconds=WINDOW, at=1_800_000_000.0)

    assert not scan_persona_text(alert.subject + "\n" + alert.body), (
        alert.subject, alert.body)


def test_the_mail_keeps_her_voice_and_no_notification_frame() -> None:
    """措辞的三条口径（用户 2026-10-11 对着那封真信改过三次）：

    ① 第一人称——是她**自己在求人帮忙**，不是运维通知；
    ② 没有 `【】` 那套小标题（通知腔才需要）；
    ③ 不交代机制（"这封是自动发的"那种话不留）。
    """

    alert = alert_module.build_alert(count=5, window_seconds=WINDOW, at=1_800_000_000.0)
    body = alert.body

    assert "我" in body, body
    assert "她" not in body, f"这是她的信，不该用第三人称说她：{body}"
    assert "帮我" in body and "看一眼" in body, f"要给出动作，而且是求人：{body}"
    assert "【" not in body and "】" not in body, body
    assert "自动" not in body and "系统" not in body and "程序" not in body, body


def test_the_moment_formatting_is_not_borrowed_from_outage_notice() -> None:
    """时刻的写法与 `outage_notice` **逐字一致，但代码是各写各的**。

    为什么不 import：两个插件之间不该有依赖——删掉 `plugins/outage_notice/`
    不能让超时通知跟着坏。判据看的是**真的引用**（模块对象、函数对象），
    不是源码里出现过那个词：`alert.py` 的注释里正大光明写着"这三行是从它那儿抄的"。
    """

    borrowed = [name for name, value in vars(alert_module).items()
                if isinstance(value, types.ModuleType) and "outage" in name.lower()]
    assert borrowed == [], f"不该依赖掉线通知：{borrowed}"
    assert alert_module.format_moment is not outage_notice.format_moment, (
        "该是各写各的三行，不是把它的函数拿过来用")

    when = 1_800_000_000.0
    assert alert_module.format_moment(when) == outage_notice.format_moment(when)


# --- ④ 装配：接上哪个口、接不上怎么办 -----------------------------------------


def test_the_requirement_points_at_mail_and_register_is_called_once() -> None:
    """`REQUIRES` 里是 `mail`；真装配里 **mail 先装、本插件后装**，且只装一次。"""

    assert tuple(timeout_plugin.REQUIRES) == ("mail",), timeout_plugin.REQUIRES
    assert timeout_plugin.TITLE == "超时通知", timeout_plugin.TITLE
    assert timeout_plugin.TITLE != "timeout_alert", "显示名不能回落到模块名"

    registered: list[str] = []
    real_register = timeout_plugin.register

    def spy(registry):
        registered.append("timeout_alert")
        return real_register(registry)

    with mock.patch.object(timeout_plugin, "register", spy):
        with _discovery_state():
            loaded = discover(PluginRegistry())

    assert "mail" in loaded and "timeout_alert" in loaded, loaded
    assert loaded.index("mail") < loaded.index("timeout_alert"), loaded
    assert registered == ["timeout_alert"], "本插件的 register 该**正好**被调一次"


def test_without_the_mail_plugin_nothing_is_registered_and_core_runs() -> None:
    """藏掉 `mail` 的插件模块 → 本插件不装、不留后台节拍、不抛异常，核心照跑。"""

    registry = plugin_registry()
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.startswith("qq_roleplay_bot.plugins.mail"):
            raise ModuleNotFoundError("No module named 'qq_roleplay_bot.plugins.mail'",
                                      name="qq_roleplay_bot.plugins.mail")
        return real_import(name, package)

    with mock.patch.object(plugins_module.importlib, "import_module", fake_import):
        with _discovery_state():
            loaded = discover(registry, only=("timeout_alert",))

    assert "timeout_alert" not in loaded, loaded
    assert "mail" not in loaded, loaded
    assert registry.backgrounds == [], "没装上的插件不该留下后台节拍"
    assert registry.mail.knows() is False


def test_a_mail_plugin_without_a_recipient_registers_nothing_and_says_so() -> None:
    """mail 装了但**没配收件人**（能力是空的）→ 明确不接，且那行话是可搜到的。"""

    registry = plugin_registry()
    registry.report = ReportSeams(snapshot=_Snapshots())

    with _logs(PLUGIN_LOGGER) as records:
        timeout_plugin.register(registry)

    assert registry.backgrounds == [], "取不到发信口就不该登记节拍"
    assert any("超时通知这次没接上" in line for line in records), records


def test_without_a_snapshot_port_there_is_nothing_to_count() -> None:
    """没有只读快照口 → 数不出超时，硬挂上去只会变成"看着装了、其实不会响"。

    判据是 `ReportSeams.snapshot` 那个**字段**（`snap()` 这个方法**总是**在，
    它背后没接口时也只是每次回 `None`）。
    """

    registry = plugin_registry()
    registry.mail.provide(_fake_sender()[0])

    with _logs(PLUGIN_LOGGER) as records:
        timeout_plugin.register(registry)

    assert registry.backgrounds == [], registry.backgrounds
    assert any("超时通知这次没接上" in line for line in records), records


def test_the_switch_off_registers_nothing() -> None:
    """总开关关掉 → 不登记任何东西（用户要"通知我"，但也得能一键关掉）。"""

    with _env(alert_module.ENV_ENABLED, "0"):
        assert alert_module.enabled() is False
        registry = plugin_registry()
        registry.mail.provide(_fake_sender()[0])
        registry.report = ReportSeams(snapshot=_Snapshots())
        with _logs(PLUGIN_LOGGER) as records:
            timeout_plugin.register(registry)

    assert registry.backgrounds == [], registry.backgrounds
    assert any("未启用" in line for line in records), records


def test_the_config_flag_defaults_to_on() -> None:
    """缺省是**开**：用户要的是"超时通知我"，不是"先配好才通知"。"""

    assert alert_module.enabled() is True


def test_the_background_plugin_names_itself_and_polls_at_the_configured_rate() -> None:
    """登记上去的东西要长得像后台插件：有名字、有间隔、`enabled` 为真。"""

    registry, _sends, _ = _wired()
    plugin = _background(registry)

    assert plugin.name == "timeout-alert"
    assert plugin.interval_seconds == POLL, plugin.interval_seconds
    assert plugin.enabled is True

    with _env(alert_module.ENV_POLL_SECONDS, "45"):
        other, _sends2, _ = _wired()
    assert _background(other).interval_seconds == 45.0


def test_the_real_assembly_hands_over_the_mails_own_sender() -> None:
    """真装配（真 `discover()`、真 mail 插件）下，接到的就是 mail 那个发信口。

    这条**只看接上了哪个口、不发信**：真发信那一路由 `_wired()`（假发信口）覆盖，
    mail 自己那一侧由 `tests/test_mail_channel.py` / `test_mail_send_to.py` 覆盖。
    核心给快照那半由 `runtime.report_seams()` 装配（不在插件树里），这里用
    `ReportSeams(snapshot=...)` 扮演它——插件要的就是这个口，不是引擎。

    收件人那个前提由 `_recipient_configured()` 显式配：源码里已经没有默认收件人了，
    没配的话 mail 会**按设计**不开放发信能力，这条就变成在验"没配"而不是"接上了谁的"。
    """

    registry = plugin_registry()
    registry.report = ReportSeams(snapshot=_Snapshots())
    with _recipient_configured():
        with _discovery_state():
            loaded = discover(registry)

    assert "timeout_alert" in loaded, loaded
    assert registry.mail.knows() is True, "真装配里 mail 该把发信能力放上来了"
    plugin = _background(registry)
    assert plugin.enabled is True
    assert plugin.sender is registry.mail.operator_sender, "接的必须是 mail 放上来的那一个"
    assert plugin.snap == registry.report.snap, "快照口也必须是核心给的那一个"


def test_a_failing_send_is_logged_and_never_propagates() -> None:
    """发信抛异常 → 只记一行自己的名字（`timeout_alert_send_failed`），不上抛。

    为什么不留给后台节拍兜：它兜住异常记的是"后台插件这一轮出错"——那句话说的是
    "插件写坏了"，而"信没发出去"是另一件事，得有它自己的名字才有排障价值。
    """

    attempts: list[tuple[str, str]] = []
    registry, _sends, snapshots = _wired(sender=_failing_sender(attempts))
    plugin = _armed(registry, snapshots)

    with _logs(ALERT_LOGGER) as records:
        results = _feed(plugin, _every([0, 1, 2, 3, 4, 5]))

    assert results[5] is None, f"发失败了就不该说发出去了：{results[5]}"
    assert len(attempts) == 1, attempts
    assert any("timeout_alert_send_failed" in line for line in records), records


def test_a_snapshot_that_raises_is_logged_and_the_round_is_skipped() -> None:
    """快照读不出来 → 记一行、这一轮跳过（后台插件的形状约定是**不许抛出**）。"""

    snapshots = _Snapshots()
    snapshots.boom = RuntimeError("快照炸了")
    registry, sends, _ = _wired(snapshots=snapshots)
    plugin = _armed(registry, snapshots)

    with _logs(BACKGROUND_LOGGER) as records:
        results = _feed(plugin, _every([0, 1]))

    assert results == [None, None], results
    assert sends == [], sends
    assert any("timeout_alert_snapshot_failed" in line for line in records), records


def test_the_plugin_never_touches_the_dialogue_side_or_another_plugin() -> None:
    """一个字的边界：不调模型、不碰对话/记忆、不认识 mail 与掉线通知的实现。

    这是**静态**判据（读源码），与判据（真跑起来看接缝）互补：
    "删掉这个插件不影响核心"要成立，前提就是它没从别的地方抓东西。
    """

    root = os.path.dirname(timeout_plugin.__file__)
    for name in PACKAGE_FILES:
        text = open(os.path.join(root, name), encoding="utf-8").read()
        # 看的是**导入**与**用法**，不是注释里出现过那个词：注释里点名"我不认 mail"、
        # "那三行是从 outage_notice 抄的"，正是这套边界该有的样子。
        for forbidden in ("outage_notice", "mail_client", "llm_client"):
            assert not re.search(rf"^\s*(?:from|import)\s+.*{forbidden}", text, re.M), \
                (name, forbidden)
        assert "registry.chat" not in text, name
        assert "build_prompt" not in text, name


def test_removing_the_whole_plugin_package_keeps_core_and_mail_running() -> None:
    """**判据**：拦掉整个插件包 → 核心与 mail 照跑（`check_module_removal.py` 的离线尺度）。

    收件人这个前提由 `_recipient_configured()` 显式配（源码已无默认收件人）：
    没配的时候 mail **按设计**不开放能力，"mail 照跑"这条判据就验不到了。
    """

    registry = plugin_registry()
    real_import = importlib.import_module

    def fake_import(name, package=None):
        if name.startswith("qq_roleplay_bot.plugins.timeout_alert"):
            raise ModuleNotFoundError(
                "No module named 'qq_roleplay_bot.plugins.timeout_alert'",
                name="qq_roleplay_bot.plugins.timeout_alert")
        return real_import(name, package)

    with _recipient_configured():
        with mock.patch.object(plugins_module.importlib, "import_module", fake_import):
            with _discovery_state():
                loaded = discover(registry)

    assert "timeout_alert" not in loaded, loaded
    assert "mail" in loaded, loaded
    assert registry.mail.knows() is True
    assert [item for item in registry.backgrounds
            if getattr(item, "name", "") == "timeout-alert"] == []


# --- 突变验证（这一节是给"改弱了会不会红"用的，不是常规用例）------------------


def _mutation_notes() -> None:
    """**交付时真跑过的两处改动**（给读的人一个可复现的改法）。

    这两处都是"把判定改弱一点"，看着像无害的小改，实际各自放跑一类假警报：

    ① **去掉窗口裁剪**——`alert.py::TimeoutWatch.observe` 里

            -        while self._samples and self._samples[0][0] < cutoff:
            -            self._samples.popleft()
            +        # （删掉这两行）

    后果是拿**最早那个采样点**当基准，于是机器睡一觉起来会把几小时前的超时
    算进"最近五分钟"。真跑：`test_a_sleep_does_not_turn_old_timeouts_into_a_false_alarm`
    与 `test_timeouts_that_fell_out_of_the_window_are_not_counted` 变红。

    ② **去掉降温**——`observe` 里

            -        if self._last_sent is not None and float(now) - self._last_sent < self.cooldown_seconds:
            -            return None
            +        # （删掉这两行）

    后果是超时期间**每一轮都写一封信**（5 分钟 5 次、10 次、15 次…）。
    真跑：`test_the_cooldown_keeps_the_second_mail_quiet_until_it_expires` 变红。

    也就是说：**"不许乱响"这件事是有断言守着的**，不是"我认为它会红"。
    """
