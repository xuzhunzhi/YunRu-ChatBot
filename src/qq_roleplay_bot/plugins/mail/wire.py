"""邮件通道的装配：读信回信 + 每日汇报 + **开放发信能力**。

从 `background_plugins.py` 原样搬来（只改 import 路径与函数名），装配逻辑没动。

三块的开关相互独立：

- **读信回信**：`QQBOT_MAIL_REPLY`（还要配主人地址与 QQ 号）
- **每日汇报**：`QQBOT_MAIL_REPORT`
- **把发信能力开放给别的插件**（`build_operator_mail_sender`）：**没有开关**——
  它是"提供能力"，用不用由使用者决定（见那个函数的说明）。
"""
from __future__ import annotations

import logging

from ... import dev_config
from ...dev_config import data_dir

logger = logging.getLogger(__name__)


# 每日汇报的检查节拍：它只做"到点了吗"，60 秒足够细（发送时刻精确到分钟）。
REPORT_TICK_SECONDS = 60.0

#: 进程内共享的 `MailStateStore`：**同一路径只造一份**（2026-10-22）。
#:
#: 起因：`build_daily_report` 与 `build_mail_channel` 原来各 `MailStateStore()` 一次，
#: 两个实例各读各写同一份 `data/mail_state.json`。收信水位与正文缓存这两样没落盘时
#: 每轮都从空状态起步（`poll_watermark` 永远 0 ⇒ 每轮重扫整个收件箱），而"已处理"
#: 去重又有两套内存视图——同一封信在同一轮里被看见两次就回两次。
#: 共享实例不能替代写入互斥（`MailChannel.poll_once` 那把锁才是那个），但它保证
#: 日报与读信通道装配出来的是**同一份**状态对象。
#:
#: 键带上**类本身**：测试会把 `mail_state.MailStateStore` 换成一个替身
#: （`tests/test_mail_report_window.py`），类换了就必须换实例，否则替身会被缓存下来
#: 带进后面的用例；键里再带上路径，`QQBOT_DATA_DIR` 一改就落到另一份状态文件。
_STORES: dict[tuple[object, object], object] = {}


def default_store():
    """装配用的唯一 store（同一路径复用同一实例）。"""

    from .mail_state import MailStateStore, default_state_path

    key = (MailStateStore, default_state_path())
    store = _STORES.get(key)
    if store is None:
        store = _STORES[key] = MailStateStore()
    return store


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
    from .daily_report import DailyReporter
    from .letter_writer import LetterWriter
    from .mail_client import MailClient

    mail = MailClient(dev_config.MAIL_CLI, workdir=mail_workdir())
    # 与读信通道**共用同一个 store**（2026-10-22）：见 `default_store` 的说明。
    store = default_store()
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
    #
    # **顺序不能靠数组顺序猜**（2026-10-13 修）：`note_letter` 是"最近在前"（新的一封
    # 插到 `[0]`），所以这里必须**从早到晚**灌，灌完最新那封才落在 `letter_history[0]`。
    # `mail_state.json` 里存的是**旧→新**（`letters.append(...)` + 只留最后 `MAX_LETTERS`
    # 条），原先那个 `reversed(...)` 正好把它灌成"最旧在前"——重启之后"上一封信"会指到
    # 十几天前那一封，`_letter_note`（它取 `letter_history[0]` 当"最近一封"）也跟着看错。
    # 按 `at` 排序，既不依赖存储顺序，也不依赖写入的人都记得这条约定。
    try:
        state = store.load()
    except Exception:  # noqa: BLE001 - 读不到就当她还没写过信，对话照常
        logger.exception("mail_state_load_failed")
    else:
        for letter in sorted(state.letters, key=_letter_at):
            report.remember_letter(letter)
    return reporter


