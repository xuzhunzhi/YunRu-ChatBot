"""多群焦点：同一时刻只有一段对话"当值"，其余群排队等。

规则见 `docs/MULTI_GROUP_FOCUS.md` 第二～四节。这个模块只做**判定与记账**：
不做模型调用、不碰 transport、不知道消息长什么样。引擎负责把这里的结论变成消息。

为什么单独成模块：焦点是一组有时间语义的状态（当值多久、话题多久没继续、
连续回了几条、谁在排队、冷却到没到），散在 `handle()` 里会变成一堆难测的条件分支。

三条释放路径（对应文档第三节）：

1. `quiet`：话题没继续超过 `quiet_seconds`（45 秒）——静默走开；
2. `replies`：连续回复满 `reply_limit`（50 条）——说一句"先处理别的消息"再走；
3. `duty`：当值满 `duty_limit`（5 分钟）——一律静默走开。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .transport import IncomingMessage

QUIET_RELEASE_SECONDS = 45.0
DUTY_LIMIT_SECONDS = 300.0
REPLY_LIMIT = 50
ACK_COOLDOWN_SECONDS = 120.0


@dataclass
class FocusState:
    """当前当值的那段对话。"""

    session_id: str
    started_at: float
    replies: int = 0
    # 最后一次"有人延续这段话题"的时刻：她被人叫到、或判定说这条还在同一条线上。
    last_related_at: float = 0.0


@dataclass
class FocusController:
    quiet_seconds: float = QUIET_RELEASE_SECONDS
    duty_limit_seconds: float = DUTY_LIMIT_SECONDS
    reply_limit: int = REPLY_LIMIT
    ack_cooldown_seconds: float = ACK_COOLDOWN_SECONDS

    hot: FocusState | None = None
    # 冷群积压：只在"明确找她"的消息上排队（@ / 提及 / 引用）。
    _queues: dict[str, deque[IncomingMessage]] = field(default_factory=dict)
    # 各群第一次进队列的顺序，决定先轮到谁（先到先得）。
    _order: list[str] = field(default_factory=list)
    _ack_at: dict[str, float] = field(default_factory=dict)
    _sweeping: bool = False

    # --- 当值 -----------------------------------------------------------------

    def is_hot(self, session_id: str) -> bool:
        return self.hot is not None and self.hot.session_id == session_id

    def acquire(self, session_id: str, now: float) -> FocusState:
        self.hot = FocusState(
            session_id=session_id, started_at=now, replies=0, last_related_at=now
        )
        return self.hot

    def release(self) -> FocusState | None:
        previous = self.hot
        self.hot = None
        return previous

    def note_related(self, now: float) -> None:
        """有人延续了这段话题：她被人叫到，或判定说这条还在同一条线上。"""

        if self.hot is not None and now > self.hot.last_related_at:
            self.hot.last_related_at = now

    def note_reply(self) -> None:
        if self.hot is not None:
            self.hot.replies += 1

    def release_reason(self, now: float) -> str | None:
        """该不该走、为什么走；不该走返回 None。

        `replies` 优先于另外两条：它决定走的时候要不要打一声招呼，
        而"连回满 50 条"是唯一需要交代的情形。
        """

        if self.hot is None:
            return None
        if self.hot.replies >= self.reply_limit:
            return "replies"
        if now - self.hot.last_related_at > self.quiet_seconds:
            return "quiet"
        if now - self.hot.started_at > self.duty_limit_seconds:
            return "duty"
        return None

    def hot_stats(self, now: float) -> dict[str, object] | None:
        if self.hot is None:
            return None
        return {
            "session_id": self.hot.session_id,
            "seconds_on_duty": round(now - self.hot.started_at, 1),
            "since_related": round(now - self.hot.last_related_at, 1),
            "replies": self.hot.replies,
        }

    # --- 积压队列 -------------------------------------------------------------

    def enqueue(self, session_id: str, message: IncomingMessage) -> int:
        queue = self._queues.get(session_id)
        if queue is None:
            queue = deque()
            self._queues[session_id] = queue
            self._order.append(session_id)
        queue.append(message)
        return len(queue)

    def queue_size(self, session_id: str) -> int:
        return len(self._queues.get(session_id, ()))

    def has_backlog(self) -> bool:
        return any(self._queues.values())

    def total_queued(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    def queued_sessions(self) -> list[str]:
        """按"第一次进队列"的顺序返回还有积压的会话。"""

        return [sid for sid in self._order if self._queues.get(sid)]

    def take_next_session(self, now: float) -> str | None:
        """轮到谁：最早开始排队、且队列非空的那个会话，并把它设为当值。"""

        for session_id in self.queued_sessions():
            self.acquire(session_id, now)
            return session_id
        return None

    def drain(self, session_id: str) -> list[IncomingMessage]:
        queue = self._queues.pop(session_id, None)
        if session_id in self._order:
            self._order.remove(session_id)
        return list(queue) if queue else []

    # --- "稍等"的冷却 ---------------------------------------------------------

    def ack_allowed(self, session_id: str, now: float) -> bool:
        last = self._ack_at.get(session_id)
        return last is None or now - last >= self.ack_cooldown_seconds

    def note_ack(self, session_id: str, now: float) -> None:
        self._ack_at[session_id] = now

    def last_ack_at(self, session_id: str) -> float | None:
        return self._ack_at.get(session_id)

    # --- 20 条巡检：同一时刻只跑一个 -------------------------------------------

    @property
    def sweeping(self) -> bool:
        return self._sweeping

    def begin_sweep(self) -> None:
        self._sweeping = True

    def end_sweep(self) -> None:
        self._sweeping = False
