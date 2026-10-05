"""**掉线事件**（本体侧的一半）——看门狗的状态迁移 + `registry.link` 那条接缝。

由来（2026-10-06 用户）：

> *"掉线可以调用 mail 插件给我发消息通知我"* · *"断一次只发一次，不要反复调用"* ·
> *"bot 本身稳定性我认为是很可靠的，不用额外通知"* · *"记住这个也是插件"* ·
> *"这个作为 mail 插件的后置插件，也就是说 mail 插件是这个插件的依赖"*

本体侧只做两件事（通知那半边是插件的事，**不在这里测**）：

1. **复用**看门狗本来就有的状态与计时（`transport.connected` +
   `CONNECTION_WARN_AFTER/EVERY_SECONDS`）做**确定的边沿**：
   `在线 → 掉线` 算一段，掉线期间反复检查不算新事件，重连后重新武装；
2. 在 `registry.link`（`plugins.LinkSeams`）上广播**"断了"这个事实**。

**恢复不发**（用户："不用额外通知"）——这条与"一次只发一次"一样是判据。

这里的用例都是**实测**：驱动真实的 `_watch_connection`（把两个计时改小），
不是"我认为它会这样"。
"""
from __future__ import annotations

import asyncio
import logging
import types
from contextlib import contextmanager

from qq_roleplay_bot import dev_config
from qq_roleplay_bot import runtime as runtime_module
from qq_roleplay_bot.plugins import LinkSeams, PluginRegistry


class _Transport:
    """看门狗用得到的全部东西：一个 `connected` 字段（与 `onebot_ws` 同名同义）。"""

    def __init__(self, connected: bool) -> None:
        self.connected = connected


def _feed(states: list[bool], notifier) -> list[bool]:
    """按顺序喂一串"连着没有"，返回每次 `observe()` 的返回值。"""

    async def run() -> list[bool]:
        return [await notifier.observe(state) for state in states]

    return asyncio.run(run())


@contextmanager
def _fast_watchdog(after: float = 0.01, every: float = 0.02):
    """把看门狗的计时改小（只有测试这么干），退出时原样还回去。"""

    previous_after = runtime_module.CONNECTION_WARN_AFTER_SECONDS
    previous_every = runtime_module.CONNECTION_WARN_EVERY_SECONDS
    runtime_module.CONNECTION_WARN_AFTER_SECONDS = after
    runtime_module.CONNECTION_WARN_EVERY_SECONDS = every
    try:
        yield
    finally:
        runtime_module.CONNECTION_WARN_AFTER_SECONDS = previous_after
        runtime_module.CONNECTION_WARN_EVERY_SECONDS = previous_every


@contextmanager
def _runtime_logs():
    """抓 `qq_roleplay_bot.runtime` 的日志行（与 `test_stage3` 里那条告警用例同法）。"""

    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    logger = logging.getLogger("qq_roleplay_bot.runtime")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# --- 状态迁移（"一次掉线只通知一次"的实现所在）--------------------------------


def test_one_outage_notifies_exactly_once() -> None:
    """掉线一次 → 接收者**正好被叫一次**；同一段里继续检查 → **不再叫**。

    这是用户最强调的一条："断一次只发一次，不要反复调用"。
    """

    calls: list[str] = []
    notifier = runtime_module._DisconnectNotifier([lambda: calls.append("断了")])

    results = _feed([False, False, False, False, False], notifier)

    assert calls == ["断了"], "同一段掉线只该通知一次"
    # 返回值：只有第一枪算"新的一段掉线"（其余是同一段里的重复检查）。
    assert results == [True, False, False, False, False], results


def test_reconnect_rearms_and_the_next_outage_notifies_again() -> None:
    """重连后**重新武装**；再次断开 = **新的一段** → 再叫一次（用户要求）。"""

    calls: list[str] = []
    notifier = runtime_module._DisconnectNotifier([lambda: calls.append("断了")])

    results = _feed([False, False, True, True, False, False], notifier)

    assert calls == ["断了", "断了"], "第二段掉线要再通知一次"
    assert results == [True, False, False, False, True, False], results


