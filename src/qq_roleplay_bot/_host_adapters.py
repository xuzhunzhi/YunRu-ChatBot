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

engine = DialogueEngine(
    client,
    host=build_host_services(call_action=call_action, transport=transport),
)
```

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
    """`MachineProbe` 的现有实现：`runtime_diagnostics.LocalRuntimeDiagnostics`。

    采不到就返回空样本——**不编数字**。
    """

    def __init__(self, diagnostics=None) -> None:
        self._diagnostics = diagnostics

    def sample(self) -> MachineSample:
        from .runtime_diagnostics import LocalRuntimeDiagnostics

        probe = self._diagnostics
        if probe is None:
            probe = LocalRuntimeDiagnostics()
            self._diagnostics = probe
        try:
            return MachineSample(
                processes=tuple(probe.processes()),
                network=dict(probe.network()),
                fans=tuple(probe.fans()),
            )
        except Exception:  # noqa: BLE001 - 诊断挂了不该带走对话
            return MachineSample()


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


def build_host_services(*, styler=None, cards=None, machine=None, audit=None,
                        balance=None, roles=None, knowledge=None) -> HostServices:
    """装配点的**唯一入口**：决定接哪些能力上去。

    **每一项都是可选参数**，不传就用现有的实现；显式传 `False` 表示"这次故意不接"
    （用来验证"没有它也能跑"）。`balance` / `roles` / `knowledge` 三项默认不接——
    它们各自有开关（`QQBOT_BALANCE_*`、角色插件在不在、知识库配没配），
    由调用方判断后传进来，而不是在这里硬编码。
    """

    return HostServices(
        styler=TypingStyler() if styler is None else styler,
        cards=HelpCardRenderer() if cards is None else cards,
        machine=LocalMachineProbe() if machine is None else machine,
        audit=FileAuditSink() if audit is None else audit,
        balance=balance or None,
        roles=roles or None,
        knowledge=knowledge or None,
    )
