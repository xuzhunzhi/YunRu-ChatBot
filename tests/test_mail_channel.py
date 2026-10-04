"""读信回信：只认主人、进引擎、寄回复，以及几条防呆（幂等 / 旧信 / 上限 / 不回自己）。"""
import asyncio
import time

from qq_roleplay_bot.plugins.mail.mail_channel import MAX_INBOUND_CHARS, MailChannel
from qq_roleplay_bot.transport import OutgoingMessage, MessageTarget

OWNER = "xuzhunzhi@foxmail.com"
OWNER_QQ = "900000001"
SELF = "shiyunru@agent.qq.com"
TARGET = MessageTarget(user_id=OWNER_QQ)

from qq_roleplay_bot.plugins import ChatSeams


def chat_seams_of(engine) -> ChatSeams:
    """把测试替身包成**窄接缝**——**用生产那一份**，不在测试里重写。

    ## 为什么不再自己写一份（2026-10-01 第二轮审查抓到）

    第一版这个 helper 把 `isinstance` 检查和 `privileged_command_level` 拒投
    **在测试文件里抄了一遍**。审查者把**两条生产护栏都拆掉**之后：
    `test_a_plugin_cannot_impersonate_anyone_through_the_chat_seam` 与
    `test_privileged_commands_are_never_executed_from_mail` **全绿**——
    因为使它们变绿的正是测试文件里那份副本，生产代码根本没被测到。

    所以现在**直接调 `runtime._chat_seams_for(engine)`**：测的就是要上线的那个实现，
    它改了测试就会红。（那个函数只依赖引擎上几个属性，不需要完整装配。）
    """

    from qq_roleplay_bot import runtime

    return runtime._chat_seams_for(engine)


def iso(offset_hours: float = 0.0) -> str:
    stamp = time.time() - offset_hours * 3600
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def item(message_id="m1", sender=OWNER, subject="在吗", created_at=None, snippet="你还好吗"):
    return {"message_id": message_id, "subject": subject, "snippet": snippet,
            "created_at": created_at or iso(), "from": {"email": sender, "name": "主人"}}


class _Mail:
    def __init__(self, messages=(), bodies=None, read_error=None):
        self._messages = list(messages)
        self._bodies = dict(bodies or {})
        self._read_error = read_error
        self.sent = []
        self.read = []

    async def list_messages(self, *, limit=10, folder="inbox", unread_only=False):
        return {"data": list(self._messages)}

    async def read_message(self, message_id):
        self.read.append(message_id)
        if self._read_error:
            raise self._read_error
        return {"body": self._bodies.get(message_id, "")}

    async def send(self, *, to, subject, body, confirmed=True, dry_run=False):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"message_id": "sent_1"}


class _State:
    def __init__(self):
        self.processed = set()
        self.replies = 0
        self.sender_counts = {}
        self.links = {}

    def is_mail_processed(self, message_id):
        return message_id in self.processed

    def mark_mail_processed(self, message_id):
        self.processed.add(message_id)

    def replies_today(self):
        return self.replies

    def record_mail_reply(self, sender=""):
        self.replies += 1
        if sender:
            self.sender_counts[sender] = self.sender_counts.get(sender, 0) + 1

    def sender_replies_today(self, sender):
        return self.sender_counts.get(sender, 0)

    def mail_link(self, address):
        return self.links.get(str(address or "").casefold(), "")

    def link_mail(self, address, user_id):
        self.links[str(address or "").casefold()] = str(user_id)


class _Engine:
    def __init__(self, reply="嗯，我在。", follow_ups=(), silent=False):
        self.reply = reply
        self.follow_ups = tuple(follow_ups)
        self.silent = silent
        self.messages = []

    async def handle(self, message):
        self.messages.append(message)
        return None if self.silent else OutgoingMessage(message.target, self.reply, paced=True)

    def take_follow_ups(self, session_id=None):
        # 引擎 2026-09-30 起按会话取续发；假引擎照这个签名来。
        return [OutgoingMessage(TARGET, text) for text in self.follow_ups]


def channel(mail, engine, state=None, **kwargs) -> MailChannel:
    return MailChannel(
        mail_client=mail, state_store=state or _State(), chat=chat_seams_of(engine),
        owner_from=OWNER, owner_user_id=OWNER_QQ, self_from=SELF, **kwargs,
    )


