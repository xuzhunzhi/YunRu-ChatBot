"""邮件通道插件：读信回信 + 每日汇报。

两条**后台插件**，一块是"有人写信来就回"，一块是"到点写一封信发出去"。
装配逻辑在 `wire.py`（从 `background_plugins.py` 原样搬来）。

**经模块调用 `wire.build_*`，不要 `from .wire import build_*`**：后者会把函数绑进本模块
的命名空间，于是"换掉 `wire.build_daily_report`"这件事对它无效——测试里踩到过，
表现是补丁打了却完全没生效。
"""
from __future__ import annotations

from ...background_plugins import DailyReportPlugin, MailChannelPlugin
from . import wire


def register(registry) -> None:
    # 读信回信：只拿窄接缝 `chat`（四个函数），**不拿引擎**。
    channel = wire.build_mail_channel(registry.chat)
    if channel is not None:
        registry.background(
            MailChannelPlugin(channel, interval_seconds=_poll_seconds()))

    # 每日汇报：走窄接缝 `report`（快照 / 模型 client / 记账 / 写信记录 / 记忆），
    # 也**不拿引擎**——那五项里没有一样是权限名单。
    reporter = wire.build_daily_report(registry.report, registry=registry)
    if reporter is not None:
        registry.background(
            DailyReportPlugin(reporter, registry.report,
                              interval_seconds=wire.REPORT_TICK_SECONDS))


def _poll_seconds() -> float:
    from ... import dev_config

    return float(dev_config.MAIL_POLL_SECONDS)
