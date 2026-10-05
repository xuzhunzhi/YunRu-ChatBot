"""join_approval 的后台实现。

**为什么这个文件在插件目录里**（2026-10-04）：它原来住在核心的
`background_plugins.py` 里，插件只是 `from ...background_plugins import JoinApprovalPlugin`
把它捞出来用——那等于**要求本体替插件携带实现**，与用户定下的规矩相悖：

> "插件是不需要本体做出改动的，本体在不动接口的情况下改动也不会影响插件"

搬过来之后，插件自包含；本体只留**框架**（`BackgroundPlugin` 协议 /
`plugin_enabled` / `build_background_plugins` / 节拍 `run_background_plugin`）。
"""

from __future__ import annotations


class JoinApprovalPlugin:
    """入群申请自动审批（`join_approval.py`）。

    它是**策略**那一半：黑名单/白名单/正则怎么判、同一批别重复处理、每轮最多几条。
    读申请、批/拒、通知超管走核心注入的 `call_action` / `notify`——
    插件手上没有 transport，也就没法绕开闸门与审计。
    """

    name = "join-approval"

    def __init__(self, poller, *, interval_seconds: float, enabled: bool = True) -> None:
        self.poller = poller
        self.interval_seconds = float(interval_seconds)
        self.enabled = bool(enabled) and poller is not None

    async def poll_once(self) -> object:
        return await self.poller.tick()