def poll(ch) -> int:
    return asyncio.run(ch.poll_once())


# --- 主路径 ---------------------------------------------------------------

def test_owner_mail_goes_through_the_engine_and_gets_a_reply() -> None:
    mail, engine = _Mail([item()], {"m1": "你昨晚睡得好吗"}), _Engine(reply="还行。你呢。")
    ch = channel(mail, engine)
    assert poll(ch) == 1
    # 进引擎的是一条私聊消息，会话单独一条线（不跟 QQ 私聊混）
    message = engine.messages[0]
    assert message.text == "你昨晚睡得好吗"
    assert message.session_id == f"mail:{OWNER_QQ}" and message.user_id == OWNER_QQ
    assert mail.sent[0]["to"] == OWNER and mail.sent[0]["subject"] == "Re: 在吗"
    assert "还行。你呢。" in mail.sent[0]["body"]


def test_reply_follow_ups_are_joined_into_one_mail() -> None:
    mail = _Mail([item()], {"m1": "在吗"})
    ch = channel(mail, _Engine(reply="在。", follow_ups=["第二句。", "第三句。"]))
    poll(ch)
    body = mail.sent[0]["body"]
    assert "在。" in body and "第二句。" in body and "第三句。" in body


def test_silent_engine_sends_nothing_but_still_marks_processed() -> None:
    mail, state = _Mail([item()], {"m1": "在吗"}), _State()
    ch = channel(mail, _Engine(silent=True), state)
    assert poll(ch) == 0
    assert mail.sent == []
    assert "m1" in state.processed, "她选择不回也是处理过了，别下一轮再问一遍"


# --- 防呆 -----------------------------------------------------------------

def test_mail_from_anyone_else_is_read_and_judged_by_default() -> None:
    """默认 `senders=any`（用户 2026-09-30 的要求）：谁都读、都走一遍判定。"""

    mail = _Mail([item(sender="stranger@example.com")], {"m1": "你好，我是陌生人"})
    engine = _Engine(reply="你好。")
    ch = channel(mail, engine)
    assert poll(ch) == 1
    assert engine.messages and engine.messages[0].text == "你好，我是陌生人"
    # 回给**发件人**，不是主人
    assert mail.sent[0]["to"] == "stranger@example.com"
    assert ch.snapshot()["strangers"] == 1
    # 陌生发件人用独立身份（不占用某个 QQ 号）
    assert engine.messages[0].user_id == "mail:stranger@example.com"


def test_senders_owner_policy_still_ignores_strangers() -> None:
    """`QQBOT_MAIL_SENDERS=owner` 时退回"只读主人"。"""

    mail, engine = _Mail([item(sender="stranger@example.com")], {}), _Engine()
    ch = channel(mail, engine, senders="owner")
    assert poll(ch) == 0
    assert engine.messages == []
    assert ch.snapshot()["ignored_sender"] == 1


def test_auto_reply_is_blocked_before_the_judge() -> None:
    """自动回复/退信：规则匹配，**在进判定之前**就拦掉（2026-09-30 用户的要求）。"""

    cases = [
        item(message_id="m1", sender="no-reply@shop.example", subject="订单确认"),
        item(message_id="m2", sender="friend@example.com", subject="自动回复：我不在办公室"),
        item(message_id="m3", sender="mailer-daemon@example.com", subject="Delivery Status Notification"),
    ]
    mail, engine = _Mail(cases, {"m1": "x", "m2": "y", "m3": "z"}), _Engine()
    ch = channel(mail, engine)
    assert poll(ch) == 0
    assert engine.messages == [], "自动回复不该花掉一次判定"
    assert ch.snapshot()["skipped_auto"] == 3
    # 也不该回头再处理
    for mid in ("m1", "m2", "m3"):
        assert mid in ch.state_store.processed


def test_auto_mail_rule_matches_sender_and_subject() -> None:
    from qq_roleplay_bot.plugins.mail.mail_channel import is_auto_mail

    assert is_auto_mail("noreply@x.com")
    assert is_auto_mail("someone@x.com", "Out of office: 我在休假")
    assert is_auto_mail("someone@x.com", "退信：投递失败")
    assert not is_auto_mail("friend@x.com", "最近在忙什么")


