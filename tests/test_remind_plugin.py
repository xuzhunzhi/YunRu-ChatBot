"""到点提醒插件（`plugins/remind/`）的离线覆盖。

核心那一半在 `tests/test_remind_intent.py` / `test_remind_email.py` / `test_remind_seam.py` /
`test_remind_send.py`（都在本体那条线上）。这一份钉住的是**插件侧那一半**：

1. **装配**：走真的 `discover()`，能力经 `registry.remind` 交上去——形状是
   **同步可调用对象** `(RemindRequest) -> None`（核心的真实形状）；
2. **只第一次问**：第一次收到提醒 → 记下"到哪儿去问"；下一拍才把问话发出去；
   他答过之后**不再问**（用户要的"以后 @ 一句就够"）；
3. **没有会话时那句人话**：他在群里选了"私聊" → 回那句人话；而且核心回
   `rejected`（真的对面拒收）时**只对他说一次**，那条提醒**留待发**；
4. **到点只对到期的发**；核心回的五种结果**各有各的处理**：
   `ok` → 已发；`no_target` / 发送口没接上 → **留待发、不烧重试次数**；
   `not_delivered` / `error` → 记一次失败、可重试（最多 3 次）；
   `uncertain` → **不再试**（重发就可能发两条）；
5. **邮件那一路走邮件插件的能力**（`MailSeams.send_to`），不经核心；
6. **插件拿不到记忆 API**（钉住）：源码静态扫 + 只暴露 `RemindRequest` 那几个字段的
   请求对象（并记下每个被读过的名字）。

另外几条：她要说出口的每一句都零机制词（`USER_TEXTS` 逐个扫）、
追加日志撞上"写了一半的行"照读不误、同一条请求重复递进来只算一条。

## 关于替身

请求与结果都用**真的**（`qq_roleplay_bot.remind.RemindRequest` / `RemindDelivery` /
`RemindSendResult`——核心那份已经落地），所以这份测试连"形状对不上"都验得到。
`registry.remind_send` 是真的槽位，直接赋值即可；`_FakeMail` 是邮件接缝的替身
（形状与 `MailSeams` 一致，两条口子都在）。
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import tempfile
from unittest.mock import patch

from qq_roleplay_bot.plugins import PluginRegistry, discover
from qq_roleplay_bot.plugins.remind import plugin as remind_plugin
from qq_roleplay_bot.plugins.remind import remind as R
from qq_roleplay_bot.plugins.remind import store as S
from qq_roleplay_bot.plugins.remind.remind import RemindCapability
from qq_roleplay_bot.plugins.remind.store import RemindStore
from qq_roleplay_bot.remind import RemindDelivery, RemindRequest, RemindSendResult

GROUP = "717151356"
GROUP_SESSION = f"group:{GROUP}"
PRIVATE_SESSION = "private:100"
PERSON = "100"
#: 固定的"现在"（所有用例的时间都相对它，别让断言依赖跑的时刻）。
NOW = 1_800_000_000.0
#: 插件包在磁盘上的位置（那条扫源码的断言要用）。
PACKAGE = pathlib.Path(R.__file__).resolve().parent


async def _no_action(action, params=None):
    return None


# --- 替身 -------------------------------------------------------------------


class _WatchedRequest:
    """**只**暴露 `RemindRequest` 的那几个字段，并记下每一个被读过的名字。

    插件多读一个属性（比如 `memory`、`engine`）在这里读不到，而且会被记进 `read`——
    用例最后断言"它只要了这几个"。这就是"插件拿不到记忆 API"那条钉子的一半。
    """

    #: 与真的 `RemindRequest` 一致（`tests/test_remind_seam.py` 把那张白名单钉在核心侧）。
    ALLOWED = ("person_id", "session_id", "group_id", "at", "what", "email")

    def __init__(self, **fields: object) -> None:
        unknown = sorted(set(fields) - set(self.ALLOWED))
        assert not unknown, f"测试自己造了不该有的字段：{unknown}"
        self._fields = dict(fields)
        self.read: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        self.read.append(name)
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name) from None


class _FakeMail:
    """邮件接缝的替身：两条口子都在（"寄给操作者"那条只为不把旧路弄坏）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.operator_sender = None

    def provide(self, sender: object) -> None:
        self.operator_sender = sender

    def provide_address_sender(self, sender: object) -> None:
        self.address_sender = sender

    def knows(self) -> bool:
        return callable(self.operator_sender)

    def knows_address_sender(self) -> bool:
        return callable(getattr(self, "address_sender", None))

    async def send_to(self, address: str, subject: str, body: str) -> dict:
        self.sent.append((address, subject, body))
        return {"status": "ok"}


