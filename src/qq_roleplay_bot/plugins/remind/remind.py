"""到点提醒——**插件侧那一半**：问一次怎么叫他、记下来、到点把"要发什么"交出去。

## 形状（用户 2026-10-08 拍板，核心那一侧已经落地）

**核心听懂 + 插件做事 + 核心发送**。群里一句"云茹，8 点提醒我交作业"是**对话调起**的：
核心在它那次判定里认出来、把时间算成**绝对时刻**、把邮箱从记忆里查好，然后交给本插件。

两个口子，形状都是核心定的（这里一个字都不改）：

```python
registry.remind = <可调用对象>                  # 插件放上来
seam(request)                                  # 核心这样调：同步，RemindRequest 唯一入参，返回值不读

registry.remind_send = <可调用对象>              # 核心放上来（缺省 None = 这侧没接上）
async (RemindDelivery) -> RemindSendResult      # 只回结果、**不抛异常**
```

`RemindDelivery(person_id, text, group_id="")` —— `group_id` 空串 = 私聊；群里 **@ 他由核心负责**
（没有 @ 能力就退回纯文本，消息照发——那是核心的决定，插件不管）。
`RemindSendResult(sent, reason, detail="")`，`reason ∈ ok / no_target / not_delivered /
uncertain / rejected / error`。**计数与重试归本插件**（核心只如实回结果）。

⚠️ 所以本能力对象的 `__call__` 是**同步、只落盘、不做 IO**——它跑在核心那一轮对话里，
不该在那儿等网络；真正发东西全在**本插件的后台节拍**（`flush`）里。

## 三种结局，三种处理（这是这一份最要紧的规则）

| 核心回的 | 意思是 | 本插件怎么做 |
| --- | --- | --- |
| `sent=True` / `ok` | 发出去了 | 记成已发（不再发） |
| `no_target`、或发送口是 `None` | **没有出口**（还没接上 / 找不到那个人） | **留在待发**，不算失败、**不烧重试次数**，每拍一行 warning |
| `not_delivered` / `error` | 确定没送出去 | 记一次失败，**可重试**（最多 `MAX_TRIES` 次） |
| `rejected` | 对面拒收（他还没私聊过她） | 1）**只对他说一次**"先私聊我一次…"；2）留待发（他私聊过就能发了） |
| `uncertain` | 不知道出去没有 | 记成**不再试**（`GIVEN_UP`）——重发就可能发两条 |

## 三件事分别在哪里发生

| 什么时候 | 谁做 | 做什么 |
| --- | --- | --- |
| 核心认到提醒请求 | `__call__(request)`（同步） | 收下这条提醒；**第一次**还要记下"到哪个会话里问他" |
| 本插件的后台节拍 | `flush(now)` | ① 把还没问出去的问话发出去；② 把到点的提醒交出去 |
| 他答了（`/提醒 换 2` 那类） | `note_reply(...)` | 记下他选的方式（答过就不再问） |

## 邮箱那一路走邮件插件的能力（不走核心）

`registry.mail.send_to(address, subject, body)`——"寄给**指定的一个**地址"那条窄口
（2026-10-08 加在 `MailSeams` 上；闸门在接缝那一侧：一次一个地址、地址必须是地址的形状）。
取不到就**不提供**那一路（问他的时候不列第 3 条），绝不自造 SMTP。

## 它拿不到什么（有测试钉住）

没有 `engine`、没有 `transport`、没有权限名单、**也没有任何读记忆的口子**：
地址只可能是核心在请求里递进来的那个值（见 `store.Preference.email`）。
`tests/test_remind_plugin.py` 里有两条钉子：一条扫插件源码（不许出现
memory / knowledge / engine / transport 这些名字），一条只暴露 `RemindRequest` 的那几个
字段、并记下每个被读过的名字。
"""
from __future__ import annotations

import inspect
import logging
import time
from datetime import datetime

from ... import dev_config
from ...remind import RemindDelivery
from .store import (CHANNEL_GROUP, CHANNEL_MAIL, CHANNEL_PRIVATE, CHANNEL_UNSET,
                    Reminder, RemindStore)