def test_privileged_commands_are_never_executed_from_mail() -> None:
    """邮件不执行命令：SMTP 的 From 能伪造，而超管判定只看 user_id（代码级查证过）。

    主人那份来信带着他的 QQ 号，所以"邮件正文里写 /super restart"本来会被当成他本人下的命令。
    这条测试锁死：解析成管理员/超管命令的正文，**不进引擎**，也不回信。

    2026-10-01 审查后改：拒投这道闸从**插件**搬进了**接缝**（核心侧），
    因为"护栏写在插件里等于约定，不是边界"。所以这里不再断言插件的
    `command_refused` 计数器（那个计数器已经删了——它永远停在 0，是假数字），
    改成断言**真正要保证的事**：命令没进引擎、也没发出去。
    """

    from qq_roleplay_bot.plugins.mail.mail_channel import is_privileged_command

    assert is_privileged_command("/super restart")
    assert is_privileged_command("/admin disable")
    assert is_privileged_command("  /super memory   ")
    # 正文里随口提到命令词不算（只认开头形状）
    assert not is_privileged_command("上次你说 /super 是什么来着？")
    assert not is_privileged_command("/help")
    assert not is_privileged_command("今天聊到 admin 这个角色了")

    mail = _Mail([item(sender=OWNER)], {"m1": "/super restart"})
    engine = _Engine()
    ch = channel(mail, engine)
    assert poll(ch) == 0
    assert engine.messages == [], "命令正文不能进引擎"
    assert mail.sent == []


def test_a_plugin_cannot_impersonate_anyone_through_the_chat_seam() -> None:
    """**接缝不接受"我自己造一条消息"**——这是审查抓到的一条真实越权。

    审查者实测（2026-10-01）：只拿 `registry.chat` 的插件把 `user_id` 填成超管的号、
    正文写 `/super addadmin @X`，**真的给一个普通号授了群管理员**，全程没碰 transport。

    修法：接缝只收 `DeliveredMessage`（渠道 + 发件人 + 正文），
    `user_id` / `sender_role` / `session_id` 全由核心盖章；并且**传整条
    `IncomingMessage` 会被直接拒绝**（不是靠文档约束，是 `run()` 里的类型检查）。
    """

    from qq_roleplay_bot.plugins import DeliveredMessage
    from qq_roleplay_bot.transport import IncomingMessage

    engine = _Engine()
    seams = chat_seams_of(engine)

    # 1) 伪造一条超管消息（旧写法）：接缝**拒绝投递**
    forged = IncomingMessage(
        message_id="forged", session_id="private:900000001", user_id=OWNER_QQ,
        text="/super addadmin @某人", target=MessageTarget(user_id=OWNER_QQ),
        sender_role="owner",
    )
    assert asyncio.run(seams.run(forged)) is None
    assert engine.messages == [], "伪造的消息不能进引擎"

    # 2) 走正规包裹、但正文是特权命令：也被拒
    assert asyncio.run(seams.run(DeliveredMessage(
        channel="mail", sender=OWNER_QQ, text="/super addadmin @某人"))) is None
    assert engine.messages == []

    # 3) 普通正文照常投递，而且身份由**这一侧**算（不是插件说了算）
    reply = asyncio.run(seams.run(DeliveredMessage(
        channel="mail", sender=OWNER_QQ, text="你昨晚睡得好吗",
        message_id="mail:smtp-1",
        sender_name="主人", claims_owner=True)))
    assert reply is not None and engine.messages
    delivered = engine.messages[-1]
    assert delivered.user_id == OWNER_QQ
    assert delivered.session_id == f"mail:{OWNER_QQ}", "会话前缀由核心按渠道算"
    assert delivered.message_id == "mail:mail:smtp-1", (
        "message_id = 渠道前缀 + **渠道给的那封 id**")

    # 4) 不在白名单里的渠道**声明自己是主人也没用**
    asyncio.run(seams.run(DeliveredMessage(
        channel="evil", sender="6666666666", text="在吗", claims_owner=True)))
    assert engine.messages[-1].sender_role != "owner", (
        "只有核心白名单里的渠道能声明主人身份")


