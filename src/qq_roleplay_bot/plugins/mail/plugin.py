"""邮件通道插件：读信回信 + 每日汇报 + **把发信能力开放给别的插件**。

两条**后台插件**，一块是"有人写信来就回"，一块是"到点写一封信发出去"。
装配逻辑在 `wire.py`（从 `background_plugins.py` 原样搬来）。

还有一件事**不是后台节拍**：它把"给操作者发一封普通邮件"经 `registry.mail`
（`plugins.MailSeams`）**提供出去**，给后置插件用——掉线通知（`plugins/outage_notice/`）
就是这么拿到发信能力的。**方向只有一个**：本插件提供，别的插件使用；
mail 这一侧不认识任何使用者的名字（用户 2026-10-06 点名的那条）。

**经模块调用 `wire.build_*`，不要 `from .wire import build_*`**：后者会把函数绑进本模块
的命名空间，于是"换掉 `wire.build_daily_report`"这件事对它无效——测试里踩到过，
表现是补丁打了却完全没生效。
"""
from __future__ import annotations

from .background import DailyReportPlugin, MailChannelPlugin
from . import wire

#: 面板上的显示名（`plugins.inventory()` 读它）。
TITLE = "邮件"


def register(registry) -> None:
    # **渠道自己声明**"我说的话可以算主人"（2026-10-06）：核心原来在 `runtime.py` 里
    # 写死 `_OWNER_CHANNELS = ("mail",)`——核心知道有一个叫 mail 的渠道。现在反过来，
    # 渠道在 `register()` 里说一句，核心只问"这个渠道声明过吗"。
    # 不接也能跑（核心那份过渡引导项还留着，行为与改之前一致），接了才是正确形状。
    # 声明**不授予任何命令权限**，最终仍要同时满足 `claims_owner is True`——
    # 判定在核心（`runtime._SeamBinder`），见 `plugins.provide_owner_channel`。
    registry.provide_owner_channel("mail")

    # **M1（2026-10-22）：这两条现在通常是 no-op**——核心已经在 `build_engine()` 里、
    # 插件 attach 之前把同两条窄口放到了 `registry.mail` 上。这里仍然调它们，是为了
    # 保住"没有核心那份时的兼容路径"（老核心 + 新插件）与 M4 删目录之前的行为：
    # builder 看到"已经有人提供过"就直接返回现有函数，**不重复提供、不报错**。
    #
    # 为什么不再由本插件当唯一提供者：判据是"删掉 `plugins/mail/` 之后
    # `outage_notice` / `timeout_alert` 仍能装、仍能发信"。把能力留在插件里，
    # 删目录就等于告警哑了；M1 之后依赖按能力判定（`REQUIRES = ("mail",)` →
    # `PluginRegistry.has_capability`），不再要求这个目录存在。
    #
    # 方向仍然只有一个：**提供者放函数，使用者取函数**；mail 不复述使用者的名字。
    wire.build_operator_mail_sender(registry.mail)

    # **第二条窄口**：把"寄给指定的一个地址"也开放出去（2026-10-08）。
    # 到点提醒（`plugins/remind/`）的邮箱那一路要寄给**那个人**，而上面那一条的
    # 收件人写死是操作者——两条能力各管各的、互不替换。M1 之后同样由核心先提供。
    wire.build_address_mail_sender(registry.mail)

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