def test_recovery_alone_never_notifies() -> None:
    """**恢复不发**（用户："bot 本身稳定性我认为是很可靠的，不用额外通知"）。"""

    calls: list[str] = []
    notifier = runtime_module._DisconnectNotifier([lambda: calls.append("断了")])

    results = _feed([True, True, True], notifier)

    assert calls == []
    assert results == [False, False, False]


def test_an_async_receiver_is_awaited() -> None:
    """接收者可以是协程函数（发信这类事大概率是异步的）。"""

    calls: list[str] = []

    async def receiver() -> None:
        calls.append("async")

    notifier = runtime_module._DisconnectNotifier([receiver])
    results = _feed([False, False], notifier)

    assert calls == ["async"]
    assert results == [True, False]


# --- 接收者抛异常不许把机器人带走 ---------------------------------------------


def test_a_raising_receiver_is_logged_and_the_rest_still_run() -> None:
    """接收者自己抛异常 → **记一笔、继续跑**；后面的接收者照常被叫。

    为什么必须有这条：发信会失败（网络、凭据、对方没配），而失败的是**插件**，
    代价不能让机器人来付——看门狗是这个进程里"消息进不来"唯一还在说话的东西。
    """

    calls: list[str] = []

    def broken() -> None:
        raise RuntimeError("发不出去（合成故障）")

    notifier = runtime_module._DisconnectNotifier([broken, lambda: calls.append("后面的")])

    with _runtime_logs() as records:
        results = _feed([False, False], notifier)

    assert results == [True, False], "接收者失败不该改变状态机（这一段仍然只播一次）"
    assert calls == ["后面的"], "一个接收者炸了不许影响后面的"
    assert any("disconnect_receiver_failed" in line for line in records), records


# --- 接缝（接口层）------------------------------------------------------------


def test_a_bare_registry_has_the_link_seam_with_no_receivers() -> None:
    """`registry.link` 就在注册表上；**没人登记时名单是空的**——核心照常跑。"""

    registry = PluginRegistry()

    assert registry.knows_link() is True, "注册表自己造的那个接缝永远是接上的"
    assert registry.disconnect_receivers() == (), "没有插件登记时必须是空名单"
    assert callable(registry.link.disconnect_sink)

    registry.link.on_disconnect(lambda: None)

    assert len(registry.disconnect_receivers()) == 1


def test_a_handmade_seam_without_a_sink_registers_nothing_and_does_not_raise() -> None:
    """手造的 `LinkSeams()`（没接上核心）是空操作——单元测试里不该崩。"""

    LinkSeams().on_disconnect(lambda: None)  # 不抛异常就算过
    assert LinkSeams().disconnect_sink is None


def test_a_non_callable_receiver_is_refused_by_the_registry() -> None:
    """`on_disconnect("这是个字符串")` 不许进名单——广播时才发现就晚了。"""

    registry = PluginRegistry()
    registry.link.on_disconnect("不是函数")
    registry.link.on_disconnect(None)

    assert registry.disconnect_receivers() == ()


def test_the_notifier_is_built_from_the_registry_receivers() -> None:
    """`_disconnect_notifier_for` 取的是**此刻**注册表上的名单（装配路径）。"""

    calls: list[str] = []
    registry = PluginRegistry()
    registry.link.on_disconnect(lambda: calls.append("断了"))
    engine = types.SimpleNamespace(plugin_registry=registry)

    notifier = runtime_module._disconnect_notifier_for(engine)

    assert len(notifier.receivers) == 1
    assert _feed([False, False], notifier) == [True, False]
    assert calls == ["断了"]


def test_an_engine_without_a_registry_gets_an_empty_notifier() -> None:
    """没有插件注册表（纯核心装配）时也要能造出播空转的广播对象。"""

    notifier = runtime_module._disconnect_notifier_for(types.SimpleNamespace())

    assert notifier.receivers == ()
    assert _feed([False], notifier) == [True]


# --- 配置开关 -----------------------------------------------------------------


def test_the_switch_off_never_calls_anyone() -> None:
    """开关关掉 → **一次都不叫**（行为与改动前逐字相同）。"""

    calls: list[str] = []
    notifier = runtime_module._DisconnectNotifier(
        [lambda: calls.append("断了")], enabled=False,
    )

    results = _feed([False, False, True, False, False], notifier)

    assert calls == []
    assert results == [False, False, False, False, False]


