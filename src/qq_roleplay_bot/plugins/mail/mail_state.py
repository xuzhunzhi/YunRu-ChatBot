"""邮箱侧的运行状态：上次汇报什么时候发的、最近发过什么。

**为什么单独一个小文件**：它是 Stage 4 的状态，跟会话/群名单无关；换机器时
`data/` 一起搬走就带上了，不需要动状态库结构（也不用加数据库表）。

写法沿用 `state_store` 那一套：临时文件 + `os.replace` 原子替换，任何读写失败都
降级为"当作没发过"——**宁可多发一封，也不要因为状态文件坏了而永远不发**。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from ...dev_config import data_dir

logger = logging.getLogger(__name__)

STATE_VERSION = 1
# 送信流水只留最近这些条：它只用来给超管看一眼和算配额，不是账本。
MAX_SEND_RECORDS = 50
# 发出去的信留最近几封。**为什么存正文**：用户 2026-09-28 的现场问题是
# "云茹不记得自己邮件发了什么，也不知道邮件是发给我了"——信是她写给主人的，
# 对话里她得能想起自己说过什么。留 5 封够回溯，不至于把状态文件撑成邮局。
MAX_LETTERS = 5
# 单封信在状态文件里最多留多少字（正文上限本来就更小，这里是二次兜底）。
MAX_LETTER_CHARS = 4000
# 处理过的来信 id 留最近这些条（2026-09-30 读信回信用）：幂等靠它，重启也不会重复回。
MAX_PROCESSED_MAIL = 100


def default_state_path() -> Path:
    root = data_dir()
    return root / "mail_state.json"


@dataclass
class MailState:
    """`last_report_at` 是**时间戳**不是日期：汇报的窗口是"距上次满 24 小时"。"""

    last_report_at: float = 0.0
    # 当天失败次数（用于"最多试 3 次"），到了新的一天自动清零。
    attempts_day: str = ""
    attempts: int = 0
    sends: list[dict] = field(default_factory=list)
    # 发出去的信（最近在后）：{"at", "to", "subject", "body"}。给"她还记得自己写了什么"用。
    letters: list[dict] = field(default_factory=list)
    # 处理过的来信 id（最近在后）：读信回信的幂等标记。**只存 id，不存正文**。
    processed_mail: list[str] = field(default_factory=list)
    # 回信计数（用来卡"一天最多回几封"）：{"day": "YYYY-MM-DD", "count": n}
    reply_day: str = ""
    replies: int = 0
    # 按发件人的回信计数（同一天内）：{"day": "YYYY-MM-DD", "counts": {"a@b": 1}}
    # 防的是"对面是自动回复 → 她再回"这种来回刷。
    sender_day: str = ""
    sender_counts: dict[str, int] = field(default_factory=dict)
    # 学会的"邮箱地址 → QQ 号"（2026-09-30）：非 QQ 邮箱的发件人在回信里报了 QQ 号，
    # 核实过就记在这里，以后就按那个 QQ 号认人。**只存映射，不存正文**。
    links: dict[str, str] = field(default_factory=dict)

    @property
    def last_send_at(self) -> float:
        return float(self.sends[-1].get("at", 0.0)) if self.sends else 0.0

    # --- 判断"该不该发"（放在 MailState 上：这是状态的语义，不是存储的事） ----

    @staticmethod
    def today_target(now: float, *, hour: int, minute: int) -> float:
        """今天的目标时刻（本地时间）的时间戳。"""

        local = time.localtime(now)
        return time.mktime((
            local.tm_year, local.tm_mon, local.tm_mday, hour, minute, 0, 0, 0, -1,
        ))

    @classmethod
    def report_due(cls, state: "MailState", now: float, *, hour: int = 23, minute: int = 0) -> bool:
        """今天的目标时刻到了、而且自那以后还没发过 → 该发。

        **不能只看"距上次满 24 小时"**：那样一旦有一次发晚了（比如 23:00 关机、次日 01:00
        才开机补发），后面就会永久停在 01:00 发——"每晚十一点"这条要求就废了。
        按"今天的目标时刻过了没有"判断，补发之后第二天仍然是 23:00。

        窗口另算：素材覆盖"距上次汇报到现在"，所以 23:00–24:00 那段既不会被漏掉
        （它进的是第二天的汇报），也不会被重复计入。
        """

        target = cls.today_target(now, hour=hour, minute=minute)
        if now < target:
            return False
        return float(state.last_report_at or 0.0) < target


class MailStateStore:
    """读写 `data/mail_state.json`；失败只记日志，绝不抛给调用方。"""

    def __init__(self, path: Path | str | None = None, *, clock=time.time) -> None:
        self.path = Path(path) if path is not None else default_state_path()
        self.clock = clock
        self.last_error = ""

    def load(self) -> MailState:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return MailState()
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("mail_state_read_failed category=%s", type(exc).__name__)
            return MailState()
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("mail_state_invalid_json; 当作没发过")
            return MailState()
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            self.last_error = "unsupported_version"
            return MailState()
        sends = data.get("sends")
        letters = data.get("letters")
        processed = data.get("processed_mail")
        return MailState(
            last_report_at=float(data.get("last_report_at") or 0.0),
            attempts_day=str(data.get("attempts_day") or ""),
            attempts=int(data.get("attempts") or 0),
            sends=[item for item in sends if isinstance(item, dict)] if isinstance(sends, list) else [],
            letters=(
                [item for item in letters if isinstance(item, dict)]
                if isinstance(letters, list) else []
            ),
            processed_mail=(
                [str(item) for item in processed if str(item).strip()]
                if isinstance(processed, list) else []
            ),
            reply_day=str(data.get("reply_day") or ""),
            replies=int(data.get("replies") or 0),
            sender_day=str(data.get("sender_day") or ""),
            sender_counts=(
                {str(k): int(v) for k, v in (data.get("sender_counts") or {}).items()}
                if isinstance(data.get("sender_counts"), dict) else {}
            ),
            links=(
                {str(k).casefold(): str(v) for k, v in (data.get("links") or {}).items()}
                if isinstance(data.get("links"), dict) else {}
            ),
        )

    def save(self, state: MailState) -> bool:
        body = {
            "version": STATE_VERSION,
            "last_report_at": round(float(state.last_report_at), 3),
            "attempts_day": state.attempts_day,
            "attempts": int(state.attempts),
            "sends": state.sends[-MAX_SEND_RECORDS:],
            "letters": state.letters[-MAX_LETTERS:],
            "processed_mail": state.processed_mail[-MAX_PROCESSED_MAIL:],
            "reply_day": state.reply_day,
            "replies": int(state.replies),
            "sender_day": state.sender_day,
            "sender_counts": {k: int(v) for k, v in list(state.sender_counts.items())[-50:]},
            "links": dict(list(state.links.items())[-100:]),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("mail_state_write_failed category=%s", type(exc).__name__)
            return False
        self.last_error = ""
        return True

    def record(
        self,
        *,
        kind: str,
        to: str,
        ok: bool,
        detail: str = "",
        state: MailState | None = None,
    ) -> MailState:
        """记一次发送结果并落盘。`kind` 现在是 `report`（以后会有 `manual`）。"""

        current = state or self.load()
        now = self.clock()
        current.sends.append({
            "at": round(now, 3), "kind": kind, "to": to, "ok": bool(ok),
            "detail": detail[:200],
        })
        current.sends = current.sends[-MAX_SEND_RECORDS:]
        if ok and kind == "report":
            current.last_report_at = now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if current.attempts_day != today:
            current.attempts_day = today
            current.attempts = 0
        if not ok and kind == "report":
            current.attempts += 1
        self.save(current)
        return current

    # --- 判断"该不该发"（这两个搬到了 MailState 上，见上） ---------------------

    def record_letter(
        self,
        *,
        subject: str,
        body: str,
        to: str,
        now: float | None = None,
        state: MailState | None = None,
    ) -> MailState:
        """把发出去的信留一份（最近在后）。**只记真的发成功的信**。

        正文进状态文件，是因为她得能想起自己写过什么；文件在 `data/` 下，
        跟 `sends` 一样是本机运行资料，不进记忆库、不进日志正文。
        """

        current = state or self.load()
        stamp = self.clock() if now is None else float(now)
        current.letters.append({
            "at": round(stamp, 3),
            "to": str(to)[:200],
            "subject": str(subject).strip()[:200],
            "body": str(body).strip()[:MAX_LETTER_CHARS],
        })
        current.letters = current.letters[-MAX_LETTERS:]
        self.save(current)
        return current

    @staticmethod
    def tries_left(state: MailState, now: float, *, limit: int = 3) -> int:
        """当天还剩几次重试。跨天自动满额。"""

        today = time.strftime("%Y-%m-%d", time.localtime(now))
        used = int(state.attempts) if state.attempts_day == today else 0
        return max(0, int(limit) - used)

    # --- 读信回信的状态（2026-09-30）-----------------------------------------

    def is_mail_processed(self, message_id: str) -> bool:
        """这封信处理过没有。**读不到状态文件时返回 False**（宁可重复一次，
        也不要因为状态坏了就永远不回信——与"宁可多发一封"同一条取舍）。"""

        target = str(message_id or "").strip()
        if not target:
            return True
        return target in self.load().processed_mail

    def mark_mail_processed(self, message_id: str) -> None:
        target = str(message_id or "").strip()
        if not target:
            return
        state = self.load()
        if target in state.processed_mail:
            return
        state.processed_mail.append(target)
        state.processed_mail = state.processed_mail[-MAX_PROCESSED_MAIL:]
        self.save(state)

    def replies_today(self) -> int:
        """今天回过几封。"""

        state = self.load()
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        return int(state.replies) if state.reply_day == today else 0

    def record_mail_reply(self, sender: str = "") -> None:
        """记一次回信（跨天自动清零）。带 `sender` 时同时记这个人的次数。"""

        state = self.load()
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if state.reply_day != today:
            state.reply_day = today
            state.replies = 0
        state.replies += 1
        target = str(sender or "").strip().casefold()
        if target:
            if state.sender_day != today:
                state.sender_day = today
                state.sender_counts = {}
            state.sender_counts[target] = int(state.sender_counts.get(target, 0)) + 1
        self.save(state)

    def sender_replies_today(self, sender: str) -> int:
        """今天给这个地址回过几封。"""

        state = self.load()
        today = time.strftime("%Y-%m-%d", time.localtime(self.clock()))
        if state.sender_day != today:
            return 0
        return int(state.sender_counts.get(str(sender or "").strip().casefold(), 0))

    # --- 邮箱地址 ↔ QQ 号（2026-09-30）--------------------------------------

    def mail_link(self, address: str) -> str:
        """这个地址登记到哪个 QQ 号上；没有就是空串。"""

        return str(self.load().links.get(str(address or "").strip().casefold(), ""))

    def link_mail(self, address: str, user_id: str) -> None:
        """记下"这个邮箱是这个人"（来自对方在回信里自报的 QQ 号）。"""

        address = str(address or "").strip().casefold()
        user_id = str(user_id or "").strip()
        if not address or not user_id:
            return
        state = self.load()
        state.links[address] = user_id
        self.save(state)

    def unlink_mail(self, address: str) -> None:
        state = self.load()
        if state.links.pop(str(address or "").strip().casefold(), None) is not None:
            self.save(state)
