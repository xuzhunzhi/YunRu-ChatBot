"""掉线通知插件的装配点：接上 `registry.link`，**用** `registry.mail` 的发信能力。

用户 2026-10-06 的原话就是这里的规格（见包的 docstring）：掉线时通过 mail 插件
给操作者发一封信；断一次只发一次；这是插件；**mail 是它的前置依赖**。

## 装配只做三件事，且顺序固定

1. 读开关（`dev_config.MAIL_OUTAGE_NOTICE_ENABLED`）；
2. **取** mail 放上来的发信能力（`registry.mail`）——取不到就**什么都不登记**；
3. 登记一个接收者（`registry.link.on_disconnect(...)`）。

## 为什么"取不到就不登记"（而不是登记一个失败时记日志的接收者）

`REQUIRES = ("mail",)` 保证 mail 的 `register()` 已经跑完，所以正常情况下能力就在。
真取不到只有两种可能：**没有 mail 插件**（那本插件在 `REQUIRES` 那一步就被跳过了，
根本走不到这里），或者**这个部署的 mail 没配收件人**（`wire.build_operator_mail_sender`
返回 `None`，能力是空的）。后一种情况下登记一个"每次掉线都记一行发不出去"的接收者
只是往日志里灌噪音——**明确地不接**比"接了但永远失败"诚实，也是这里 `logger.warning`
要说清的事（"掉线通知这次没接上"必须是可搜到的一行，不是静默）。

## 为什么这里不 import mail 的任何模块

只认 `registry.mail` 这个**接缝**（`plugins.MailSeams`）：mail 里的
`MailClient` / `wire` / 配置长什么样，本插件一个字都不知道，也不该知道。
这样一来"删掉 `plugins/outage_notice/` 不影响 mail 与核心"是**结构上**成立的，
反过来"mail 换成别的实现"也只要求它继续 `provide` 同一个函数。
"""
from __future__ import annotations

import logging

from ... import dev_config
from .outage_notice import notice_on_disconnect

logger = logging.getLogger(__name__)

#: **前置插件**：发信能力由 `mail` 提供，方向不许反（用户 2026-10-06 点名）。
#: `discover()` 会先装 mail，mail 装不上就跳过本插件（并记一行
#: `plugin_skipped_missing_dependency`），核心照常跑。
REQUIRES = ("mail",)

#: 面板上的显示名（`plugins.inventory()` 读它）。
TITLE = "掉线通知"

#: **突变验证用的开关**（`tests/test_outage_notice.py` 的 `test_the_mutation_hook_...`）：
#: 打开它，接收者就从"被叫一次 = 发一封"变成"**被叫一次 = 发两封**"——
#: 也就是把用户最强调的那条（"断一次只发一次，不要反复调用"）**改弱**成"反复调用"。
#: 生产路径永远是 `False`（没有任何代码会把它打开，只有测试会临时翻它）。
#: 留着它的价值：交付时"改弱了会不会红"是可以自己跑一次的，不是一句承诺。
MUTATION_SEND_EVERY_EVENT = False


def register(registry) -> None:
    """把"掉线那一次"接到 mail 的发信能力上。取不到能力就明确不接。"""

    if not dev_config.MAIL_OUTAGE_NOTICE_ENABLED:
        logger.info("掉线通知未启用（QQBOT_MAIL_OUTAGE_NOTICE=0）")
        return

    # 与 `group_admin` 取 `shared_roles()` 同一个形状：**前置给的能力，
    # 取不到就退回"这个功能这次没有"，绝不半挂着。**
    send = getattr(registry.mail, "operator_sender", None)
    if not callable(send):
        logger.warning("掉线通知这次没接上：mail 没提供发信能力（没配收件人？）")
        return

    async def receiver() -> None:
        """核心在**一段掉线开始时**叫这一次（同一个接收者被叫第二次 = 新的一段）。

        这里刻意不记任何"发过没有"的状态：那是 `runtime._DisconnectNotifier` 的活，
        在这里再记一份就是两处各记一份（用户最强调的"断一次只发一次"会因此分叉）。
        """

        await notice_on_disconnect(send)
        if MUTATION_SEND_EVERY_EVENT:  # pragma: no cover - 只有突变验证会打开它
            await notice_on_disconnect(send)

    registry.link.on_disconnect(receiver)
    logger.info("掉线通知已接上：断线一次给操作者发一封邮件")
