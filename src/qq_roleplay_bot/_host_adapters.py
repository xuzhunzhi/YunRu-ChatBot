"""把现有的那几个模块**适配成 `_host` 的接口**。

`_host` 定的是"引擎需要什么"，这里放"现有的哪个模块能满足它"。适配器只有一层薄壳，
**不含任何业务逻辑**——逻辑还在原模块里，所以行为一个字节都没变。

## 为什么要有适配器，而不是让原模块直接实现 Protocol

1. **原模块的结构不该被接口牵着走**。比如 `typing_sim.delay_plan` 是模块级函数、
   还带一堆可调参数；`_host.OutboundStyler` 是对象方法。硬把它们改成一样，
   等于让"接口"倒逼实现。
2. **装配点要能一眼看出接了什么**。`build_host_services()` 就是那张清单。
3. **换实现时只改这里**。将来把停顿换成别的节奏策略，核心一行都不用动。

## 用法（装配点）

```python
from ._host_adapters import build_host_services

engine = DialogueEngine(client, host=build_host_services())
```

要"这次故意不接某一项"，给它传 `False`（三态见 `build_host_services` 的 docstring）。

> 2026-10-02 修：这里原来写的是
> `build_host_services(call_action=call_action, transport=transport)`——
> **那两个参数在签名里根本不存在**（是更早一版的写法残留）。照它写会 `TypeError`。

**注意**：适配器里的 import 全在函数内。放模块顶部就等于又把原模块拴回底层了，
那正是这次要拆掉的东西。
"""
from __future__ import annotations

from ._host import HostServices, MachineSample


class TypingStyler:
    """`OutboundStyler` 的现有实现：`typing_sim` 的分段与停顿。

    它就是"拟人化停顿"——**出站表现**，不是"说什么"。所以它可插可拔：
    拔掉之后一次发出去，对话内容一模一样。
    """

    def split(self, text: str, *, limit: int = 3) -> tuple[str, ...]:
        from .typing_sim import split_segments

        # `typing_sim` 自己的 `MAX_SEGMENTS` 是默认值；显式传进来的 limit 优先。
        return split_segments(text, limit=limit) if limit != 3 else split_segments(text)

    def delay_plan(self, segments: tuple[str, ...] | list[str]) -> tuple[float, ...]:
        from .typing_sim import delay_plan

        return delay_plan(segments)


class HelpCardRenderer:
    """`CardRenderer` 的现有实现：`help_card` 画 PNG。

    `available` 是"这台机器有没有 Pillow 与字体"——没有就明确为假，
    调用方回纯文本，而不是给一张空图。
    """

    @property
    def available(self) -> bool:
        from .help_card import available

        return bool(available())

    def render(self, title: str, text: str, *, width: int = 880) -> bytes | None:
        from .help_card import render

        try:
            return render(title, text, width=width)
        except Exception:  # noqa: BLE001 - 画不出来就回纯文本，不让它带走一条回复
            return None


class LocalMachineProbe:
    """`MachineProbe` 的现有实现。

    ## ⚠️ 它现在**采不到东西**，而且这是显式的（2026-10-02 修）

    `MachineProbe.sample()` 是**同步**方法，而 `runtime_diagnostics` 的取数方法是
    `async def`（`processes` / `network` / `fans`）。同步方法里 await 不了，
    所以原来那版这样写：

        processes=tuple(probe.processes())     # 拿到的是**协程对象**，不是数据

    后果是**永远返回空样本**，并且在垃圾回收时留一条
    `RuntimeWarning: coroutine '...' was never awaited`。
    也就是它**假装在采**——那正是这个仓库反复栽过的"代码/文档撒谎"。

    与其继续假装，不如显式返回空样本，并把桥接留成待办：
    真要用 `host.machine` 时，得把 `MachineProbe.sample` 改成 `async`，
    或者让装配点接到一个**同步**的取样器上。
    `AGENTS.md` §3.3 记着 `host.machine` 目前**没有消费者**
    （`runtime.py` 里只有一处赋值、零处读取），所以现在没人会读到这个空样本。

    `diagnostics` 参数保留，是为了让已有的装配行（`runtime.py` 里那句）继续成立；
    **它目前不被使用**——不要以为传进来就会采到东西。
    """

    def __init__(self, diagnostics=None) -> None:
        self._diagnostics = diagnostics

    def sample(self) -> MachineSample:
        # 显式空样本：不调用那些 async 取数方法（调了只会拿到没 await 的协程）。
        return MachineSample()


