"""宿主接口的验收测试：**没装任何能力时，引擎必须照常跑完**。

这一族的测试全部基于同一条判据（用户："删掉它，Stage 3 会不会出问题"）：
`_host` 里这些能力**一个都不能是底层的必需品**。所以：

1. `HostServices()` 用默认值就能建起来；
2. 每一项空实现都**不抛异常、不返回垃圾**，只返回"没有"；
3. 缺省的 `balance` / `roles` / `knowledge` 是 `None`——**"没有这项能力"必须和
   "有一个什么都不做的实现"分得开**（有 `BalanceSource` 才注册 `/balance`，
   空实现会让调用方以为"能做但结果为空"）。
"""
from qq_roleplay_bot._host import (
    HostServices,
    KnowledgeHit,
    MachineSample,
    NoAudit,
    NoCards,
    NoMachine,
    NoStyler,
)


def test_default_host_builds_with_nothing_installed() -> None:
    host = HostServices()
    assert isinstance(host.styler, NoStyler)
    assert isinstance(host.cards, NoCards)
    assert isinstance(host.machine, NoMachine)
    assert isinstance(host.audit, NoAudit)


def test_missing_optional_capabilities_are_none_not_stubs() -> None:
    """三项**可以缺省**的能力必须是 `None`。

    如果给它们一个"什么都不做"的空实现，调用方就分不清
    "这台机器没有余额查询"和"余额查询到了 0"——前者该不注册命令，后者该回一个数。
    """

    host = HostServices()
    assert host.balance is None
    assert host.roles is None
    assert host.knowledge is None


def test_styler_without_an_implementation_sends_in_one_piece() -> None:
    """没人实现停顿 → 一次发出去，不等待、不切段。"""

    styler = NoStyler()
    assert styler.split("第一句。第二句。第三句。") == ("第一句。第二句。第三句。",)
    assert styler.split("") == ()
    assert styler.delay_plan(("a", "b", "c")) == (0.0, 0.0, 0.0)


def test_card_renderer_without_an_implementation_declines() -> None:
    """画不出来要**明确说出来**（`available` 为假），而不是给一张空图。"""

    cards = NoCards()
    assert cards.available is False
    assert cards.render("标题", "正文") is None


def test_machine_probe_without_an_implementation_samples_nothing() -> None:
    """采不到就报空——**不编数字**。"""

    sample = NoMachine().sample()
    assert sample == MachineSample()
    assert sample.processes == () and sample.network == {} and sample.fans == ()


def test_audit_sink_without_an_implementation_records_nothing() -> None:
    audit = NoAudit()
    audit.record("kick", detail={"group": "1"}, result="ok", source="webui", token="secret")
    assert audit.tail() == []


def test_knowledge_hit_carries_a_source_so_data_can_be_attributed() -> None:
    """检索命中要带来源：它进 prompt 时是**不可信 DATA**，得能标清楚出自哪里。"""

    hit = KnowledgeHit(text="她喜欢喝奶茶", source="operator/人物.md", score=0.8)
    assert hit.source and hit.text


# --- 2026-10-02 补的（外部审查第四轮）-----------------------------------------