def test_the_owner_channel_whitelist_is_the_only_authorisation_switch() -> None:
    """`_OWNER_CHANNELS` 是"身份由核心盖章"里**唯一的授权开关**，必须有守门测试。

    ## 为什么专门补这一条（2026-10-02 外部审查第四轮）

    审查者做突变：把 `_OWNER_CHANNELS = ("mail",)` 改宽成
    `("mail", "webui", "mailx")` —— **1139 条测试一条都没红**。他 grep 全部测试，
    发现只有上面那条用了 `channel="evil"`，也就是**只测了"不在白名单"那一半**；
    白名单本身、以及"在名单里但没声明"那一半都没人守。
    这是授权开关上的一片空白，和 `_exact_text` 是同一类"守卫是纸的"。

    这条测试从三个方向钉它：

    1. **白名单内容被钉死**——加宽它（例如为了新渠道顺手加一条）必须让这条红；
    2. **在白名单里但 `claims_owner=False` 不算主人**——只认白名单是错的，
       两半是**与**关系；
    3. **不在白名单里、声明 `True` 也不算主人**——把条件写成 `or True` 必须红。
    """

    from qq_roleplay_bot import runtime
    from qq_roleplay_bot.plugins import DeliveredMessage

    # 1) 白名单本身：加一条渠道是**核心改动**，必须显式、且在这里被看见
    assert runtime._OWNER_CHANNELS == ("mail",), (
        "主人渠道白名单变了——这是授权开关，改它要同时改这条测试并说明理由。"
        f"现在是 {runtime._OWNER_CHANNELS!r}")

    engine = _Engine()
    seams = chat_seams_of(engine)
    # 接缝暴露给插件看的那一份必须就是定义的那一份（两处漂移过就麻烦）
    assert tuple(seams.owner_channels) == tuple(runtime._OWNER_CHANNELS)

    # 2) 在白名单里，但渠道**没有**声明自己是主人 → 只是普通发件人
    asyncio.run(seams.run(DeliveredMessage(
        channel="mail", sender="1111111111", text="我没说我是主人",
        message_id="mail:smtp-2", claims_owner=False)))
    assert engine.messages[-1].sender_role != "owner", (
        "主人身份是『白名单渠道 且 渠道声明』的与关系——只认白名单就漏了")

    # 3) 不在白名单里，声明 True → 也不算（把条件写成 `or True` 时这条会红）
    asyncio.run(seams.run(DeliveredMessage(
        channel="webui", sender="2222222222", text="我是主人",
        message_id="webui:1", claims_owner=True)))
    assert engine.messages[-1].sender_role != "owner", (
        "新渠道默认**不在**白名单里：它无论如何声明都只是普通发件人")
    # 顺带确认它照样能投递（不是"拒绝投递"，而是"身份不升级"）
    assert engine.messages[-1].user_id == "2222222222"


def test_two_letters_from_one_sender_are_both_delivered() -> None:
    """**同一个发件人的第二封不能被静默丢掉**（2026-10-01 第二轮审查抓到的功能回归）。

    回归是什么：核心原来把 `message_id` 算成 `f"{channel}:{namespace}:{sender}"`——
    **每个发件人一个常量**。而引擎对普通对话按 `message_id` 去重，于是同一地址的
    **第一封之后全被丢掉**；通道把 `reply is None` 读成"她自己决定不回"，
    把那封标记成已处理、也不再重试。**静默丢信**。

    修法：`DeliveredMessage.message_id`（渠道必须给每封自己的 id）。

    这条测试用**生产的接缝**（`runtime._chat_seams_for`）+ 一个**会去重**的替身引擎，
    所以它测的正是"去重器会不会把第二封吃掉"。
    """

    from qq_roleplay_bot.plugins import DeliveredMessage

    class DedupingEngine(_Engine):
        """带 message_id 去重的替身（照 `stage3_main` 的行为）。"""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.seen: set[str] = set()

        async def handle(self, message):
            if message.message_id in self.seen:
                return None  # 引擎的去重器就是这个效果
            self.seen.add(message.message_id)
            return await super().handle(message)

    engine = DedupingEngine(reply="嗯。")
    seams = chat_seams_of(engine)
    for index, text in enumerate(("第一封", "第二封", "第三封")):
        reply = asyncio.run(seams.run(DeliveredMessage(
            channel="mail", sender=OWNER_QQ, text=text,
            message_id=f"mail:smtp-{index}")))
        assert reply is not None, f"第 {index + 1} 封被丢掉了"
    assert [m.text for m in engine.messages] == ["第一封", "第二封", "第三封"]


