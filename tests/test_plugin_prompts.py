"""插件的 prompt 扩展：**恋人 / 剧情 / 关系那类插件靠它影响 she 说什么**。

由来（用户 2026-10-01）："有人要做恋人插件，要在判定 agent / 回复 agent 的 prompt 里
插入内容，这个怎么办？"

答案分两层：

1. **接口本来就有**（`extensions.PromptSources` + `PromptPlugin`），设计也是对的：
   材料当不可信 DATA、过 sanitize、有长度上限、异常降级、`after_decision` 只观察。
2. **但路断了两处**（这次补上）：
   - 没有任何登记口 → 补 `PluginRegistry.provide_prompts()` / `runtime` 里
     `prompt_sources.add_plugins(...)`；
   - **判定 agent 收不到插件材料** → 补 `build_judge_hint()`（上限 200 字）
     与 `prompt_sources.judge_hints()`，`_judge()` 在判定之前收。

这个文件的判据和 `AGENTS.md` 2.1/2.2 完全一致：**插件能让她"知道得更多"，
不能让她"变成另一个人"**——所以每条测试都在钉"材料只进 DATA 段、有上限、可降级"。

## ⚠️ 2026-10-04 从 `stage4-plugins` 搬到 `stage3-plugin-host` 时的实况（别读成"全绿"）

**本文件里有一批用例搬过来之后跑不了**，因为它们在 `stage4-plugins` 上依赖的核心侧
代码在这一条分支上**还没移植**。它们全部**原样保留**（`unittest.skip` +
逐条写明原因），**没有删、没有改弱、也没有把断言打散**——所以它们会**大声**列在
`skipped` 里，而不是静默变绿。

缺的东西只有两处，都在**判定 agent 那一条 prompt 路**上（属于 Stage 3 的对话/prompt
改动，不在这次"接缝移植"的范围内，按 `AGENTS.md` §一 需要单独谈）：

| 缺的 | 在哪 | 为什么它不属于"接缝移植" |
| --- | --- | --- |
| `extensions.MAX_JUDGE_HINT_LENGTH` + `PromptSources.judge_hints()` | `extensions.py` | 新增一条**判定**取材口 |
| `dialogue_judge.build_judge_messages(plugin_hints=…)`（并把 `identity` 挪进 DATA 边界） | `dialogue_judge.py` | 改**判定 prompt 的正文构造**——`AGENTS.md` §一 明确：动 prompt 的改动属于 Stage 3，要单独决定 |

下面能跑的那些**覆盖了这次真正接上的那一段**：登记口
（`PluginRegistry.provide_prompts()` + `runtime` 的 `prompt_sources.add_plugins(...)`）、
材料标来源 / 截断 / 异常降级、以及 `ReportSeams` 与 `ChatSeams` 两条"接缝是不是闸门"的钉子。
"""
import asyncio
import unittest