def test_build_host_services_is_three_state() -> None:
    """`build_host_services` 的三种取值，**与它 docstring 承诺的一致**。

    2026-10-02 之前代码是 `TypingStyler() if styler is None else styler`，
    于是 docstring 里"显式传 `False` 表示这次故意不接"是**假的**：
    `False` 会被原样装进 `HostServices`，之后 `host.styler.split(...)`
    就是 `AttributeError`（只有 `balance` / `roles` / `knowledge` 走 `x or None`
    才被归一）。外部审查第四轮点的就是这处"文档与实参矛盾"。现在三态是真的。
    """

    from qq_roleplay_bot._host import HostServices as _HostServices
    from qq_roleplay_bot._host_adapters import (
        FileAuditSink,
        HelpCardRenderer,
        LocalMachineProbe,
        TypingStyler,
        build_host_services,
    )

    # 1) 不传：现成实现（不是空实现）
    default = build_host_services()
    assert isinstance(default.styler, TypingStyler)
    assert isinstance(default.cards, HelpCardRenderer)
    assert isinstance(default.machine, LocalMachineProbe)
    assert isinstance(default.audit, FileAuditSink)

    # 2) 传 False：**故意不接** → 空实现，而且不可能再出现"装了个 False"
    off = build_host_services(styler=False, cards=False, machine=False, audit=False)
    assert isinstance(off, _HostServices)
    assert isinstance(off.styler, NoStyler)
    assert isinstance(off.cards, NoCards)
    assert isinstance(off.machine, NoMachine)
    assert isinstance(off.audit, NoAudit)
    # 关键：每一项都**真的可用**（空实现也不能一调就炸）
    assert off.styler.delay_plan(("a",)) == (0.0,)
    assert off.cards.available is False
    assert off.machine.sample() == MachineSample()
    assert off.audit.tail() == []

    # 3) 传对象：就用它
    sentinel = object.__new__(NoStyler)
    assert build_host_services(styler=sentinel).styler is sentinel

    # 4) 后三项的缺省是"没有能力"（None），传 False 也归一到 None
    assert build_host_services(balance=False, roles=False, knowledge=False).balance is None


def test_engine_default_host_is_not_the_empty_host() -> None:
    """**不传 host 时是有停顿的**——别照 `_host` 那句写反的示例去推理。

    那句原来是 `self.host = host or HostServices()  # 全空实现`，与生产相反：
    `stage3_main.DialogueEngine.__init__` 走的是 `default_host()`，而它与
    `HostServices()` 行为不同（实测）：

        default_host().styler.delay_plan(...)  == (6.0,)   有停顿
        HostServices().styler.delay_plan(...)  == (0.0,)   一次发出去
        default_host().cards.available         is True
        HostServices().cards.available         is False

    为什么默认**不**给空实现：`QQBOT_TYPING_SIM` 默认是开的，"拆接口"这一步
    必须行为不变（见 `default_host` 的 docstring）。
    """

    from qq_roleplay_bot.stage3_main import default_host

    real = default_host()
    empty = HostServices()
    assert real.styler.delay_plan(("a", "b")) != empty.styler.delay_plan(("a", "b"))
    assert real.cards.available is True and empty.cards.available is False


def test_the_machine_probe_explicitly_samples_nothing() -> None:
    """`LocalMachineProbe.sample()` **显式**返回空样本，不再假装在采。

    2026-10-02 修之前它是这样写的：

        processes=tuple(probe.processes())     # 拿到的是**协程对象**，不是数据

    而 `runtime_diagnostics` 的取数方法是 `async def`——同步方法里 await 不了。
    后果是永远返回空样本 + 一条 "coroutine ... was never awaited"
    （那条警告在**垃圾回收**时才发，所以要显式 `gc.collect()` 才抓得到）。

    注意 `host.machine` 目前**没有消费者**（`runtime.py` 里只有一处赋值、
    零处读取），所以这是"没人发现的静默缺陷"，不是"某条路径坏了"。
    这条测试钉住的是**修好之后的行为**：显式空、并且**不留协程残骸**。
    """

    import gc
    import warnings

    from qq_roleplay_bot._host_adapters import LocalMachineProbe

    class _Diagnostics:
        """`runtime_diagnostics` 的形状：取数方法是 **async**。"""

        async def processes(self):
            return ({"pid": 1},)

        async def network(self):
            return {"eth0": {"sent": 1}}

        async def fans(self):
            return ({"name": "fan1"},)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sample = LocalMachineProbe(_Diagnostics()).sample()
        gc.collect()

    never_awaited = [w for w in caught
                     if issubclass(w.category, RuntimeWarning)
                     and "never awaited" in str(w.message)]
    assert sample == MachineSample()
    assert not never_awaited, (
        "又出现'协程没被 await'了——说明 sample() 回头去调那些 async 取数方法了。"
        "要真采数据就得把 MachineProbe.sample 改成 async，别在同步方法里假装能 await")
