"""后台插件（Stage 4 的另一半）：入群审批、读信回信、每日汇报。

由来（2026-09-30 用户）：先把群管理与群主命令搬进插件，随后指出
"**入群审批也是插件，属于 stage4 内容**"。它确实不是命令——没人发一句话，
它自己按节拍去查有没有待处理的入群申请。所以插件分两种：

- **命令插件**：一条消息进来 → 文本 / `ImageReply` / `ActionRequest`；
- **后台插件**：时间到了 → `poll_once()`。

这个文件钉的是：节拍只有一份（`run_background_plugin`）、装配只有一个入口
（`build_background_plugins`）、**插件不持有 transport**（要动外部世界就走核心注入的
`call_action` / `notify`）、以及关掉开关就整个不存在。
"""
import asyncio

from qq_roleplay_bot import background_plugins as bp
# `JoinApprovalPlugin` 的实现**在插件目录里**（2026-10-04：插件自包含，
# 核心的 `background_plugins.py` 只留框架）。这条 import 原来指向核心，是搬家的
# 遗留；改的只是**位置**，下面每一条断言一个字没动。
from qq_roleplay_bot.plugins.join_approval.background import JoinApprovalPlugin
from qq_roleplay_bot.plugins import PluginRegistry
from qq_roleplay_bot.plugins.join_approval.join_approval import JoinApprovalPolicy, JoinApprovalPoller
from qq_roleplay_bot.plugins.roles.roles import ROLE_OWNER, SelfRoleCache

GROUP = "717151356"
ME = "900000001"


class FakePlugin:
    name = "fake"

    def __init__(self, interval_seconds=0.01, explode=False, enabled=True):
        self.interval_seconds = interval_seconds
        self.explode = explode
        self.enabled = enabled
        self.calls = 0

    async def poll_once(self):
        self.calls += 1
        if self.explode:
            raise RuntimeError("这一轮炸了")
        return self.calls


def _run_loop(plugin, seconds=0.06, min_interval=0.01):
    """跑一小会儿节拍循环，然后取消。返回取消后有没有干净退出。"""

    async def main():
        task = asyncio.create_task(
            bp.run_background_plugin(plugin, first_delay=0, min_interval=min_interval))
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    return asyncio.run(main())


# --- 节拍：只有一份 ---------------------------------------------------------

def test_loop_keeps_polling_on_the_interval() -> None:
    plugin = FakePlugin()
    assert _run_loop(plugin) is True
    assert plugin.calls >= 2, "节拍应当反复调用 poll_once"


def test_loop_survives_a_failing_round() -> None:
    """一轮抛异常不能停掉这条通道（旧的三份循环各自兜了一次，现在只有这一处）。"""

    plugin = FakePlugin(explode=True)
    _run_loop(plugin)
    assert plugin.calls >= 2, "出错之后还要继续跑下一轮"


def test_first_delay_is_configurable() -> None:
    """启动后第一轮要等一会儿（对面常常晚几秒连上来），但默认值可注掉。"""

    async def main():
        plugin = FakePlugin()
        task = asyncio.create_task(
            bp.run_background_plugin(plugin, first_delay=0.02, min_interval=0.01))
        await asyncio.sleep(0.005)
        early = plugin.calls
        await asyncio.sleep(0.04)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return early

    assert asyncio.run(main()) == 0, "延迟期间不该跑第一轮"


def test_interval_has_a_floor() -> None:
    """节拍下限防"配置写 1 秒把对面打爆"。"""

    plugin = FakePlugin(interval_seconds=0.01)
    _run_loop(plugin, seconds=0.05, min_interval=bp.MIN_INTERVAL_SECONDS)
    # 下限默认 15 秒：只可能跑第一轮
    assert plugin.calls == 1


def test_enabled_flag_is_honoured() -> None:
    assert bp.plugin_enabled(FakePlugin(enabled=True)) is True
    assert bp.plugin_enabled(FakePlugin(enabled=False)) is False
    # 没声明 enabled 的按启用处理（它自己在 poll_once 里判断）
    class Bare:
        name = "bare"
        interval_seconds = 60

        async def poll_once(self):
            return None

    assert bp.plugin_enabled(Bare()) is True


