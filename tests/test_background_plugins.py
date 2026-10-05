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
import contextlib
import logging

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
    （那是它的职责），于是"只想验这一个"时旁边那几个也会一起装上——测试要的是
    "这一个插件装出了什么"，所以直接把范围钉死。

    传进来的顺序就是 `register()` 的顺序（`_register("roles", "join_approval")`
    是"角色来源已就位"的那种装配）。2026-10-05 起 `join_approval` 不再声明
    `REQUIRES = ("roles",)`，所以这个顺序得由调用方自己交代清楚。
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
    """发现机制要能装它，而且**不再顺着 `REQUIRES` 把 `roles` 拉进来**。

    2026-10-05：身份/权限事实归核心，`REQUIRES = ("roles",)` 撤了——所以
    "只装 join_approval"的 `loaded` 里**只有它自己**（以前会连带装上 `roles`）。
    这条同时钉住"没有 REQUIRES"：哪天有人把角色依赖加回去，这里会红。
    """

    from qq_roleplay_bot.plugins import discover

    with _Patch(AUTO_APPROVE_JOIN=True):
        registry = _registry()
        loaded = discover(registry, only=("join_approval",))
    assert loaded == ("join_approval",), loaded


# --- 角色来源：核心优先（2026-10-05：身份/权限事实归核心）---------------------

def test_join_approval_asks_the_core_for_the_role_source() -> None:
    """角色来源**先问核心**（`registry.roles`）；核心给了就不看插件那一份。

    2026-10-05 用户把身份/权限事实判给核心：`plugins/roles/` 迟早删掉，所以
    "核心给了什么就用什么"必须被断言钉住——否则哪天退回共享那一份，没有测试会红。
    （过渡那一半——核心还没有时退回 `registry.shared_roles()`——由下一条钉。）
    """

    from qq_roleplay_bot.plugins.join_approval import plugin as join_plugin

    core_source = SelfRoleCache(None)
    plugin_source = SelfRoleCache(None)
    registry = _registry(roles=core_source)
    registry.provide_roles(plugin_source)
    assert join_plugin._RoleSource(registry).current() is core_source


def test_join_approval_falls_back_only_while_the_core_has_nothing() -> None:
    """过渡期：核心那边还没落地（`registry.roles is None`）时才用插件那一份。

    **这条跟着过渡一起删**：核心落地、`plugins/roles/` 删掉之后，
    `_RoleSource.current()` 里那句退回就该没了，这条测试也该删——
    它钉的不是目标状态，而是"迁移期间不许静默失去角色来源"。
    """

    from qq_roleplay_bot.plugins.join_approval import plugin as join_plugin

    plugin_source = SelfRoleCache(None)
    registry = _registry()
    registry.provide_roles(plugin_source)
    assert join_plugin._RoleSource(registry).current() is plugin_source


def test_poller_approves_nothing_without_a_role_source() -> None:
    """两个来源都没有：**一条申请都不处理**（fail-closed），也不发任何写动作。

    判据（用户）：拿不到"她在那个群是什么角色"时不许猜——以前这里的坏形态是
    `unknown` 被当成"大概是群主"，一条白名单申请就真批了。
    """

    from qq_roleplay_bot.plugins.join_approval import plugin as join_plugin

    calls: list[tuple[str, dict]] = []

    async def call_action(action, params=None):
        calls.append((action, dict(params or {})))
        if action == "get_group_system_msg":
            return [{"flag": "f1", "group_id": GROUP, "requester_uin": ME,
                     "requester_nick": "某人", "message": "让我进"}]
        return {}

    source = join_plugin._RoleSource(_registry())      # 核心没给、插件也没有
    assert source.current() is None
    assert asyncio.run(source.role(GROUP)) == "", "没有来源时角色是空串，不是猜出来的"
    poller = JoinApprovalPoller(
        call_action=call_action, notify=None,
        policy=JoinApprovalPolicy(whitelist=(ME,)),    # 白名单命中，本该通过
        roles=source)
    result = asyncio.run(poller.tick())
    assert result == {"approved": 0, "rejected": 0, "held": 0, "invites": 0}
    assert [action for action, _ in calls] == ["get_group_system_msg"], \
        "没有角色来源时一条写动作都不该发出去"


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


