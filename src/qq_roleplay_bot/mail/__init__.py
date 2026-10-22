"""核心的邮件能力（M1：先把**发信**搬进来）。

## M1 搬了什么、没搬什么

搬进来的是"把一封信发出去"所需的最小一串：配置读取、`agently-cli` 子进程、
`send`、标题清洗与错误分类（见 `client.py`）。**没搬**写信/日报/收信——
那三块要到 M2–M4（`dev/data/plans/02-邮件重构.md` §三）。

判据（用户 2026-10-22 的 M1 要求）：**删掉 `plugins/mail/` 之后，
`outage_notice` / `timeout_alert` 仍能装、仍能发信。**

## 为什么由核心 provide，而不是让这两个插件自己造 `MailClient`

`AGENTS.md` §2.3："出站能力（怎么把东西发出去）属于底层，不是插件的事。"
发信就是出站；插件只该说"这是要送到操作者手上的内容"，不该知道 CLI、凭据、
收件人配置长什么样。M1 之前那条能力由 `plugins/mail/` 在 `register()` 里
`registry.mail.provide(...)` 提供——于是"删掉邮件插件"就等于"告警哑了"。
现在核心在 `runtime.build_engine()` 里、**插件 attach 之前**放好同一条能力，
插件再提供时看到已有人提供就 no-op（`plugins/mail/wire.py` 的 builder 本来
就是"已有人提供就不覆盖"的形状）。

## 两条窄口，各自独立

| 接缝字段 | 谁用 | 收件人 |
| --- | --- | --- |
| `registry.mail.operator_sender` | 掉线通知 / 超时通知 | 配置里那一个（`MAIL_REPORT_TO` → `MAIL_OWNER_FROM`） |
| `registry.mail.address_sender` | 到点提醒 / RSS | **调用方指定一个**地址（接缝侧只收一个、拒多收件人） |

两条都只是 `(subject, body) -> await` / `(address, subject, body) -> await` 的**函数**，
不是客户端、不是引擎、不是配置——插件仍然拿不到 transport / engine / 凭据。

## fail-closed

- **没配收件人**（`MAIL_REPORT_TO` / `MAIL_OWNER_FROM` 都是空）：不提供
  `operator_sender`，只留一条 warning。使用者据此明确"这次没有这条路"，
  而不是每次掉线都往日志里灌一行发不出去。
- **给指定地址那条**不需要"操作者是谁"，所以照常提供；地址形状与"只收一个"
  的闸门在 `MailSeams.send_to`，不在这一侧。
- provide 本身**从不抛**：装配阶段一个配置问题不该让核心起不来。
"""
from __future__ import annotations

import logging

from .client import (
    DEFAULT_SUBJECT,
    MailClient,
    MailError,
    build_client,
    clean_body,
    clean_email,
    clean_subject,
    mail_workdir,
    operator_recipient,
    safe_subject,
    sanitize_subject,
)

# `pyflakes` 不认 `# noqa`，所以把对外导出的名字列进 `__all__`：它们既是公共 API
# （插件树测试按 `qq_roleplay_bot.mail` 取），也是 M4 之后插件侧唯一该 import 的那一份。
__all__ = [
    "DEFAULT_SUBJECT",
    "MailClient",
    "MailError",
    "build_client",
    "clean_body",
    "clean_email",
    "clean_subject",
    "mail_workdir",
    "operator_recipient",
    "provide_senders",
    "safe_subject",
    "sanitize_subject",
]

logger = logging.getLogger(__name__)


def _already(value: object) -> bool:
    return callable(value)


def provide_senders(seams, *, client: object | None = None):
    """把两条发信窄口放到 `registry.mail`（`plugins.MailSeams`）上。

    **已经有人提供过就不覆盖**（与插件侧 builder 同一条纪律）：测试可能先塞了
    假发信函数，或者别的装配先接好了那条线；这里换掉它只会让"我塞的那个还在不在"
    变成一个没法从日志看出来的问题。

    返回 `(operator_sender, address_sender)`：两个都是**这一刻**放上去（或原本就在）
    的可调用对象；`operator_sender` 没配收件人时是 `None`。返回值只给测试与日志用，
    插件拿不到（它们从 `registry.mail` 取）。
    """

    operator = getattr(seams, "operator_sender", None)
    if _already(operator):
        logger.info("核心：发信能力已经有人提供过，操作者那一条不覆盖")
    else:
        operator = None
        recipient = operator_recipient()
        if not recipient:
            # 不猜地址：见 `client.operator_recipient` 的说明。
            logger.warning(
                "核心：没有配收件人（QQBOT_MAIL_REPORT_TO），本次不提供"
                "「给操作者发一封」的能力")
        else:
            sender_client = client if client is not None else build_client()

            async def send_operator_mail(subject: str, body: str) -> dict:
                """给操作者发一封普通邮件。主题由调用方给，所以在这里洗一道。"""

                return await sender_client.send(
                    to=recipient,
                    subject=safe_subject(subject, where="core_operator"),
                    body=str(body),
                )

            seams.provide(send_operator_mail)
            operator = send_operator_mail
            logger.info("核心：邮件能力已提供——给操作者发一封（收件人=%s）", recipient)

    address = getattr(seams, "address_sender", None)
    if _already(address):
        logger.info("核心：寄给指定地址的能力已经有人提供过，不覆盖")
    else:
        address_client = client if client is not None else build_client()

        async def send_to(address_value: str, subject: str, body: str) -> dict:
            """寄给**一个**地址；"是不是一个合法地址"由 `MailSeams.send_to` 把关。"""

            return await address_client.send(
                to=str(address_value),
                subject=safe_subject(subject, where="core_send_to"),
                body=str(body),
            )

        seams.provide_address_sender(send_to)
        address = send_to
        logger.info("核心：邮件能力已提供——可以寄给指定的一个地址")

    return operator, address
