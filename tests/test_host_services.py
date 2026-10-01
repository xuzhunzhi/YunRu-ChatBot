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
