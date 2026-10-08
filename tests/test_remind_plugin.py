"""到点提醒插件（`plugins/remind/`）的离线覆盖。

这一份钉住的是**插件侧那一半**（核心那一半在 `tests/test_remind_intent.py`，
在本体那条线上）：

1. **三个选择只在第一次问**：第一次回那句三选一，选过之后"@ 一句就够"（不再问）；
2. **没有会话时那句人话**：他选"私聊"却还没私聊过她 → 回人话，不装作发得出去；
3. **插件拿不到记忆 API**（钉住）：请求对象**只**暴露允许的那几个字段、并记下每个
   被读过的名字；再加上一条扫源码的静态断言——插件包里不许出现任何记忆/引擎/传输层的名字；
4. **到点只对到期的发**：没到点的一条都不碰，发过的不再发；
5. **邮件那一路走邮件插件的能力**：不自己写 SMTP，也不把邮件错寄给操作者。

另外几条是这只剩半边时最容易烂掉的地方：**核心还没那个口子时插件照样装得上**
（看/改那一半今天就能用）、发不出去要**记下来可重试**而不是静默算成功、
她要说出口的话里**一个机制词都不许有**。

## 关于替身

* `_FakeCore`：真的 `PluginRegistry` 有 `__slots__`（`remind` 这个口子要核心加），
  所以这里用子类挂一个——核心那一半落到本体之后，换成真注册表就是同一件事。
* `_FakeMail`：邮件接缝的替身，**多一个"寄给某个人"的口子**（真接缝现在只有
  "给操作者发一封"）。那一条是这次要跟核心/mail 对齐的假定接口之一。
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

GROUP = "717151356"
GROUP_SESSION = f"group:{GROUP}"
PERSON = "100"
#: 固定的"现在"（测试里所有时间都相对它，别让用例依赖跑的时刻）。
NOW = 1_800_000_000.0
#: 插件包在磁盘上的位置（那条扫源码的断言要用）。
PACKAGE = pathlib.Path(R.__file__).resolve().parent


async def _no_action(action, params=None):
    return None


# --- 替身 -------------------------------------------------------------------


class _WatchedRequest:
    """核心递进来的那一条：**只**暴露允许的字段，并记下每一个被读过的名字。

    插件多读一个属性（比如 `memory`、`engine`）在这里读不到，而且会被记进 `read`——
    测试最后断言"它只要了这几个"。这就是"插件拿不到记忆 API"那条钉子的一半。
    """

    #: 允许插件读的全部字段（核心那份 `RemindRequest` 的字段 + "能不能私聊"那个事实）。
    ALLOWED = ("person_id", "session_id", "group_id", "at", "what", "email",
               "private_reachable", "has_private_session", "private_ok")

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


class _FakeCore(PluginRegistry):
    """核心侧那一半的替身：**多一个 `remind` 槽位**（真注册表还没加）。"""


class _FakeMail:
    """邮件接缝的替身：把"寄给某个人"那条口子记下来，别的与真接缝一致。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.operator_sender = None

    def provide(self, sender: object) -> None:
        self.operator_sender = sender

    async def send_to(self, address: str, subject: str, body: str) -> dict:
        self.sent.append((address, subject, body))
        return {"status": "ok"}


#: "核心没告诉我们这个事实"用这个哨兵——那样那个字段**根本不存在**（不是设成 False）。
_MISSING = object()


def _request(**fields: object) -> _WatchedRequest:
    body = {"person_id": PERSON, "session_id": GROUP_SESSION, "group_id": GROUP,
            "at": NOW + 60.0, "what": "交作业", "email": "lele@example.com",
            "private_reachable": True}
    body.update(fields)
    return _WatchedRequest(**{key: value for key, value in body.items()
                              if value is not _MISSING})


def _capability(root, *, hand_off=None, mail_send=None) -> RemindCapability:
    return RemindCapability(RemindStore(root), mail_send=mail_send, hand_off=hand_off,
                            clock=lambda: NOW)


def _poller(registry):
    return next(item for item in registry.backgrounds
                if getattr(item, "name", "") == "remind")


def _command(registry):
    return next(item for item in registry.commands
                if getattr(item, "name", "") == "remind")


