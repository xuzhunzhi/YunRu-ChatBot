"""宿主接口：引擎要用、但**自己不实现**的那些能力。

## 为什么要有这一层

引擎（Stage 3）干的是"说不说、说什么、记什么、拦什么"。可它有五件事做不到，
也不该由它来做：

| 它需要 | 为什么不是 Stage 3 的事 |
| --- | --- |
| 把话**发出去**的样子（分段、停顿） | 这是出站表现，属于渠道 |
| 把帮助画成**图片** | 这是渲染，属于渠道 |
| 知道这台机器的**进程/网卡/风扇** | 这是运维 |
| 把操作**记进审计** | 这是运维 |
| 查**余额**、查**知识库**、查**她自己的群角色** | 各自是独立能力 |

以前这五件事是**直接 import** 进来的：`stage3_main` 顶部 `from .typing_sim import …`，
`runtime` 顶部 `from .vision import …`。后果是**删掉任何一个，核心都起不来**——
哪怕删掉的只是"帮助卡片长什么样"。

现在翻转过来：**引擎只说"我需要一个 `MachineProbe`"，实现由装配点给进来**；
谁都不给的时候用 `stubs.py` 里的空实现，引擎退化成"没这项能力"，
而**不是 ImportError**。

## 判据仍然只有一条

> 删掉它，Stage 3 会不会出问题。

按这条判据，本文件里的东西**都不是底层的必需品**，它们是**可插的能力**。
反过来也成立：任何一个 `_host` 实现被删掉，Stage 3 都必须照跑。

## 怎么用

```python
class DialogueEngine:
    def __init__(self, ..., host: HostServices | None = None) -> None:
        self.host = host or HostServices()      # 全空实现

# 要用的时候
for delay in self.host.styler.delay_plan(segments):
    await asyncio.sleep(delay)
```

**不要**在核心模块顶部 `from qq_roleplay_bot.typing_sim import …`——
那样又把它拴回底层了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "OutboundStyler",
    "CardRenderer",
    "MachineProbe",
    "MachineSample",
    "AuditSink",
    "BalanceSource",
    "RoleLookup",
    "KnowledgeIndex",
    "KnowledgeHit",
    "HostServices",
    "NoStyler",
    "NoCards",
    "NoMachine",
    "NoAudit",
]


# --- 1. 出站表现：分段与停顿 ------------------------------------------------

class OutboundStyler(Protocol):
    """把一段回话切成几段，并给出每段之间的停顿秒数。

    "怎么发出去"（拟人化停顿、分段）是**出站表现**，不是"说什么"。
    """

    def split(self, text: str, *, limit: int = 3) -> tuple[str, ...]:
        """切成最多 `limit` 段。切不动就原样返回一段。"""

    def delay_plan(self, segments: tuple[str, ...] | list[str]) -> tuple[float, ...]:
        """每段之前的等待秒数（长度与 `segments` 相同）。"""


# --- 2. 出站渲染：帮助卡片 --------------------------------------------------

class CardRenderer(Protocol):
    """把标题 + 正文画成一张图。画不出来时返回 None（调用方回纯文本）。"""

    @property
    def available(self) -> bool:
        """这台机器能不能画（字体在不在）。False 时不要调 `render`。"""

    def render(self, title: str, text: str, *, width: int = 880) -> bytes | None:
        """返回 PNG 字节。"""


# --- 3. 这台机器本身 --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MachineSample:
    """一次机器采样。**任何一项都允许为空**——采不到就不报，不猜。"""

    processes: tuple[dict[str, object], ...] = ()
    network: dict[str, object] = field(default_factory=dict)
    fans: tuple[dict[str, object], ...] = ()


class MachineProbe(Protocol):
    """进程 / 网卡 / 风扇。运维用，不是对话用。"""

    def sample(self) -> MachineSample:
        """现取一次样本。"""


# --- 4. 审计 ----------------------------------------------------------------

class AuditSink(Protocol):
    """操作审计。**只记录，不参与判定**。"""

    def record(self, action: str, *, detail: dict[str, object] | None = None,
               result: str = "", source: str = "", token: str = "") -> None:
        """记一条。`token` 只用于算指纹，**原文不许落盘**。"""

    def tail(self, count: int = 50) -> list[dict[str, object]]:
        """最近若干条（面板的"操作记录"读它）。"""


# --- 5. 余额 ----------------------------------------------------------------

class BalanceSource(Protocol):
    """余额查询。查不到就抛异常，由调用方回一句人话。"""

    def query(self) -> dict[str, object]:
        """返回 `{"available": ..., "used": ..., ...}`（金额单位由实现决定）。"""


# --- 6. 她自己的群角色 ------------------------------------------------------

class RoleLookup(Protocol):
    """她在某个群里是 owner / admin / member。

    **查不到必须返回 `unknown`，绝不猜"大概是群主"**（fail-closed）——
    群主动作靠它放行。
    """

    async def role(self, group_id: str) -> str:
        """`owner` / `admin` / `member` / `unknown`。"""

    async def self_id(self) -> str:
        """她自己的 QQ 号；拿不到返回空串。"""


# --- 7. 知识检索 ------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    """一条检索命中。`text` 会作为**不可信 DATA** 进 prompt，绝不是指令。"""

    text: str
    source: str = ""
    score: float = 0.0


class KnowledgeIndex(Protocol):
    """长期知识检索（RAG）。没有它，对话照常，只是少一层背景。"""

    def search(self, query: str, *, limit: int = 5) -> tuple[KnowledgeHit, ...]:
        """按语义取最相关的几条。"""


# --- 空实现：谁都装时的默认 ------------------------------------------------
#
# 它们**不报错、不做任何事**，只保证"没装这项能力"时引擎还能正常走完：
# 一次发出去不再停顿、卡片画不出来回纯文本、采样是空的、审计不记。


class NoStyler:
    """不分段、不停顿：一次发出去。"""

    def split(self, text: str, *, limit: int = 3) -> tuple[str, ...]:
        return (text,) if text else ()

    def delay_plan(self, segments: tuple[str, ...] | list[str]) -> tuple[float, ...]:
        return tuple(0.0 for _ in segments)


class NoCards:
    """画不出来。调用方据此回纯文本。"""

    @property
    def available(self) -> bool:
        return False

    def render(self, title: str, text: str, *, width: int = 880) -> bytes | None:
        return None


class NoMachine:
    """采不到任何样本（不猜、不编）。"""

    def sample(self) -> MachineSample:
        return MachineSample()


class NoAudit:
    """不记审计。"""

    def record(self, action: str, *, detail: dict[str, object] | None = None,
               result: str = "", source: str = "", token: str = "") -> None:
        return None

    def tail(self, count: int = 50) -> list[dict[str, object]]:
        return []


# --- 汇总 -------------------------------------------------------------------

@dataclass(slots=True)
class HostServices:
    """装配点给引擎的一包能力。**每一项都可缺省**（缺省即空实现）。

    引擎只认这个对象，不认具体是谁实现的——换一套实现（比如把停顿换成别的
    节奏策略）不需要改引擎一行。
    """

    styler: OutboundStyler = field(default_factory=NoStyler)
    cards: CardRenderer = field(default_factory=NoCards)
    machine: MachineProbe = field(default_factory=NoMachine)
    audit: AuditSink = field(default_factory=NoAudit)
    #: 下面三项缺省是 `None`：**"没有这项能力"和"有一个什么都不做的实现"要分得开**。
    #: 区分它们是有用的——有 `BalanceSource` 才会注册 `/balance` 这条命令，
    #: 有 `RoleLookup` 才谈得上群主动作。空实现会让调用方以为"能做但结果为空"。
    balance: BalanceSource | None = None
    roles: RoleLookup | None = None
    knowledge: KnowledgeIndex | None = None