class HostMachineProbe:
    """机器诊断的**可插默认值**：有 `runtime_diagnostics` 就用它，没有就采不到。

    与 `LocalMachineProbe` 的区别：那个是"拿到了诊断对象、把它的取数方法包一层"；
    这个是"连诊断模块都可能在别处"。引擎默认用它——所以删掉 `runtime_diagnostics.py`
    时，`/super processes|lan|fan` 会如实回"采不到"，而不是让核心起不来。

    接口与 `runtime_diagnostics.LocalRuntimeDiagnostics` 一致（`processes` / `network`
    / `fans`），这样命令那一侧不用改。
    """

    def __init__(self) -> None:
        self._inner = None
        self._tried = False

    def _probe(self):
        if not self._tried:
            self._tried = True
            try:
                from .runtime_diagnostics import LocalRuntimeDiagnostics

                self._inner = LocalRuntimeDiagnostics()
            except Exception:  # noqa: BLE001 - 没有这项能力就采不到，不是错误
                self._inner = None
        return self._inner

    async def processes(self, mode: str = "") -> object:
        probe = self._probe()
        if probe is None:
            return []
        return await probe.processes(mode)

    async def network(self) -> object:
        probe = self._probe()
        if probe is None:
            return {}
        return await probe.network()

    async def fans(self) -> object:
        probe = self._probe()
        if probe is None:
            return []
        return await probe.fans()

    def ensure_sampler(self) -> None:
        probe = self._probe()
        if probe is not None and hasattr(probe, "ensure_sampler"):
            probe.ensure_sampler()


class FileAuditSink:
    """`AuditSink` 的现有实现：`control_audit.ControlAudit`（落 `data/`）。"""

    def __init__(self, audit=None) -> None:
        self._audit = audit

    def _sink(self):
        if self._audit is None:
            from .control_audit import ControlAudit

            self._audit = ControlAudit()
        return self._audit

    def record(self, action: str, *, detail: dict[str, object] | None = None,
               result: str = "", source: str = "", token: str = "") -> None:
        self._sink().record(action, detail=detail, result=result, source=source, token=token)

    def tail(self, count: int = 50) -> list[dict[str, object]]:
        return self._sink().tail(count)


def _choose(value, default_factory, empty_factory):
    """三态选择：**没指定 / 故意不接 / 用这个**。

    这个形状是为了让 `build_host_services` docstring 里那句"显式传 `False` 表示
    这次故意不接"**变成真的**。2026-10-02 之前代码是
    `TypingStyler() if styler is None else styler`，于是传 `False` 会把 `False`
    **原样装进** `HostServices`（之后 `self.host.styler.split(...)` 就
    `AttributeError`），而只有 `balance` / `roles` / `knowledge` 走 `x or None`
    才被归一。外部审查第四轮点的就是这处"文档与实参矛盾"。

    ## ⚠️ 只认**字面** `False`，别的假值一律原样使用

    判据是 `value is False`，所以 `0` / `""` / `[]` / `False` 的字面量以外，
    那些"看着像不接"的假值会被**原样装进去**：

        传 0      → host.styler is 0      → 之后 .split(...) AttributeError
        传 ""     → host.styler is ""
        传 []     → host.styler is []

    这不是文档撒谎（docstring 只承诺三态），但**是个能炸的写法**，
    所以写在这里明说。要"不接"就写 `False`，别写 `0`。
    外部审查第五轮实测过这四种输入，报告里要求"任选一个修法，但别留着不说"——
    这里选了"说清楚"，因为收紧成 `value is False or value is None` 会把
    "两态"和"三态"混起来，反而更容易误用。
    """

    if value is None:
        return default_factory()
    if value is False:
        return empty_factory()
    return value


def build_host_services(*, styler=None, cards=None, machine=None, audit=None,
                        balance=None, roles=None, knowledge=None) -> HostServices:
    """装配点的**唯一入口**：决定接哪些能力上去。

    **每一项都可选，三种取值**：

    | 传什么 | 结果 |
    | --- | --- |
    | 不传 / `None` | 用**这一侧现成的实现**（`TypingStyler` / `HelpCardRenderer` / `LocalMachineProbe` / `FileAuditSink`） |
    | 显式 `False` | **这次故意不接**，换成空实现（`_host.NoStyler` 等）——用来验证"没有它也能跑" |
    | 一个对象 | 就用它 |

    `balance` / `roles` / `knowledge` 三项**缺省是"没有这项能力"**（`None`），
    而不是空实现——`HostServices` 的类 docstring 讲了为什么要分开：
    有 `BalanceSource` 才注册 `/balance`，空实现会让调用方以为"能做但结果为空"。
    这三项传 `False` 同样归一到 `None`。它们各自有开关
    （`QQBOT_BALANCE_*`、角色查询在不在、知识库配没配），由调用方判断后传进来，
    不在这里硬编码。
    """

    from ._host import NoAudit, NoCards, NoMachine, NoStyler

    return HostServices(
        styler=_choose(styler, TypingStyler, NoStyler),
        cards=_choose(cards, HelpCardRenderer, NoCards),
        machine=_choose(machine, LocalMachineProbe, NoMachine),
        audit=_choose(audit, FileAuditSink, NoAudit),
        balance=balance or None,
        roles=roles or None,
        knowledge=knowledge or None,
    )
