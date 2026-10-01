"""低频触发与消息去重：Stage 3 主循环共享的基础设施。

从旧的 Stage 2 模块中抽出，避免 Stage 3 依赖回退版本。两者行为与
`ARCHITECTURE.md` 记录的语义一致：普通状态按批量或等待时间触发，
显式 @ 与私聊调试属于强制触发。
"""
from __future__ import annotations

import time
from collections import deque

from qq_roleplay_bot.transport import IncomingMessage


class MessageDeduplicator:
    """按 message_id 抑制 OneBot 重复事件，窗口固定不会无限增长。"""

    def __init__(self, max_size: int = 4096) -> None:
        self.max_size = max_size
        self.seen: set[str] = set()
        self.order: deque[str] = deque()

    def accept(self, message_id: str) -> bool:
        if message_id in self.seen:
            return False
        self.seen.add(message_id)
        self.order.append(message_id)
        while len(self.order) > self.max_size:
            self.seen.discard(self.order.popleft())
        return True


class IdleTrigger:
    """普通状态的低频检查器；进入对话后由 Stage 3 逐条判断。"""

    def __init__(
        self,
        batch_size: int = 20,
        interval_seconds: float = 60.0,
        cooldown_seconds: float = 60.0,
        max_buffer_size: int = 100,
    ) -> None:
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self._buffer: deque[IncomingMessage] = deque(maxlen=max_buffer_size)
        self._first_message_at: float | None = None
        self._last_trigger_at: float | None = None

    def add(self, message: IncomingMessage, *, force: bool = False) -> list[IncomingMessage] | None:
        now = time.monotonic()
        self._buffer.append(message)
        if self._first_message_at is None:
            self._first_message_at = now
        # 显式 @ 机器人和私聊调试属于强制触发：必须绕过冷却，否则会出现
        # “被叫到却因为上一轮触发的冷却而完全沉默”。冷却只约束阈值触发。
        if not force and self._in_cooldown(now):
            return None
        if not force and len(self._buffer) < self.batch_size and now - self._first_message_at < self.interval_seconds:
            return None
        batch = list(self._buffer)[-self.batch_size:]
        self.reset()
        self._last_trigger_at = now
        return batch

    def reset(self) -> None:
        """清空待处理缓冲并解除冷却。

        显式退出对话、管理员关群或清理会话都会调用它；如果保留 _last_trigger_at，
        用户刚结束一轮对话后再发消息会被上一轮的冷却静默挡住最长一个冷却周期。
        """

        self._buffer.clear()
        self._first_message_at = None
        self._last_trigger_at = None

    def _in_cooldown(self, now: float) -> bool:
        return self._last_trigger_at is not None and now - self._last_trigger_at < self.cooldown_seconds