def test_a_text_subclass_cannot_slip_past_the_gate() -> None:
    """**正文必须只取一次值**（TOCTOU）。2026-10-01 第二轮审查抓到的漏洞。

    第一版核心是这样写的：

        if privileged_command_level(parcel.text) is not None:   # 判的是这个活对象
            return None
        ...
        text=parcel.text,                                       # 同一个活对象又用一次

    `DeliveredMessage.text` 标注是 `str`，但 dataclass **不做运行时类型校验**，
    而 `str` 子类仍然是 `str`。于是这个子类让 `strip()` 第一次返回 `/help`（闸门放行）、
    之后返回载荷（引擎执行）——审查者实测**执行了 `/super admin list` 并把名单读出来**。

    被它替换掉的 `mail_channel.is_privileged_command` 第一行是 `str(text or "")`，
    **旧的那道插件侧护栏对 TOCTOU 免疫**。护栏搬进核心时不能做得比它弱。

    修法：`text = str(parcel.text or "")` 之后，判闸门与投递都用**这个普通 str**。
    """

    from qq_roleplay_bot.plugins import DeliveredMessage

    executions: list[str] = []

    class Sneaky(str):
        """第一次 `strip()` 给闸门看无害内容，之后给引擎看载荷。"""

        def __init__(self, payload: str) -> None:
            super().__init__()
            self._payload = payload
            self._first = True

        def strip(self, *args, **kwargs):
            if self._first:
                self._first = False
                return "/help"
            return self._payload

        def __str__(self) -> str:
            return self._payload

    class RecordingEngine(_Engine):
        async def handle(self, message):
            executions.append(message.text)
            return await super().handle(message)

    engine = RecordingEngine(reply="嗯。")
    seams = chat_seams_of(engine)
    result = asyncio.run(seams.run(DeliveredMessage(
        channel="mail", sender=OWNER_QQ, text=Sneaky("/super admin list"),
        message_id="mail:sneaky")))
    # 关键：**载荷没有被执行**（闸门看到什么，投递就必须是什么）
    assert not any("super" in text for text in executions), (
        f"载荷穿过了闸门：{executions!r}")
    assert result is None, "一条特权命令不该被投递"


