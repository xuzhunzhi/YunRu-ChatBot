"""到点提醒**自己的**数据：一个人的叫他方式 + 一串还没发的提醒。

## 为什么数据在插件这一侧

用户 2026-10-08 的口径：**核心听懂 + 插件做事 + 核心发送**。"该怎么叫他"与
"还有哪几条没发"是这件事自己的状态，核心一个字不认识——核心只在认到提醒请求时把
（谁 · 什么事 · 什么时刻 · 他的邮箱）递进来，插件收下；到点了插件再把"要发什么"交回去。

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

#: 三种叫他方式。**没有第四种**：加一种要同时改核心的发送侧，那是另一侧的事。
CHANNEL_GROUP = "group"
CHANNEL_PRIVATE = "private"
CHANNEL_MAIL = "mail"
CHANNELS = (CHANNEL_GROUP, CHANNEL_PRIVATE, CHANNEL_MAIL)

#: 一条提醒的四种状态。
WAITING = "waiting"
SENT = "sent"
FAILED = "failed"
CANCELLED = "cancelled"

#: 一条提醒最多试几次。"发不出去记下来可重试"不等于无限重试——一直失败是配置或
#: 渠道的问题，无限重试只会每半分钟往日志里灌一行。
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
    """一个人**已经被问过**之后的答案：以后用哪种方式叫他、他有没有邮箱。"""

    person_id: str
    channel: str
    email: str = ""

    @property
    def has_email(self) -> bool:
        return bool(self.email)


@dataclass(frozen=True, slots=True)
class Reminder:
    """一条到点提醒。`at` 是**核心算好的绝对时刻**（epoch 秒）——插件不做时间推算。"""

    key: str
    person_id: str
    session_id: str
    group_id: str
    channel: str
    at: float
    what: str
    state: str = WAITING
    tries: int = 0
    last_error: str = ""

    @property
    def live(self) -> bool:
        """还算"待发"（没发出去、也没被取消）。"""

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
        self._clock = clock

    # --- 人 -----------------------------------------------------------------

    def preference(self, person_id: str) -> Preference | None:
        """他上次选的那种方式；从没被问过就是 `None`（= 该问一次）。"""

        event = self._people.replay().get(str(person_id))
        if event is None:
            return None
        channel = str(event.get("channel") or "")
        if channel not in CHANNELS:
            return None
        return Preference(person_id=str(person_id), channel=channel,
                          email=str(event.get("email") or ""))

    def remember_person(self, person_id: str, *, channel: str | None = None,
                        email: str | None = None) -> Preference:
        """记下/刷新这个人的方式与地址。**只在新值非空时覆盖**（免得一次没带地址就抹掉）。

        地址是核心递过来的那一份（见模块头）；`email=""` 表示"这次核心没给"，
        不等于"他没有邮箱"，所以不覆盖。
        """

        current = self.preference(person_id)
        keep_channel = current.channel if current is not None else ""
        keep_email = current.email if current is not None else ""
        if channel is not None and channel in CHANNELS:
            keep_channel = channel
        if email:
            keep_email = str(email)
        event = {"key": str(person_id), "channel": keep_channel, "email": keep_email,
                 "at": self._clock()}
        self._people.append(event)
        return Preference(person_id=str(person_id), channel=keep_channel, email=keep_email)

    # --- 提醒 ---------------------------------------------------------------

    def _all(self) -> dict[str, Reminder]:
        found: dict[str, Reminder] = {}
        for key, event in self._reminders.replay().items():
            channel = str(event.get("channel") or "")
            if channel not in CHANNELS:
                continue
            found[key] = Reminder(
                key=key,
                person_id=str(event.get("person_id") or ""),
                session_id=str(event.get("session_id") or ""),
                group_id=str(event.get("group_id") or ""),
                channel=channel,
                at=float(event.get("at") or 0.0),
                what=str(event.get("what") or ""),
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
            what: str, channel: str) -> Reminder:
        """收下一条提醒。**同一条再来一次就是同一条**（主键一样，覆盖成待发）。"""

        key = reminder_key(person_id, session_id, at, what)
        return self._write(Reminder(key=key, person_id=str(person_id),
                                    session_id=str(session_id), group_id=str(group_id),
                                    channel=str(channel), at=float(at), what=str(what)))

    def list_for(self, person_id: str, *, now: float | None = None,
                 include_finished: bool = False) -> tuple[Reminder, ...]:
        """这个人**还没发出去**的提醒，按时刻先后排。"""

        moment = self._clock() if now is None else float(now)
        found = [item for item in self._all().values()
                 if item.person_id == str(person_id)
                 and item.at > moment - 86400      # 一天以前的不再列（早该发过了）
                 and (include_finished or item.live)]
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

    def compact(self) -> None:
        """把两本日志各自压一次（行数远多于有效条数时才真写）。"""

        self._people.maybe_compact(self._people.replay())
        self._reminders.maybe_compact(self._reminders.replay())
