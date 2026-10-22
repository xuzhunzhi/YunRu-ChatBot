from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .security import sanitize_chat_text
from .transport import IncomingMessage

if TYPE_CHECKING:
    from .stage3_runtime import ContextState, DialogueDecision


logger = logging.getLogger(__name__)

MAX_PLUGIN_PROMPT_LENGTH = 2000
MAX_EXTRA_PROMPT_LENGTH = 4000
MAX_KNOWLEDGE_ITEMS = 5
MAX_KNOWLEDGE_ITEM_LENGTH = 3000
#: `QQBOT_KNOWLEDGE_ITEMS` 的合法范围：上限仍是"最多给几条"，下限 1（0 条等于关掉知识库，
#: 要关就用别的开关，别用这个数表达）。
KNOWLEDGE_ITEM_LIMIT_MIN = 1
KNOWLEDGE_ITEM_LIMIT_MAX = 10


def knowledge_item_limit() -> int:
    """每一轮最多给回复 agent 几条知识（`QQBOT_KNOWLEDGE_ITEMS`，默认 5，夹在 1..10）。

    由来（2026-10-18）：`limit=5` 是在"判定说 YES 才查"的时代定的；改成**每轮都查**之后，
    名额会不会太多要拿实测的注入字数判断（用户：如果 p90 注入字数 > 3000 就把上限降到 3）。
    所以把它做成**调用时现读**的配置，不重启就能改。
    """

    raw = os.environ.get("QQBOT_KNOWLEDGE_ITEMS", "").strip()
    if not raw:
        return MAX_KNOWLEDGE_ITEMS
    try:
        value = int(raw)
    except ValueError:
        return MAX_KNOWLEDGE_ITEMS
    return max(KNOWLEDGE_ITEM_LIMIT_MIN, min(value, KNOWLEDGE_ITEM_LIMIT_MAX))


@dataclass(frozen=True, slots=True)
class PromptContext:
    """供扩展读取的当前事件视图；扩展不能直接修改 Stage 3 状态。"""

    session_id: str
    message: IncomingMessage
    recent_messages: tuple[IncomingMessage, ...]
    mode: str
    trigger: str
    context: "ContextState"


@dataclass(frozen=True, slots=True)
class PromptFragment:
    """来自插件的补充材料，会被当作不可信 DATA 放入 user prompt。"""

    source: str
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    source: str
    title: str
    content: str
    #: 检索侧判的**权威度**（官方 5 / 面板 4 / 官方补充 3 / 同人 2 / 世界知识 1）。
    #: 2026-10-18 加：最终分 = 相关程度 × 权威度，所以权威度必须**跟着条目走**——
    #: 模型判档那一层在 `PromptSources` 之后，只看得到 `KnowledgeItem`。
    #: 0 = 不知道（测试/外部实现的假条目），排序时按 1 处理，不会把它算没。
    authority: int = 0
    #: 检索侧判的**相关程度**（0/1/2，见 `knowledge_base.relevance_tier`）。
    #: 模型判档那一层的兜底：模型没给某一块/整次解析失败时用它。
    tier: int = 0


@dataclass(frozen=True, slots=True)
class PromptMaterial:
    """一次模型调用使用的可选扩展材料。"""

    plugin_fragments: tuple[PromptFragment, ...] = ()
    knowledge_items: tuple[KnowledgeItem, ...] = ()
    extra_prompt: str = ""


class PromptPlugin(Protocol):
    """插件只提供补充 DATA 或接收结果通知，不能改写安全策略和发送目标。"""

    name: str

    async def build_prompt(self, context: PromptContext) -> str | None:
        """返回有限长度的补充材料；返回值不会成为 system prompt。"""

    async def after_decision(self, context: PromptContext, decision: "DialogueDecision") -> None:
        """观察模型决定，可用于记录或外部索引，不得改变决定。"""


class KnowledgeBase(Protocol):
    async def search(self, query: str, *, limit: int) -> list[KnowledgeItem]:
        """按当前消息检索知识；知识内容会被当作不可信 DATA。"""


class ExtraPromptProvider(Protocol):
    async def get_prompt(self, context: PromptContext) -> str | None:
        """提供补充 prompt 材料；不能覆盖固定 system prompt。"""


