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
import os
import time
from datetime import datetime

from ... import dev_config
from ...remind import RemindDelivery
from .store import (CHANNEL_GROUP, CHANNEL_MAIL, CHANNEL_PRIVATE,
                    Reminder, RemindStore)

logger = logging.getLogger(__name__)

#: 到点的节拍：提醒是"到分钟"的事，半分钟看一次足够细（核心的节拍循环另有下限）。
POLL_SECONDS = 30.0

#: **一拍最多交出去多少条**（2026-10-09 审查第 4 条：实测一次 300 条全在一拍发出去，
#: 那就是 300 条消息的突发）。多的留在待发里，下一拍接着发——不是丢掉。
MAX_PER_TICK = 20

#: 用户会读到的每一句（测试逐个扫机制词——"协议 / 接口 / 调用 / 触发 / 令牌"这类词
#: 一个字都不许出现在她要说出口的话里）。**加新句子时必须同时加进这个元组。**
NO_PRIVATE = "私聊得你先来私聊我一次，之后我就能私聊提醒你了。"
NOTHING = "这会儿没有还没发的提醒。"
LIST_HEAD = "你还有这些提醒："
LIST_TAIL = "回「/提醒 删 序号」可以不提醒了。"
LIST_TAIL_EMPTY = "想让我提醒你什么，说一句「几点提醒我什么事」就行。"
USAGE = "用法：「/提醒」看还有哪些；「/提醒 删 2」删掉第 2 条。"
BAD_INDEX = "没找到第 {index} 条——先发一句「/提醒」看看有哪些。"
#: **主题是固定文案**（2026-10-09 审查第 5 条）：主题是邮件那边**唯一进命令行**的自由文本，
#: 而 `clean_subject` 会因为 `& " %` 这类字符**拒发**——原来的 `"到点提醒：{what}"`
#: 于是让"写了一句带引号的提醒"变成"这封信永远寄不出去、还安静地挂着"。
#: `what` 只进正文（正文走文件、不走命令行，这些字符没问题）。
MAIL_SUBJECT = "到点提醒"
MAIL_BODY = "你让我在这个时候提醒你：{what}\n—— 这封是到点自动寄的。"
#: 她要说的一句人话：这条提醒**寄不出去**（地址或内容不合邮件那边的口径）。
MAIL_FAILED = "你那条提醒我没寄出去——邮箱地址或者内容不太合适，你改一下再说一声，我再试。"
MAIL_QUOTA = "今天的邮件已经寄到上限了，这条我没寄出去。急的话先在群里说一声。"
DUE_TEXT = "到点了：{what}"

#: 邮件那边**确定性**拒绝的几类（`MailError.kind`）。这些再试也不会变：
#: 地址不合口径、主题/正文格式被拒。**与"网络出错"分开**，否则就是白烧三次 + 一直挂着。
MAIL_REFUSAL_KINDS = frozenset({"bad_recipient", "bad_subject", "empty_body",
                                "too_long", "secret_in_body"})

#: 邮件那一路的两个上限（**每人每天 / 全局每天**）。用户 2026-10-08 的口径是"最保守先上"：
#: 任何能触发提醒的人都能让**操作者的邮箱**替他寄信，所以默认每人 1 封、全局 5 封。
#:
#: **读环境变量而不是 `dev_config`**：`dev_config` 是核心的文件，插件线不许改它
#: （`tests/check_plugin_branch_scope.py` 会拦）。两个名字写进 `docs/CONFIG_GUIDE.md`。
#: **调用时现读**（改完不用重启）+ **钳制**（配置写歪了不许把上限放开）。
QUOTA_PERSON_VAR = "QQBOT_REMIND_MAIL_PER_PERSON_PER_DAY"
QUOTA_TOTAL_VAR = "QQBOT_REMIND_MAIL_PER_DAY"
QUOTA_PERSON_DEFAULT = 1
QUOTA_TOTAL_DEFAULT = 5
QUOTA_PERSON_MAX = 20
QUOTA_TOTAL_MAX = 50


#: 这一拍跑完的几种结局（`_deliver_*` 的返回值）。
#: 她要说出口的话（测试扫这一个元组）。**加新句子就加进来。**
USER_TEXTS = (NO_PRIVATE, NOTHING, LIST_HEAD, LIST_TAIL, LIST_TAIL_EMPTY,
              USAGE, BAD_INDEX, MAIL_SUBJECT, MAIL_BODY, MAIL_FAILED, MAIL_QUOTA,
              DUE_TEXT)