def _core(into: list, *, sent: bool = True, reason: str = "ok", detail: str = ""):
    """造一个核心发送口的替身：记下来，按给定的结果如实回（**不抛异常**）。"""

    async def sender(delivery: RemindDelivery) -> RemindSendResult:
        into.append(delivery)
        return RemindSendResult(sent=sent, reason=reason, detail=detail)

    return sender


def _request(**fields: object) -> RemindRequest:
    body = {"person_id": PERSON, "session_id": GROUP_SESSION, "group_id": GROUP,
            "at": NOW + 60.0, "what": "交作业", "email": "lele@example.com"}
    body.update(fields)
    return RemindRequest(**body)  # type: ignore[arg-type]


def _capability(root, *, core=None, mail_send=None) -> RemindCapability:
    return RemindCapability(RemindStore(root), mail_send=mail_send,
                            core_sender=(lambda: core), clock=lambda: NOW)


def _poller(registry):
    return next(item for item in registry.backgrounds
                if getattr(item, "name", "") == "remind")


def _command(registry):
    return next(item for item in registry.commands
                if getattr(item, "name", "") == "remind")


# --- 1. 装配（走真的 discover），形状是同步可调用对象 ------------------------


def test_the_plugin_hands_its_capability_to_the_core_through_discovery() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QQBOT_DATA_DIR": tmp}):
        registry = PluginRegistry(call_action=_no_action)
        registry.mail = _FakeMail()
        registry.remind_send = _core([])
        loaded = discover(registry, only=("remind",))

        assert loaded[:2] == ("mail", "remind"), f"前置要先装：{loaded}"
        seam = getattr(registry, "remind", None)
        assert isinstance(seam, RemindCapability), "交上去的不是那个能力对象"
        # **同步**：核心那一侧就是 `seam(request)`（不 await、不读返回值）。
        assert not asyncio.iscoroutinefunction(seam), "接缝是同步的，不许是协程函数"
        seam(_request())
        assert len(RemindStore(pathlib.Path(tmp) / S.DIR_NAME).list_for(PERSON, now=NOW)) == 1

        poller = _poller(registry)
        assert poller.interval_seconds > 0 and poller.enabled is True
        assert remind_plugin.REQUIRES == ("mail",) and remind_plugin.TITLE
        command = _command(registry)
        assert command.name == "remind" and command.min_level == "public"


def test_it_still_loads_when_the_cores_send_seam_is_missing() -> None:
    """核心那条发送口还没接上（`remind_send is None`）：插件照装、节拍照跑，只是发不出去。"""

    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QQBOT_DATA_DIR": tmp}):
        registry = PluginRegistry(call_action=_no_action)
        registry.mail = _FakeMail()
        assert registry.remind_send is None
        discover(registry, only=("remind",))

        registry.remind(_request())
        assert asyncio.run(_poller(registry).poll_once()) == ()
        store = RemindStore(pathlib.Path(tmp) / S.DIR_NAME)
        item = store.list_for(PERSON, now=NOW)[0]
        assert item.state == S.WAITING and item.tries == 0, "没有出口不该烧重试次数"


# --- 2. 只第一次问 -----------------------------------------------------------


def test_the_question_is_asked_once_and_only_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, core=_core(handed), mail_send=_FakeMail().send_to)

        capability(_request())                              # 核心递进来（同步）
        assert handed == [], "收下请求那一瞬间不该发东西（它跑在核心那一轮对话里）"
        # 下一拍才把问话发出去。
        asyncio.run(capability.flush(now=NOW))
        assert len(handed) == 1
        question = handed[0]
        assert question.person_id == PERSON and question.group_id == GROUP
        for line in (R.ASK_GROUP, R.ASK_PRIVATE, R.ASK_MAIL, R.ASK_TAIL):
            assert line in question.text, question.text
        assert "交作业" in question.text

        # 他答了 → 记下来 + 回一句确认。
        assert capability.note_reply(PERSON, "2", session_id=PRIVATE_SESSION) == \
            R.CHOOSE_OK[S.CHANNEL_PRIVATE]
        # 再来的提醒：**不再问**，直接排上（"以后 @ 一句就够"）。
        capability(_request(at=NOW + 7200.0, what="交物理作业"))
        assert asyncio.run(capability.flush(now=NOW)) == ()
        waiting = capability._store.list_for(PERSON)
        assert [item.what for item in waiting] == ["交作业", "交物理作业"]
        assert {item.channel for item in waiting} == {S.CHANNEL_PRIVATE}