def _bounded(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return sanitize_chat_text(value, max_length=limit).strip()


class PromptSources:
    """Stage 3 的扩展汇聚端口。

    这里刻意不允许扩展修改 system prompt、会话状态、白名单或出站目标。
    任何扩展异常都会降级为空材料，避免插件或知识库故障扩大为消息处理故障。
    """

    def __init__(
        self,
        *,
        plugins: "tuple[PromptPlugin, ...] | list[PromptPlugin]" = (),
        knowledge_base: KnowledgeBase | None = None,
        extra_prompt_provider: ExtraPromptProvider | None = None,
    ) -> None:
        # **用 list 而不是 tuple**：插件是装配之后才发现的（`attach_plugins` 跑在
        # 引擎构造之后），所以这里是**同一份可变列表**，`add_plugins()` 往里追加，
        # 引擎手上那份立刻就能看到。用 tuple 会逼出"再构造一次 PromptSources"的
        # 别扭顺序（2026-10-01 改）。
        self.plugins: list[PromptPlugin] = list(plugins)
        self.knowledge_base = knowledge_base
        self.extra_prompt_provider = extra_prompt_provider

    def add_plugins(self, plugins: "tuple[PromptPlugin, ...] | list[PromptPlugin]") -> None:
        """追加 prompt 扩展（`runtime.build_engine` 在发现插件之后调）。"""

        self.plugins.extend(plugins)

    async def collect(self, context: PromptContext, *, knowledge_enabled: bool = True) -> PromptMaterial:
        """汇聚一次调用的扩展材料。

        **`knowledge_enabled` 已经不再是"判定说要不要查"那道门**（2026-10-18 用户裁定：
        「我不介意查库，消息积压了就多条一判，ds速度是够的」）。引擎**每一轮 REPLY 都查**，
        由**相关度**决定给她看哪几条（`knowledge_base.relevance_tier`：0 档一条都不取）。
        参数保留只为端口层（测试里显式关掉知识库用），**生产路径不再传它**。

        为什么废掉那道门：判定口径写着"议论她本人、@ 她、提她名字都不算"，于是
        "问她自己的过去"这条路**系统性不检索**——实测 1,579 条判定里 NO 1,411 条
        （89.4%）、YES 只有 20 条（1.3%）。现在改成"永远查 + 按相关度筛"。

        `limit` 走 `knowledge_item_limit()`（`QQBOT_KNOWLEDGE_ITEMS`，默认 5）。
        """

        limit = knowledge_item_limit()
        fragments: list[PromptFragment] = []
        for plugin in self.plugins:
            try:
                value = await plugin.build_prompt(context)
            except Exception:
                logger.exception("Stage 3 plugin prompt failed: plugin=%s", getattr(plugin, "name", "unknown"))
                continue
            text = _bounded(value, MAX_PLUGIN_PROMPT_LENGTH)
            if text:
                fragments.append(PromptFragment(getattr(plugin, "name", "plugin"), text))

        knowledge_items: list[KnowledgeItem] = []
        if self.knowledge_base is not None and knowledge_enabled:
            lookup_ok = True
            try:
                results = await self.knowledge_base.search(
                    _bounded(context.message.text, 1000),
                    limit=limit,
                )
            except Exception:
                logger.exception("Stage 3 knowledge lookup failed")
                results = []
                lookup_ok = False
            if not isinstance(results, list):
                results = []
            for item in results[:limit]:
                if not isinstance(item, KnowledgeItem):
                    continue
                knowledge_items.append(
                    KnowledgeItem(
                        source=_bounded(item.source, 200),
                        title=_bounded(item.title, 300),
                        content=_bounded(item.content, MAX_KNOWLEDGE_ITEM_LENGTH),
                        # 权威度与档位要跟着条目走（模型判档那一层在 collect 之后，
                        # 只看得到 KnowledgeItem；见 `KnowledgeItem.authority` 的说明）。
                        authority=int(getattr(item, "authority", 0) or 0),
                        tier=int(getattr(item, "tier", 0) or 0),
                    )
                )
            # **她的档案兜底**（2026-10-18 用户定）：这一轮一条都没留下（= 所有候选都在 0 档，
            # 本来会输出空态）时，追一条 ≤200 字的"基本情况"。
            #
            # 三条边界：
            # * **不算进那 5 条名额**——它是兜底，不是检索结果（`results[:limit]` 已经走完，
            #   这里 append 之后不再截断）；
            # * 有 ≥1 档命中就**不**注入（不跟正常检索重复）；
            # * **查库本身失败时不注入**（`lookup_ok`）：那种"没料"是故障，不是"全 0 档"，
            #   而 `tests/test_knowledge_base.py` 也从 2026-09-28 起钉着"异常必须降级为空"。
            if lookup_ok and not knowledge_items:
                from .knowledge_base import profile_fallback_item  # noqa: PLC0415 - 避免循环 import

                knowledge_items.append(profile_fallback_item())

        extra_prompt = ""
        if self.extra_prompt_provider is not None:
            try:
                extra_prompt = _bounded(
                    await self.extra_prompt_provider.get_prompt(context),
                    MAX_EXTRA_PROMPT_LENGTH,
                )
            except Exception:
                logger.exception("Stage 3 extra prompt lookup failed")

        return PromptMaterial(tuple(fragments), tuple(knowledge_items), extra_prompt)

    async def notify(self, context: PromptContext, decision: "DialogueDecision") -> None:
        for plugin in self.plugins:
            hook = getattr(plugin, "after_decision", None)
            if not callable(hook):
                continue
            try:
                await hook(context, decision)
            except Exception:
                logger.exception("Stage 3 plugin notification failed: plugin=%s", getattr(plugin, "name", "unknown"))
