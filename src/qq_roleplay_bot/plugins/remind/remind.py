"""到点提醒——**插件侧那一半**：问一次怎么叫他、记下来、到点把"要发什么"交出去。

## 形状（用户 2026-10-08 拍板）

**核心听懂 + 插件做事 + 核心发送**。群里一句"云茹，8 点提醒我交作业"是**对话调起**的：
核心在它那次判定里认出来、把时间算成**绝对时刻**，然后把（谁 · 什么事 · 什么时刻 ·
他的邮箱）递给本插件。插件收下之后：

| 什么时候 | 核心调谁 | 插件做什么 |
| --- | --- | --- |
| 认到提醒请求 | `plan(request)` | 第一次 → 回一句问"怎么叫你"；问过了 → **不再问**，直接收下这条提醒 |
| 他回了选择 | `choose(request, 回话)` | 认得出就记下来 + 回一句确认；认不出就**再问一次**（不猜） |
| 到点了 | 本插件的后台节拍 `poll_once()` | 把到期的挑出来、算好"要发什么"，交给核心的发送口 |

**一句话都不发的那些情况同样是判据**：给不全的请求（没人 / 没事情 / 时刻非法）一律当
没给；他选了"私聊"却还没私聊过她，就回一句人话，而不是假装发得出去。

## 交出去的那个窄能力（**假定**核心这样取——对齐前先这么写）

```python
registry.remind = build           # 插件的 register() 里赋值（形状同 registry.vision）
engine.remind = build(hand_off)   # 核心装配时调一次，把它的发送口递进来
await engine.remind.plan(request)           # 核心 → 插件
await engine.remind.choose(request, 回话)    # 核心 → 插件
```

```python
async def hand_off(delivery: RemindDelivery) -> object   # 插件 → 核心：请你发这个
```

`RemindDelivery` 就是"内容/意图"，与 `ActionRequest` / `ImageReply` 同一路数：
**插件不发送任何东西**，它只说"该发什么、发给谁、要不要 @ 他"，发由核心发
（核心才知道怎么发、发不出去怎么补）。

⚠️ 这一份是**假定**的接口：核心那一半当时还没提交完。形状以核心那份为准，
对不上的地方只改 `attach()` 与 `_deliver()` 两处。

## 邮箱那一路用的是邮件插件的能力

`registry.mail` 现在给的是"**给操作者**发一封普通邮件"（`MailSeams.notify`，
收件人写死在 mail 自己的配置里）。提醒要寄给**那一个人**，所以这里假定 mail 的接缝上
还有一个"寄给某个人"的窄口 `send_to(address, subject, body)`——**取不到就不提供
邮箱那一路**（见 `plugin.person_mail_sender`）。绝不自造 SMTP，也绝不把邮件错寄给操作者。

## 它拿不到什么（有测试钉住）

没有 `engine`、没有 `transport`、没有权限名单、**也没有任何读记忆的口子**：
地址只可能是核心在请求里递进来的那个值（见 `store.Preference.email`）。
`tests/test_remind_plugin.py` 里有一条把"插件源码里出现记忆 API"扫出来的用例，
还有一条只暴露允许字段的请求对象——插件多读一个属性就会当场炸。
"""
from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from datetime import datetime

