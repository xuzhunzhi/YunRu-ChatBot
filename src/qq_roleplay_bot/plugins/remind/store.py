"""到点提醒**自己的**数据：一个人的叫他方式 + 一串还没发的提醒。

## 为什么数据在插件这一侧

用户 2026-10-08 的口径：**核心听懂 + 插件做事 + 核心发送**。"该怎么叫他"与
"还有哪几条没发"是这件事自己的状态，核心一个字不认识——核心只在认到提醒请求时把
（谁 · 什么事 · 什么时刻 · 他的邮箱）递进来，插件收下；到点了自己算该发什么。

## 为什么是 jsonl、为什么落在 `data/` 下

写法照 `plugins/mail/mail_state.py`：运行数据放 `data_dir()`（`data/` 被 `.gitignore`
忽略，换台机器带上 `data/` 就接着跑），**不是**放在源码树里——源码树里写运行状态会被
两样东西同时抓住：git 会看见它，`tests/check_plugin_branch_scope.py` 也会
（`src/` 下多出未跟踪文件）。

一行一条事件、追加写（`JsonlLog`），读的时候按主键折叠成"最新那一条"：

- 崩在写一半只丢最后一行，不会把整份数据带走；
- "发不出去要能重试"就是"再追加一条 `failed`"，不需要原地改文件；
- 折叠后的条数远小于行数时顺手压一次（`compact`），别让文件长成日志。

## 一件事说清楚：邮箱地址是**核心递过来的**

本插件**没有、也不该有**读记忆的口子（`tests/test_remind_plugin.py` 里有断言钉住）。
它手上的地址只可能来自核心在某次提醒请求里递过来的那个值，而且**每次收到请求就刷新**。
它不是插件自己查来的，也不是第二个真相源。
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import time
from dataclasses import dataclass, replace

from ...dev_config import data_dir

logger = logging.getLogger(__name__)

#: 相对 `data_dir()` 的那一层（`data/remind/`）。
DIR_NAME = "remind"
PEOPLE_LOG = "people.jsonl"
REMINDERS_LOG = "reminders.jsonl"
#: 寄出去的每一封邮件记一行（**只记谁 / 寄到哪 / 什么时候**，绝不记正文）：
#: 它是"每人每天 / 全局每天"两个上限的账本（2026-10-09 独立审查：邮件那一路原来
#: **没有任何授权或频次闸**——任何能触发提醒的人三步就能让操作者的邮箱替它寄信）。
MAIL_SENT_LOG = "mail_sent.jsonl"
#: 账本保留几天（过期行在读的时候顺手丢掉）。
MAIL_LEDGER_KEEP_DAYS = 7

#: 三种叫他方式。**没有第四种**：加一种要同时改核心的发送侧，那是另一侧的事。
CHANNEL_GROUP = "group"
CHANNEL_PRIVATE = "private"
CHANNEL_MAIL = "mail"
CHANNELS = (CHANNEL_GROUP, CHANNEL_PRIVATE, CHANNEL_MAIL)
CHANNEL_UNSET = ""

#: 一条提醒的状态。
WAITING = "waiting"
SENT = "sent"
FAILED = "failed"
CANCELLED = "cancelled"
#: **不再试了**（核心回 `rejected` / `uncertain`——对面拒收、或不知道发出去没有）。
#: 与 `FAILED` 分开：`FAILED` 是"还会再试"，这个是"到此为止，下一拍不许再发"。
#: 尤其是 `uncertain`（不知道那条到底出去没有）**绝不能**重发——重发就可能发两条。
GIVEN_UP = "given_up"

#: 一条提醒最多试几次。"发不出去记下来可重试"不等于无限重试——一直失败是配置或
#: 渠道的问题，无限重试只会每半分钟往日志里灌一行。
#:
#: **"没有出口"不算一次重试**（见 `remind.RemindCapability._deliver`）：核心的发送口
#: 还没接上、或邮件插件没给那条口子——那是部署缺口，不是这条提醒发失败了；那种情况
#: 留在待发里，接缝一落地就发得出去，**不烧次数**（用户 2026-10-08 的裁决）。
MAX_TRIES = 3

#: 折叠阈值：行数超过"有效条数 × 这个倍数"时，把日志压一次。
COMPACT_FACTOR = 4

#: **一个人最多挂多少条还没发的提醒**（2026-10-09 审查第 4 条：实测一次 300 条全在
#: 一拍交出去）。超了就丢**最旧**的那条并记一行（照核心那两份 dict 的插入序淘汰写法）。
MAX_LIVE_PER_PERSON = 32
#: 已经发完 / 取消 / 放弃的条目，留几天就从数据里清掉（审查："已发的 300 条一条不清"）。
KEEP_FINISHED_DAYS = 7
FINISHED_STATES = (SENT, CANCELLED, GIVEN_UP)


def reminder_key(person_id: str, session_id: str, at: float, what: str) -> str:
    """一条提醒的稳定主键（同一个人、同一个时刻、同一件事 = 同一条）。

    用它而不是序号：核心可能把同一条请求递两次（重试、重复判定），
    序号会让"同一件事"变成两条提醒。
    """

    raw = f"{person_id}|{session_id}|{int(at)}|{what}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Preference:
    """一个人的那份状态：他选了哪种方式、他有没有邮箱、那句问话发没发出去。

    `channel == ""` = **还没答**（第一次收到他的提醒时就是这样）；这时
    **只问一次**（用户 2026-10-08：以后 @ 一句就够）。
    """

    person_id: str
    email: str = ""
    #: **长期偏好**（只有他说"以后都这么干"才有）：核心给 `forever=True` 时才写。
    #: 用户 2026-10-09 改的规矩是"**每次都问**"——"这次群里叫我"只改那一条提醒，
    #: 不动偏好；只有 `forever` 才粘。所以 `channel` 与 `sticky` 是一对：
    #: **`sticky` 不为真时 `channel` 一律不作数**。
    channel: str = ""
    sticky: bool = False
    #: **已经跟他说过"私聊得你先来私聊我一次"了**（按人记：同一句只说一次）。
    told_rejected: bool = False
    #: **已经跟他说过"那条提醒我没寄出去"了**（同上：按人，别刷屏）。
    told_mail_failed: bool = False

    @property
    def has_email(self) -> bool:
        return bool(self.email)


@dataclass(frozen=True, slots=True)
class Reminder:
    """一条到点提醒。`at` 是**核心算好的绝对时刻**（epoch 秒）——插件不做时间推算。

    `channel` **由核心给**（`RemindRequest.channel`：他这次要哪一种）。到点就按它发；
    要发到群里却没拿到群号、或者人号不是纯数字时，那一条**不出**（留待发，见 `remind.py`）。
    """

    key: str
    person_id: str
    session_id: str
    group_id: str
    at: float
    what: str
    channel: str = CHANNEL_UNSET
    state: str = WAITING
    tries: int = 0
    last_error: str = ""


    @property
    def live(self) -> bool:
        """还算"待发"（没发出去、也没被取消 / 放弃）。"""

        return self.state in (WAITING, FAILED)

    def is_due(self, now: float, *, max_tries: int = MAX_TRIES) -> bool:
        """**现在该发它了吗**：到点了、还没发出去、也没试够次数。"""

        return self.live and self.tries < max_tries and self.at <= float(now)

    def as_event(self) -> dict:
        return {"key": self.key, "person_id": self.person_id,
                "session_id": self.session_id, "group_id": self.group_id,
                "channel": self.channel, "at": self.at, "what": self.what,
                "state": self.state, "tries": self.tries,
                "last_error": self.last_error}


class JsonlLog:
    """一行一条事件的追加日志；读的时候按主键折叠成"最新那一条"。

    坏行（写到一半崩了）**只跳过那一行**并记一声，不让它带走整份数据——
    这正是选追加写而不是"每次重写整份"的理由。
    """

    def __init__(self, path: pathlib.Path | str) -> None:
        self.path = pathlib.Path(path)

    def append(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()

    def replay(self) -> dict[str, dict]:
        """按主键折叠出最新一条；键缺了或有坏行的都跳过（跳过要出声）。"""

        latest: dict[str, dict] = {}
        for line in self._raw_lines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("remind_log_bad_line path=%s（这一行跳过，别的照读）",
                               self.path.name)
                continue
            key = str(event.get("key") or "")
            if key:
                latest[key] = event
        return latest

    def compact(self, latest: dict[str, dict]) -> None:
        """把折叠结果写回去（先写临时文件再替换，免得压到一半断电什么都没有）。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(event, ensure_ascii=False) + "\n"
                       for event in latest.values())
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self.path)

    def maybe_compact(self, latest: dict[str, dict]) -> None:
        lines = len([line for line in self._raw_lines() if line.strip()])
        if lines >= max(COMPACT_FACTOR * max(len(latest), 1), 64):
            self.compact(latest)
            logger.info("remind_log_compacted path=%s lines=%s live=%s",
                        self.path.name, lines, len(latest))


