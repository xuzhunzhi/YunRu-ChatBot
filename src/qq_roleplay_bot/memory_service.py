from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from .memory_config import MemorySettings
from .memory_inbox import MemoryInbox
from .memory_maintenance_agent import MemoryMaintenanceAgent
from .memory_model import MemoryMaterial, MemoryMetrics
from .memory_store import MemoryStore

logger = logging.getLogger(__name__)


class MemoryService:
    def __init__(self, client, allowed_groups, *, settings: MemorySettings | None = None,
                 store: MemoryStore | None = None, feature_logs=None):
        self.settings = settings or MemorySettings()
        self.allowed_groups = allowed_groups
        self.store = store or MemoryStore(self.settings.data_dir / "memory.sqlite3", retention_seconds=self.settings.retention_days * 86400, protect=True)
        self.metrics = MemoryMetrics()
        self.inbox = MemoryInbox(self.store, allowed_groups, self.metrics, clock=self.store.clock)
        self.client = client
        self.agent = MemoryMaintenanceAgent(client, self.store, allowed_groups, self.metrics,
                                             timeout=self.settings.model_timeout, max_batches=self.settings.max_batches,
                                             feature_logs=feature_logs)
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        if not self.settings.enabled or self._tasks:
            return
        self._tasks = [asyncio.create_task(self.inbox.run(), name="memory-inbox"),
                       asyncio.create_task(self.agent.run(self.settings.interval_seconds), name="memory-maintenance")]

    def capture(self, message):
        if self.settings.enabled:
            self.inbox.offer(message, session_key=getattr(message, "session_id", None))

    def record_sent_reply(self, message, text):
        if self.settings.enabled:
            self.inbox.offer(message, reply=text, session_key=getattr(message, "session_id", None))

    async def retrieve(self, message, topic="", mentioned=()) -> MemoryMaterial:
        group = message.target.group_id
        if not self.settings.enabled or group not in self.allowed_groups():
            return MemoryMaterial()
        self.metrics.reads += 1
        # 先各自限长再拼接：否则超长消息会把 topic 整段挤出检索窗口。
        query = f"{message.text[:600]} {topic[:300]}"
        # 跨人召回只借本群范围内的记录，所以提到的人最多取两个（理由见 MEMORY_PEOPLE.md）。
        people = tuple(dict.fromkeys(str(uid) for uid in mentioned if uid))[:2]
        try:
            records = await asyncio.wait_for(asyncio.to_thread(
                self.store.retrieve, group, message.user_id, query, people), timeout=0.3)
            if group not in self.allowed_groups():
                return MemoryMaterial()
            self.metrics.hits += len(records)
            return await self._decorate(group, records)
        except Exception as exc:
            self.metrics.failures += 1
            logger.warning("memory_lookup_failed category=%s", type(exc).__name__)
            return MemoryMaterial()

    async def _decorate(self, group: str, records) -> MemoryMaterial:
        """给记忆补上"跟谁有关"和那些人的名字——渲染成 `about="乐乐"` 而不是裸 QQ 号。"""

        if not records:
            return MemoryMaterial()
        ids = [r.id for r in records]
        try:
            subjects = await asyncio.to_thread(self.store.record_subjects_of, ids)
            wanted = {uid for users in subjects.values() for uid in users}
            wanted.update(r.subject_user_id for r in records if r.subject_user_id)
            names = await asyncio.to_thread(self.store.person_names, group, wanted)
        except Exception as exc:  # noqa: BLE001 - 装饰失败不该丢掉记忆本体
            logger.warning("memory_decorate_failed category=%s", type(exc).__name__)
            return MemoryMaterial(tuple(records))
        return MemoryMaterial(tuple(records), subjects, names)

    async def identity_for(self, message) -> MemoryMaterial:
        """判定 agent 用的最小记忆：这个人的称呼与边界（≤2 条，不写 last_used_at）。

        判定决定"要不要开口"，看不到整块记忆（prompt 有体积硬约束）；但"跟谁说话、
        有没有踩红线"正是分寸判断需要的。读库失败一律回落成空——不能因为记忆读不到
        就影响判定。
        """

        group = message.target.group_id
        if not self.settings.enabled or group not in self.allowed_groups():
            return MemoryMaterial()
        try:
            records = await asyncio.wait_for(asyncio.to_thread(
                self.store.speaker_identity, group, message.user_id), timeout=0.3)
        except Exception as exc:  # noqa: BLE001
            self.metrics.failures += 1
            logger.warning("memory_identity_failed category=%s", type(exc).__name__)
            return MemoryMaterial()
        return MemoryMaterial(records)

    async def profile_for(self, message) -> str:
        """当前说话人的**人物画像**（她对这个人的整体印象）；没有就是空串。

        为什么单独取而不是混进 `retrieve`：画像要跟着**人**走——不管这一句在聊什么，
        只要是他说话，她就该记得自己对他的印象。混进按词命中的检索反而会时有时无。
        """

        group = message.target.group_id
        if not self.settings.enabled or group not in self.allowed_groups():
            return ""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.store.profile, message.user_id), timeout=0.3)
        except Exception as exc:  # noqa: BLE001 - 画像读不到不该影响回复
            self.metrics.failures += 1
            logger.warning("memory_profile_failed category=%s", type(exc).__name__)
            return ""

    async def care_note(self, message, *, topic_shifted: bool, current_terms=()) -> dict | None:
        """该不该在这一轮顺口关心一句（用户 2026-09-28 要的"善意的追加提醒"）。

        条件全都要满足，一条都不靠模型自觉：

        1. 这个说话人有未提醒过的 `status`（正在发烧、明天有考试）；
        2. **话题已经离开那件事**（判定说 `related=NO`）或者那条状态已经 ≥1 天；
        3. 眼前这句话本身没在说那件事（避免刚说完就复读）；
        4. 这一轮本来就要开口（调用方只在 REPLY 之后才问）。

        返回 `{"id","content","age_days"}`；提醒过就写 `reminded_at`，一条状态只提醒一次。
        """

        group = message.target.group_id
        if not self.settings.enabled or group not in self.allowed_groups():
            return None
        try:
            note = await asyncio.to_thread(self.store.pending_status_note, group, message.user_id)
        except Exception as exc:  # noqa: BLE001 - 关心提示拿不到不该影响回复
            logger.warning("memory_status_note_failed category=%s", type(exc).__name__)
            return None
        if not note:
            return None
        if not topic_shifted and note["age_days"] < 1:
            return None
        text = (message.text or "").casefold()
        if any(term and term in text for term in current_terms):
            return None  # 那句话里正在说这件事，不用她再提
        return note

    async def mark_care_noted(self, record_id: str) -> None:
        try:
            await asyncio.to_thread(self.store.mark_reminded, record_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_status_mark_failed category=%s", type(exc).__name__)

    def snapshot(self):
        return {"enabled": self.settings.enabled, "queued_events": self.inbox.queue.qsize(), **asdict(self.metrics)}

    async def close(self):
        if not self._tasks:
            return
        # 先停模型维护（下标 1），再冲刷有界的捕获队列（下标 0）。
        maintenance, inbox_task = self._tasks[1], self._tasks[0]
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        try:
            await asyncio.wait_for(self.inbox.flush(), timeout=3.0)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("memory_inbox_flush_timeout queued=%s", self.inbox.queue.qsize())
        inbox_task.cancel()
        await asyncio.gather(inbox_task, return_exceptions=True)
        self._tasks.clear()