SENT_OUT = "sent"           # 发出去了
PENDING = "pending"         # 没有出口（发送口没接上 / 找不到那个人）→ 留待发，不烧次数
RETRY = "retry"             # 确定没送出去 → 记一次失败，可重试
NO_SESSION = "no_session"   # 对面拒收（他还没私聊过她）→ 说一次人话，留待发
GIVEN_UP = "given_up"       # 别重发（不知道出去没有）→ 不再试
REFUSED = "refused"         # **确定性发不出去**（地址/格式不合口径、配额用完）→ 说一句 + 收口


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


def channel_of(request: object, session_id: str) -> str:
    """核心给的那一档（`RemindRequest.channel`）→ 本插件内部的三种渠道。

    核心的词是 `group` / `private` / `email`（见核心 `remind.REMIND_CHANNEL_WORDS`），
    本插件内部叫 `CHANNEL_GROUP` / `CHANNEL_PRIVATE` / `CHANNEL_MAIL`。
    **核心没给**（老版本、或装错了）就按"他当时说话的地方"定——fail-closed，不猜。
    """

    word = str(_field(request, "channel", "") or "").strip().casefold()
    if word in (CHANNEL_GROUP, CHANNEL_PRIVATE):
        return word
    if word in (CHANNEL_MAIL, "email", "mail"):
        return CHANNEL_MAIL
    return session_channel(session_id)


