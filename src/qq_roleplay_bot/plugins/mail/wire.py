"""邮件通道的装配：读信回信 + 每日汇报。

从 `background_plugins.py` 原样搬来（只改 import 路径与函数名），装配逻辑没动。

两块的开关相互独立：

- **读信回信**：`QQBOT_MAIL_REPLY`（还要配主人地址与 QQ 号）
- **每日汇报**：`QQBOT_MAIL_REPORT`
"""
from __future__ import annotations

import logging

from ... import dev_config
from ...dev_config import data_dir

logger = logging.getLogger(__name__)


# 每日汇报的检查节拍：它只做"到点了吗"，60 秒足够细（发送时刻精确到分钟）。
REPORT_TICK_SECONDS = 60.0


def build_daily_report(report, *, registry=None):
    """装配每日汇报。**没装 CLI / 没开开关就返回 None**，对话照常。

    `report` 是**窄接缝**（`plugins.ReportSeams`），不是引擎——见那个类的说明：
    日报只用得到快照 / 模型 client / 记账 / 写信记录 / 记忆五项，**没有一样是权限名单**。

    `registry` 给的是回填口：汇报器经由 `registry.register_reporter(reporter)` 登记回核心，
    而不是在这里直接写 `engine.daily_reporter`——后者是逆流（换一套思维链路时，
    新引擎未必有这个名字）。
    """

    if not dev_config.MAIL_REPORT_ENABLED:
        logger.info("每日汇报未启用（QQBOT_MAIL_REPORT=0）")
        return None
    from ...plugins.mail.daily_report import DailyReporter
    from ...plugins.mail.letter_writer import LetterWriter
    from ...plugins.mail.mail_client import MailClient
    from ...plugins.mail.mail_state import MailStateStore

    mail = MailClient(dev_config.MAIL_CLI, workdir=mail_workdir())
    store = MailStateStore()
    # 写信 agent 用**自己的 client 与 user_id**（2026-09-30 用户要求）：
    # 以前借用回复 agent 的 client，信与群聊共用一份缓存隔离空间。
    #
    # 2026-10-01：client 改从**接缝**取（`report.letter_client`），不再
    # `from ...runtime import _build_letter_client` ——那是核心的**私有**函数，
    # 插件 import 它等于把"核心必须叫这个名字"写死进了插件；
    # 换一套思维链路时那个名字未必还在。见 `plugins.ReportSeams`。
    #
    # 2026-10-05：接缝给的是**工厂**（`() -> client`），**不是 client 本身**，
    # 所以这里必须**调用**它。原来直接把工厂塞给 `LetterWriter`，`writer.client`
    # 拿到的是个函数，真去写信时炸在 `await self.client.complete(...)`——
    # 生产日志实测：`letter_draft_failed category=AttributeError`，而且只在
    # 到点写第一封信时才炸，装配阶段一点异常都看不出来。
    # 变量名照着接缝叫 `make_letter_client`，免得下一个读的人再把它当成 client。
    make_letter_client = getattr(report, "letter_client", None)
    letter_client = make_letter_client() if callable(make_letter_client) else None
    if letter_client is None:
        logger.warning("写信 agent 没有模型通道（接缝没给 letter_client），本次不启用日报")
        return None

    writer = LetterWriter(letter_client, max_chars=dev_config.MAIL_REPORT_MAX_CHARS)
    reporter = DailyReporter(
        mail_client=mail,
        state_store=store,
        recipient=dev_config.MAIL_REPORT_TO,
        send_at=dev_config.MAIL_REPORT_AT,
        enabled=True,
        max_chars=dev_config.MAIL_REPORT_MAX_CHARS,
        max_tries=dev_config.MAIL_REPORT_MAX_TRIES,
        writer=writer,
    )
    if registry is not None:
        registry.register_reporter(reporter)
    # 把她以前写出去的信灌回核心（最近在前）：这样**重启之后**她照样记得自己写过什么、
    # 那些信是写给谁的。存储层是唯一来源，核心只持有内存副本。
    try:
        state = store.load()
    except Exception:  # noqa: BLE001 - 读不到就当她还没写过信，对话照常
        logger.exception("mail_state_load_failed")
    else:
        for letter in reversed(state.letters):
            report.remember_letter(letter)
    return reporter


def build_mail_channel(chat):
    """装配读信回信。关掉开关、没配主人地址就返回 None。

    `chat` 是**窄接缝**（`plugins.ChatSeams`），不是引擎：通道只用它做四件事
    （读"不该被冒充的号"、放行私聊、投递消息、取续发段）。**它发不出消息、读不到名单全集。**
    """

    if not dev_config.MAIL_REPLY_ENABLED:
        logger.info("读信回信未启用（QQBOT_MAIL_REPLY=0）")
        return None
    if not (dev_config.MAIL_OWNER_FROM and dev_config.MAIL_OWNER_USER_ID):
        logger.warning("读信回信没配主人地址或 QQ 号，本次不启用")
        return None
    from ...plugins.mail.mail_channel import MailChannel
    from ...plugins.mail.mail_client import MailClient
    from ...plugins.mail.mail_state import MailStateStore

    # 邮件进来时走的是"私聊"这条路，主人那份用他自己的 QQ 号；陌生发件人由通道在
    # 收到信时**自己登记**进私聊白名单（策略留在 Stage 4 通道里，引擎的判定不动）。
    if (dev_config.MAIL_SENDERS.strip().casefold() in {"owner", "主人"}
            and dev_config.MAIL_OWNER_USER_ID not in dev_config.PRIVATE_DEBUG_USER_IDS):
        logger.warning(
            "读信回信：QQ %s 不在私聊白名单里，来信会被引擎静默丢弃（把 TA 加进 "
            "QQBOT_PRIVATE_DEBUG_USER_IDS 或超管名单）",
            dev_config.MAIL_OWNER_USER_ID,
        )
    return MailChannel(
        mail_client=MailClient(dev_config.MAIL_CLI, workdir=mail_workdir()),
        state_store=MailStateStore(),
        chat=chat,
        owner_from=dev_config.MAIL_OWNER_FROM,        owner_user_id=dev_config.MAIL_OWNER_USER_ID,
        enabled=True,
        max_replies_per_day=dev_config.MAIL_MAX_REPLIES_PER_DAY,
        self_from=dev_config.MAIL_SELF_FROM,
        max_age_hours=dev_config.MAIL_MAX_AGE_HOURS,
        senders=dev_config.MAIL_SENDERS,
        max_replies_per_sender=dev_config.MAIL_MAX_REPLIES_PER_SENDER,
    )


def mail_workdir():
    """邮箱 CLI 的工作目录。和 `data/` 一起搬走就能接着跑。"""

    return data_dir() / "mail_outbox"