# --- 1. 装配（走真的 discover），以及"核心还没那个口子"那一态 -----------------


def test_the_plugin_hands_its_capability_to_the_core_through_discovery() -> None:
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QQBOT_DATA_DIR": tmp}):
        registry = _FakeCore(call_action=_no_action)
        registry.mail = _FakeMail()
        loaded = discover(registry, only=("remind",))

        assert loaded[:2] == ("mail", "remind"), f"前置要先装：{loaded}"
        assert callable(getattr(registry, "remind", None)), "核心那一侧的口子没接上"

        handed: list = []
        capability = registry.remind(lambda delivery: handed.append(delivery))
        assert isinstance(capability, RemindCapability)

        poller = _poller(registry)
        assert poller.interval_seconds > 0 and poller.enabled is True
        assert asyncio.run(poller.poll_once()) == ()      # 什么都没有时要能空跑

        command = _command(registry)
        assert command.name == "remind" and command.min_level == "public"
        assert remind_plugin.REQUIRES == ("mail",) and remind_plugin.TITLE


def test_it_still_loads_when_the_core_has_no_seam_yet() -> None:
    """核心还没有那个口子时：**不抛、照样装进来**，看/改那一半今天就能用。

    这条钉的是**过渡期**的行为：插件坏也要只坏自己那一半，不许把装配带走
    （`discover()` 会把 `register()` 抛异常的插件整个跳过——那才是静默丢功能）。
    """

    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"QQBOT_DATA_DIR": tmp}):
        registry = PluginRegistry(call_action=_no_action)   # 现状：注册表上没有 remind 这个口子

        loaded = discover(registry, only=("remind",))

        assert "remind" in loaded, "核心还没有那个口子时，插件照样要装进来"
        assert getattr(registry, "remind", None) is None
        assert _command(registry).name == "remind"
        assert asyncio.run(_poller(registry).poll_once()) == ()


# --- 2. 三个选择只在第一次问 -------------------------------------------------


def test_the_three_choices_are_asked_once_and_only_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        seen: list = []
        capability = _capability(tmp, hand_off=lambda delivery: seen.append(delivery),
                                 mail_send=_FakeMail().send_to)
        first = _request()

        question = asyncio.run(capability.plan(first))
        assert question is not None
        for line in (R.ASK_GROUP, R.ASK_PRIVATE, R.ASK_MAIL, R.ASK_TAIL):
            assert line in question, question
        assert "交作业" in question

        assert asyncio.run(capability.choose(first, "2")) == R.CHOOSE_OK[S.CHANNEL_PRIVATE]
        assert capability._store.preference(PERSON).channel == S.CHANNEL_PRIVATE

        # 第二次：**不再问**，而且这条提醒直接收下了（用户要的"@ 一句就够"）。
        second = _request(at=NOW + 7200.0, what="交物理作业")
        assert asyncio.run(capability.plan(second)) is None
        waiting = capability._store.list_for(PERSON)
        assert [item.what for item in waiting] == ["交作业", "交物理作业"]
        assert {item.channel for item in waiting} == {S.CHANNEL_PRIVATE}
        assert first.read and set(first.read) <= set(_WatchedRequest.ALLOWED)


def test_only_the_choices_that_really_work_are_offered() -> None:
    """没有邮箱（或邮件那一路没接上）时**不列第 3 条**——列一条做不到的是骗人。"""

    with tempfile.TemporaryDirectory() as tmp:
        without_mail = _capability(tmp, mail_send=None)
        with_address = asyncio.run(without_mail.plan(_request()))
        assert R.ASK_GROUP in with_address and R.ASK_PRIVATE in with_address
        assert R.ASK_MAIL not in with_address, with_address

        with_mail = _capability(tmp, mail_send=_FakeMail().send_to)
        without_address = asyncio.run(with_mail.plan(_request(email="")))
        assert R.ASK_MAIL not in without_address, without_address


def test_choosing_mail_needs_an_address() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, mail_send=_FakeMail().send_to)
        request = _request(email="")

        assert asyncio.run(capability.choose(request, "3")) == R.NEED_EMAIL
        # 没地址就不许装成"已经收下了"。
        assert capability._store.list_for(PERSON) == ()
        assert capability._store.preference(PERSON) is None