def session_channel(session_id: str) -> str:
    """**他当时是在哪儿说的**：私聊里说的 → `private`，别的（群里）→ `group`。

    这是"他能不能被私聊"唯一能从插件这一侧看到的线索（`onebot_ws` 把会话 id 拼成
    `private:<号>` / `group:<群号>`）。看不见就按 `group` **fail-closed**——
    宁可在那句人话里请他先私聊一次，也不假装私聊发得出去。
    （真正"发不发得出去"由核心回的结果说了算：`rejected` = 他还没私聊过她。）
    """

    return CHANNEL_PRIVATE if str(session_id or "").startswith("private:") else CHANNEL_GROUP


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
        # **渠道由核心给**（`RemindRequest.channel`，非空：`group` / `private` / `email`）——
        # 2026-10-09 的契约：核心已经在她自己的话里问过"怎么收"，交过来的永远是"值齐了"
        # 的那一条。插件**不再发罐头问话**（再问就是同一件事两个声音）。
        channel = channel_of(request, session_id)
        # `forever`：**只有它为真才许粘成长期偏好**（用户 2026-10-09 改的规矩：每次都问，
        # 除非他说"以后都这么干"）。字段缺失 / 老请求 / False → **一律不粘**，下次照问。
        forever = bool(_field(request, "forever", False))
        # "**这次群里叫我**"那条路：核心是把**同一条请求换个 channel 再递一次**
        # （`stage3_main` 里 `replace(last, channel=…)`，`at`/`what`/会话都没动）。
        # 所以这里什么都不用特判——`add()` 的主键是 `人|会话|时刻|事情`，同一条会被
        # **就地改掉**（不新增），正是"只改这一条、不动偏好"。
        self._store.remember_person(person, email=email)
        if forever:
            self._store.remember_person(person, channel=channel, sticky=True)
        self._store.add(person_id=person, session_id=session_id, group_id=group_id,
                        at=at, what=what, channel=channel)
        logger.info("remind_accepted person=%s channel=%s forever=%s",
                    person, channel, forever)

    @property
    def can_mail(self) -> bool:
        """邮箱那一路现在能不能用（邮件插件给了"寄给某个地址"那条口子没有）。"""

        return self._mail_send is not None

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
        handed.extend(await self._send_due(moment, gaps, room=MAX_PER_TICK - len(handed)))
        if gaps:
            logger.warning("remind_no_way_out 这一拍有 %s 件事送不出去（留待发，不算失败）：%s",
                           len(gaps), "；".join(sorted(set(gaps))))
        self._store.compact()
        return tuple(handed)

    async def _send_due(self, moment: float, gaps: list[str], *,
                        room: int = MAX_PER_TICK) -> list[RemindDelivery]:
        """把到点的那些交出去；还没答渠道的，按**他当时说话的地方**叫他并记下来。

        `room` 是这一拍还允许交出去几条（`MAX_PER_TICK` 减去问话用掉的那几拍）——
        到点的比这多就**先发早的**，剩下的留在待发里，下一拍接着发（不丢）。
        """

        handed: list[RemindDelivery] = []
        pending_due = self._store.due(moment)
        if len(pending_due) > room:
            logger.warning("remind_tick_capped due=%s sending=%s（其余留到下一拍）",
                           len(pending_due), max(room, 0))
            pending_due = pending_due[:max(room, 0)]
        for item in pending_due:
            channel = item.channel or session_channel(item.session_id)
            notice = ""
            if channel == CHANNEL_MAIL:
                outcome, why, notice = await self._deliver_mail(item, now=moment)
                delivery = None
            else:
                group_id = item.group_id if channel == CHANNEL_GROUP else ""
                outcome, why, delivery = await self._deliver_core(
                    item.person_id, DUE_TEXT.format(what=item.what), channel=channel,
                    group_id=group_id)
            if outcome == SENT_OUT:
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
                await self._tell_once(item, NO_PRIVATE, flag="told_rejected", gaps=gaps)
                # **留待发**：他私聊过她之后这一条就能发出去——只记下原因，
                # 不烧重试次数、也不算失败（用户 2026-10-08 的裁决）。
                self._store.mark_pending(item.key, why)
            elif outcome == REFUSED:
                # 这一条**确定性发不出去**（邮箱地址不合邮件那边的口径 / 主题·正文格式、
                # 或者今天的配额用完了）：不许当"可重试"白烧三次，也不许安静地永远挂着——
                # 跟他说一句人话（**按人只说一次**），然后把这条收口。
                gaps.append(why)
                await self._tell_once(item, notice or MAIL_FAILED,
                                      flag="told_mail_failed", gaps=gaps)
                self._store.mark_given_up(item.key, why)
                logger.warning("remind_refused person=%s channel=%s why=%s（不再试）",
                               item.person_id, channel, why)
            elif outcome == GIVEN_UP:
                self._store.mark_given_up(item.key, why)
                logger.warning("remind_given_up person=%s why=%s（不再试）", item.person_id, why)
            else:
                self._store.mark_failed(item.key, why)
                logger.warning(
                    "remind_send_failed person=%s channel=%s why=%s（记一笔，下一次再试）",
                    item.person_id, channel, why)
        return handed

    async def _tell_once(self, item: Reminder, text: str, *, flag: str,
                         gaps: list[str]) -> None:
        """对他说一句人话——**按人只说一次**（2026-10-09 审查：原来按条记 → 刷屏）。

        说在**他当时说话的地方**（群里说的就发到那个群、@ 他）。他本来就在私聊里说的
        时候这条不适用（私聊那条路正是发不出去的那条），但仍然记下"说过了"，
        免得每一拍都为此再试一遍。
        """

        person = self._store.preference(item.person_id)
        if person is not None and getattr(person, flag, False):
            return
        if not str(item.session_id).startswith("group:"):
            self._store.mark_told(item.person_id, flag)
            return
        outcome, why, _ = await self._deliver_core(item.person_id, text,
                                                  channel=CHANNEL_GROUP,
                                                  group_id=item.group_id)
        if outcome == PENDING:
            gaps.append(why)                       # 没有出口：留待发，下一拍再说
            return
        # 别的结果（发出去了 / 拒收 / 不知道 / 确定失败）都**记下"说过了"**：
        # 这句话是"告知"，不是"必须送到"——反复重试只会变成骚扰。
        self._store.mark_told(item.person_id, flag)

    def _quota(self) -> tuple[int, int]:
        """读两个上限（**每人每天 / 全局每天**）：调用时现读环境变量 + 钳制。

        钳制是两层：认不出（空 / 不是数）就用默认值；认出来但超出上限就压到上限。
        **不写进 `dev_config`**：那是核心的文件，插件线不许改它（那道范围门槛会拦）。
        """

        def read(name: str, default: int, ceiling: int) -> int:
            raw = str(os.environ.get(name, "") or "").strip()
            try:
                value = int(raw) if raw else default
            except ValueError:
                logger.warning("remind_quota_bad_value name=%s value=%r（用默认值）", name, raw)
                value = default
            return max(0, min(ceiling, value))

        return (read(QUOTA_PERSON_VAR, QUOTA_PERSON_DEFAULT, QUOTA_PERSON_MAX),
                read(QUOTA_TOTAL_VAR, QUOTA_TOTAL_DEFAULT, QUOTA_TOTAL_MAX))

    def _today(self, moment: float) -> str:
        """今天（按**核心那份显式配置的时区**，与"到点"用的是同一个）。"""

        try:
            return datetime.fromtimestamp(float(moment),
                                          tz=dev_config.local_timezone()).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return "unknown"

    async def _deliver_mail(self, item: Reminder, *, now: float) -> tuple[str, str, str]:
        """邮箱那一路：走邮件插件的能力（`MailSeams.send_to`），**不经核心**。

        三件事按顺序：

        1. **配额闸**（2026-10-09 审查第 1 条，人裁按最保守）：每人每天 / 全局每天各一个
           上限，超了就 `REFUSED` + 一句人话（**"不是失败"，所以不烧重试次数**）；
        2. 真的寄（主题是固定文案，`what` 只进正文）；
        3. `(结局, 原因, 要跟他说的人话)`：**确定性失败与可重试失败分开**——地址不合邮件
           那边的口径（`bad_recipient`）、格式被拒（`bad_subject`/…）、我们自己的地址闸门
           （`ValueError`）**再试一万次也不会变** → `REFUSED`；别的（网络、CLI）才是 `RETRY`。

        每次**尝试**都留一行审计（谁 · 寄到哪 · 成没成），**绝不记正文**。
        """

        if self._mail_send is None:
            return PENDING, "邮件插件没给「寄给某个地址」的口子", ""
        person = self._store.preference(item.person_id)
        address = person.email if person is not None else ""
        if not address:
            return PENDING, "手上没有他的邮箱（核心这次没递过来）", ""
        day = self._today(now)
        per_person, total = self._quota()
        used_person = self._store.mail_sent_on(day, person=item.person_id)
        used_total = self._store.mail_sent_on(day)
        if used_person >= per_person or used_total >= total:
            logger.warning("remind_mail_quota person=%s day=%s per_person=%s/%s total=%s/%s",
                           item.person_id, day, used_person, per_person, used_total, total)
            return REFUSED, f"今天的邮件配额用完了（{used_person}/{per_person}、{used_total}/{total}）", \
                MAIL_QUOTA
        subject = MAIL_SUBJECT                       # **固定文案**：`what` 只进正文
        body = MAIL_BODY.format(what=item.what)
        try:
            await _maybe_await(self._mail_send(address, subject, body))
        except Exception as exc:  # noqa: BLE001 - 发不出去要记下来，不许带走这一拍
            kind = str(getattr(exc, "kind", "") or "")
            logger.info("remind_mail_audit person=%s to=%s day=%s result=failed kind=%s",
                        item.person_id, address, day, kind or type(exc).__name__)
            if isinstance(exc, ValueError) or kind in MAIL_REFUSAL_KINDS:
                return REFUSED, f"{kind or type(exc).__name__}: {exc}", MAIL_FAILED
            return RETRY, f"{type(exc).__name__}: {exc}", ""
        self._store.note_mail_sent(day, item.person_id)
        logger.info("remind_mail_audit person=%s to=%s day=%s result=sent",
                    item.person_id, address, day)
        return SENT_OUT, "", ""

    async def _deliver_core(self, person_id: str, text: str, *, channel: str,
                            group_id: str) -> tuple[str, str, RemindDelivery | None]:
        """交给核心的发送口：`(结局, 原因, 交出去的那条意图)`。

        核心那条口子**只回结果、不抛异常**（`RemindSendResult`），分类见模块头那张表：
        `no_target` = 没有出口（留待发）；`not_delivered` / `error` = 确定没送出去（可重试）；
        `rejected` = 对面拒收（说一次人话、留待发）；`uncertain` = 不知道（不再试）。
        """

        # **两处"别产出这种请求"的守卫**（2026-10-09 审查第 6、7 条，插件侧的半边）：
        #   ① 群里那一路要 @ 他，而核心把 person_id **原样**塞进 at 段（`onebot_ws.send_at`
        #      只试 `int()`、失败就回原串）——`all` 在 NapCat 语义里是 **@全体**。
        #      人号不是纯数字就**不出这一条**（留待发）。
        #   ② `channel=group` 但**没有群号**：核心会**按私聊**发（实测目标 user_id），
        #      与插件回他的"好，到时候我在群里 @ 你"不符——宁可不出。
        # （核心那两处本身也该收紧，但那是**本体树**的事，我不动那里，只写进汇报。）
        if channel == CHANNEL_GROUP:
            if not str(group_id or "").strip():
                return PENDING, "要发到群里却没拿到群号（核心会按私聊发），这一条先不出", None
            if not str(person_id).isdigit():
                return PENDING, f"人号不是数字（{person_id!r}），不敢让核心去 @ 他", None
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
