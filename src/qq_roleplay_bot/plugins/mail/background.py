"""mail 的后台实现。

**为什么这个文件在插件目录里**（2026-10-04）：它原来住在核心的
`background_plugins.py` 里，插件只是 `from ...background_plugins import MailChannelPlugin`
把它捞出来用——那等于**要求本体替插件携带实现**，与用户定下的规矩相悖：

> "插件是不需要本体做出改动的，本体在不动接口的情况下改动也不会影响插件"

搬过来之后，插件自包含；本体只留**框架**（`BackgroundPlugin` 协议 /
`plugin_enabled` / `build_background_plugins` / 节拍 `run_background_plugin`）。
"""

from __future__ import annotations


class MailChannelPlugin:
    """读信回信（`mail_channel.py`）。它走引擎的公开接缝，本来就不碰 transport。"""

    name = "mail-channel"

    def __init__(self, channel, *, interval_seconds: float) -> None:
        self.channel = channel
        self.interval_seconds = float(interval_seconds)
        self.enabled = channel is not None

    async def poll_once(self) -> object:
        return await self.channel.poll_once()


class DailyReportPlugin:
    """每日汇报（`daily_report.py`）：到点就写一封发出去。

    "到点"由 `DailyReporter.due()` 判——所以这里只是个包装，不是调度实现。

    拿的是**窄接缝** `report`（`plugins.ReportSeams`），不是引擎：
    日报只用得到快照 / 模型 client / 记账 / 写信记录 / 记忆五项。
    """

    name = "daily-report"

    def __init__(self, reporter, report, *, interval_seconds: float) -> None:
        self.reporter = reporter
        self.report = report
        self.interval_seconds = float(interval_seconds)
        self.enabled = reporter is not None

    async def poll_once(self) -> object:
        if self.reporter is None or not self.reporter.due():
            return None
        return await self.reporter.run_once(self.report)