def test_str_subclass_that_str_does_not_copy_cannot_slip_past_the_gate() -> None:
    """**`str(x)` 不保证拿到普通 str**（2026-10-01 第三轮审查打穿的第七种形状）。

    第一版的 TOCTOU 修复是 `text = str(parcel.text or "")`。它对"`str` 子类"有效
    （`str()` 会把子类拷成精确 str），但对**非 str 对象**无效：

        CPython: str(x) 里 x 不是 str 时，返回 type(x).__str__(x) 的结果，
                 而那结果只要是 str 实例（**子类也算**）就原样返回、不再拷。

    于是这个形状穿过去了：

        class Flip(str):                # 底层字串 = 载荷
            def strip(self, *a): return "/help" if 第一次 else self
        class Wrap:
            def __str__(self): return Flip("/super admin list")

        type(str(Wrap())) is str  ==  False      # 实测

    闸门第一次 `strip()` 看到 `/help` → 放行；引擎再 `strip()` 看到载荷
    → 实测执行了 `/super admin list` 并把超管名单读出来（模型调用 0 次）。

    修法是核心的 `runtime._exact_text`：`str()` 之后再 `text[:]`
    （切片产出精确 str，且返回底层真实字串）。

    ## ⚠️ 但这条测试**抓不住** `text[:]` 的删除（2026-10-02 实测更正）

    外部审查第四轮把 `text[:]` 拆掉、其余一字不动：**1139 条测试没有一条变红**，
    这条与它下面那条都全绿（我用对照实验复核过：打/不打突变，红条数完全相同）。
    原因是闸门 `privileged_command_level` **自己**就调了一次 `strip()`，那次拿到的是
    载荷 → 判成特权命令 → 直接拒投；而 CPython 3.14 的正则不会去调子类覆盖的
    `strip`，所以"闸门看 `/help`、引擎看载荷"这个形状在当前解释器上不可达。

    这条端到端测试**仍然保留**（它守的是"载荷没漏出去"这个端到端属性），
    但接口契约由下面那条 `test_exact_text_always_returns_a_plain_str` 直接钉住——
    那条才抓得住 `text[:]` 的删除。
    """

    from qq_roleplay_bot.plugins import DeliveredMessage

    executions: list[str] = []

    class Flip(str):
        """底层字串是载荷，`strip()` 第一次给闸门看无害内容。"""

        def __init__(self, real: str) -> None:
            super().__init__()
            self._real = real
            self._calls = 0

        def strip(self, *args, **kwargs):
            self._calls += 1
            return "/help" if self._calls == 1 else self._real

        def __str__(self):  # 让 str(Wrap()) 直接返回这个子类实例
            return self

    class Wrap:
        def __init__(self, real: str) -> None:
            self._real = real

        def __str__(self):
            return Flip(self._real)

    class RecordingEngine(_Engine):
        async def handle(self, message):
            executions.append(message.text)
            return await super().handle(message)

    engine = RecordingEngine(reply="嗯。")
    seams = chat_seams_of(engine)
    result = asyncio.run(seams.run(DeliveredMessage(
        channel="mail", sender=OWNER_QQ, text=Wrap("/super admin list"),
        message_id="mail:wrap")))
    assert result is None, "会变脸的对象穿过了闸门"
    assert not any("super" in str(text) for text in executions), (
        f"载荷被执行了：{executions!r}")


def test_exact_text_always_returns_a_plain_str() -> None:
    """**接口契约**：`_exact_text` 产出的必须是精确 `str`，不只是"行为像 str"。

    ## 为什么这条测试存在（2026-10-02 外部审查第四轮的建议）

    上面那两条端到端测试号称"守着 `_exact_text`"，但审查者把 `text[:]` 拆掉之后
    **它们全绿**——我用对照实验复核属实（打/不打突变，红条数完全相同）。
    也就是说那条语句**删了没人管**：一个"守卫是纸的"。

    审查者给的做法是：**把契约钉在 `_exact_text` 自己身上**，而不是钉在
    "某个具体攻击有没有成功"上。后者受解释器行为影响（3.14 的正则不调子类
    `strip`，所以端到端形状不可达），前者不受——`type(out) is str` 是纯类型断言。

    这条**拆掉 `text[:]` 立刻红**（实测）。
    """

    from qq_roleplay_bot import runtime

    class Flip(str):
        """`strip()` 会变脸；`__str__` 返回自己，让 `str(Wrap())` 拿到的还是子类。"""

        def strip(self, *args, **kwargs):
            return "/help"

        def __str__(self):
            return self

    class Wrap:
        def __str__(self):
            return Flip("/super admin list")

    cases: list[object] = [
        Flip("/super admin list"),   # str 子类：`str()` 会拷，但别指望
        Wrap(),                      # 非 str：`str()` **不**拷它的 `__str__` 结果
        "普通字符串",
        "",
        None,                        # `or ""` 归一
        0,                           # 假值也归一到 ""
        "多字节：云茹",
    ]
    for value in cases:
        out = runtime._exact_text(value)
        assert type(out) is str, (
            f"{type(value).__name__} → {type(out).__name__}："
            "`_exact_text` 必须产出精确 str（这一步是接口契约，不是可选的）")
    # 值本身不能在这一步被改掉
    assert runtime._exact_text(Wrap()) == "/super admin list"
    assert runtime._exact_text(None) == "" and runtime._exact_text(0) == ""