def test_the_question_is_not_repeated_while_he_has_not_answered() -> None:
    """他还没答的时候：问话**只发一次**，不会每一拍都说一遍"你想我怎么叫你"。"""

    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, core=_core(handed), mail_send=_FakeMail().send_to)

        capability(_request())
        asyncio.run(capability.flush(now=NOW))
        assert len(handed) == 1
        # 他还没答，再跑三拍：一句都不该再问。
        for _ in range(3):
            asyncio.run(capability.flush(now=NOW))
        assert len(handed) == 1, f"问话被重复发了：{[item.text for item in handed]}"

        # 他还没答时又说了一条提醒：**还是只有那一句**问话（不是一人一句）。
        capability(_request(at=NOW + 3600.0, what="带伞"))
        asyncio.run(capability.flush(now=NOW))
        assert len(handed) == 1, "又来一条就又问了一遍"
        # 问的是**先说**的那一条（问话已经发出去了，不再被后面那条改写）。
        assert "交作业" in handed[0].text and "带伞" not in handed[0].text


def test_the_person_who_never_private_chatted_is_told_the_plain_truth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
        capability(_request())
        asyncio.run(capability.flush(now=NOW))

        # 他这句话在**群里**说的 → 看不到"他私聊过她"的证据 → 人话（fail-closed）。
        assert capability.note_reply(PERSON, "2", session_id=GROUP_SESSION) == R.NO_PRIVATE
        assert capability._store.preference(PERSON).channel == S.CHANNEL_PRIVATE

        # 他这句话是**私聊里**说的 → 正常确认。
        assert capability.note_reply(PERSON, "2", session_id=PRIVATE_SESSION) == \
            R.CHOOSE_OK[S.CHANNEL_PRIVATE]


def test_the_question_path_classifies_results_like_the_due_path() -> None:
    """**审查 2026-10-09 那条**：问话那条路原来只分"成功 / 没有出口"，
    于是 `uncertain`/`rejected`/`not_delivered`/`error` 一律只记一行日志、状态不动 →
    每 30 秒重发、永不封顶（实测 4 拍各送出 4 次），与模块头那张表**自相矛盾**。
    """

    # 四种"确定/不必再试"的结果：各只该送出**有限**的次数，然后收口。
    for reason, expected in (("uncertain", 1), ("rejected", 1),
                             ("not_delivered", S.MAX_TRIES), ("error", S.MAX_TRIES)):
        with tempfile.TemporaryDirectory() as tmp:
            calls: list = []
            capability = _capability(tmp, core=_core(calls, sent=False, reason=reason),
                                     mail_send=_FakeMail().send_to)
            capability(_request())
            for _ in range(5):                       # 5 拍
                asyncio.run(capability.flush(now=NOW))
            assert len(calls) == expected, (reason, len(calls))
            # 收口之后：要么"问过了"，要么"试够了"——都不该再排着。
            assert capability._store.asking() == (), reason
            assert capability._store.preference(PERSON).asked is True, reason

    # `no_target` / 接缝没接上：**留待发**——每拍都试，但**不烧次数**、不算失败。
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        capability = _capability(tmp, core=_core(calls, sent=False, reason="no_target"))
        capability(_request())
        for _ in range(5):
            asyncio.run(capability.flush(now=NOW))
        assert len(calls) == 5, "没有出口时每拍都会试一次（留待发）"
        pref = capability._store.preference(PERSON)
        assert pref.ask_tries == 0 and pref.asked is False and pref.ask_session
        assert capability._store.asking() != (), "没有出口时不该把问话丢掉"

    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=None)     # 接缝还没接上
        capability(_request())
        for _ in range(3):
            asyncio.run(capability.flush(now=NOW))
        pref = capability._store.preference(PERSON)
        assert pref.ask_tries == 0 and pref.asked is False and pref.ask_session


def test_a_choice_that_cannot_be_read_is_asked_again() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
        capability(_request())
        for reply in ("随便吧", "1 或者 2", "", "看情况"):
            assert capability.note_reply(PERSON, reply, session_id=GROUP_SESSION) == \
                R.ASK_AGAIN, reply
        # 没认出来就是没认出来：渠道还是空的，下一次照样问（不静默记一个默认值）。
        assert not capability._store.preference(PERSON).channel
        # 他手上有邮箱、邮件那一路也接上了 → 选 3 就是真能办到的，正常答应。
        assert capability.note_reply(PERSON, "3", session_id=GROUP_SESSION) == \
            R.CHOOSE_OK[S.CHANNEL_MAIL]
        # 换一个**没有邮箱**的人：这时选 3 只能先跟他要地址（不许答应做不到的事）。
        empty = _capability(pathlib.Path(tmp) / "no-email", core=_core([]),
                            mail_send=_FakeMail().send_to)
        assert empty.note_reply("200", "3", session_id=GROUP_SESSION) == R.NEED_EMAIL


