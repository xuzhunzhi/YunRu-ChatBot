"""超时通知插件的装配点：读开关、取两个接缝、挂上后台节拍。

用户 2026-10-12 的原话就是这里的规格（见包的 docstring）：短时间内频繁超时，
就发一封邮件通知操作者。

## 装配只做四件事，且顺序固定

1. 读开关（`QQBOT_MAIL_TIMEOUT_ALERT`，缺省开）；
2. **取** mail 放上来的发信能力（`registry.mail.operator_sender`）；
3. **取**核心给的只读快照口（`registry.report.snap`）；
4. 把后台节拍登记上去（`registry.background(...)`）。

## 为什么"取不到就不登记"（而不是登记一个每次记日志的节拍）

`REQUIRES = ("mail",)` 保证 mail 的 `register()` 已经跑完，所以正常情况下发信能力就在。
真取不到只有两种可能：**没有 mail 插件**（那本插件在 `REQUIRES` 那一步就被跳过了，
根本走不到这里），或者**这个部署的 mail 没配收件人**
（`wire.build_operator_mail_sender` 返回 `None`，能力是空的）。后一种情况下挂一个
"每隔一分钟记一行发不出去"的节拍只是往日志里灌噪音——**明确地不接**比
"接了但永远失败"诚实，而且那两行 `logger.warning`（"超时通知这次没接上"）
必须是可搜到的，不是静默。快照口取不到是同一道理：没有快照就数不出超时，
硬挂上去只会变成"看着装了、其实不会响"的告警。

## 为什么这里不 import mail 的任何模块

只认 `registry.mail` 这个**接缝**（`plugins.MailSeams`）：mail 里的 `MailClient` /
`wire` / 配置长什么样，本插件一个字都不知道，也不该知道。这样
"删掉 `plugins/timeout_alert/` 不影响 mail 与核心"是**结构上**成立的，
反过来"mail 换成别的实现"也只要求它继续 `provide` 同一个函数。
"""
from __future__ import annotations

import logging

from . import alert as alert_module
from .background import TimeoutAlertPlugin

logger = logging.getLogger(__name__)

#: **前置插件**：发信能力由 `mail` 提供，方向不许反（同 `outage_notice` 的规矩）。
#: `discover()` 会先装 mail，mail 装不上就跳过本插件（并记一行
#: `plugin_skipped_missing_dependency`），核心照常跑。
REQUIRES = ("mail",)

#: 面板上的显示名（`plugins.inventory()` 读它；必须与模块名不同）。
TITLE = "超时通知"


def register(registry) -> None:
    """把"每看一次快照"接到 mail 的发信能力上。取不到东西就明确不接。"""

    if not alert_module.enabled():
        logger.info("超时通知未启用（QQBOT_MAIL_TIMEOUT_ALERT=0）")
        return

    # **前置给的能力，取不到就退回"这个功能这次没有"，绝不半挂着**。
    send = getattr(registry.mail, "operator_sender", None)
    if not callable(send):
        logger.warning("超时通知这次没接上：mail 没提供发信能力（没配收件人？）")
        return

    # 只读快照口由核心从 `runtime.report_seams()` 放上来，拿不到就数不出超时。
    # 看的是 `ReportSeams.snapshot` 那个**字段**，不是 `snap()` 这个方法：
    # 方法**总是**在——核心没接上快照时它也只是每次回 `None`——拿"方法在不在"
    # 当判据，就会装出一个"看着接了、其实永远数不出东西"的告警（最糟的失败方式：
    # 安静地不报警）。`plugin_support.plugin_registry()` 造的就是那种没接上的注册表。
    if not callable(getattr(registry.report, "snapshot", None)):
        logger.warning("超时通知这次没接上：核心没给只读快照（registry.report.snapshot）")
        return
    snap = registry.report.snap

    window = alert_module.window_seconds()
    interval = alert_module.poll_seconds()
    if interval * 2.0 > window:
        # 采样太粗 = 窗口里可能凑不到两个采样点 = 什么都判不出来。不拦，但要说出来。
        logger.warning("超时通知的采样间隔（%.0f 秒）相对窗口（%.0f 秒）太粗，"
                       "会数不准（QQBOT_MAIL_TIMEOUT_ALERT_POLL / _WINDOW）",
                       interval, window)

    watch = alert_module.TimeoutWatch(
        window_seconds=window,
        threshold=alert_module.threshold(),
        cooldown_seconds=alert_module.cooldown_seconds(),
    )
    registry.background(TimeoutAlertPlugin(
        watch, send=send, snap=snap, interval_seconds=interval,
    ))
    logger.info("超时通知已接上：%s 里够 %d 次超时给操作者写一封（%s 内不重复写）",
                alert_module.window_label(window),
                alert_module.threshold(),
                alert_module.window_label(alert_module.cooldown_seconds()))