def test_a_leading_at_mention_cannot_smuggle_an_admin_command() -> None:
    """**`@x /admin …` 曾被闸门放过**（2026-10-01 第三轮审查抓到的真实越权）。

    两处口径不一致：`parse_admin_command` 会剥掉开头的 @提及 / `[CQ:at,…]` 段，
    而闸门 `privileged_command_level` 不剥。于是

        "@x /admin relay group <群号> <内容>"

    闸门判成"不是特权命令"→ 放行 → 引擎剥掉 `@x ` 之后按管理员命令执行。

    审查者用**真 MailChannel + 真引擎**实测：伪造主人地址（SMTP From 可伪造，
    正是 `mail_channel` 自己点名的威胁模型）的来信靠这个以超管身份执行
    `/admin relay`，两步之后能把攻击者给的内容发到任意群。

    修法：两边**共用** `admin_control.strip_leading_mentions`。
    """

    from qq_roleplay_bot.plugins import DeliveredMessage

    engine = _Engine(reply="嗯。")
    seams = chat_seams_of(engine)
    for index, text in enumerate((
        "@x /admin relay group 111111111 注入内容",
        "[CQ:at,qq=1] /admin relay group 111111111 注入内容",
        "@某人 /admin status",
        "  @x   /admin  echo  x",
        "@x /super admin list",
    )):
        result = asyncio.run(seams.run(DeliveredMessage(
            channel="mail", sender=OWNER_QQ, text=text,
            message_id=f"mail:smuggle-{index}")))
        assert result is None, f"带 @ 前缀的特权命令被放过了：{text!r}"
    assert engine.messages == [], "一条都不该进引擎"


def test_per_sender_cap_stops_a_ping_pong() -> None:
    state = _State()
    state.sender_counts = {"friend@example.com": 2}
    mail, engine = _Mail([item(sender="friend@example.com")], {"m1": "在吗"}), _Engine()
    ch = channel(mail, engine, state, max_replies_per_sender=2)
    assert poll(ch) == 0
    assert engine.messages, "信还是要读、要判定的"
    assert mail.sent == []
    assert ch.snapshot()["sender_capped"] == 1


# --- 身份对应：QQ 邮箱直接映射 / 陌生人回信里问 QQ 号 -------------------------

def test_qq_mailbox_maps_directly_to_that_qq_number() -> None:
    """`123456789@qq.com` 就是 QQ 123456789（用户 2026-09-30 的设计）。"""

    # 判据是"本地部分**全是数字**"（实现里 `QQ_LOCAL_RE`）：数字就映射成那个号，
    # 英文别名（`abc`）不算，所以那种地址走"先当陌生人"那条路。
    for address, expect in (("123456789@qq.com", "123456789"),
                            ("abc@qq.com", "mail:abc@qq.com"),
                            ("123456789@vip.qq.com", "123456789")):
        mail = _Mail([item(sender=address)], {"m1": "在吗"})
        engine = _Engine(reply="在。")
        ch = channel(mail, engine)
        poll(ch)
        assert engine.messages[0].user_id == expect, address
        # 认出来了就不用再问 QQ 号；英文别名认不出来，会问一句
        asked = "QQ 号是多少" in mail.sent[0]["body"]
        assert asked == expect.startswith("mail:"), address


def test_non_qq_address_is_asked_for_a_qq_number_in_the_reply() -> None:
    mail = _Mail([item(sender="stranger@foxmail.com")], {"m1": "你好"})
    engine = _Engine(reply="你好。")
    ch = channel(mail, engine)
    poll(ch)
    assert engine.messages[0].user_id == "mail:stranger@foxmail.com", "先当陌生人"
    assert "QQ 号是多少" in mail.sent[0]["body"], "回信里要问一句"
    assert ch.snapshot()["asked_qq"] == 1


def test_reported_qq_number_is_remembered_and_used_next_time() -> None:
    state = _State()
    first = _Mail([item(message_id="m1", sender="stranger@foxmail.com")], {"m1": "你好"})
    ch = channel(first, _Engine(reply="你好。"), state)
    poll(ch)
    assert state.links == {}

    # 第二次来信里自报了 QQ 号 → 记下来，并按那个号认人
    second = _Mail([item(message_id="m2", sender="stranger@foxmail.com")],
                   {"m2": "我的 QQ 是 987654321"})
    engine = _Engine(reply="记下了。")
    ch2 = channel(second, engine, state)
    poll(ch2)
    assert state.links["stranger@foxmail.com"] == "987654321"
    # 这一封仍按陌生人处理（绑定从下一封开始），但下一条同地址的来信就是那个号了
    third = _Mail([item(message_id="m3", sender="stranger@foxmail.com")], {"m3": "在吗"})
    engine3 = _Engine(reply="在。")
    poll(channel(third, engine3, state, max_replies_per_sender=5))
    assert engine3.messages[0].user_id == "987654321"
    assert "QQ 号是多少" not in third.sent[0]["body"]