def test_only_the_choices_that_really_work_are_offered() -> None:
    """没有邮箱（或邮件那一路没接上）时**不列第 3 条**——列一条做不到的是骗人。"""

    with tempfile.TemporaryDirectory() as tmp:
        without_mail = _capability(tmp, core=_core([]), mail_send=None)
        with_address = without_mail.ask_text("交作业", has_email=True)
        assert R.ASK_GROUP in with_address and R.ASK_PRIVATE in with_address
        assert R.ASK_MAIL not in with_address, with_address

        with_mail = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
        without_address = with_mail.ask_text("交作业", has_email=False)
        assert R.ASK_MAIL not in without_address, without_address

        # 没接上邮件那一路时选 3 → 一句人话，而不是"答应得好好的"。
        assert without_mail.note_reply(PERSON, "3", session_id=GROUP_SESSION) == R.NO_MAIL


# --- 3. 到点只对到期的发；核心回的每一种结果各有各的处理 ---------------------


def test_only_the_due_ones_are_handed_over() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, core=_core(handed))
        store = capability._store
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="该发的那条", channel=S.CHANNEL_GROUP)
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW + 3600, what="还没到的那条", channel=S.CHANNEL_GROUP)

        sent = asyncio.run(capability.flush(now=NOW))

        assert [item.text for item in sent] == [R.DUE_TEXT.format(what="该发的那条")]
        assert sent[0].person_id == PERSON and sent[0].group_id == GROUP
        assert [item.what for item in store.list_for(PERSON, now=NOW)] == ["还没到的那条"]
        # 再跑一拍：发过的不再发。
        assert asyncio.run(capability.flush(now=NOW)) == ()
        assert len(handed) == 1


def test_without_a_way_out_it_stays_pending_and_burns_no_retry() -> None:
    """**没有出口**（发送口没接上 / 核心说 no_target）：留待发、不烧次数。"""

    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=None)
        capability._store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                              at=NOW - 1, what="交作业", channel=S.CHANNEL_PRIVATE)

        for _ in range(5):
            assert asyncio.run(capability.flush(now=NOW)) == ()

        item = capability._store.list_for(PERSON, now=NOW)[0]
        assert item.state == S.WAITING and item.tries == 0, item
        assert "还没接上" in item.last_error, item      # 把"为什么还没发出去"记在那条上
        # 接缝一接上，同一条立刻发得出去（前面那 5 拍什么都没烧掉）。
        handed: list = []
        capability._core_sender = lambda: _core(handed)
        assert len(asyncio.run(capability.flush(now=NOW))) == 1
        assert capability._store.get(item.key).state == S.SENT

        # 核心说"找不到那个人"（no_target）也是同一种：留待发。
        other = _capability(pathlib.Path(tmp) / "b",
                            core=_core([], sent=False, reason="no_target"))
        other._store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                         at=NOW - 1, what="交作业", channel=S.CHANNEL_PRIVATE)
        for _ in range(3):
            assert asyncio.run(other.flush(now=NOW)) == ()
        kept = other._store.list_for(PERSON, now=NOW)[0]
        assert kept.state == S.WAITING and kept.tries == 0 and "no_target" in kept.last_error


def test_a_failed_delivery_is_recorded_and_can_be_retried() -> None:
    """核心说"确定没送出去"（`not_delivered` / `error`）：记一次失败，可重试。"""

    with tempfile.TemporaryDirectory() as tmp:
        calls: list = []
        capability = _capability(tmp, core=_core(calls, sent=False, reason="not_delivered"))
        store = capability._store
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="交作业", channel=S.CHANNEL_GROUP)

        assert asyncio.run(capability.flush(now=NOW)) == ()
        item = store.list_for(PERSON, now=NOW)[0]
        assert item.state == S.FAILED and item.tries == 1 and "not_delivered" in item.last_error

        for _ in range(S.MAX_TRIES - 1):
            asyncio.run(capability.flush(now=NOW))
        assert store.due(NOW) == (), "试够次数了还在发？"
        assert len(calls) == S.MAX_TRIES, f"试的次数不对：{len(calls)}"


def test_a_rejected_private_reminder_tells_him_once_and_stays_pending() -> None:
    """核心回 `rejected`（他还没私聊过她）：说一次人话，而且**留待发**。"""

    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []

        async def rejected(delivery: RemindDelivery) -> RemindSendResult:
            handed.append(delivery)
            if delivery.text == R.NO_PRIVATE:
                return RemindSendResult(True, "ok")          # 人话发到群里成了
            return RemindSendResult(False, "rejected", "还不是好友")

        capability = _capability(tmp, core=rejected)
        store = capability._store
        store.remember_person(PERSON, channel=S.CHANNEL_PRIVATE)
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="交作业", channel=S.CHANNEL_PRIVATE)

        asyncio.run(capability.flush(now=NOW))
        assert [item.text for item in handed][0] == R.DUE_TEXT.format(what="交作业")
        assert R.NO_PRIVATE in [item.text for item in handed], "那句人话没跟他说"
        item = store.list_for(PERSON, now=NOW)[0]
        assert item.state == S.WAITING and item.tries == 0, item
        assert store.preference(PERSON).told_rejected is True

        # 下一拍：**不再重复说**那句人话（否则就是每半分钟骚扰他一次）。
        asyncio.run(capability.flush(now=NOW))
        assert [item.text for item in handed].count(R.NO_PRIVATE) == 1


