"""Bounded asynchronous capture; receipt of a message never waits for SQLite."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from .memory_model import InboxEvent, MemoryMetrics, safe_memory_text
from .memory_store import MemoryStore
from .security import sanitize_chat_text
from .transport import IncomingMessage

logger = logging.getLogger(__name__)


class MemoryInbox:
    def __init__(self, store: MemoryStore, allowed_groups, metrics: MemoryMetrics, *, clock=time.time):
        self.store = store
        self.allowed_groups = allowed_groups
        self.metrics = metrics
        self.clock = clock
        # 队列里带上发送者显示名：它要进 `people` 表（记忆归属渲染成名字、维护 agent
        # 挑关联人都靠它），而 InboxEvent 本身不带名字。
        self.queue: asyncio.Queue[tuple[InboxEvent, str]] = asyncio.Queue(maxsize=512)

    def offer(self, message: IncomingMessage, *, reply: str | None = None,
              session_key: str | None = None) -> bool:
        group = message.target.group_id
        if group not in self.allowed_groups() or message.is_bot_message:
            return False
        raw = reply if reply is not None else message.text
        # Credentials/local diagnostics do not enter either temporary storage or the agent.
        if not raw or len(raw) > 8000 or not safe_memory_text(raw):
            self.metrics.dropped += 1
            return False
        text = sanitize_chat_text(raw, max_length=1000).strip()
        if not text:
            return False
        speaker = "yunru" if reply is not None else "user"
        # 去重键必须带上会话：同一个人在同一个群里 user_id 相同，但会话标识
        # 才能区分群聊与私聊，避免不同会话的相同 message_id 互相顶掉。
        scope = session_key or group
        key = f"{group}:{scope}:{message.user_id}:{message.message_id}:{speaker}"
        event = InboxEvent(hashlib.sha256(key.encode()).hexdigest(), group, message.user_id, speaker, text, self.clock())
        # 她自己的回复不算"这个人的显示名"来源，只有真人发言才更新名册。
        name = "" if reply is not None else message.sender_name
        try:
            self.queue.put_nowait((event, name))
        except asyncio.QueueFull:
            self.metrics.dropped += 1
            return False
        self.metrics.enqueued += 1
        return True

    async def run(self):
        while True:
            event, sender_name = await self.queue.get()
            try:
                if event.group_id in self.allowed_groups():
                    saved = False
                    for attempt in range(3):
                        try:
                            saved = await asyncio.to_thread(self.store.append, event,
                                                            sender_name=sender_name)
                            break
                        except Exception as exc:
                            if attempt == 2:
                                raise exc
                            await asyncio.sleep(0.2 * (attempt + 1))
                    if not saved:
                        self.metrics.dropped += 1
            except Exception as exc:
                self.metrics.failures += 1
                logger.warning("memory_capture_failed category=%s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def flush(self):
        await self.queue.join()