logger = logging.getLogger(__name__)

#: 到点的节拍：提醒是"到分钟"的事，半分钟看一次足够细（核心的节拍循环另有下限）。
POLL_SECONDS = 30.0

#: 用户会读到的每一句（测试逐个扫机制词——"协议 / 接口 / 调用 / 触发 / 令牌"这类词
#: 一个字都不许出现在她要说出口的话里）。**加新句子时必须同时加进这个元组。**
ASK_HEAD = "要我提醒你「{what}」——你想我怎么叫你？"
ASK_GROUP = "1 在本群里 @ 你"
ASK_PRIVATE = "2 私聊你"
ASK_MAIL = "3 寄到你的邮箱"
ASK_TAIL = "回我一句「/提醒 换 2」（想选哪个就把那个数写进去）就行。"
CHOOSE_OK = {
    CHANNEL_GROUP: "好，到时候我在群里 @ 你。",
    CHANNEL_PRIVATE: "好，到时候我私聊你。",
    CHANNEL_MAIL: "好，到时候我寄邮件给你。",
}
NO_PRIVATE = "私聊得你先来私聊我一次，之后我就能私聊提醒你了。"
NO_MAIL = "我还没法把邮件寄给你——要不先选 1 或者 2？"
NEED_EMAIL = "我还不知道你的邮箱。你把地址发我一句（比如「我的邮箱是 xx@yy」），下次就能寄给你。"
ASK_AGAIN = "我只看懂 1、2、3 这三个数——你想我怎么叫你？"
NOTHING = "这会儿没有还没发的提醒。"
LIST_HEAD = "你还有这些提醒："
LIST_TAIL = "回「/提醒 删 序号」可以不提醒了，「/提醒 换 1|2|3」换我叫你的方式。"
LIST_TAIL_EMPTY = "想让我提醒你什么，说一句「几点提醒我什么事」就行。"
USAGE = "用法：「/提醒」看还有哪些；「/提醒 删 2」删掉第 2 条；「/提醒 换 2」换我叫你的方式。"
BAD_INDEX = "没找到第 {index} 条——先发一句「/提醒」看看有哪些。"
MAIL_SUBJECT = "到点提醒：{what}"
MAIL_BODY = "你让我在这个时候提醒你：{what}\n—— 这封是到点自动寄的。"
DUE_TEXT = "到点了：{what}"

#: 她要说出口的话（测试扫这一个元组）。**加新句子就加进来。**
USER_TEXTS = (ASK_HEAD, ASK_GROUP, ASK_PRIVATE, ASK_MAIL, ASK_TAIL,
              *CHOOSE_OK.values(), NO_PRIVATE, NO_MAIL, NEED_EMAIL, ASK_AGAIN,
              NOTHING, LIST_HEAD, LIST_TAIL, LIST_TAIL_EMPTY, USAGE, BAD_INDEX,
              MAIL_SUBJECT, MAIL_BODY, DUE_TEXT)

#: 这一拍跑完的几种结局（`_deliver_*` 的返回值）。
SENT_OUT = "sent"           # 发出去了
PENDING = "pending"         # 没有出口（发送口没接上 / 找不到那个人）→ 留待发，不烧次数
RETRY = "retry"             # 确定没送出去 → 记一次失败，可重试
NO_SESSION = "no_session"   # 对面拒收（他还没私聊过她）→ 说一次人话，留待发
GIVEN_UP = "given_up"       # 别重发（不知道出去没有）→ 不再试


def _field(source: object, name: str, default: object = None) -> object:
    """读请求/结果上的一个字段；**没有就当没有**。"""

    return getattr(source, name, default)