def test_uncertain_is_never_retried() -> None:
    """核心回 `uncertain`（不知道那条出去没有）：**不再试**——重发就可能发两条。"""

    with tempfile.TemporaryDirectory() as tmp:
        calls: list = []
        capability = _capability(tmp, core=_core(calls, sent=False, reason="uncertain"))
        store = capability._store
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="交作业", channel=S.CHANNEL_GROUP)

        asyncio.run(capability.flush(now=NOW))
        assert len(calls) == 1
        for _ in range(3):
            asyncio.run(capability.flush(now=NOW))
        assert len(calls) == 1, "uncertain 又发了一遍（可能重复提醒）"
        item = next(iter(store._all().values()))     # noqa: SLF001 - 直接看那条的状态
        assert item.state == S.GIVEN_UP and "uncertain" in item.last_error
        assert store.list_for(PERSON, now=NOW) == (), "已经放弃的还挂在待发列表里"


def test_an_unanswered_channel_falls_back_to_where_he_spoke() -> None:
    """他没答就用**他当时说话的地方**叫他，并把这次的选择记下来（以后不再问）。"""

    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, core=_core(handed))
        capability(_request(session_id=GROUP_SESSION))
        asyncio.run(capability.flush(now=NOW))          # 问话发出去了
        # 他没答，时间到了。
        asyncio.run(capability.flush(now=NOW + 3600))

        assert handed[-1].group_id == GROUP, handed[-1]
        assert capability._store.preference(PERSON).channel == S.CHANNEL_GROUP
        assert capability._store.preference(PERSON).ask_session == ""


# --- 4. 邮件那一路走邮件插件的能力 -------------------------------------------


def test_the_mail_channel_goes_through_the_mail_plugins_ability() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mail = _FakeMail()
        handed: list = []
        capability = _capability(tmp, core=_core(handed), mail_send=mail.send_to)
        store = capability._store
        store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)

        sent = asyncio.run(capability.flush(now=NOW))

        assert len(mail.sent) == 1, "邮件那一路没走邮件插件"
        address, subject, body = mail.sent[0]
        assert address == "lele@example.com"
        assert subject == R.MAIL_SUBJECT and "交作业" in body
        assert handed == [], "邮件那一路不该再经核心发一遍"
        assert sent == ()