def test_choosing_mail_when_the_mail_seam_is_missing_says_so() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, mail_send=None)
        assert asyncio.run(capability.choose(_request(), "3")) == R.NO_MAIL
        assert capability._store.list_for(PERSON) == ()


def test_a_choice_that_cannot_be_read_is_asked_again() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, mail_send=_FakeMail().send_to)
        for reply in ("随便吧", "1 或者 2", "", "看情况"):
            assert asyncio.run(capability.choose(_request(), reply)) == R.ASK_AGAIN, reply
        # 没认出来就是没认出来：什么都没记下，下一次照样问（不静默记一个默认值）。
        assert capability._store.preference(PERSON) is None
        assert capability._store.list_for(PERSON) == ()
        assert asyncio.run(capability.plan(_request())) is not None


# --- 3. 没有会话时那句人话 ---------------------------------------------------


def test_the_person_who_never_private_chatted_is_told_the_plain_truth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, hand_off=lambda delivery: True,
                                 mail_send=_FakeMail().send_to)
        # 核心**没告诉**我们他能不能被私聊（字段不在）——按 fail-closed 当不能。
        silent_core = _request(private_reachable=_MISSING)
        assert asyncio.run(capability.choose(silent_core, "2")) == R.NO_PRIVATE

        # 记住了（他选的就是它），但话要说清。
        assert capability._store.preference(PERSON).channel == S.CHANNEL_PRIVATE
        assert [item.channel for item in capability._store.list_for(PERSON)] == \
            [S.CHANNEL_PRIVATE]

        # 核心说"他能被私聊"时：正常确认，没有那句人话。
        told = _request(person_id="200", private_reachable=True, at=NOW + 60.0)
        assert asyncio.run(capability.choose(told, "2")) == R.CHOOSE_OK[S.CHANNEL_PRIVATE]


# --- 4. 到点只对到期的发 -----------------------------------------------------


def test_only_the_due_ones_are_handed_over() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        handed: list = []
        capability = _capability(tmp, hand_off=lambda delivery: handed.append(delivery))
        store = capability._store
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="该发的那条", channel=S.CHANNEL_GROUP)
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW + 3600, what="还没到的那条", channel=S.CHANNEL_GROUP)

        sent = asyncio.run(capability.flush_due(now=NOW))

        assert [delivery.text for delivery in sent] == [R.DUE_TEXT.format(what="该发的那条")]
        assert [delivery.text for delivery in handed] == [d.text for d in sent]
        assert sent[0].channel == S.CHANNEL_GROUP and sent[0].mention is True
        # 发过的不再发；没到点的还在。
        assert asyncio.run(capability.flush_due(now=NOW)) == ()
        waiting = store.list_for(PERSON, now=NOW)
        assert [item.what for item in waiting] == ["还没到的那条"]
        assert waiting[0].state == S.WAITING and waiting[0].tries == 0


def test_a_failed_delivery_is_recorded_and_can_be_retried() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        calls: list = []

        async def broken(delivery):
            calls.append(delivery)
            raise RuntimeError("对面没接上")

        capability = _capability(tmp, hand_off=broken)
        store = capability._store
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="交作业", channel=S.CHANNEL_GROUP)

        assert asyncio.run(capability.flush_due(now=NOW)) == ()
        item = store.list_for(PERSON, now=NOW, include_finished=True)[0]
        assert item.state == S.FAILED and item.tries == 1 and "RuntimeError" in item.last_error

        # 下一次节拍接着试（"失败的记下来可重试"），试够次数就停。
        for _ in range(S.MAX_TRIES - 1):
            asyncio.run(capability.flush_due(now=NOW))
        assert store.due(NOW) == (), "试够次数了还在发？"
        assert len(calls) == S.MAX_TRIES, f"试的次数不对：{len(calls)}"
        stuck = store.list_for(PERSON, now=NOW)[0]
        assert stuck.state == S.FAILED and stuck.tries == S.MAX_TRIES