class RemindStore:
    """一份数据两本日志：**人**（他选的方式）与**提醒**（还没发的那几条）。"""

    def __init__(self, root: pathlib.Path | str | None = None, *, clock=time.time) -> None:
        base = pathlib.Path(root) if root is not None else data_dir() / DIR_NAME
        self._people = JsonlLog(base / PEOPLE_LOG)
        self._reminders = JsonlLog(base / REMINDERS_LOG)
        self._mail = JsonlLog(base / MAIL_SENT_LOG)
        self._clock = clock

    # --- 人 -----------------------------------------------------------------

    def preference(self, person_id: str) -> Preference | None:
        """这个人的那份状态；从没被问过就是 `None`。"""

        event = self._people.replay().get(str(person_id))
        if event is None:
            return None
        # **`sticky` 不为真就当没有偏好**：2026-10-09 之前写的行里只有 `channel`、
        # 没有这个键，那是"选过一次就记住"那套旧语义——读的时候一律不认
        # （**不清洗数据**，只是不当数）。
        sticky = bool(event.get("sticky") or False)
        return Preference(person_id=str(person_id),
                          email=str(event.get("email") or ""),
                          channel=str(event.get("channel") or "") if sticky else "",
                          sticky=sticky,
                          told_rejected=bool(event.get("told_rejected") or False),
                          told_mail_failed=bool(event.get("told_mail_failed") or False))

    #: `mark_told()` 认得的那两个记号（"已经跟他说过这句话了"，按人记）。
    TOLD_FLAGS = ("told_rejected", "told_mail_failed")

    def _write_person(self, person_id: str, **changes: object) -> Preference:
        """把"这个人当前的状态"**整份**写一条事件（只覆盖 `changes` 里那几个字段）。

        为什么整份写、而不是只写改了的字段：字段会越加越多，而"某个调用点忘了带上某个
        字段"是**静默清空**它——那正是 2026-10-09 审查里"`told_no_session` 记在提醒上"
        这类形状的来源。整份写之后，新加一个字段只要改**这一处**。
        """

        current = self.preference(person_id) or Preference(person_id=str(person_id))
        event: dict = {"key": str(person_id), "at": self._clock(),
                       "email": current.email,
                       "channel": current.channel, "sticky": current.sticky,
                       "told_rejected": current.told_rejected,
                       "told_mail_failed": current.told_mail_failed}
        event.update(changes)
        self._people.append(event)
        return self.preference(person_id)  # type: ignore[return-value]

    def remember_person(self, person_id: str, *, email: str | None = None,
                        channel: str | None = None,
                        sticky: bool = False) -> Preference:
        """记下/刷新这个人的地址；**长期偏好只有 `sticky=True` 时才写**。

        地址是核心递过来的那一份（见模块头）：`email=""` 表示"这次核心没给"，不等于
        "他没有邮箱"，所以不覆盖。

        `channel` 是"以后都按这个来"那一档——**只有核心给了 `forever=True`**
        （= 调用方传 `sticky=True`）才写下去。`sticky=False`（或者干脆没给 channel）时
        **连旧的偏好也不动**：用户 2026-10-09 的规矩是"这次群里叫我"只改那一条提醒。
        """

        current = self.preference(person_id)
        keep = current if current is not None else Preference(person_id=str(person_id))
        changes: dict = {}
        if email:
            changes["email"] = str(email)
        if channel and sticky:
            changes["channel"] = str(channel)
            changes["sticky"] = True
        if not changes:
            return keep
        return self._write_person(person_id, **changes)

    def mark_told(self, person_id: str, flag: str) -> Preference | None:
        """记下"这句人话已经跟他说过了"（`TOLD_FLAGS` 里那两个之一，**按人**）。

        2026-10-09 审查：这记号原来记在**每一条提醒**上 → 三条同时到点被拒就刷三遍。
        按人记之后，同一句话一个人只说一次（其余几次只进日志）。
        """

        if flag not in self.TOLD_FLAGS:
            raise ValueError(f"不认识的记号：{flag}")
        # **没有这条记录也要建一条**：他第一次被拒时 records 里可能还没有他
        # （`remember_person(email="")` 不写盘），那就等于记号存不下来 → 每拍都说一遍。
        return self._write_person(person_id, **{flag: True})

    def _all(self) -> dict[str, Reminder]:
        found: dict[str, Reminder] = {}
        for key, event in self._reminders.replay().items():
            channel = str(event.get("channel") or "")
            if channel and channel not in CHANNELS:
                continue                      # 认不出的渠道：跳过（不猜），别带走整份数据
            found[key] = Reminder(
                key=key,
                person_id=str(event.get("person_id") or ""),
                session_id=str(event.get("session_id") or ""),
                group_id=str(event.get("group_id") or ""),
                at=float(event.get("at") or 0.0),
                what=str(event.get("what") or ""),
                channel=channel,
                state=str(event.get("state") or WAITING),
                tries=int(event.get("tries") or 0),
                last_error=str(event.get("last_error") or ""),
            )
        return found

    def get(self, key: str) -> Reminder | None:
        return self._all().get(str(key))

    def _write(self, reminder: Reminder) -> Reminder:
        self._reminders.append(reminder.as_event())
        return reminder

    def add(self, *, person_id: str, session_id: str, group_id: str, at: float,
            what: str, channel: str = CHANNEL_UNSET) -> Reminder:
        """收下一条提醒。**同一条再来一次就是同一条**（主键一样，覆盖成待发）。

        收下之后按人看一眼**还没发的条数**：超过 `MAX_LIVE_PER_PERSON` 就丢**最旧**的
        那几条（记一行 warning + 条数）。不这么做的话，一个人（或者一个刷屏的群）
        能往这里塞任意多条，到点那一拍把它们全交出去（审查实测 300 条）。
        """

        key = reminder_key(person_id, session_id, at, what)
        item = self._write(Reminder(key=key, person_id=str(person_id),
                                    session_id=str(session_id), group_id=str(group_id),
                                    channel=str(channel), at=float(at), what=str(what)))
        self._trim_person(str(person_id))
        return item

    def _trim_person(self, person_id: str) -> int:
        """把这个人的待发条数压到上限以内（丢最旧的）；返回丢了几条。"""

        live = sorted((item for item in self._all().values()
                       if item.person_id == person_id and item.live),
                      key=lambda item: (item.at, item.key))
        dropped = 0
        while len(live) > MAX_LIVE_PER_PERSON:
            oldest = live.pop(0)
            self._write(replace(oldest, state=CANCELLED,
                                last_error=f"超过每人 {MAX_LIVE_PER_PERSON} 条，这条最旧、丢掉了"))
            dropped += 1
        if dropped:
            logger.warning("remind_list_capped person=%s dropped=%s cap=%s",
                           person_id, dropped, MAX_LIVE_PER_PERSON)
        return dropped

    def list_for(self, person_id: str, *, now: float | None = None) -> tuple[Reminder, ...]:
        """这个人**还没发出去**的提醒，按时刻先后排。"""

        moment = self._clock() if now is None else float(now)
        found = [item for item in self._all().values()
                 if item.person_id == str(person_id)
                 and item.at > moment - 86400      # 一天以前的不再列（早该发过了）
                 and item.state in (WAITING, FAILED)]
        return tuple(sorted(found, key=lambda item: (item.at, item.key)))

    def mark_sent(self, key: str) -> Reminder | None:
        item = self.get(key)
        if item is None:
            return None
        return self._replace(item, state=SENT, last_error="")

    def mark_failed(self, key: str, error: str) -> Reminder | None:
        item = self.get(key)
        if item is None:
            return None
        return self._replace(item, state=FAILED, tries=item.tries + 1,
                             last_error=str(error)[:200])

    def cancel(self, key: str) -> Reminder | None:
        item = self.get(key)
        if item is None or not item.live:
            return None
        return self._replace(item, state=CANCELLED)

    def mark_pending(self, key: str, reason: str) -> Reminder | None:
        """这次**没有出口**（对面拒收 / 找不到那个人 / 发送口还没接上）→ 留在待发。

        只把原因记下来：状态不动、**不**加 `tries`——它不是"发失败了"，
        下一拍（或者他私聊过她之后）照样要发。
        """

        item = self.get(key)
        if item is None:
            return None
        return self._replace(item, state=WAITING, last_error=str(reason)[:200])

    def mark_given_up(self, key: str, error: str) -> Reminder | None:
        """核心说"别重发"（`uncertain`：不知道那条到底出去没有）→ 记成不再试。

        **不**把它算成"待发"：`list_for` 与 `due` 都不再碰它（它不会再发了），
        原因留在数据里，日志里也有那一行。
        """

        item = self.get(key)
        if item is None:
            return None
        return self._replace(item, state=GIVEN_UP, last_error=str(error)[:200])


    def set_channel(self, key: str, channel: str) -> Reminder | None:
        item = self.get(key)
        if item is None or not item.live or channel not in CHANNELS:
            return None
        return self._replace(item, channel=str(channel))

    def _replace(self, item: Reminder, **changes: object) -> Reminder:
        return self._write(replace(item, **changes))


    def due(self, now: float, *, max_tries: int = MAX_TRIES) -> tuple[Reminder, ...]:
        """**现在该发的**（早的先发）；试够次数的与取消掉的不在里面。"""

        found = [item for item in self._all().values()
                 if item.is_due(now, max_tries=max_tries)]
        return tuple(sorted(found, key=lambda item: (item.at, item.key)))

    # --- 邮件配额（每人每天 / 全局每天） ---------------------------------------

    def mail_ledger(self) -> tuple[dict[str, dict], ...]:
        """最近若干天的寄信记录（`MAIL_LEDGER_KEEP_DAYS` 天以内，过期行不返回）。"""

        cutoff = float(self._clock()) - MAIL_LEDGER_KEEP_DAYS * 86400.0
        rows = [dict(event, key=str(key))
                for key, event in self._mail.replay().items()
                if float(event.get("at") or 0.0) >= cutoff]
        return tuple(rows)

    def mail_sent_on(self, day: str, *, person: str | None = None) -> int:
        """`day` 那天寄了几封（给了 `person` 就只数他的）。"""

        return sum(1 for row in self.mail_ledger()
                   if str(row.get("day") or "") == str(day)
                   and (person is None or str(row.get("person") or "") == str(person)))

    def note_mail_sent(self, day: str, person: str) -> None:
        """记一封（主键带上时间戳，同一天同一个人寄多封也各占一行）。"""

        self._mail.append({"key": f"{day}|{person}|{int(self._clock() * 1000)}",
                           "day": str(day), "person": str(person),
                           "at": self._clock()})

    def compact(self) -> None:
        """把三本日志各自压一次（行数远多于有效条数时才真写）。"""

        self._people.maybe_compact(self._people.replay())
        # 账本按"天"裁：过期的行直接丢掉（`replay()` 里按主键折叠，过期行没了就没了）。
        ledger_all = {str(row["key"]): {k: v for k, v in row.items() if k != "key"}
                      for row in self.mail_ledger()}
        if len(ledger_all) < len(self._mail.replay()):
            self._mail.compact(ledger_all)           # 有东西要清就真写一次
        else:
            self._mail.maybe_compact(ledger_all)
        # 发完 / 取消 / 放弃的条目留 `KEEP_FINISHED_DAYS` 天就从待发数据里清掉
        # （审查："已发的 300 条一条不清"）。`replay()` 按主键折叠，所以"不写回去"
        # 就等于"删掉"；真写一次之后它们就没了。
        cutoff = float(self._clock()) - KEEP_FINISHED_DAYS * 86400.0
        recorded = self._reminders.replay()
        kept = {key: event for key, event in recorded.items()
                if str(event.get("state") or "") not in FINISHED_STATES
                or float(event.get("at") or 0.0) >= cutoff}
        if len(kept) < len(recorded):
            self._reminders.compact(kept)            # 有东西要清就真写一次
        else:
            self._reminders.maybe_compact(kept)
