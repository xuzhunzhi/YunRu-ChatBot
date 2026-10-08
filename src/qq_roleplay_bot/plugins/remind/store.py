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
#: **还没答**（第一次收到他的提醒、那句"你想我怎么叫你"还没发出去 / 还没答）。
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
    `ask_session` 记着"到哪个会话里去问他"（他当时说话的地方），问出去之后清掉——
    **只问一次**（用户 2026-10-08：以后 @ 一句就够）。
    """

    person_id: str
    channel: str = CHANNEL_UNSET
    email: str = ""
    #: 还没问出去时：问在哪个会话 / 哪个群 / 提醒的是什么事（问话里要带上它）。
    ask_session: str = ""
    ask_group: str = ""
    ask_what: str = ""
    #: **已经问过他了**（不管他答没答）。有了它，"只第一次问"才是真的按人只问一次——
    #: 否则他每再提一条提醒，插件都会再问一遍"你想我怎么叫你"。
    asked: bool = False
    #: 那句问话**确定送失败**了几次（"没有出口"不算——同 `MAX_TRIES` 的规矩）。
    #: 没有它，"问一句怎么叫他"那条路失败时会每 30 秒重发、永不封顶
    #: （2026-10-09 独立审查实测：四种失败各送出去 4 次）。
    ask_tries: int = 0
    #: **已经跟他说过"私聊得你先来私聊我一次"了**。
    #: 2026-10-09 审查：原来这个记号记在**每一条提醒**上，于是三条同时到点且都被拒 →
    #: 他在群里收到**三遍**同一句人话。改成**按人**记：同一句只说一次。
    told_rejected: bool = False
    #: **已经跟他说过"那条提醒我没寄出去"了**（同上：按人，别刷屏）。
    told_mail_failed: bool = False

    @property
    def has_email(self) -> bool:
        return bool(self.email)

    @property
    def needs_ask(self) -> bool:
        """**该问他"怎么叫你"吗**：他还没答、还没问过，而且问话已经排上了。"""

        return not self.channel and not self.asked and bool(self.ask_session)


@dataclass(frozen=True, slots=True)
class Reminder:
    """一条到点提醒。`at` 是**核心算好的绝对时刻**（epoch 秒）——插件不做时间推算。

    `channel == ""` = 收下时他还没答"怎么叫你"；到点仍没答，就按**他当时说话的地方**
    叫他（群里说的就群里 @、私聊说的就私聊），并把这次的选择记下来。
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
        return Preference(person_id=str(person_id),
                          channel=str(event.get("channel") or CHANNEL_UNSET),
                          email=str(event.get("email") or ""),
                          ask_session=str(event.get("ask_session") or ""),
                          ask_group=str(event.get("ask_group") or ""),
                          ask_what=str(event.get("ask_what") or ""),
                          asked=bool(event.get("asked") or False),
                          ask_tries=int(event.get("ask_tries") or 0),
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
                       "channel": current.channel, "email": current.email,
                       "ask_session": current.ask_session, "ask_group": current.ask_group,
                       "ask_what": current.ask_what, "asked": current.asked,
                       "ask_tries": current.ask_tries,
                       "told_rejected": current.told_rejected,
                       "told_mail_failed": current.told_mail_failed}
        event.update(changes)
        self._people.append(event)
        return self.preference(person_id)  # type: ignore[return-value]

    def remember_person(self, person_id: str, *, channel: str | None = None,
                        email: str | None = None) -> Preference:
        """记下/刷新这个人的方式与地址；**没给的那几项保持原样**。

        地址是核心递过来的那一份（见模块头）；`email=""` 表示"这次核心没给"，
        不等于"他没有邮箱"，所以不覆盖。`channel=""` 同理（那是"还没答"）。
        """

        current = self.preference(person_id)
        keep = current if current is not None else Preference(person_id=str(person_id))
        changes: dict = {}
        if channel:
            changes["channel"] = str(channel)
        if email:
            changes["email"] = str(email)
        if not changes:
            return keep
        return self._write_person(person_id, **changes)

    def note_ask(self, person_id: str, *, session_id: str, group_id: str,
                 what: str) -> Preference:
        """记下"到哪个会话里问他怎么叫他"（**问出去之前**一直留着）。"""

        return self._write_person(person_id, ask_session=str(session_id),
                                  ask_group=str(group_id), ask_what=str(what))

    def mark_asked(self, person_id: str) -> Preference | None:
        """那句问话发出去了（或不用再问了）→ 清掉待问，并且**记下"问过了"**。

        两件事一起做是有意的：`asked=True` 才是"只问一次"的保证——他之后再提十条提醒，
        也不会再收到一遍"你想我怎么叫你"。
        """

        if self.preference(person_id) is None:
            return None
        return self._write_person(person_id, ask_session="", ask_group="", ask_what="",
                                  asked=True, ask_tries=0)

    def bump_ask_tries(self, person_id: str) -> int:
        """那句问话**确定送失败**了一次 → 次数 +1，返回新的次数。

        与到点那条路同一个规矩：只有"确定没送出去"才算一次；"没有出口"（接缝没接上、
        找不到那个人）不算——那种情况下留待发，接缝一接上就发得出去。
        """

        current = self.preference(person_id)
        if current is None:
            return 0
        self._write_person(person_id, ask_tries=current.ask_tries + 1)
        return current.ask_tries + 1

    def mark_told(self, person_id: str, flag: str) -> Preference | None:
        """记下"这句人话已经跟他说过了"（`TOLD_FLAGS` 里那两个之一，**按人**）。

        2026-10-09 审查：这记号原来记在**每一条提醒**上 → 三条同时到点被拒就刷三遍。
        按人记之后，同一句话一个人只说一次（其余几次只进日志）。
        """

        if flag not in self.TOLD_FLAGS:
            raise ValueError(f"不认识的记号：{flag}")
        if self.preference(person_id) is None:
            return None
        return self._write_person(person_id, **{flag: True})

    def asking(self) -> tuple[Preference, ...]:
        """**该问还没问**的那几个人（问话还没发出去、而且从没问过）。"""

        found: list[Preference] = []
        for key, event in self._people.replay().items():
            if str(event.get("channel") or "") or not str(event.get("ask_session") or ""):
                continue
            if bool(event.get("asked") or False):
                continue
            found.append(Preference(person_id=str(key),
                                    email=str(event.get("email") or ""),
                                    ask_session=str(event.get("ask_session") or ""),
                                    ask_group=str(event.get("ask_group") or ""),
                                    ask_what=str(event.get("ask_what") or ""),
                                    ask_tries=int(event.get("ask_tries") or 0)))
        return tuple(sorted(found, key=lambda item: item.person_id))

    # --- 提醒 ---------------------------------------------------------------

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
        """收下一条提醒。**同一条再来一次就是同一条**（主键一样，覆盖成待发）。"""

        key = reminder_key(person_id, session_id, at, what)
        return self._write(Reminder(key=key, person_id=str(person_id),
                                    session_id=str(session_id), group_id=str(group_id),
                                    channel=str(channel), at=float(at), what=str(what)))

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

    def adopt_channel(self, person_id: str, channel: str) -> int:
        """他刚选了 `channel` → 把这个人**还没定方式**的待发提醒一起改成它。

        为什么必须有这一步：他第一次说"8 点提醒我交作业"时那条提醒是**在他答之前**
        收下的（`channel=""`）。他随后选了"私聊"，那条提醒要是还留着"没定"，
        到点就会按"他当时说话的地方"（可能是群里）叫他——**与他的选择相反**。
        返回改了几条。
        """

        if channel not in CHANNELS:
            return 0
        changed = 0
        for item in self._all().values():
            if item.person_id == str(person_id) and item.live and not item.channel:
                self._replace(item, channel=str(channel))
                changed += 1
        return changed

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
        self._reminders.maybe_compact(self._reminders.replay())
        # 账本按"天"裁：过期的行直接丢掉（`replay()` 里按主键折叠，过期行没了就没了）。
        live = {str(row["key"]): {k: v for k, v in row.items() if k != "key"}
                for row in self.mail_ledger()}
        self._mail.maybe_compact(live)