class _BadMail:
    """邮件接缝的替身：一寄就抛给定的那个异常（`kind` 分类那条路要用）。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def send_to(self, address: str, subject: str, body: str) -> dict:
        self.calls += 1
        raise self.error


def _mail_refusal(call):
    """跑一次 mail 那边的校验，把**它真的会抛的那个**异常拿到手（别自己造一个像的）。"""

    from qq_roleplay_bot.plugins.mail.mail_client import MailError

    try:
        call()
    except MailError as exc:
        return exc
    raise AssertionError("这一句居然没被邮件那边拒——那这条用例就没在验东西")


def test_the_subject_is_fixed_so_quotes_and_ampersands_still_go_out() -> None:
    """审查 2026-10-09 第 5 条：主题原来是 `到点提醒：{what}`，而主题是邮件那边**唯一进
    命令行**的自由文本，`& " %` 这些字符会让它**拒发**——于是"提醒里带了引号"就等于
    "这封信永远寄不出去"。现在主题是固定文案，`what` 只进正文（正文走文件）。"""

    with tempfile.TemporaryDirectory() as tmp:
        mail = _FakeMail()
        capability = _capability(tmp, core=_core([]), mail_send=mail.send_to)
        store = capability._store
        store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what='交"作业" & 带伞 50%', channel=S.CHANNEL_MAIL)

        asyncio.run(capability.flush(now=NOW))

        assert len(mail.sent) == 1
        _address, subject, body = mail.sent[0]
        assert subject == R.MAIL_SUBJECT
        assert not (set(subject) & set('"\'`%&|<>^!()$')), subject
        # 原话照旧进正文（那里不走命令行，这些字符没问题）。
        assert '&' in body and '%' in body and '交"作业"' in body


def test_a_mail_failure_that_will_never_succeed_is_refused_not_retried() -> None:
    """**确定性失败不许当"可重试"**（审查第 5 条）：地址/格式不合邮件那边的口径时，
    原来会白烧三次、然后永远卡在"待发"里且用户看不到原因。现在：试**一次**、
    跟他说一句人话（**按人只一次**）、把这条收口。"""

    from qq_roleplay_bot.plugins.mail.mail_client import clean_email, clean_subject

    refusals = [
        _mail_refusal(lambda: clean_email("lele@example.c0m")),      # 地址口径不一致
        _mail_refusal(lambda: clean_subject('提醒他交"作业"')),        # 主题含禁用字符
    ]
    for refusal in refusals:
        with tempfile.TemporaryDirectory() as tmp:
            handed: list = []
            mail = _BadMail(refusal)
            capability = _capability(tmp, core=_core(handed), mail_send=mail.send_to)
            store = capability._store
            store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)

            for _ in range(4):                       # 4 拍
                asyncio.run(capability.flush(now=NOW))

            assert mail.calls == 1, f"{refusal.kind} 被重试了 {mail.calls} 次"
            told = [item.text for item in handed]
            assert told.count(R.MAIL_FAILED) == 1, told      # 人话：按人只说一次
            assert capability._store.list_for(PERSON, now=NOW) == ()   # 收口了，不再挂着
            item = next(iter(capability._store._all().values()))       # noqa: SLF001
            assert item.state == S.GIVEN_UP and refusal.kind in item.last_error

        # 对照：**可重试**的那一类（网络那种）照旧试满 MAX_TRIES。
        with tempfile.TemporaryDirectory() as tmp:
            mail = _BadMail(RuntimeError("网络断了"))
            capability = _capability(tmp, core=_core([]), mail_send=mail.send_to)
            store = capability._store
            store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)
            for _ in range(5):
                asyncio.run(capability.flush(now=NOW))
            assert mail.calls == S.MAX_TRIES, mail.calls


def test_the_mail_channel_has_a_per_person_and_a_global_daily_cap() -> None:
    """审查第 1 条（人裁，按最保守）：任何能触发提醒的人原来三步就能让**操作者的邮箱**
    替他寄信、条数不限。现在每人每天 1 封、全局每天 5 封（调用时现读 + 钳制），
    超限就回一句人话、**不烧重试次数**、而且每次尝试都留一行审计（不含正文）。"""

    def env(**overrides):
        return patch.dict(os.environ, {**{"QQBOT_DATA_DIR": ""}, **overrides})

    # 每人每天 1 封：同一个人两条到点 → 只寄一封，第二条被配额挡住并告诉他。
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"QQBOT_REMIND_MAIL_PER_PERSON_PER_DAY": "1",
                                     "QQBOT_REMIND_MAIL_PER_DAY": "5"}):
            handed: list = []
            mail = _FakeMail()
            capability = _capability(tmp, core=_core(handed), mail_send=mail.send_to)
            store = capability._store
            store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 2, what="带伞", channel=S.CHANNEL_MAIL)

            asyncio.run(capability.flush(now=NOW))

            assert len(mail.sent) == 1, "每人每天 1 封没生效"
            assert [item.text for item in handed].count(R.MAIL_QUOTA) == 1, handed
            assert capability._store.mail_sent_on(capability._today(NOW),
                                                  person=PERSON) == 1

    # 全局每天 5 封：6 个人各一条 → 只寄 5 封。
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"QQBOT_REMIND_MAIL_PER_PERSON_PER_DAY": "1",
                                     "QQBOT_REMIND_MAIL_PER_DAY": "5"}):
            mail = _FakeMail()
            capability = _capability(tmp, core=_core([]), mail_send=mail.send_to)
            store = capability._store
            for index in range(6):
                who = str(200 + index)
                store.remember_person(who, channel=S.CHANNEL_MAIL, email=f"p{index}@example.com")
                store.add(person_id=who, session_id=GROUP_SESSION, group_id=GROUP,
                          at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)
            asyncio.run(capability.flush(now=NOW))
            assert len(mail.sent) == 5, len(mail.sent)

    # 配置写歪了 / 越界：钳制，不许把上限放开。
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"QQBOT_REMIND_MAIL_PER_PERSON_PER_DAY": "999",
                                     "QQBOT_REMIND_MAIL_PER_DAY": "不是数"}):
            capability = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
            assert capability._quota() == (R.QUOTA_PERSON_MAX, R.QUOTA_TOTAL_DEFAULT)
        with patch.dict(os.environ, {"QQBOT_REMIND_MAIL_PER_PERSON_PER_DAY": "0",
                                     "QQBOT_REMIND_MAIL_PER_DAY": "0"}):
            capability = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
            assert capability._quota() == (0, 0)
            store = capability._store
            store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)
            mail = capability._mail_send
            asyncio.run(capability.flush(now=NOW))
            assert store.due(NOW) == () and capability._store.mail_sent_on(
                capability._today(NOW)) == 0


def test_the_mail_audit_line_never_carries_the_body(caplog=None) -> None:
    """审计留一行（谁 · 寄到哪 · 成没成），**正文不进日志**。"""

    import logging as _logging

    with tempfile.TemporaryDirectory() as tmp:
        records: list = []

        class _Grab(_logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Grab()
        logger = _logging.getLogger("qq_roleplay_bot.plugins.remind.remind")
        before = logger.level
        logger.addHandler(handler)
        logger.setLevel(_logging.INFO)          # 审计那一行是 INFO，别被根的级别挡掉
        try:
            mail = _FakeMail()
            capability = _capability(tmp, core=_core([]), mail_send=mail.send_to)
            store = capability._store
            store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)
            asyncio.run(capability.flush(now=NOW))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(before)

        audit = [line for line in records if line.startswith("remind_mail_audit")]
        assert len(audit) == 1, records
        assert "lele@example.com" in audit[0] and "result=sent" in audit[0]
        assert "交作业" not in audit[0], audit[0]        # 正文不许进日志


def test_the_pending_list_and_one_tick_are_both_capped() -> None:
    """审查第 4 条：实测"一次 300 条全在一拍交出去"，而且发过的条目一条不清。"""

    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, core=_core(handed))
        store = capability._store
        for index in range(40):                    # 一个人塞 40 条
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW + 600 + index, what=f"第{index}条", channel=S.CHANNEL_GROUP)
        live = store.list_for(PERSON, now=NOW)
        assert len(live) == S.MAX_LIVE_PER_PERSON, len(live)
        # 丢的是**最旧**的（第 0..7 条），最新的还在。
        assert live[-1].what == "第39条"
        assert "第0条" not in [item.what for item in live]

    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, core=_core(handed))
        store = capability._store
        for index in range(30):                    # 30 条**全部到点**
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1 - index, what=f"第{index}条", channel=S.CHANNEL_GROUP)
        sent = asyncio.run(capability.flush(now=NOW))
        assert len(sent) == R.MAX_PER_TICK, len(sent)
        assert len(store.list_for(PERSON, now=NOW)) == 30 - R.MAX_PER_TICK
        # 下一拍接着发剩下的（不是丢掉）。
        sent2 = asyncio.run(capability.flush(now=NOW))
        assert len(sent2) == 10, len(sent2)
        assert store.list_for(PERSON, now=NOW) == ()


def test_finished_entries_do_not_pile_up_forever() -> None:
    """发完的条目留 7 天就从待发数据里清掉（审查："已发的 300 条一条不清"）。"""

    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]))
        store = capability._store
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="刚发完的", channel=S.CHANNEL_GROUP)
        key = store.list_for(PERSON, now=NOW)[0].key
        store.mark_sent(key)
        # 让它"发完很久了"：把时钟往后拨 8 天。
        store._clock = lambda: NOW + 8 * 86400          # noqa: SLF001
        store.compact()
        store.compact()                                  # 真写一次
        assert store.get(key) is None, "发完 8 天的条目还留在数据里"


def test_the_plain_truth_is_told_once_per_person_not_per_reminder() -> None:
    """审查第 3 条：三条提醒同时到点且都被拒 → 原来他在群里收到**三遍**同一句人话。"""

    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []

        async def rejected(delivery: RemindDelivery) -> RemindSendResult:
            handed.append(delivery)
            if delivery.text == R.NO_PRIVATE:
                return RemindSendResult(True, "ok")          # 人话发到群里成了
            return RemindSendResult(False, "rejected", "还不是好友")

        capability = _capability(tmp, core=rejected)
        store = capability._store
        store.remember_person(PERSON, channel=S.CHANNEL_PRIVATE)
        for index, what in enumerate(("交作业", "带伞", "买牛奶")):
            store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                      at=NOW - 1 - index, what=what, channel=S.CHANNEL_PRIVATE)

        asyncio.run(capability.flush(now=NOW))

        told = [item.text for item in handed]
        assert told.count(R.NO_PRIVATE) == 1, f"同一句人话说了 {told.count(R.NO_PRIVATE)} 遍"
        # 三条都还在待发里（他私聊过她之后都发得出去）。
        assert len(store.list_for(PERSON, now=NOW)) == 3
        assert store.preference(PERSON).told_rejected is True


# --- 5. 看 / 改（命令那条入口用的是能力上同一份函数） -----------------------


def test_the_command_lists_cancels_and_switches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
        store = capability._store
        store.remember_person(PERSON, channel=S.CHANNEL_PRIVATE, email="lele@example.com")
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW + 600, what="交作业", channel=S.CHANNEL_PRIVATE)
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW + 1200, what="带伞", channel=S.CHANNEL_PRIVATE)
        command = remind_plugin.RemindCommand(capability)

        listed = asyncio.run(command.handle(_message("/提醒")))
        assert listed.startswith(R.LIST_HEAD)
        assert "1 " in listed and "交作业" in listed
        assert "2 " in listed and "带伞" in listed, listed
        assert "私聊" in listed

        gone = asyncio.run(command.handle(_message("/提醒 删 1")))
        assert "交作业" in gone
        assert [item.what for item in store.list_for(PERSON, now=NOW)] == ["带伞"]

        switched = asyncio.run(command.handle(_message("/提醒 换 邮箱")))
        assert switched == R.CHOOSE_OK[S.CHANNEL_MAIL]
        assert store.preference(PERSON).channel == S.CHANNEL_MAIL

        assert asyncio.run(command.handle(_message("/提醒 删 9"))) == \
            R.BAD_INDEX.format(index=9)
        assert asyncio.run(command.handle(_message("/提醒 瞎写"))) == R.USAGE
        assert command.match(_message("/提醒")) and not command.match(_message("提醒我"))


def _message(text: str, *, session_id: str = GROUP_SESSION):
    class _Message:
        user_id = PERSON
    _Message.text = text
    _Message.session_id = session_id
    return _Message()


# --- 6. 钉子：插件拿不到记忆 API（源码扫 + 请求只暴露允许的字段） -------------


#: 插件包里**一个字都不许出现**的名字（记忆 / 知识库 / 对话 / 引擎 / 传输层 / 权限名单）。
FORBIDDEN = ("memory", "Memory", "knowledge", "Knowledge", "persona", "base_prompt",
             "prompt_library", "slang", "quote_learning", "engine", "transport",
             "admin_user_ids", "super_admin", "chat_log", "data_boundary")


def test_the_plugin_source_never_names_a_memory_or_engine_api() -> None:
    """扫的是**代码里的名字**（import / 属性 / 变量），不是注释与说明文字。"""

    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            for name in names:
                for bad in FORBIDDEN:
                    if bad in name:
                        offenders.append(f"{path.name}: {name}")
    assert not offenders, "插件里出现了不该碰的东西：" + "；".join(offenders)


def test_the_request_object_is_the_whole_world_it_gets() -> None:
    """交进来的那条请求里只该有那几个字段——插件读别的就读不到（会被记下来）。"""

    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]), mail_send=_FakeMail().send_to)
        watched = _WatchedRequest(person_id=PERSON, session_id=GROUP_SESSION,
                                  group_id=GROUP, at=NOW + 60.0, what="交作业",
                                  email="lele@example.com")

        capability(watched)
        asyncio.run(capability.flush(now=NOW))
        capability.note_reply(PERSON, "1", session_id=GROUP_SESSION)

        assert watched.read, "一条都没读过？那这条断言没在验东西"
        assert set(watched.read) <= set(_WatchedRequest.ALLOWED), watched.read
        for forbidden in ("memory", "engine", "transport", "registry"):
            assert forbidden not in watched.read
        # 能力对象上也没有引擎 / 传输层 / 记忆 / 权限名单这几样。
        for forbidden in ("engine", "transport", "memory", "registry",
                          "admin_user_ids", "super_admin_user_ids"):
            assert not hasattr(capability, forbidden), forbidden


# --- 7. 她要说出口的话：零机制词 ---------------------------------------------


def test_her_words_have_no_mechanism_words() -> None:
    banned = ("检查", "触发", "调用", "协议", "接口", "提示词", "上下文", "记忆库",
              "记忆", "Stage", "stage", "API", "api", "校验", "令牌", "插件", "系统")
    for text in R.USER_TEXTS:
        for word in banned:
            assert word not in text, f"「{word}」不许出现在她要说的话里：{text}"


def test_every_sentence_it_says_is_declared_scan_able() -> None:
    """新加句子要同时进 `USER_TEXTS`（否则上面那条扫不到它）。"""

    assert R.DUE_TEXT in R.USER_TEXTS and R.MAIL_BODY in R.USER_TEXTS
    assert len(R.USER_TEXTS) == len(set(R.USER_TEXTS))
    assert all(isinstance(text, str) and text for text in R.USER_TEXTS)


# --- 8. 它自己的数据 ---------------------------------------------------------


def test_the_log_survives_a_reload_and_a_torn_line() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]))
        capability._store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                              at=NOW + 600, what="交作业", channel=S.CHANNEL_GROUP)
        # 模拟"写在半路崩了"：文件末尾留半行。
        path = pathlib.Path(tmp) / S.REMINDERS_LOG
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"key": "half')

        again = RemindStore(tmp)
        assert [item.what for item in again.list_for(PERSON, now=NOW)] == ["交作业"]
        assert again.preference(PERSON) is None       # 没被问过


def test_the_same_request_twice_is_the_same_reminder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, core=_core([]))
        for _ in range(2):
            capability._store.add(person_id=PERSON, session_id=GROUP_SESSION,
                                  group_id=GROUP, at=NOW + 600, what="交作业",
                                  channel=S.CHANNEL_GROUP)
        assert len(capability._store.list_for(PERSON, now=NOW)) == 1