# --- 装配：一个入口，开关决定有没有 ------------------------------------------

class FakeEngine:
    def note_letter(self, letter):  # pragma: no cover - 只有日报装配会用到
        return None


class _Patch:
    """离线测试入口不用 pytest fixture，所以补丁自己进自己出。

    补丁打在**各个插件自己的 `wire.py`**（`build_*` 住在那里）以及 `dev_config` 上；
    以前打的是 `background_plugins._build_mail_channel`——那个名字已经被"一个功能一个
    文件夹"的拆分割掉（见 `plugins/*/wire.py`），所以那时这些测试早就在打空气。
    """

    def __init__(self, **replacements) -> None:
        self.replacements = replacements
        self.originals: list[tuple[object, str, object]] = []

    def __enter__(self):
        from qq_roleplay_bot import dev_config
        from qq_roleplay_bot.plugins.join_approval import plugin as join_plugin
        from qq_roleplay_bot.plugins.mail import wire as mail_wire

        targets = {"build_join_approval": join_plugin,
                   "build_mail_channel": mail_wire,
                   "build_daily_report": mail_wire}
        for name, value in self.replacements.items():
            target = targets.get(name, dev_config)
            self.originals.append((target, name, getattr(target, name)))
            setattr(target, name, value)
        return self

    def __exit__(self, *exc) -> None:
        for target, name, value in reversed(self.originals):
            setattr(target, name, value)


def _registry(engine=None, **kwargs):
    """一个带窄接缝的插件注册表。"""

    async def call_action(action, params=None):
        return {"user_id": "900000002", "role": ROLE_OWNER}

    return PluginRegistry(
                          call_action=call_action, notify=lambda *a: None, **kwargs)


def _register(*plugins: str) -> PluginRegistry:
    """**直接**调这几个插件的 `register()`，不跑整个 `discover()`。

    为什么不 `discover(only=...)`：发现机制会顺着 `REQUIRES` 把前置插件也装上
    （那是它的职责），于是这里会连带装上 `roles`、甚至面板——测试要的是"这两个插件
    各自装出了什么"，所以直接把范围钉死。装配顺序由 `roles` 先行的约定在这里手写。
    """

    import importlib

    registry = _registry()
    for name in plugins:
        module = importlib.import_module(f"qq_roleplay_bot.plugins.{name}.plugin")
        module.register(registry)
    return registry


def test_builder_registers_join_approval_as_a_plugin() -> None:
    from qq_roleplay_bot import dev_config

    with _Patch(AUTO_APPROVE_JOIN=True):
        registry = _register("roles", "join_approval")
    names = [plugin.name for plugin in registry.backgrounds]
    assert "join-approval" in names
    join = next(p for p in registry.backgrounds if p.name == "join-approval")
    assert join.interval_seconds == dev_config.APPROVE_POLL_SECONDS
    assert join.enabled is True


def test_join_approval_is_loaded_by_the_discovery_mechanism() -> None:
    """发现机制也要能装它（前置 `roles` 会被自动带上）。"""

    from qq_roleplay_bot.plugins import discover

    with _Patch(AUTO_APPROVE_JOIN=True):
        registry = _registry()
        loaded = discover(registry, only=("join_approval",))
    assert "roles" in loaded and "join_approval" in loaded, loaded


def test_builder_skips_disabled_channels() -> None:
    with _Patch(AUTO_APPROVE_JOIN=False, build_mail_channel=lambda engine: None,
                build_daily_report=lambda engine, **kwargs: None):
        registry = _register("mail")
    assert registry.backgrounds == [], "全关掉时不该装配出任何后台插件"


