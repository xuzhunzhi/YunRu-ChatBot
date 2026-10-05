"""读信回信：把主人的来信当成一条私聊消息，交回引擎走判定与回复，再把回复寄回去。

由来（2026-09-30 用户："加入读信回信功能，跟收到消息一样过一遍 agent"）。

设计上刻意做的几件事：

1. **不复制主循环**（AGENTS 2.3）。这里只做"取信 → 造一条消息 → 交给对话流程"，
   走的是窄接缝 `chat.run`（`plugins.ChatSeams`），**不持有引擎**。
   → 把返回的正文寄出去"。判定"要不要回、回什么"仍然全部落在 Stage 3 的引擎里：
   邮件正文进的是同一个不可信 DATA 区，安全规则、关系档位、注意力判定一条都不绕过。
2. **fail-closed**：只认配置里的主人地址。别人发来的信**不进模型**（连读都不读），
   只记一个计数——否则任何人都能往她的邮箱塞一段话来给她下指令。
3. **幂等**：处理过的 `message_id` 记进 `MailState`，重启也不会重复回。
4. **不会来回刷**：有每日回复上限；绝不回自己的地址（那是死循环）。
5. 回信是**整段正文**：邮件不像 QQ 能连发好几条，所以把引擎拆出的分段合起来寄。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from ...plugins import DeliveredMessage

logger = logging.getLogger(__name__)

# 单封来信最多读多少字进引擎：邮件可以任意长，不能让它把 prompt 灌爆。
MAX_INBOUND_CHARS = 4000
# 回信正文上限（CLI 那边另有 1MB 的硬上限，这里只是"回信要读得下去"）。
MAX_REPLY_CHARS = 3000
# 一次轮询最多处理几封，避免积压时一口气跑十几轮模型调用。
MAX_PER_POLL = 5
# 太旧的来信不回：默认 48 小时。上限卡住的那几封会攒到第二天，
# 隔好几天再回一封"你好吗"很奇怪。
MAX_AGE_HOURS = 48.0


def _normalize(address: object) -> str:
    return str(address or "").strip().casefold()


# 机器发的信（no-reply / 退信 / 邮局 / 自动回复）：**在进判定之前就用规则拦掉**。
# 2026-09-30 用户："直接规则匹配自动回复就好，识别到自动回复就拦截在进判断 agent 前就好了"。
# 理由：这类信的内容是模板（"我不在办公室"），判定 agent 看了也只会说不用回，
# 白花一次调用，还可能被模板里的话带偏。规则拦比模型判更便宜也更确定。
AUTO_SENDER_HINTS = ("no-reply", "noreply", "donotreply", "do-not-reply",
                     "mailer-daemon", "postmaster", "bounce")
AUTO_SUBJECT_HINTS = ("自动回复", "自动回信", "自动应答", "自动答复", "我是自动",
                      "auto-reply", "auto reply", "autoreply", "automatic reply",
                      "out of office", "out-of-office", "delivery status notification",
                      "undeliverable", "mail delivery failed", "退信", "投递失败")
SENDERS_ANY = "any"
SENDERS_OWNER = "owner"


def parse_senders(value: object) -> str | set[str]:
    """`any` / `owner` / 逗号分隔的地址名单。"""

    text = str(value or "").strip()
    if not text or text.casefold() == SENDERS_ANY:
        return SENDERS_ANY
    if text.casefold() == SENDERS_OWNER:
        return SENDERS_OWNER
    return {item.strip().casefold() for item in text.split(",") if item.strip()}


def is_auto_mail(address: str, subject: str = "") -> bool:
    """这封信是不是机器发的（自动回复/退信）。纯规则匹配，不问模型。"""

    local = _normalize(address).split("@", 1)[0]
    if any(hint in local for hint in AUTO_SENDER_HINTS):
        return True
    text = str(subject or "").casefold()
    return any(hint in text for hint in AUTO_SUBJECT_HINTS)


def is_privileged_command(text: str) -> bool:
    """正文是不是一条**管理员/超管命令**。

    ## 这道护栏已经搬进核心（2026-10-01 审查后）

    它原来由**这个插件自己**调（在收信时拦一道）。审查指出那条做法有根本问题：

    > 护栏写在插件里等于**约定**，不是边界——而 `chat.deliver` 当时把"以任意身份
    > 说话"这个原语发给了每一个插件，还要求每个插件都记得自己拦。

    所以拒投改在核心的接缝里（`runtime._chat_seams_for.deliver`：渠道注入的话
    只要长得像特权命令就拒投）。这里保留这个函数是为了**向后兼容**（有测试与
    外部调用在用），实现直接委托给核心的 `privileged_command_level`——
    不再维护第二套正则，避免两边判定漂移。
    """

    from ...security import privileged_command_level

    return privileged_command_level(text) is not None


# --- 身份对应（2026-09-30 用户设计）----------------------------------------
# 1. **QQ 邮箱直接映射**：`123456789@qq.com` 就是 QQ 123456789（本地部分全是数字才算，
#    `attacker@gmail.com` 这种英文别名不算）。`@vip.qq.com` / `@foxmail.com` 同理；
#    主人那个 `xuzhunzhi@foxmail.com` 不是数字，所以走配置里的对应。
# 2. **非 QQ 邮箱**：先当陌生人（`mail:<地址>` 独立身份），回信里问一句他的 QQ 号；
#    对方报号之后核实并记下来（`mail_state.links`），以后就按那个 QQ 号认人。
QQ_MAIL_DOMAINS = ("qq.com", "vip.qq.com", "foxmail.com")
QQ_LOCAL_RE = re.compile(r"^\d{5,11}$")
# 在正文里找"自报的 QQ 号"：只认 5~11 位数字，且整封信里**只有一个**候选才敢认。
QQ_IN_TEXT_RE = re.compile(r"(?<!\d)(\d{5,11})(?!\d)")
ASK_QQ_LINE = "对了，你的 QQ 号是多少？我下次好认得出是你。"


def qq_from_address(address: str) -> str:
    """QQ 邮箱 → QQ 号；不是 QQ 邮箱或本地部分不是纯数字就返回空串。"""

    text = _normalize(address)
    if "@" not in text:
        return ""
    local, domain = text.rsplit("@", 1)
    if domain not in QQ_MAIL_DOMAINS:
        return ""
    return local if QQ_LOCAL_RE.match(local) else ""


def qq_from_text(text: str) -> str:
    """从正文里读出自报的 QQ 号；不唯一就不认（宁可再问一次，也别认错人）。"""

    found = set(QQ_IN_TEXT_RE.findall(str(text or "")))
    if len(found) != 1:
        return ""
    return found.pop()


def _age_hours(created_at: object, now: float) -> float | None:
    """来信时间到现在的小时数；解析不了就返回 None（当作不过期）。"""

    text = str(created_at or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp.timestamp()) / 3600.0)


class MailChannel:
    """按主人地址收信，走引擎，再回信。`enabled=False` 时完全不动。"""

    def __init__(
        self,
        *,
        mail_client,
        state_store,
        chat,
        owner_from: str,
        owner_user_id: str,
        enabled: bool = True,
        max_replies_per_day: int = 5,
        self_from: str = "",
        max_age_hours: float = MAX_AGE_HOURS,
        senders: str = "any",
        max_replies_per_sender: int = 2,
        clock=time.time,
    ) -> None:
        self.mail = mail_client
        self.state_store = state_store
        # **窄接缝**（`plugins.ChatSeams`），不是引擎对象。
        # 2026-10-01 修掉的越界：这里原来存的是 `engine`，于是通道能直读
        # `super_admin_user_ids` / `admin_user_ids` / 私聊白名单，
        # 也能自己调 `engine.handle()`——"插件拿不到权限名单"那条硬约束当时是假的。
        # 现在只拿四个函数：读"不该被冒充的号"、放行私聊、投递消息、取续发段。
        self.chat = chat
        self.owner_from = _normalize(owner_from)
        self.owner_user_id = str(owner_user_id or "").strip()
        # 她自己的地址：即便有人把它配成了"主人地址"，也绝不回自己（死循环）。
        self.self_from = _normalize(self_from)
        # 谁的信会被读进来（2026-09-30 用户要求）：`any` = 谁都读，判定决定回不回；
        # `owner` = 只读主人的；也可以给逗号分隔的地址白名单。
        self.senders = parse_senders(senders)
        self.enabled = bool(enabled) and bool(self.owner_from) and bool(self.owner_user_id)
        self.max_replies_per_day = max(0, int(max_replies_per_day))
        self.max_replies_per_sender = max(0, int(max_replies_per_sender))
        self.max_age_hours = max(0.0, float(max_age_hours))
        self.clock = clock
        self.stats = {"polls": 0, "seen": 0, "ignored_sender": 0, "skipped_done": 0,
                      "skipped_stale": 0, "skipped_auto": 0, "replied": 0, "silent": 0,
                      "failed": 0, "capped": 0, "sender_capped": 0, "strangers": 0,
                      "asked_qq": 0, "linked": 0, "link_refused": 0,
                      "link_unavailable": 0}
        # 注：这里原来还有 `command_refused`。**拒投已经归核心**（接缝里
        # `privileged_command_level` 那道判定），插件这边看不到"被拒了"与
        # "判定说不回"的区别（两者都是 `run()` 回 None），所以那个计数器永远停在 0——
        # 留着它就是给运维一个假数字，删掉。
        self.last_error = ""
        self._allowed_private: set[str] = set()
        # 被规则挡下的信按 message_id 去重后再计数：同一封会在每轮里被重新看到，
        # 不记 id 的话计数会随着轮询次数一直涨（日志里看着像"一直在收"）。
        self._ignored_seen: set[str] = set()

    # --- 一轮 ---------------------------------------------------------------

    def _accepts(self, sender: str) -> bool:
        """这个发件人的信读不读。`any` 全收；`owner` 只收主人；否则按名单。"""

        if self.senders == "any":
            return True
        if self.senders == "owner":
            return sender == self.owner_from
        return sender in self.senders

    def _user_id_for(self, sender: str) -> str:
        """来信人在引擎里的身份。

        顺序（2026-09-30 用户设计）：
        1. 主人地址 → 他的 QQ 号（跟群里是同一个人，关系与画像连续）；
        2. **QQ 邮箱**（`123456789@qq.com` / `@vip.qq.com` / `@foxmail.com`）→ 直接就是那个 QQ 号；
        3. 对方在以前的回信里自报过 QQ 号（`mail_state.links`）→ 用那个号；
        4. 都不行 → `mail:<地址>` 的独立身份（陌生人），回信里会问一句他的 QQ 号。
        """

        if sender == self.owner_from:
            return self.owner_user_id
        by_address = qq_from_address(sender)
        if by_address:
            return by_address
        linked = self._linked(sender)
        if linked:
            return linked
        return f"mail:{sender}"

    def _linked(self, sender: str) -> str:
        reader = getattr(self.state_store, "mail_link", None)
        return str(reader(sender) or "") if callable(reader) else ""

    def _learn_link(self, sender: str, body: str) -> str:
        """对方在信里自报 QQ 号 → 核实后记下来，以后按这个号认人。

        核实的三条：格式是 5~11 位数字；整封信里只有一个候选（不唯一就再问一次）；
        **不能是管理员/超管/主人自己的号**——否则任何人都能自称是主人，
        而超管判定只看 user_id（见 `is_privileged_command` 的说明）。
        """

        if not sender or sender == self.owner_from:
            return ""
        if qq_from_address(sender) or self._linked(sender):
            return ""  # 已经认得，不用再学
        claimed = qq_from_text(body)
        if not claimed:
            return ""
        # **只读**一次"不该被冒充的号"（超管 + 配置里的管理员）。
        # 以前这里是直读 `engine.super_admin_user_ids` / `engine.admin_user_ids`——
        # 通道能拿到权限名单，那条硬约束当时是假的。现在走窄接缝，只拿到集合本身。
        #
        # **接缝没接上时宁可不绑定**（2026-10-01 审查点的 fail-open）：
        # 但要用 `knows_reserved()` 分清两种"空集"——
        #   * 接缝没接上 → 读不到名单，这层护栏会静默消失 → **拒绝绑定并出声**；
        #   * 这台机器真没配管理员 → 合法的部署状态，照常绑定。
        # 只看 `reserved()` 是否为空会把后者也误杀（邮件绑定整条不工作）。
        if not self.chat.knows_reserved():
            self.stats["link_unavailable"] += 1
            logger.warning(
                "mail_link_skipped sender=%s reason=reserved_seam_missing"
                "（接缝没接上：读不到超管/管理员名单，无法判断这是不是个该保护的号）", sender)
            return ""
        reserved = {self.owner_user_id} | set(self.chat.reserved())
        if claimed in reserved:
            self.stats["link_refused"] += 1
            logger.warning("mail_link_refused sender=%s reason=reserved_id", sender)
            return ""
        writer = getattr(self.state_store, "link_mail", None)
        if callable(writer):
            writer(sender, claimed)
            self.stats["linked"] += 1
            logger.info("mail_link_learned sender=%s user_id=%s", sender, claimed)
        return claimed

    async def _allow_private(self, user_id: str) -> None:
        """把来信人放进引擎的私聊白名单。

        邮件走的是"私聊"这条路，而引擎那道闸只放白名单里的人（fail-closed）。
        所以**这条通道自己负责**把收进来的发件人登记进去——策略留在 Stage 4 的通道里，
        引擎的权限判定一个字不改。只登记一次，并各记一行日志。
        """

        if user_id in self._allowed_private:
            return
        self._allowed_private.add(user_id)
        # 走窄接缝放行（核心那边拿着名单）。以前是直改 `engine.private_debug_user_ids`。
        await self.chat.allow(user_id)
        logger.info("mail_sender_allowed user_id=%s", user_id)

    async def poll_once(self) -> int:
        """读一轮未读信并回信；返回**真正回了**的封数。"""

        if not self.enabled:
            return 0
        self.stats["polls"] += 1
        try:
            payload = await self.mail.list_messages(limit=MAX_PER_POLL, folder="inbox", unread_only=True)
        except Exception as exc:  # noqa: BLE001 - 邮箱读不到只是这一轮没有信
            self._fail("list", exc)
            return 0
        messages = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            return 0

        replied = 0
        for item in messages:
            if not isinstance(item, dict):
                continue
            self.stats["seen"] += 1
            sender = _normalize((item.get("from") or {}).get("email"))
            message_id = str(item.get("message_id") or "").strip()
            if self.self_from and sender == self.self_from:
                # 她自己的地址：回了就是死循环，直接跳过（不记账也算处理过）。
                self.stats["ignored_sender"] += 1
                logger.warning("mail_from_self_ignored")
                self.state_store.mark_mail_processed(message_id)
                continue
            if not self._accepts(sender):
                # 配置成只读主人（或名单）时，别人的信**不进模型**。
                # 按 message_id 去重：同一封每轮都会再看到，计数只该涨一次。
                if message_id and message_id not in self._ignored_seen:
                    self._ignored_seen.add(message_id)
                    self.stats["ignored_sender"] += 1
                    logger.info("mail_ignored_sender sender=%s count=%s",
                                sender, self.stats["ignored_sender"])
                continue
            subject = str(item.get("subject") or "")
            if is_auto_mail(sender, subject):
                # 自动回复/退信：**在进判定之前**拦掉（规则匹配，不问模型）。
                self.stats["skipped_auto"] += 1
                logger.info("mail_skipped_auto sender=%s", sender)
                self.state_store.mark_mail_processed(message_id)
                continue
            if sender != self.owner_from:
                self.stats["strangers"] += 1
            if not message_id or self.state_store.is_mail_processed(message_id):
                self.stats["skipped_done"] += 1
                continue
            age = _age_hours(item.get("created_at"), self.clock())
            if age is not None and age > self.max_age_hours:
                self.stats["skipped_stale"] += 1
                logger.info("mail_skipped_stale age_hours=%.0f", age)
                self.state_store.mark_mail_processed(message_id)
                continue
            if self._replies_today() >= self.max_replies_per_day:
                self.stats["capped"] += 1
                logger.warning("mail_reply_capped today=%s", self._replies_today())
                continue
            try:
                if await self._handle_one(item, message_id, sender=sender, subject=subject):
                    replied += 1
            except Exception as exc:  # noqa: BLE001 - 一封处理失败不该停掉整轮
                self._fail("handle", exc)
            finally:
                # 无论回没回都记成"处理过"：否则下一轮会把同一封再送一遍，
                # 而"她选择不回"是**她的决定**，不该被反复问。
                self.state_store.mark_mail_processed(message_id)
        return replied

    async def _handle_one(self, item: dict, message_id: str, *, sender: str, subject: str) -> bool:
        body = await self._read_body(item, message_id)
        if not body:
            self.stats["silent"] += 1
            return False
        # 陌生人自报 QQ 号 → 核实后绑定身份（下一条同地址的来信就认得他了）。
        self._learn_link(sender, body)
        user_id = self._user_id_for(sender)
        unknown = user_id.startswith("mail:")
        # 引擎那道私聊闸只放白名单里的人；这条通道自己把收进来的发件人登记进去。
        await self._allow_private(user_id)
        # 交给对话流程（窄接缝 `chat.run`），不是自己调 `engine.handle`。
        #
        # **交的是"原话"，不是一整条消息**（2026-10-01 审查后改）：以前这里造
        # `IncomingMessage` 并自己填 `user_id` / `sender_role`，等于插件能指定身份——
        # 审查者实测过用它授群管理员。现在身份与特权判定都在核心：
        # `session_id` / `message_id` / `sender_role` 由接缝盖章，正文里出现
        # `/super` 或 `/admin` 一律拒投（所以下面那道自己写的护栏撤了）。
        parcel = DeliveredMessage(
            channel="mail",
            sender=user_id,
            text=body,
            # **每封自己的 id**：核心对普通对话按 message_id 去重，
            # 不给的话同一发件人的第二封起会被静默丢掉（2026-10-01 审查抓到）。
            message_id=f"mail:{message_id}",
            # 邮件单独一条会话线：邮件里的上下文不该和 QQ 私聊混在一起。
            session_namespace="mail",
            sender_name="主人" if sender == self.owner_from else sender,
            claims_owner=(sender == self.owner_from),
        )
        reply = await self.chat.run(parcel)
        # 续发段**按会话**取（引擎 2026-09-30 起支持）：邮件的分段不会跟群聊的串台。
        follow_ups = self.chat.follow_ups(f"mail:{user_id}")
        parts = [reply.text] if reply is not None and reply.text else []
        parts.extend(part.text for part in follow_ups if part.text)
        text = "\n\n".join(part.strip() for part in parts if part.strip())[:MAX_REPLY_CHARS]
        if not text:
            # 判定说"不用回"——那就是她自己决定不回，与群里一致。
            self.stats["silent"] += 1
            logger.info("mail_no_reply sender=%s message_id=%s", sender, message_id)
            return False
        if unknown:
            # 还没认出这个人是谁：回信里问一句他的 QQ 号（下一封会按上面那条去认）。
            # 问一句是**通道加的固定一句**，不指望模型每次都记得问。
            if ASK_QQ_LINE not in text:
                text = f"{text}\n\n{ASK_QQ_LINE}"
            self.stats["asked_qq"] += 1
        if self._sender_replies_today(sender) >= self.max_replies_per_sender:
            self.stats["sender_capped"] += 1
            logger.info("mail_sender_capped sender=%s", sender)
            return False
        await self.mail.send(
            to=sender, subject=self._reply_subject(subject), body=text,
        )
        self.state_store.record_mail_reply(sender)
        self.stats["replied"] += 1
        logger.info("mail_replied sender=%s message_id=%s length=%s", sender, message_id, len(text))
        return True

    async def _read_body(self, item: dict, message_id: str) -> str:
        """取正文：列表接口只给摘要，正文要 `message +read`。"""

        snippet = str(item.get("snippet") or "").strip()
        try:
            payload = await self.mail.read_message(message_id)
        except Exception as exc:  # noqa: BLE001 - 读不到正文就退回摘要（仍可能是完整信）
            self._fail("read", exc)
            return snippet[:MAX_INBOUND_CHARS]
        for key in ("body", "text", "content", "body_text", "plain"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()[:MAX_INBOUND_CHARS]
        inner = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(inner, dict):
            for key in ("body", "text", "content"):
                value = inner.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:MAX_INBOUND_CHARS]
        return snippet[:MAX_INBOUND_CHARS]

    @staticmethod
    def _reply_subject(subject: str) -> str:
        clean = " ".join(str(subject or "").split())[:160]
        if not clean:
            return "Re: 你的信"
        return clean if clean.lower().startswith("re:") else f"Re: {clean}"

    def _replies_today(self) -> int:
        reader = getattr(self.state_store, "replies_today", None)
        return int(reader()) if callable(reader) else 0

    def _sender_replies_today(self, sender: str) -> int:
        reader = getattr(self.state_store, "sender_replies_today", None)
        return int(reader(sender)) if callable(reader) else 0

    def _fail(self, where: str, exc: Exception) -> None:
        self.stats["failed"] += 1
        self.last_error = f"{where}:{type(exc).__name__}"
        logger.warning("mail_channel_failed where=%s category=%s", where, type(exc).__name__)

    def snapshot(self) -> dict[str, object]:
        return {**self.stats, "enabled": self.enabled, "owner_from": self.owner_from,
                "last_error": self.last_error}
