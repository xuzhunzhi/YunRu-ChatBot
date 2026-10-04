from __future__ import annotations

import logging
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

        `knowledge_enabled=False` 时**根本不查知识库**——判定说"这一句不在问她的世界"
        就别去翻资料：实测不做门时 79 条真实闲聊里 60% 会召回世界观文本（平均 1300 字），
        白白占 prompt 还容易把话题带偏（`data/lore_gate_eval.py`）。
        """

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
            try:
                results = await self.knowledge_base.search(
                    _bounded(context.message.text, 1000),
                    limit=MAX_KNOWLEDGE_ITEMS,
                )
            except Exception:
                logger.exception("Stage 3 knowledge lookup failed")
                results = []
            if not isinstance(results, list):
                results = []
            for item in results[:MAX_KNOWLEDGE_ITEMS]:
                if not isinstance(item, KnowledgeItem):
                    continue
                knowledge_items.append(
                    KnowledgeItem(
                        source=_bounded(item.source, 200),
                        title=_bounded(item.title, 300),
                        content=_bounded(item.content, MAX_KNOWLEDGE_ITEM_LENGTH),
                    )
                )

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