from ... import dev_config
from .store import (CHANNEL_GROUP, CHANNEL_MAIL, CHANNEL_PRIVATE,
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
ASK_TAIL = "回我一个数字就行。"
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


@dataclass(frozen=True, slots=True)
class RemindDelivery:
    """交给核心去发的**内容/意图**。插件不发送任何东西——它只说这一句该发给谁。

    `channel` 决定核心走哪条路：`group`（发到群里，要不要 @ 看 `mention`）、
    `private`（发给他本人）、`mail`（寄到 `email`，由邮件插件的能力发）。
    """

    channel: str
    person_id: str
    group_id: str
    text: str
    subject: str = ""
    email: str = ""
    mention: bool = True


def _field(source: object, name: str, default: object = None) -> object:
    """读请求上的一个字段；**没有就当没有**（插件的输入只有核心递进来的那几个）。"""

    return getattr(source, name, default)


def _read_request(request: object) -> tuple[str, float, str]:
    """取出核心递进来的三样（谁 / 什么时刻 / 什么事）。与核心那份 `RemindRequest` 对齐。"""

    person = str(_field(request, "person_id", "") or "")
    what = str(_field(request, "what", "") or "").strip()
    try:
        at = float(_field(request, "at", 0.0) or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    return person, at, what


def _private_reachable(request: object) -> bool:
    """他**能不能被私聊**（得他先私聊过她一次）——只有核心知道这件事。

    字段名以核心那份为准；这里容几个写法，改的时候只动这一处。
    **核心没给这个事实时按 `False`**：宁可不承诺一个自己证不了的渠道
    （那样他会在群里被告知"先私聊我一次"），也不假装发得出去。
    """

    for name in ("private_reachable", "has_private_session", "private_ok"):
        value = getattr(request, name, None)
        if value is not None:
            return bool(value)
    return False


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
    """同步或异步的接缝都能用（核心那一半的写法还没定死）。"""

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
            CHANNEL_MAIL: "邮件"}.get(channel, channel)


class RemindCapability:
    """核心经 `registry.remind` 拿到的那个窄能力（见模块头那张表）。

    手上只有三样：**自己的数据**（`store`）、邮件插件给的那条窄口（`mail_send`，
    可能没有）、核心递进来的发送口（`hand_off`，可能还没接上）。
    **没有引擎、没有 transport、没有记忆。**
    """

    def __init__(self, store: RemindStore | None = None, *, mail_send=None,
                 hand_off=None, clock=time.time) -> None:
        self._store = store if store is not None else RemindStore()
        self._mail_send = mail_send if callable(mail_send) else None
        self._hand_off = hand_off if callable(hand_off) else None
        self._clock = clock

    # --- 核心装配时递进来的那一样 -------------------------------------------

    def attach(self, hand_off) -> None:
        """核心把它的发送口递进来（核心调 `build(hand_off)` 时落到这里）。

        没接上也不影响别的：那条提醒会**记成"没发出去、可重试"**，
        而不是静默算成功——"以为发了其实没发"是这件事最贵的失败形状。
        """

        self._hand_off = hand_off if callable(hand_off) else None

    @property
    def can_mail(self) -> bool:
        """邮箱那一路现在能不能用（邮件插件给了"寄给某个人"的口子没有）。"""

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

    # --- 核心 → 插件：认到提醒请求 -------------------------------------------

    async def plan(self, request: object) -> str | None:
        """核心认到一次提醒请求。

        返回一句要她说的（第一次：问怎么叫他），或 `None` = **不用多说**
        （已经问过了，这条提醒直接收下——用户要的"以后 @ 一句就够"）。
        """

        person, at, what = _read_request(request)
        if not person or not what or at <= 0:
            return None                      # 给不全就当没给（与核心那份口径一致）
        email = str(_field(request, "email", "") or "")
        known = self._store.preference(person)
        if known is None or (email and email != known.email):
            known = self._store.remember_person(person, email=email)
        if known.channel in self._available(has_email=known.has_email):
            self._accept(request, person=person, at=at, what=what, channel=known.channel)
            return None
        return self.ask_text(what, has_email=known.has_email)

    async def choose(self, request: object, reply: str) -> str | None:
        """他回了一句（应该是那个选择）。**认不出就再问一次**，不猜、也不装作记下了。"""

        person, at, what = _read_request(request)
        if not person or not what or at <= 0:
            return None
        email = str(_field(request, "email", "") or "")
        picked = parse_channel(reply, allow_mail=bool(email) and self.can_mail)
        if picked is None:
            body = (reply or "").strip()
            wants_mail = "3" in body or "邮箱" in body or "邮件" in body
            if wants_mail and not self.can_mail:
                return NO_MAIL               # 邮件那一路这次没接上
            if wants_mail and not email:
                return NEED_EMAIL            # 能寄，但还不知道寄哪儿
            return ASK_AGAIN
        if picked == CHANNEL_MAIL and not email:
            return NEED_EMAIL                # 没地址就先别记，让他把地址发来
        self._store.remember_person(person, channel=picked, email=email)
        self._accept(request, person=person, at=at, what=what, channel=picked)
        if picked == CHANNEL_PRIVATE and not _private_reachable(request):
            # 记住了（他选的就是它），但**把话说清**：她这会儿还私聊不到他。
            return NO_PRIVATE
        return CHOOSE_OK[picked]

    def _accept(self, request: object, *, person: str, at: float, what: str,
                channel: str) -> Reminder:
        return self._store.add(person_id=person, session_id=str(_field(request, "session_id", "")),
                               group_id=str(_field(request, "group_id", "") or ""),
                               at=at, what=what, channel=channel)

    # --- 看 / 改（命令那条入口用；核心将来认得"看我的提醒"时用同一条） ----------

    async def listing(self, person_id: str) -> str:
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

    async def cancel(self, person_id: str, index: int) -> str:
        """删掉他列表里的第 `index` 条（从 1 数起，与 `listing` 同一套序号）。"""

        items = self._store.list_for(person_id)
        if index < 1 or index > len(items):
            return BAD_INDEX.format(index=index)
        item = items[index - 1]
        self._store.cancel(item.key)
        return f"好，{_friendly_time(item.at)} 那条「{item.what}」不提醒了。"

    async def set_channel(self, person_id: str, reply: str) -> str:
        """换"以后叫我"的方式（已经收下的那几条不动——各自记着各自的方式）。"""

        known = self._store.preference(person_id)
        picked = parse_channel(reply, allow_mail=bool(known and known.has_email)
                               and self.can_mail)
        if picked is None:
            return USAGE
        self._store.remember_person(person_id, channel=picked)
        return CHOOSE_OK[picked]

    # --- 到点：本插件自己算，核心发 -------------------------------------------

    async def flush_due(self, *, now: float | None = None) -> tuple[RemindDelivery, ...]:
        """把**到期的**那些挑出来交出去；返回这一次真交出去的内容/意图。

        没到点的、已经发过的、取消掉的、试够次数的**一条都不碰**；
        交出去失败的记一条 `failed`（下一次节拍再试，`MAX_TRIES` 次为止）。
        """

        moment = self._clock() if now is None else float(now)
        handed: list[RemindDelivery] = []
        for reminder in self._store.due(moment):
            delivery = self._delivery_of(reminder)
            ok, why = await self._deliver(delivery)
            if ok:
                self._store.mark_sent(reminder.key)
                handed.append(delivery)
                logger.info("remind_sent person=%s channel=%s at=%s",
                            reminder.person_id, reminder.channel, int(reminder.at))
            else:
                self._store.mark_failed(reminder.key, why)
                logger.warning("remind_send_failed person=%s channel=%s why=%s"
                               "（记下，下一次再试）",
                               reminder.person_id, reminder.channel, why)
        self._store.compact()
        return tuple(handed)

    def _delivery_of(self, reminder: Reminder) -> RemindDelivery:
        if reminder.channel == CHANNEL_MAIL:
            person = self._store.preference(reminder.person_id)
            return RemindDelivery(channel=CHANNEL_MAIL, person_id=reminder.person_id,
                                  group_id=reminder.group_id,
                                  text=MAIL_BODY.format(what=reminder.what),
                                  subject=MAIL_SUBJECT.format(what=reminder.what),
                                  email=person.email if person is not None else "",
                                  mention=False)
        return RemindDelivery(channel=reminder.channel, person_id=reminder.person_id,
                              group_id=reminder.group_id,
                              text=DUE_TEXT.format(what=reminder.what),
                              mention=reminder.channel == CHANNEL_GROUP)

    async def _deliver(self, delivery: RemindDelivery) -> tuple[bool, str]:
        """把一份内容/意图交出去。**邮件那一路走邮件插件的能力，别的一律交给核心。**"""

        if delivery.channel == CHANNEL_MAIL:
            if self._mail_send is None:
                return False, "邮件插件没给「寄给某个人」的口子"
            if not delivery.email:
                return False, "手上没有他的邮箱（核心这次没递过来）"
            try:
                await _maybe_await(self._mail_send(delivery.email, delivery.subject,
                                                   delivery.text))
            except Exception as exc:  # noqa: BLE001 - 发不出去要记下来，不许带走这一轮
                return False, f"{type(exc).__name__}: {exc}"
            return True, ""
        if self._hand_off is None:
            return False, "核心这一版还没把发送口递过来"
        try:
            result = await _maybe_await(self._hand_off(delivery))
        except Exception as exc:  # noqa: BLE001 - 同上
            return False, f"{type(exc).__name__}: {exc}"
        if result is None:
            return True, ""
        return bool(result), "" if result else "核心说这条没发出去"