def test_builder_wraps_mail_and_report() -> None:
    from qq_roleplay_bot import dev_config

    class FakeChannel:
        async def poll_once(self):
            return 1

    class FakeReporter:
        def due(self):
            return False

        async def run_once(self, engine):  # pragma: no cover - due 为假不会调用
            return None

    with _Patch(build_mail_channel=lambda chat: FakeChannel(),
                build_daily_report=lambda report, **kwargs: FakeReporter()):
        registry = _register("mail")
    names = sorted(plugin.name for plugin in registry.backgrounds)
    # 登记顺序：读信回信在前、日报在后（2026-10-01 起如此——两条通道的构造顺序变了）。
    # 这里按**集合**断言，不再依赖顺序；要钉顺序就单独写一条。
    assert names == ["daily-report", "mail-channel"], names
    by_name = {plugin.name: plugin for plugin in registry.backgrounds}
    assert by_name["mail-channel"].interval_seconds == dev_config.MAIL_POLL_SECONDS
    assert asyncio.run(by_name["mail-channel"].poll_once()) == 1
    # due() 为假 → 不写不发
    assert asyncio.run(by_name["daily-report"].poll_once()) is None


# --- 插件不持有 transport ----------------------------------------------------

def test_join_plugin_and_poller_hold_no_transport() -> None:
    calls: list[tuple[str, dict]] = []

    async def call_action(action, params=None):
        calls.append((action, dict(params or {})))
        return []

    async def notify(text):
        return None

    poller = JoinApprovalPoller(call_action=call_action, notify=notify,
                                policy=JoinApprovalPolicy(), roles=SelfRoleCache(None))
    plugin = JoinApprovalPlugin(poller, interval_seconds=60)
    for obj in (poller, plugin):
        for forbidden in ("transport", "capabilities", "self_roles"):
            assert not hasattr(obj, forbidden), forbidden
    # poll_once 走的还是注入的接缝
    asyncio.run(plugin.poll_once())
    assert calls == [("get_group_system_msg", {})]


def test_poller_uses_the_injected_notify() -> None:
    sent: list[str] = []

    async def call_action(action, params=None):
        if action == "get_group_system_msg":
            return [{"flag": "f1", "group_id": GROUP, "requester_uin": 1009,
                     "requester_nick": "甲", "message": "路过", "checked": False}]
        return {}

    async def notify(text):
        sent.append(text)

    roles = SelfRoleCache(_RoleClient(ROLE_OWNER))
    poller = JoinApprovalPoller(call_action=call_action, notify=notify,
                                policy=JoinApprovalPolicy(), roles=roles)
    result = asyncio.run(poller.tick())
    assert result["held"] == 1
    assert sent and "申请加入" in sent[0]
    assert not hasattr(poller, "transport")


class _RoleClient:
    def __init__(self, role):
        self.role = role

    async def call(self, action, params=None):
        if action == "get_login_info":
            return {"user_id": "900000002"}
        return {"role": self.role}


def test_core_seams_gate_and_unwrap() -> None:
    """核心那两个接缝：先过闸门，再把回执拆成 `data`（插件因此不懂协议）。"""

    from qq_roleplay_bot.capabilities import CapabilityRegistry
    from qq_roleplay_bot.runtime import _plugin_action_seams

    class FakeTransport:
        def __init__(self):
            self.calls = []

        async def call_api(self, action, params=None):
            self.calls.append((action, dict(params or {})))
            return {"status": "ok", "retcode": 0, "data": [{"flag": "x"}]}

    class FakeEngineWithRegistry:
        capabilities = CapabilityRegistry()

        def __init__(self):
            self.outbox = None

    transport = FakeTransport()
    engine = FakeEngineWithRegistry()
    call_action, _ = _plugin_action_seams(engine, transport)
    # 读：按 `read` 那道闸放行，并且回执被拆成了 data
    assert asyncio.run(call_action("get_group_system_msg", {})) == [{"flag": "x"}]
    # 写：只有 `join_approval` 那一条能过
    assert asyncio.run(call_action("set_group_add_request", {"flag": "x", "approve": True})) == [{"flag": "x"}]
    # 别的用途的 action 走这条接缝会被拒（发送类不在只读名单里）。
    # **抛的是 `plugins.ActionDenied`**（2026-10-01 改）：核心把 `CapabilityDenied`
    # 翻译成接缝模块自己的异常类型——插件不该认识 `capabilities.py`。
    from qq_roleplay_bot.plugins import ActionDenied

    try:
        asyncio.run(call_action("send_group_msg", {"group_id": 1, "message": "hi"}))
    except ActionDenied:
        pass
    else:  # pragma: no cover - 不该走到
        raise AssertionError("入群审批的接缝不该放行发送类 action")