def test_the_switch_is_read_from_dev_config_when_the_notifier_is_built() -> None:
    """装配时读 `dev_config.DISCONNECT_NOTICE_ENABLED`（`QQBOT_DISCONNECT_NOTICE`）。"""

    engine = types.SimpleNamespace(plugin_registry=PluginRegistry())
    previous = runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED
    try:
        runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED = False
        assert runtime_module._disconnect_notifier_for(engine).enabled is False
        runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED = True
        assert runtime_module._disconnect_notifier_for(engine).enabled is True
    finally:
        runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED = previous


def test_the_switch_defaults_to_on_in_the_config_module() -> None:
    """**默认开**（用户要的是"掉线通知我"，不是"要先配置才通知"）。

    这条读的是 `dev_config` 的**实际值**：只有当这台机器显式写了
    `QQBOT_DISCONNECT_NOTICE=0` 才会红——那正是"默认"被改掉的信号。
    """

    assert dev_config.DISCONNECT_NOTICE_ENABLED is True


# --- 真实看门狗（端到端：状态 + 计时 + 接缝连起来）----------------------------


def test_the_watchdog_notifies_once_per_outage_end_to_end() -> None:
    """走真实 `_watch_connection`：一段掉线只叫一次，重连后再断**再叫一次**。

    这一条是"接缝真的接在既有看门狗上"的凭据：它复用的是原来的计时
    （`CONNECTION_WARN_*`）与原状态（`transport.connected`），没有第二个定时器。
    """

    calls: list[str] = []
    registry = PluginRegistry()
    registry.link.on_disconnect(lambda: calls.append("断了"))
    transport = _Transport(connected=False)
    notifier = runtime_module._disconnect_notifier_for(
        types.SimpleNamespace(plugin_registry=registry)
    )

    with _fast_watchdog(), _runtime_logs() as records:
        async def run() -> None:
            task = asyncio.create_task(runtime_module._watch_connection(transport, notifier))
            try:
                await asyncio.sleep(0.15)          # 第一段掉线（多次采样）
                assert calls == ["断了"], f"同一段掉线叫了 {len(calls)} 次"
                transport.connected = True
                await asyncio.sleep(0.10)          # 重连 = 重新武装
                assert calls == ["断了"], "恢复不许通知"
                transport.connected = False
                await asyncio.sleep(0.10)          # 第二段掉线
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())

    assert calls == ["断了", "断了"], calls
    assert any("连上来" in line for line in records), "原来的告警日志照打"


def test_the_watchdog_stays_quiet_with_the_switch_off() -> None:
    """开关关掉时走同一条真实路径：告警日志照打，接收者一次都不叫。"""

    calls: list[str] = []
    engine = types.SimpleNamespace(plugin_registry=PluginRegistry())
    engine.plugin_registry.link.on_disconnect(lambda: calls.append("断了"))
    transport = _Transport(connected=False)
    previous = runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED
    runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED = False
    try:
        notifier = runtime_module._disconnect_notifier_for(engine)
        with _fast_watchdog(), _runtime_logs() as records:
            async def run() -> None:
                task = asyncio.create_task(
                    runtime_module._watch_connection(transport, notifier)
                )
                try:
                    await asyncio.sleep(0.10)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            asyncio.run(run())
    finally:
        runtime_module.dev_config.DISCONNECT_NOTICE_ENABLED = previous

    assert calls == []
    assert any("连上来" in line for line in records), "关掉开关不该把原来的告警一起关掉"


def test_the_watchdog_runs_without_any_receiver() -> None:
    """**没有插件注册**（也没给广播对象）时核心照常跑：看门狗不报错、告警照打。"""

    transport = _Transport(connected=False)

    with _fast_watchdog(), _runtime_logs() as records:
        async def run() -> None:
            task = asyncio.create_task(runtime_module._watch_connection(transport))
            try:
                await asyncio.sleep(0.08)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())

    assert any("连上来" in line for line in records), records