def test_without_the_cores_send_seam_it_is_recorded_not_swallowed() -> None:
    """核心还没把发送口递过来时：**记成没发出去**，不许静默算成功。"""

    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, hand_off=None)
        capability._store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                              at=NOW - 1, what="交作业", channel=S.CHANNEL_PRIVATE)

        assert asyncio.run(capability.flush_due(now=NOW)) == ()
        item = capability._store.list_for(PERSON, now=NOW, include_finished=True)[0]
        assert item.state == S.FAILED and "核心" in item.last_error, item


# --- 5. 邮件那一路走邮件插件的能力 -------------------------------------------


def test_the_mail_channel_goes_through_the_mail_plugins_ability() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        mail = _FakeMail()
        handed: list = []
        capability = _capability(tmp, mail_send=mail.send_to,
                                 hand_off=lambda delivery: handed.append(delivery))
        store = capability._store
        store.remember_person(PERSON, channel=S.CHANNEL_MAIL, email="lele@example.com")
        store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                  at=NOW - 1, what="交作业", channel=S.CHANNEL_MAIL)

        sent = asyncio.run(capability.flush_due(now=NOW))

        assert len(mail.sent) == 1, "邮件那一路没走邮件插件"
        address, subject, body = mail.sent[0]
        assert address == "lele@example.com"
        assert subject == R.MAIL_SUBJECT.format(what="交作业") and "交作业" in body
        assert handed == [], "邮件那一路不该再经核心的发送口发一遍"
        assert sent[0].email == "lele@example.com" and sent[0].mention is False


# --- 6. 看 / 改（命令那条入口，用的是能力上同一份函数） -----------------------


def test_the_command_lists_cancels_and_switches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, hand_off=lambda delivery: True,
                                 mail_send=_FakeMail().send_to)
        store = capability._store
        # 有地址才换得到邮件那一路（没地址时它只回一行用法，见下面那条断言）。
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

        assert asyncio.run(command.handle(_message("/提醒 删 9"))) == R.BAD_INDEX.format(index=9)
        assert asyncio.run(command.handle(_message("/提醒 瞎写"))) == R.USAGE
        assert command.match(_message("/提醒")) and not command.match(_message("提醒我"))


def _message(text: str):
    class _Message:
        user_id = PERSON
    _Message.text = text
    return _Message()


# --- 7. 钉子：插件拿不到记忆 API（源码扫 + 请求只暴露允许的字段） -------------


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
    """一条请求里只该有那几个字段——插件读别的就读不到（`_WatchedRequest` 会记下来）。"""

    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, hand_off=lambda delivery: True,
                                 mail_send=_FakeMail().send_to)
        request = _request(private_reachable=True)
        asyncio.run(capability.plan(request))
        asyncio.run(capability.choose(request, "1"))

        assert request.read, "一条都没读过？那这条断言没在验东西"
        assert set(request.read) <= set(_WatchedRequest.ALLOWED), request.read
        for forbidden in ("memory", "engine", "transport", "registry"):
            assert forbidden not in request.read
        # 能力对象上也没有引擎 / 传输层 / 记忆这三样。
        for forbidden in ("engine", "transport", "memory", "registry"):
            assert not hasattr(capability, forbidden), forbidden


# --- 8. 她要说出口的话：零机制词 ---------------------------------------------


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


# --- 9. 它自己的数据 ---------------------------------------------------------


def test_the_log_survives_a_reload_and_a_torn_line() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, hand_off=lambda delivery: True)
        capability._store.add(person_id=PERSON, session_id=GROUP_SESSION, group_id=GROUP,
                              at=NOW + 600, what="交作业", channel=S.CHANNEL_GROUP)
        # 模拟"写在半路崩了"：文件末尾留半行。
        path = pathlib.Path(tmp) / S.REMINDERS_LOG
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"key": "half')

        again = RemindStore(tmp)
        items = again.list_for(PERSON, now=NOW)
        assert [item.what for item in items] == ["交作业"]
        assert again.preference(PERSON) is None       # 没被问过


def test_the_same_request_twice_is_the_same_reminder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        capability = _capability(tmp, hand_off=lambda delivery: True)
        for _ in range(2):
            capability._store.add(person_id=PERSON, session_id=GROUP_SESSION,
                                  group_id=GROUP, at=NOW + 600, what="交作业",
                                  channel=S.CHANNEL_GROUP)
        assert len(capability._store.list_for(PERSON, now=NOW)) == 1