from qq_roleplay_bot.extensions import (
    MAX_PLUGIN_PROMPT_LENGTH,
    PromptContext,
    PromptSources,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

#: 判定那条路上缺的两个名字。**故意 import 进本模块**（而不是把断言里的名字删掉）：
#: 这样"判定 prompt 那条路接上了没有"是一个**可判定的事实**，skip 也挂在这个事实
#: 上——路一接上（`MAX_JUDGE_HINT_LENGTH` 在、`build_judge_messages` 吃 `plugin_hints`），
#: 这些用例**自己就会开始跑**，不需要谁记得回来删 skip（那种"记得"一定会忘）。
try:  # pragma: no cover - 这条分支上走 except
    from qq_roleplay_bot.extensions import MAX_JUDGE_HINT_LENGTH
except ImportError:  # pragma: no cover
    MAX_JUDGE_HINT_LENGTH = None
try:  # pragma: no cover - 这条分支上走 except
    from qq_roleplay_bot.dialogue_judge import build_judge_messages
except ImportError:  # pragma: no cover
    build_judge_messages = None

_HINTS_READY = MAX_JUDGE_HINT_LENGTH is not None and build_judge_messages is not None

#: 为什么这些用例在这条分支上跳过（逐条带在 `unittest.skip` 的理由里）。
#: 一句话：**判定 prompt 那条路还没移植**——见模块 docstring 那张表。
_MISSING_JUDGE_HINT_SUPPORT = (
    "这条分支还没有 `extensions.MAX_JUDGE_HINT_LENGTH` / `PromptSources.judge_hints()` / "
    "`build_judge_messages(plugin_hints=…)`：它们在 stage4-plugins 上属于"
    "**判定 prompt** 那条路（Stage 3 的对话/prompt 改动），本次接缝移植的范围里没有它。"
    "断言原样保留；那条路一接上，skip 的**条件**就不成立，这些用例会自动开始跑。"
)

GROUP = "717151356"
ME = "900000001"


def message(text: str = "@她 在吗") -> IncomingMessage:
    return IncomingMessage(message_id=f"m:{text}", session_id=f"group:{GROUP}", user_id=ME,
                           text=text, target=MessageTarget(group_id=GROUP))


def context() -> PromptContext:
    return PromptContext(session_id=f"group:{GROUP}", message=message(), recent_messages=(),
                         mode="active", trigger="mention", context=None)


class LoverPlugin:
    """照"恋人插件"的形态写的最小实现（用户举的例子）。"""

    name = "lover"

    def __init__(self, *, material="他前天说过这周很忙，她答应过等他忙完再找他。",
                 hint="这一句是那个她答应过要等的人说的。", explode=False):
        self.material = material
        self.hint = hint
        self.explode = explode
        self.seen: list[object] = []

    async def build_prompt(self, ctx):
        if self.explode:
            raise RuntimeError("插件炸了")
        return self.material

    async def build_judge_hint(self, ctx):
        if self.explode:
            raise RuntimeError("插件炸了")
        return self.hint

    async def after_decision(self, ctx, decision):
        self.seen.append(decision)


# --- 1. 登记口 -----------------------------------------------------------------

def test_registry_can_register_prompt_plugins() -> None:
    """**断点①**：以前没有任何登记口，`PromptSources.plugins` 永远是空元组。

    也就是说"插件往 prompt 插内容"这条路是死的——不是没实现，是没人能接上。
    """

    from qq_roleplay_bot.plugins import PluginRegistry

    registry = PluginRegistry()
    assert registry.shared_prompts() == ()
    lover = LoverPlugin()
    registry.provide_prompts(lover)
    assert registry.shared_prompts() == (lover,)
    # 登记 None 不该进去（防御性：插件写错时不至于让汇聚口炸）
    registry.provide_prompts(None)
    assert registry.shared_prompts() == (lover,)


def test_prompt_sources_accept_plugins_added_after_construction() -> None:
    """装配顺序：插件是引擎构造**之后**才发现的，所以汇聚口必须能追加。

    用同一份**可变列表**是有意的——否则会被逼出"再构造一次 PromptSources"的别扭顺序。
    """

    sources = PromptSources()
    assert sources.plugins == []
    sources.add_plugins([LoverPlugin()])
    material = asyncio.run(sources.collect(context(), knowledge_enabled=False))
    assert len(material.plugin_fragments) == 1
    assert material.plugin_fragments[0].source == "lover"


# --- 2. 回复 prompt：材料是 DATA、有上限、可降级 -----------------------------

def test_plugin_material_is_labelled_and_bounded() -> None:
    """标来源 + 长度上限。**这两条缺一不可**：

    - 不标来源，模型分不清这是插件说的还是她自己的人设；
    - 不限长，插件能把 prompt 撑爆（或者塞一整篇"设定"）。
    """

    long_plugin = LoverPlugin(material="设定：" + "x" * (MAX_PLUGIN_PROMPT_LENGTH + 800))
    sources = PromptSources(plugins=[long_plugin])
    material = asyncio.run(sources.collect(context(), knowledge_enabled=False))
    fragment = material.plugin_fragments[0]
    assert fragment.source == "lover"
    assert len(fragment.text) <= MAX_PLUGIN_PROMPT_LENGTH


def test_a_broken_prompt_plugin_degrades_to_nothing() -> None:
    """插件抛异常 → 降级为空材料，不带走这条消息（fail-open 到"没有材料"）。"""

    sources = PromptSources(plugins=[LoverPlugin(explode=True)])
    material = asyncio.run(sources.collect(context(), knowledge_enabled=False))
    assert material.plugin_fragments == ()


# --- 3. 判定 prompt：短提示走 DATA 段 ---------------------------------------
#
# ⚠️ 这一段整段**还没接上**（见模块 docstring 那张表）：它依赖
# `extensions.MAX_JUDGE_HINT_LENGTH` / `PromptSources.judge_hints()` /
# `build_judge_messages(plugin_hints=…)`——都在"判定 prompt"那条路上，
# 本次"接缝移植"没有动它。断言原样留着，只加 skip + 原因。

@unittest.skipUnless(_HINTS_READY, _MISSING_JUDGE_HINT_SUPPORT)
def test_judge_hints_are_collected_and_bounded() -> None:
    """**断点②**：判定 agent 以前完全看不到插件材料，于是插件影响不了"说不说"。"""

    plugin = LoverPlugin(hint="y" * (MAX_JUDGE_HINT_LENGTH + 500))
    sources = PromptSources(plugins=[plugin])
    hints = asyncio.run(sources.judge_hints(context()))
    assert len(hints) == 1
    assert hints[0].source == "lover"
    assert len(hints[0].text) <= MAX_JUDGE_HINT_LENGTH, "判定那边只允许一句话"


@unittest.skipUnless(_HINTS_READY, _MISSING_JUDGE_HINT_SUPPORT)
def test_plugin_without_a_judge_hint_is_skipped() -> None:
    """不实现 `build_judge_hint` 的插件 = 没有这条——**向后兼容**，行为一个字不变。"""

    class Plain:
        name = "plain"

        async def build_prompt(self, ctx):
            return "只有回复材料"

    sources = PromptSources(plugins=[Plain()])
    assert asyncio.run(sources.judge_hints(context())) == ()
    # 回复那条照旧能用
    material = asyncio.run(sources.collect(context(), knowledge_enabled=False))
    assert material.plugin_fragments[0].source == "plain"


@unittest.skipUnless(_HINTS_READY, _MISSING_JUDGE_HINT_SUPPORT)
def test_judge_hint_lands_inside_the_untrusted_data_section() -> None:
    """提示必须落在 DATA 段**之内**，不能跑到 system 前缀里。

    这条是"人格稳定"的直接断言：插件写的东西是外部提供的，永远是不可信数据。
    """

    request = build_judge_messages(
        [],
        current=message(),
        trigger="mention",
        addressed=True,
        plugin_hints=(("lover", "他是那个她答应过要等的人。"),),
    )
    system = request[0]["content"]
    user = request[1]["content"]
    # system 前缀里不许出现插件材料
    assert "答应过要等的人" not in system
    # DATA 段里有，而且标了来源
    assert "答应过要等的人" in user
    assert "[lover]" in user
    # 落在 DATA 边界之内
    begin = user.index("--- UNTRUSTED CHAT DATA BEGIN ---")
    end = user.index("--- UNTRUSTED CHAT DATA END ---")
    assert begin < user.index("[lover]") < end, "插件材料必须夹在 DATA 边界里"


@unittest.skipUnless(_HINTS_READY, _MISSING_JUDGE_HINT_SUPPORT)
def test_broken_judge_hint_degrades_instead_of_failing_the_round() -> None:
    """插件在判定那条路上炸了，判定这一轮照样能跑（材料降级为空）。"""

    sources = PromptSources(plugins=[LoverPlugin(explode=True)])
    assert asyncio.run(sources.judge_hints(context())) == ()


@unittest.skipUnless(_HINTS_READY, _MISSING_JUDGE_HINT_SUPPORT)
def test_a_hanging_plugin_cannot_hang_the_judge_path() -> None:
    """**插件挂住 ≠ 插件报错**——审查 2026-10-01 抓到的新增路径没有超时。

    实测过的情况：回复段那条路一直有 `asyncio.wait_for(...)`，而这次新加的
    `judge_hints()` 是直接 `await`，于是一个 `await asyncio.sleep(inf)` 的
    `build_judge_hint` 会把**判定**（在每条消息的主链上）永久挂住。

    这条测试按核心的写法跑一遍，验超时真的会触发。
    """

    import asyncio as _asyncio

    class Hanging:
        name = "hang"

        async def build_prompt(self, ctx):
            await _asyncio.sleep(3600)

        async def build_judge_hint(self, ctx):
            await _asyncio.sleep(3600)  # 永不返回

    async def _collect_with_core_timeout():
        sources = PromptSources(plugins=[Hanging()])
        try:
            return await _asyncio.wait_for(sources.judge_hints(context()), timeout=0.15)
        except _asyncio.TimeoutError:
            return "TIMED_OUT"

    assert _asyncio.run(_collect_with_core_timeout()) == "TIMED_OUT", (
        "判定那条路必须能被超时掐断，否则一个坏插件能挂死整个判定")


def test_report_seams_no_longer_accepts_an_engine_shaped_object() -> None:
    """`ReportSeams(snapshot=<引擎>)` 不该还能用（审查 2026-10-01 提到的那条"放宽"）。

    以前 `snap()` 里有一段兜底：`snapshot` 不是函数、但对象上有 `.snapshot()` 方法时
    就去点它。那会让"传一个引擎进来"也能工作——正说明接缝在结构上不构成闸门。
    现在形状定死：不是可调用的就取不到（`None`）。
    """

    from qq_roleplay_bot.plugins import ReportSeams

    class EngineShaped:
        def snapshot(self):
            return "引擎形状的快照"

    assert ReportSeams(snapshot=EngineShaped()).snap() is None, (
        "不该再去点参数对象的 .snapshot() —— 传错形状就该取不到")

    # 正常形状照旧
    assert ReportSeams(snapshot=lambda: "正常").snap() == "正常"


def test_knows_reserved_tells_missing_seam_from_empty_list() -> None:
    """**"没有名单"与"没接上"必须分得清**（2026-10-01 审查点的 fail-open）。

    `reserved()` 的缺省是空集，而空集有两种成因：

    - 接缝**没接上** → 读不到名单 → 依赖它的护栏会**静默失效**（mail 的防冒充就是）；
    - 这台机器**真没配管理员** → 合法部署状态 → 功能该照常。

    第一版只看"是不是空集"，于是要么 fail-open，要么把没配管理员的机器上的邮件绑定
    整条误杀（实测踩到：两条邮件测试直接红）。`knows_reserved()` 给"来源在不在"。
    """

    from qq_roleplay_bot.plugins import ChatSeams

    # 手造的（= 接缝没接上）
    bare = ChatSeams()
    assert bare.reserved() == frozenset()
    assert bare.knows_reserved() is False, "缺省占位符必须被认出来是'没接上'"

    # 接上了、但这台机器没有管理员（合法的空列表）
    wired_empty = ChatSeams(reserved_user_ids=lambda: ())
    assert wired_empty.reserved() == frozenset()
    assert wired_empty.knows_reserved() is True, (
        "接上了就该说接上了——空集不等于没接上，否则会误杀没配管理员的部署")

    # 接上了、有名单
    wired = ChatSeams(reserved_user_ids=lambda: ["900000001"])
    assert wired.reserved() == frozenset({"900000001"})
    assert wired.knows_reserved() is True