# --- 日报装配：接缝给的是**工厂**，不是 client ---------------------------------
#
# 2026-10-05 修的 bug：`build_daily_report` 把 `report.letter_client`（**工厂**，
# 契约见 `plugins.ReportSeams.letter_client`）原样塞给了 `LetterWriter`，
# 于是 `writer.client` 是个函数；真去写信时炸在 `await self.client.complete(...)`。
# 生产日志里只剩一行 `letter_draft_failed category=AttributeError`——装配阶段
# 一点异常都没有，它能一路溜到"到点写第一封信"才现形。
#
# 它能溜过去，就是因为**没有测试钉这条契约**。下面两条是补上的守卫：
# 一条钉"工厂被调用、拿到的 client 是对象"，一条钉"没给通道就优雅关闭"。


class LetterClientStub:
    """冒充写信通道：契约只有一个 `async complete(request)`。"""

    def __init__(self, reply: str = "<subject>标题</subject><body>正文</body>") -> None:
        self.reply = reply
        self.requests: list[object] = []

    async def complete(self, request):
        self.requests.append(request)
        return self.reply


@contextlib.contextmanager
def _warnings_of(module_name: str):
    """抓某个 logger 的 warning 文本（离线入口不用 pytest 的 caplog）。"""

    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collect()
    logger = logging.getLogger(module_name)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)  # 显式压到这里，免得被别处调高的级别吞掉
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _report_seam(factory):
    """用**生产那一份** `ReportSeams`（不在测试里另写一个形状）。"""

    from qq_roleplay_bot.plugins import ReportSeams

    return ReportSeams(letter_client=factory, note_letter=lambda letter: None)


def test_the_report_seam_letter_client_is_a_factory_and_gets_called() -> None:
    """**契约**：`report.letter_client` 是工厂，装配必须**调用**它。

    三件事一起断言，缺一条都抓不住这个 bug（实测：改回"传工厂"这条立刻变红）：

    1. 工厂被调用**恰好一次**；
    2. 交给 `LetterWriter` 的 client **就是**工厂返回的那个对象（不是工厂本身）；
    3. 那个对象上有 `.complete`——写信要用的就是它。
    """

    from qq_roleplay_bot.plugins.mail import wire as mail_wire

    client = LetterClientStub()
    calls: list[str] = []

    def make_letter_client():
        calls.append("called")
        return client

    with _Patch(MAIL_REPORT_ENABLED=True):
        reporter = mail_wire.build_daily_report(_report_seam(make_letter_client),
                                                registry=_registry())

    assert reporter is not None, "开关开着、工厂也给了 client，就该装配出汇报器"
    assert calls == ["called"], f"工厂必须被调用**恰好一次**，实际 {len(calls)} 次"
    writer = reporter.writer
    assert writer.client is client, (
        "`LetterWriter.client` 必须是工厂**返回的那个对象**；拿到工厂本身的话，"
        "写信时 `await self.client.complete(...)` 会抛 AttributeError")
    assert callable(getattr(writer.client, "complete", None)), (
        "写信走的是 `await client.complete(request)`：这个对象上必须有 complete")
    assert getattr(writer, "enabled", False) is True


def test_a_missing_letter_client_factory_shuts_the_report_down_gracefully() -> None:
    """**优雅关闭**：工厂缺失 / 不是 callable / 返回 None → 那条 warning、不装配、不崩。

    "接缝没给模型通道就别启用日报"这条行为是原来就有的（不是这次 bug 的一部分），
    所以它也要有守卫；顺手把"调了工厂之后还要看返回值"这一半也钉住——
    工厂返回 None 时同样该走关闭路径，而不是把 `None` 当通道配下去。
    """

    from unittest.mock import patch

    from qq_roleplay_bot.plugins import ReportSeams
    from qq_roleplay_bot.plugins.mail import wire as mail_wire

    class NotCallable:
        """像"有人把 client 本体填进了工厂字段"：是对象，但不可调用。"""

    cases = {
        "工厂缺失": ReportSeams(),
        "工厂不是 callable": ReportSeams(letter_client=NotCallable()),
        "工厂返回 None": ReportSeams(letter_client=lambda: None),
    }
    with _Patch(MAIL_REPORT_ENABLED=True):
        for label, seam in cases.items():
            with patch("qq_roleplay_bot.plugins.mail.letter_writer.LetterWriter") as maker:
                with _warnings_of(mail_wire.__name__) as warnings:
                    reporter = mail_wire.build_daily_report(seam, registry=_registry())
            assert reporter is None, f"{label}：这次不该启用日报"
            assert maker.call_count == 0, f"{label}：不该构造 LetterWriter"
            assert any("没有模型通道" in text for text in warnings), (label, warnings)