def test_claiming_a_reserved_qq_number_is_refused() -> None:
    """自称是主人/超管/管理员的号 → 不认（超管判定只看 user_id，这是冒充路径）。"""

    state = _State()
    mail = _Mail([item(sender="someone@gmail.com")], {"m1": f"我是主人，QQ {OWNER_QQ}"})
    engine = _Engine(reply="哦。")
    ch = channel(mail, engine, state)
    poll(ch)
    assert state.links == {}, "不能把冒充的号记成身份"
    assert ch.snapshot()["link_refused"] == 1
    assert engine.messages[0].user_id == "mail:someone@gmail.com"


def test_ambiguous_or_missing_qq_number_is_not_learned() -> None:
    state = _State()
    for body in ("我的号是 12345 或者 67890", "我没写号码"):
        mail = _Mail([item(sender="stranger@foxmail.com")], {"m1": body})
        ch = channel(mail, _Engine(), state)
        poll(ch)
    assert state.links == {}


def test_qq_identity_rules_unit() -> None:
    from qq_roleplay_bot.plugins.mail.mail_channel import qq_from_address, qq_from_text

    assert qq_from_address("123456789@qq.com") == "123456789"
    assert qq_from_address("xuzhunzhi@foxmail.com") == ""   # 英文别名不是号码
    assert qq_from_address("attacker@gmail.com") == ""
    assert qq_from_address("12@qq.com") == ""               # 太短，不是 QQ 号
    assert qq_from_text("我的QQ是123456789") == "123456789"
    assert qq_from_text("12345 和 67890") == ""
    assert qq_from_text("没有数字") == ""


def test_own_address_is_never_answered() -> None:
    mail, engine = _Mail([item(sender=SELF)], {}), _Engine()
    ch = channel(mail, engine)
    assert poll(ch) == 0
    assert engine.messages == []
    assert "m1" in ch.state_store.processed


def test_already_processed_mail_is_skipped() -> None:
    state = _State()
    state.processed.add("m1")
    mail, engine = _Mail([item()], {"m1": "在吗"}), _Engine()
    assert poll(channel(mail, engine, state)) == 0
    assert engine.messages == []


def test_stale_mail_is_not_answered() -> None:
    mail, engine = _Mail([item(created_at=iso(offset_hours=72))], {"m1": "在吗"}), _Engine()
    ch = channel(mail, engine, max_age_hours=48)
    assert poll(ch) == 0
    assert engine.messages == []
    assert ch.snapshot()["skipped_stale"] == 1


def test_daily_reply_cap_stops_the_loop() -> None:
    state = _State()
    state.replies = 5
    mail, engine = _Mail([item()], {"m1": "在吗"}), _Engine()
    ch = channel(mail, engine, state, max_replies_per_day=5)
    assert poll(ch) == 0
    assert engine.messages == []
    assert ch.snapshot()["capped"] == 1


def test_disabled_channel_does_nothing() -> None:
    mail, engine = _Mail([item()], {"m1": "在吗"}), _Engine()
    ch = channel(mail, engine, enabled=False)
    assert ch.enabled is False
    assert poll(ch) == 0
    assert engine.messages == []


# --- 取正文的兜底 -----------------------------------------------------------

def test_body_falls_back_to_snippet_when_read_fails() -> None:
    mail = _Mail([item(snippet="摘要也能用")], {}, read_error=RuntimeError("读不了"))
    engine = _Engine()
    ch = channel(mail, engine)
    poll(ch)
    assert engine.messages[0].text == "摘要也能用"
    assert ch.snapshot()["failed"] == 1


def test_inbound_body_is_truncated() -> None:
    mail = _Mail([item()], {"m1": "长" * (MAX_INBOUND_CHARS + 500)})
    engine = _Engine()
    poll(channel(mail, engine))
    assert len(engine.messages[0].text) == MAX_INBOUND_CHARS