def _read_request(request: object) -> tuple[str, float, str]:
    """取出核心递进来的三样（谁 / 什么时刻 / 什么事）。与 `remind.RemindRequest` 对齐。"""

    person = str(_field(request, "person_id", "") or "")
    what = str(_field(request, "what", "") or "").strip()
    try:
        at = float(_field(request, "at", 0.0) or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    return person, at, what


def session_channel(session_id: str) -> str:
    """**他当时是在哪儿说的**：私聊里说的 → `private`，别的（群里）→ `group`。

    这是"他能不能被私聊"唯一能从插件这一侧看到的线索（`onebot_ws` 把会话 id 拼成
    `private:<号>` / `group:<群号>`）。看不见就按 `group` **fail-closed**——
    宁可在那句人话里请他先私聊一次，也不假装私聊发得出去。
    （真正"发不发得出去"由核心回的结果说了算：`rejected` = 他还没私聊过她。）
    """

    return CHANNEL_PRIVATE if str(session_id or "").startswith("private:") else CHANNEL_GROUP


def parse_channel(text: str, *, allow_mail: bool = True) -> str | None:
    """把他回的那句话认成一种方式；**认不出返回 `None`**（调用方据此再问一次，不猜）。

    收 `1` / `2` / `3`，也收"本群 / 群里"、"私聊"、"邮箱 / 邮件"。
    一句话里出现两个不同的数字（"1 或者 2"）**不算选**。
    """

    body = (text or "").strip()
    if not body:
        return None
    digits = {char for char in body if char in "123"}
    if len(digits) == 1:
        picked = digits.pop()
        if picked == "1":
            return CHANNEL_GROUP
        if picked == "2":
            return CHANNEL_PRIVATE
        return CHANNEL_MAIL if allow_mail else None
    if any(word in body for word in ("本群", "群里", "群聊", "@")):
        return CHANNEL_GROUP
    if "私聊" in body:
        return CHANNEL_PRIVATE
    if any(word in body for word in ("邮箱", "邮件", "写信")):
        return CHANNEL_MAIL if allow_mail else None
    return None


async def _maybe_await(value: object) -> object:
    """同步或异步的接缝都能用（核心那条两条口子的写法不必是同一种）。"""

    if inspect.isawaitable(value):
        return await value
    return value


def _friendly_time(at: float) -> str:
    """给人看的时间。走**核心那份显式配置的时区**（`dev_config.local_timezone()`），
    不看部署机器的时区——与核心算绝对时刻时用的是同一个。只用于回话，不用来判时间。
    """

    try:
        return datetime.fromtimestamp(float(at), tz=dev_config.local_timezone()).strftime(
            "%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "到点"


def _channel_words(channel: str) -> str:
    return {CHANNEL_GROUP: "群里 @ 你", CHANNEL_PRIVATE: "私聊",
            CHANNEL_MAIL: "邮件"}.get(channel, "还没定")


class RemindCapability:
    """核心经 `registry.remind` 拿到的那个窄能力（见模块头那张表）。

    手上只有三样：**自己的数据**（`store`）、邮件插件那条窄口（`mail_send`，可能没有）、
    核心那条发送口的**懒读取**（`core_sender`，可能还没接上）。
    **没有引擎、没有 transport、没有权限名单、没有记忆。**
    """

    def __init__(self, store: RemindStore | None = None, *, mail_send=None,
                 core_sender=None, clock=time.time) -> None:
        self._store = store if store is not None else RemindStore()
        self._mail_send = mail_send if callable(mail_send) else None
        self._core_sender = core_sender if callable(core_sender) else (lambda: None)
        self._clock = clock

    # --- 核心 → 插件：认到一次提醒请求（**同步**，只落盘） ---------------------

    def __call__(self, request: object) -> None:
        """核心把装好的那一条递进来（`registry.remind` 的真实形状）。

        **不返回任何东西**（核心不读返回值），**不做 IO**（它跑在核心那一轮对话里）。
        收下之后：他已经说过怎么叫他 → 这条直接排上；**第一次** → 记下"到哪儿去问他"，
        由后台节拍把那一句问话发出去。
        """

        person, at, what = _read_request(request)
        if not person or not what or at <= 0:
            logger.info("remind_request_incomplete person=%s（给不全就当没给）", person)
            return
        session_id = str(_field(request, "session_id", "") or "")
        group_id = str(_field(request, "group_id", "") or "")
        email = str(_field(request, "email", "") or "")
        known = self._store.preference(person)
        if known is None or (email and email != known.email):
            known = self._store.remember_person(person, email=email)
        if known.channel in self._available(has_email=known.has_email):
            # 他早说过怎么叫他 → **不再问**（用户要的"以后 @ 一句就够"）。
            self._store.add(person_id=person, session_id=session_id, group_id=group_id,
                            at=at, what=what, channel=known.channel)
            logger.info("remind_accepted person=%s channel=%s", person, known.channel)
            return
        self._store.add(person_id=person, session_id=session_id, group_id=group_id,
                        at=at, what=what, channel=CHANNEL_UNSET)
        if not known.ask_session:
            self._store.note_ask(person, session_id=session_id, group_id=group_id, what=what)
            logger.info("remind_ask_queued person=%s（第一次：要问一句怎么叫他）", person)

    # --- 他答了 ---------------------------------------------------------------

    def note_reply(self, person_id: str, reply: str, *, session_id: str = "") -> str:
        """他回了一句（应该是那个选择）→ 记下来，返回要回给他的那句话。

        **今天这条路由 `/提醒 换 N` 走通**（命令插件收得到消息）。核心将来若愿意把
        "正在等回答的那一句"也转给插件，调**同一个函数**就行——两处不会分叉。
        认不出就**再问一次**：不猜、也不静默记一个默认值。
        """

        known = self._store.preference(person_id)
        has_email = bool(known and known.has_email)
        picked = parse_channel(reply, allow_mail=has_email and self.can_mail)
        if picked is None:
            body = (reply or "").strip()
            wants_mail = "3" in body or "邮箱" in body or "邮件" in body
            if wants_mail and not self.can_mail:
                return NO_MAIL               # 邮件那一路这次没接上
            if wants_mail and not has_email:
                return NEED_EMAIL            # 能寄，但还不知道寄哪儿
            return ASK_AGAIN
        if picked == CHANNEL_MAIL and not has_email:
            return NEED_EMAIL                # 没地址就先别记，让他把地址发来
        self._store.remember_person(person_id, channel=picked)
        self._store.clear_ask(person_id)
        # 他答之前收下的那几条（`channel=""`）也归到他刚选的方式上——否则那几条到点会
        # 按"他当时说话的地方"叫他，与他的选择相反。
        self._store.adopt_channel(person_id, picked)
        if picked == CHANNEL_PRIVATE and session_channel(session_id) != CHANNEL_PRIVATE:
            # 记住了（他选的就是它），但**把话说清**：他这句话是在群里说的，
            # 插件这一侧看不到任何"他私聊过她"的证据 → 先把那句人话说了。
            # （真发的时候如果对面拒收，核心会回 `rejected`，那时还有一次补救。）
            return NO_PRIVATE
        return CHOOSE_OK[picked]

    @property
    def can_mail(self) -> bool:
        """邮箱那一路现在能不能用（邮件插件给了"寄给某个地址"那条口子没有）。"""

        return self._mail_send is not None

    def _available(self, *, has_email: bool) -> tuple[str, ...]:
        found = [CHANNEL_GROUP, CHANNEL_PRIVATE]
        if has_email and self.can_mail:
            found.append(CHANNEL_MAIL)
        return tuple(found)

    def ask_text(self, what: str, *, has_email: bool) -> str:
        """第一次问他的那一句。**只列真能做的那几条**——列一条做不到的是骗人。"""

        lines = [ASK_HEAD.format(what=what), ASK_GROUP, ASK_PRIVATE]
        if has_email and self.can_mail:
            lines.append(ASK_MAIL)
        lines.append(ASK_TAIL)
        return "\n".join(lines)

    # --- 看 / 改（命令那条入口用；核心将来认得"看我的提醒"时用同一条） ----------

    def listing(self, person_id: str) -> str:
        """他问"我还有哪些提醒"。"""

        items = self._store.list_for(person_id)
        if not items:
            return f"{NOTHING}\n{LIST_TAIL_EMPTY}"
        lines = [LIST_HEAD]
        for index, item in enumerate(items, start=1):
            lines.append(f"{index} {_friendly_time(item.at)} {item.what}"
                         f"（{_channel_words(item.channel)}）")
        lines.append(LIST_TAIL)
        return "\n".join(lines)

    def cancel(self, person_id: str, index: int) -> str:
        """删掉他列表里的第 `index` 条（从 1 数起，与 `listing` 同一套序号）。"""

        items = self._store.list_for(person_id)
        if index < 1 or index > len(items):
            return BAD_INDEX.format(index=index)
        item = items[index - 1]
        self._store.cancel(item.key)
        return f"好，{_friendly_time(item.at)} 那条「{item.what}」不提醒了。"

    # --- 到点：本插件自己算，核心发 -------------------------------------------

    async def flush(self, *, now: float | None = None) -> tuple[RemindDelivery, ...]:
        """跑一拍：**先问该问的，再发该发的**；返回这一拍交给**核心**的那些意图。

        （邮件那一路走邮件插件、不交核心，所以不在返回值里——它的凭证是
        `mail_send` 真收到了那一次调用，测试直接验那一条。）

        没到点的、发过的、取消的、试够次数的、已经放弃的**一条都不碰**；
        "没有出口"**不算失败**：留在待发里、不烧重试次数，每一拍记一行 warning。
        """

        moment = self._clock() if now is None else float(now)
        handed: list[RemindDelivery] = []
        gaps: list[str] = []
        handed.extend(await self._ask_pending(gaps))
        handed.extend(await self._send_due(moment, gaps))
        if gaps:
            logger.warning("remind_no_way_out 这一拍有 %s 件事送不出去（留待发，不算失败）：%s",
                           len(gaps), "；".join(sorted(set(gaps))))
        self._store.compact()
        return tuple(handed)

    async def _ask_pending(self, gaps: list[str]) -> list[RemindDelivery]:
        """把还没问出去的那句"你想我怎么叫你"发出去（发出去了才清掉：**只问一次**）。"""

        handed: list[RemindDelivery] = []
        for person in self._store.asking():
            group_id = (person.ask_group
                        if session_channel(person.ask_session) == CHANNEL_GROUP else "")
            outcome, why, delivery = await self._deliver_core(
                person.person_id,
                self.ask_text(person.ask_what, has_email=person.has_email),
                group_id=group_id)
            if outcome == SENT_OUT and delivery is not None:
                self._store.clear_ask(person.person_id)
                handed.append(delivery)
            elif outcome == PENDING:
                gaps.append(why)
            else:
                logger.warning("remind_ask_failed person=%s outcome=%s why=%s",
                               person.person_id, outcome, why)
        return handed

    async def _send_due(self, moment: float, gaps: list[str]) -> list[RemindDelivery]:
        """把到点的那些交出去；还没答渠道的，按**他当时说话的地方**叫他并记下来。"""

        handed: list[RemindDelivery] = []
        for item in self._store.due(moment):
            channel = item.channel or session_channel(item.session_id)
            if channel == CHANNEL_MAIL:
                outcome, why = await self._deliver_mail(item)
                delivery = None
            else:
                group_id = item.group_id if channel == CHANNEL_GROUP else ""
                outcome, why, delivery = await self._deliver_core(
                    item.person_id, DUE_TEXT.format(what=item.what), group_id=group_id)
            if outcome == SENT_OUT:
                if not item.channel:
                    # 他没答就用他当时说话的地方——把这次的选择记下来，以后不再问。
                    self._store.remember_person(item.person_id, channel=channel)
                    self._store.clear_ask(item.person_id)
                self._store.mark_sent(item.key)
                if delivery is not None:
                    handed.append(delivery)
                logger.info("remind_sent person=%s channel=%s at=%s",
                            item.person_id, channel, int(item.at))
            elif outcome == PENDING:
                gaps.append(why)
                # 留待发：把"为什么还没发出去"记在那条上（状态不动、不加 tries）。
                self._store.mark_pending(item.key, why)
            elif outcome == NO_SESSION:
                gaps.append(why)
                await self._tell_him_once(item, gaps)
                # **留待发**：他私聊过她之后这一条就能发出去——只记下原因，
                # 不烧重试次数、也不算失败（用户 2026-10-08 的裁决）。
                self._store.mark_pending(item.key, why)
            elif outcome == GIVEN_UP:
                self._store.mark_given_up(item.key, why)
                logger.warning("remind_given_up person=%s why=%s（不再试）", item.person_id, why)
            else:
                self._store.mark_failed(item.key, why)
                logger.warning(
                    "remind_send_failed person=%s channel=%s why=%s（记一笔，下一次再试）",
                    item.person_id, channel, why)
        return handed

    async def _tell_him_once(self, item: Reminder, gaps: list[str]) -> None:
        """核心回 `rejected`（他还没私聊过她）→ **只对他说一次**"先私聊我一次…"。

        说在**他当时说话的地方**（群里说的就发到那个群、@ 他）。他本来就在私聊里说的
        时候这条不适用（私聊那条路正是发不出去的那条）。
        """

        if item.told_no_session or not str(item.session_id).startswith("group:"):
            return
        outcome, why, _ = await self._deliver_core(item.person_id, NO_PRIVATE,
                                                  group_id=item.group_id)
        if outcome == SENT_OUT:
            self._store.mark_told(item.key)
        else:
            gaps.append(why)

    async def _deliver_mail(self, item: Reminder) -> tuple[str, str]:
        """邮箱那一路：走邮件插件的能力（`MailSeams.send_to`），**不经核心**。"""

        if self._mail_send is None:
            return PENDING, "邮件插件没给「寄给某个地址」的口子"
        person = self._store.preference(item.person_id)
        address = person.email if person is not None else ""
        if not address:
            return PENDING, "手上没有他的邮箱（核心这次没递过来）"
        try:
            await _maybe_await(self._mail_send(
                address, MAIL_SUBJECT.format(what=item.what),
                MAIL_BODY.format(what=item.what)))
        except Exception as exc:  # noqa: BLE001 - 发不出去要记下来，不许带走这一拍
            return RETRY, f"{type(exc).__name__}: {exc}"
        return SENT_OUT, ""

    async def _deliver_core(self, person_id: str, text: str, *,
                            group_id: str) -> tuple[str, str, RemindDelivery | None]:
        """交给核心的发送口：`(结局, 原因, 交出去的那条意图)`。

        核心那条口子**只回结果、不抛异常**（`RemindSendResult`），分类见模块头那张表：
        `no_target` = 没有出口（留待发）；`not_delivered` / `error` = 确定没送出去（可重试）；
        `rejected` = 对面拒收（说一次人话、留待发）；`uncertain` = 不知道（不再试）。
        """

        delivery = RemindDelivery(person_id=str(person_id), text=str(text),
                                  group_id=str(group_id or ""))
        sender = self._core_sender()
        if not callable(sender):
            return PENDING, "核心的发送口（registry.remind_send）还没接上", delivery
        try:
            result = await _maybe_await(sender(delivery))
        except Exception as exc:  # noqa: BLE001 - 核心说它不抛；真抛了按"可重试"记一笔
            return RETRY, f"{type(exc).__name__}: {exc}", delivery
        sent = bool(_field(result, "sent", False))
        reason = str(_field(result, "reason", "") or "")
        detail = str(_field(result, "detail", "") or "")
        if sent or reason == "ok":
            return SENT_OUT, "", delivery
        if reason == "no_target":
            return PENDING, f"核心找不到那个人（no_target {detail}）".strip(), delivery
        if reason == "rejected":
            return NO_SESSION, f"对面拒收（rejected {detail}）".strip(), delivery
        if reason == "uncertain":
            return GIVEN_UP, f"不知道发出去没有（uncertain {detail}）".strip(), delivery
        return RETRY, f"{reason or 'error'} {detail}".strip(), delivery
