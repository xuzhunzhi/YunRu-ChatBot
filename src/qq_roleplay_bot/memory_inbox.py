"""Bounded asynchronous capture; receipt of a message never waits for SQLite."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from .memory_model import (InboxEvent, MemoryMetrics, is_private_memory_group,
                           memory_group_for, safe_memory_text)
from .memory_store import MemoryStore
from .security import sanitize_chat_text
from .transport import IncomingMessage

logger = logging.getLogger(__name__)


class MemoryInbox:
    def __init__(self, store: MemoryStore, allowed_groups, metrics: MemoryMetrics, *, clock=time.time):
        self.store = store
        # **记忆开放的群**（`/super enable memory`，在那个群里说的那一句）。调用时现读，
        # 所以命令一改就生效。它与"这个群开不开对话"（`/admin enable|disable`）
        # 是**两张互不相干的名单**：2026-10-08 之前这里是同一个集合，于是关掉一个群的
        # 对话顺手就把它的待处理材料当成"白名单变了"**静默删掉**（体检实测丢过 249 条）。
        self.allowed_groups = allowed_groups
        self.metrics = metrics
        self.clock = clock
        # 队列里带上发送者显示名：它要进 `people` 表（记忆归属渲染成名字、维护 agent
        # 挑关联人都靠它），而 InboxEvent 本身不带名字。
        self.queue: asyncio.Queue[tuple[InboxEvent, str]] = asyncio.Queue(maxsize=512)

    def offer(self, message: IncomingMessage, *, reply: str | None = None,
              session_key: str | None = None) -> bool:
        # 私聊没有群号，但记忆的每一层（收件箱、维护批、检索）都以群号为轴，
        # 所以在这里把它当成伪群 `dm:<uid>`（理由见 `memory_model.DM_GROUP_PREFIX`）。
        group = memory_group_for(message.target.group_id, message.user_id,
                                 channel=getattr(message, "channel", ""))
        # 收不收：**群聊**看这个群开没开记忆（`/super enable memory`）；**私聊全收**
        # （用户 2026-10-22「记所有人」，不再只收主人）。回不回是群对话开关的事，
        # 与这张名单互不相干。
        #
        # 私聊内容**不做群侧隔离**，这是用户当天的口径：「记忆进群，私聊内容泄漏
        # 风险自负」——她学到的东西照旧可以升成 `user_global`（群里也能用）。
        # 群侧检索不会看到 `dm:` 的 `user_group` / `group` 记录，靠的是命名空间天然
        # 不相交，**不是**新造一层 visibility 过滤。
        if group is None or message.is_bot_message:
            return False
        if not is_private_memory_group(group) and group not in self.allowed_groups():
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
                # 排队之后这个群的记忆被关掉：不写库（关掉就是"不再收"）。
                # **已入库的行一条都不动**——重新打开后它们照旧被维护处理。
                #
                # 私聊伪群不受群名单约束（与 `offer` 同一判据）：它的开关是记忆总开关
                # （`MemorySettings.enabled`，关掉时 `MemoryService.capture` 根本不会入队）。
                if (not is_private_memory_group(event.group_id)
                        and event.group_id not in self.allowed_groups()):
                    self.metrics.dropped += 1
                else:
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
