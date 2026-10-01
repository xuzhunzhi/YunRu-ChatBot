"""读信回信：只认主人、进引擎、寄回复，以及几条防呆（幂等 / 旧信 / 上限 / 不回自己）。"""
import asyncio
import time

from qq_roleplay_bot.mail_channel import MAX_INBOUND_CHARS, MailChannel
from qq_roleplay_bot.transport import OutgoingMessage, MessageTarget

OWNER = "xuzhunzhi@foxmail.com"
OWNER_QQ = "900000001"
SELF = "shiyunru@agent.qq.com"
TARGET = MessageTarget(user_id=OWNER_QQ)


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
        mail_client=mail, state_store=state or _State(), engine=engine,
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
    from qq_roleplay_bot.mail_channel import is_auto_mail

    assert is_auto_mail("noreply@x.com")
    assert is_auto_mail("someone@x.com", "Out of office: 我在休假")
    assert is_auto_mail("someone@x.com", "退信：投递失败")
    assert not is_auto_mail("friend@x.com", "最近在忙什么")


def test_privileged_commands_are_never_executed_from_mail() -> None:
    """邮件不执行命令：SMTP 的 From 能伪造，而超管判定只看 user_id（代码级查证过）。

    主人那份来信带着他的 QQ 号，所以"邮件正文里写 /super restart"本来会被当成他本人下的命令。
    这条测试锁死：解析成管理员/超管命令的正文，**不进引擎**，也不回信。
    """

    from qq_roleplay_bot.mail_channel import is_privileged_command

    assert is_privileged_command("/super restart")
    assert is_privileged_command("/admin disable")
    assert is_privileged_command("  /super memory   ")
    # 正文里随口提到命令词不算（只认第一行以 / 开头且带 super/admin 的写法）
    assert not is_privileged_command("上次你说 /super 是什么来着？") 
    assert not is_privileged_command("/help")
    assert not is_privileged_command("今天聊到 admin 这个角色了")

    mail = _Mail([item(sender=OWNER)], {"m1": "/super restart"})
    engine = _Engine()
    ch = channel(mail, engine)
    assert poll(ch) == 0
    assert engine.messages == [], "命令正文不能进引擎"
    assert mail.sent == []
    assert ch.snapshot()["command_refused"] == 1


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
    from qq_roleplay_bot.mail_channel import qq_from_address, qq_from_text

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