def _letter_at(letter: object) -> float:
    """排序键：一封信的墙上时间（坏值当 0，排到最前，不影响新的一封）。"""

    if not isinstance(letter, dict):
        return 0.0
    try:
        return float(letter.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_operator_mail_sender(seams):
    """把**"给操作者发一封普通邮件"**这个能力放上注册表（`registry.mail`）。

    由来（2026-10-06 用户）：*"掉线可以调用 mail 插件给我发消息通知我"* ·
    *"这个作为 mail 插件的后置插件，也就是说 mail 插件是这个插件的依赖"*。

    ## 这里只做一件事：把 mail 已有的发信能力**开放出去**

    在这之前，`MailClient` 只在 `build_daily_report` / `build_mail_channel` 里各造一个，
    两个都**只服务自己**——别的插件没有口子。这个函数补的就是那个口子，没有第二套逻辑：
    收件人还是配置里那一个、发信还是 `MailClient.send`、错误还是 `MailError`。

    ## 收件人：**复用 `MAIL_REPORT_TO`，不新写一份配置**

    用户说的"用 mail 插件现有的配置"就是这个意思。回落到 `MAIL_OWNER_FROM`
    （它本身也默认等于 `MAIL_REPORT_TO`）只是"配了主人地址没配汇报地址"时少一条静默死路，
    不引入新配置项。

    **读数在哪一刻发生**（2026-10-16 补）：主读数 `QQBOT_MAIL_REPORT_TO` 走
    `dev_config.mail_report_to()`，也就是**装配这一刻现读**环境，不是 import 期常量——
    与那个函数的 docstring 同一条纪律（原话："发信是**这一刻**要往哪寄的决定，所以现读"）。
    空串 = 没配 = **不开放这个能力**（`return None`）；这一条是用户定的，别改成"猜一个默认地址"。

    ## 与开关的关系（刻意与 `MAIL_REPORT_ENABLED` **无关**）

    这个能力**不看 `MAIL_REPORT_ENABLED`**：那是"每天那封汇报发不发"的开关，
    和"别的插件能不能借邮箱发一封信"是两件事——关掉日报不该让掉线通知变成哑巴
    （用户要的是"断了就告诉我"）。发不发由**使用者**的开关决定（见
    `dev_config.MAIL_OUTAGE_NOTICE_ENABLED`）。

    反过来也有代价，说清楚：**登记了这个能力之后，就是"这个部署里插件能发信"**。
    它仍然不是"给任意地址发信"——收件人写死在这一个函数里（`MailSeams` 的说明）。

    ## 一件事：**已经有人提供过就不覆盖**

    **M1（2026-10-22）之后这条守卫就是兼容路径**：核心已经在 `runtime.build_engine()`
    里、插件 attach 之前调 `qq_roleplay_bot.mail.provide_senders()` 把同一条能力放好了，
    所以本函数在真装配里走的是下面这个 `existing` 分支——**no-op，不重复提供、不报错**。
    插件目录在 M4 之前仍然保留（还管读信回信/日报/写信），这条路径保证它和核心
    同时在场时不会互相覆盖；M4 删掉本文件后，核心那份继续工作。

    `registry.mail.provide(...)` 只在**还没有**发信函数时才登记。理由是踩出来的
    （2026-10-06，写掉线通知的测试时）：`discover()` 会把前置 `mail` 装一遍——
    哪怕调用方只想要后置那一个（`only=("outage_notice",)` 也会顺着 `REQUIRES` 把 mail
    装上）。于是"测试或部署自己先塞了一个发信函数"会被这里**静默盖掉**，
    而盖掉之后那一封会去起真的 CLI（实测：`MailError: credential_lock_unavailable`，
    用例里表现为"假发信函数一次都没被调用"）。

    生产路径上注册表是新的、`operator_sender` 是 `None`，所以这条守卫在真装配里
    不改变任何行为；它挡住的是"重复装配把别人已经接好的那条线换掉"这种失败形状
    （与 `PluginRegistry.provide_roles` 的"谁放的就用谁的"是同一条道理）。
    真要换，显式调 `registry.mail.provide(...)`（它自己仍然是后到者覆盖先到者）。
    """

    from .mail_client import MailClient

    existing = getattr(seams, "operator_sender", None)
    if callable(existing):
        # 别人已经接好了（测试的手造注册表、或将来某处先登记了一份）。
        # 不覆盖：那条线是**调用方**建立起来的，替换它只会让"我塞的那个还在不在"
        # 变成一个没法从日志看出来的问题（见 docstring 里那次实测）。
        logger.info("发信能力已经有人提供过，本次不覆盖")
        return existing

    # 收件人**在这一刻读**（2026-10-16 补的一致性）：`dev_config.mail_report_to()` 的
    # docstring 把口径写死了——"发信是**这一刻**要往哪寄的决定，所以现读"——而这里原来读
    # 的是 **import 期常量** `dev_config.MAIL_REPORT_TO`：面板 / 测试 / 干跑改完环境之后
    # 常量还是旧值，于是"这一刻往哪寄"被一个 import 时的快照决定了。同一套口径里的不一致。
    #
    # 空串仍然 = **不开放发信能力**（用户 2026-10-16：收件人**"必须由 `.env` 给"**）。
    # 宁可闭嘴，也不猜一个默认地址——猜错就是把信寄给陌生人，比不发严重得多。
    recipient = (dev_config.mail_report_to() or "").strip()
    if not recipient:
        # 回落到主人地址。**这一项仍然读 import 期常量**（`MAIL_OWNER_FROM` 在 dev_config 里
        # 没有现读函数，它是 `MAIL_REPORT_TO` 的回落项）：本次只把"主读数"改成现读，
        # 不顺手给这一项发明新接缝（见提交说明里列的"另外发现、只报告"）。
        recipient = (dev_config.MAIL_OWNER_FROM or "").strip()
    if not recipient:
        logger.warning("没有配收件人（QQBOT_MAIL_REPORT_TO），本次不开放发信能力")
        return None
    client = MailClient(dev_config.MAIL_CLI, workdir=mail_workdir())

    async def send_operator_mail(subject: str, body: str) -> dict:
        """给操作者发一封**普通邮件**（不是日报、不是回信）。收件人由上面那个闭包定。

        **主题在这里洗一道**（2026-10-16）：这个函数的 `subject` 是**调用方给的自由文本**
        （接口上就是），所以不指望每个调用方自己记得——今天两位调用方
        （`outage_notice.SUBJECT` / `timeout_alert.SUBJECT`）都是常量文案，
        这一道对它们是空操作（洗不出改动就不打日志）。清洗在 `clean_subject` 之前。
        """

        from .mail_client import safe_subject

        return await client.send(to=recipient,
                                 subject=safe_subject(subject, where="operator"),
                                 body=str(body))

    seams.provide(send_operator_mail)
    logger.info("邮件能力已开放给别的插件：收件人=%s", recipient)
    return send_operator_mail


def build_address_mail_sender(seams):
    """把**"寄给某个地址"**这条窄能力放上注册表（`registry.mail.send_to`）。

    由来（2026-10-08）：到点提醒的邮箱那一路要寄给**那个人**，而
    `build_operator_mail_sender` 开放的那一条收件人写死是操作者（`MAIL_REPORT_TO`）。
    两条能力**分开登记、各自独立**：合成一条会让"能不能寄给一个指定地址"与
    "操作者是谁"缠在一起，而它们本来是两件事。

    ## 这一条比上一条宽在哪（如实说）

    上一条是"给操作者发一封"，用法指定不了收件人；这一条**收件人由调用方给**。
    所以闸门放在**接缝**那一侧（`MailSeams.send_to` → `_one_address`）：一次只收
    **一个**地址、地址必须是地址的形状、换行与多收件人一律拒。这一侧只做执行——
    与上一条**同一个** `MailClient`、同一套配置、同一套错误（`MailError`），
    **没有第二套发信逻辑**。

    ## 与开关的关系（与上一条同一口径）

    不看 `MAIL_REPORT_ENABLED`（那是"每天那封汇报发不发"），也不看 `MAIL_REPLY_ENABLED`。
    发不发由**使用者**决定：到点提醒那一侧按人存着他的地址，**用户选了**才发。

    ## 已经有人提供过就不覆盖（同上一条那次踩坑）

    理由与 `build_operator_mail_sender` 那段一样。**M1 之后这一条同样是兼容路径**：
    核心先提供 `address_sender`，本函数看到 `existing` 就 no-op；测试或部署自己先塞的
    那一条也不该被静默换掉。M4 删掉本文件后核心那份继续工作。
    """

    from .mail_client import MailClient

    existing = getattr(seams, "address_sender", None)
    if callable(existing):
        logger.info("寄给某个地址的能力已经有人提供过，本次不覆盖")
        return existing
    client = MailClient(dev_config.MAIL_CLI, workdir=mail_workdir())

    async def send_to(address: str, subject: str, body: str) -> dict:
        """寄给**一个**地址（"是不是一个合法地址"由接缝那一侧把关，这里只执行）。

        **主题也在这里洗一道**（2026-10-16），理由同上：`subject` 由调用方给。
        今天唯一的调用方（到点提醒）用的是固定文案 `MAIL_SUBJECT = "到点提醒"`
        （`what` 只进正文），所以对它是空操作。
        """

        from .mail_client import safe_subject

        return await client.send(to=str(address),
                                 subject=safe_subject(subject, where="send_to"),
                                 body=str(body))

    seams.provide_address_sender(send_to)
    logger.info("邮件能力已开放给别的插件：可以寄给指定的一个地址")
    return send_to


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
    from .mail_channel import MailChannel
    from .mail_client import MailClient

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
        state_store=default_store(),
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
