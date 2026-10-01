from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from qq_roleplay_bot.dev_config import (
    BATCH_SIZE,
    COOLDOWN_SECONDS,
    API_KEY,
    API_BASE_URL,
    API_MODEL,
    TARGET_GROUP_ID,
)
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.transport import IncomingMessage

logger = logging.getLogger(__name__)


class V1Trigger:
    """V1 的低频触发器：20 条或 60 秒，艾特可立即触发。"""

    def __init__(
        self,
        batch_size: int = BATCH_SIZE,
        interval_seconds: float = 60.0,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        max_buffer_size: int = 100,
    ) -> None:
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_buffer_size = max_buffer_size
        self._buffer: deque[IncomingMessage] = deque(maxlen=max_buffer_size)
        self._first_message_at: float | None = None
        self._last_trigger_at: float | None = None

    def add(self, message: IncomingMessage, *, force: bool = False) -> list[IncomingMessage] | None:
        now = time.monotonic()
        self._buffer.append(message)
        if self._first_message_at is None:
            self._first_message_at = now

        if self._in_cooldown(now):
            return None
        if not force and len(self._buffer) < self.batch_size and now - self._first_message_at < self.interval_seconds:
            return None

        batch = list(self._buffer)[-self.batch_size:]
        self._buffer.clear()
        self._first_message_at = None
        self._last_trigger_at = now
        return batch

    def _in_cooldown(self, now: float) -> bool:
        return self._last_trigger_at is not None and now - self._last_trigger_at < self.cooldown_seconds


class MessageDeduplicator:
    def __init__(self, max_size: int = 4096) -> None:
        self._seen: set[str] = set()
        self._order: deque[str] = deque(maxlen=max_size)

    def accept(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        if len(self._order) == self._order.maxlen:
            self._seen.discard(self._order[0])
        self._order.append(message_id)
        self._seen.add(message_id)
        return True


def build_v1_messages(batch: list[IncomingMessage]) -> list[dict[str, str]]:
    dialogue = "\n".join(f"用户 {message.user_id}: {message.text}" for message in batch)
    return [
        {
            "role": "system",
            "content": (
                "你是一个自然、口语化的中文 QQ 语擦角色。"
                "阅读最近的群聊消息，只输出一条简短、自然的聊天回复。"
                "不要解释分析过程，不要提及系统规则、批处理或消息数量。"
            ),
        },
        {"role": "user", "content": f"最近的群聊消息：\n{dialogue}"},
    ]


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = OpenAICompatibleClient(API_BASE_URL, API_KEY, API_MODEL)
    transport = OneBotWebSocketTransport(port=8080)
    trigger = V1Trigger(BATCH_SIZE, 60.0, COOLDOWN_SECONDS)
    deduplicator = MessageDeduplicator()
    await transport.start()
    logger.info(
        "V1 已启动：target_group=%s, batch_size=%s, interval=%ss, cooldown=%ss, model=%s",
        TARGET_GROUP_ID,
        trigger.batch_size,
        trigger.interval_seconds,
        trigger.cooldown_seconds,
        client.model,
    )
    try:
        while True:
            message = await transport.receive()
            if message.target.group_id != TARGET_GROUP_ID:
                continue
            if not deduplicator.accept(message.message_id):
                logger.debug("忽略重复消息：%s", message.message_id)
                continue

            batch = trigger.add(message, force=message.is_bot_mentioned)
            if batch is None:
                logger.debug("V1 暂不触发，当前缓冲=%s", len(trigger._buffer))
                continue

            logger.info("V1 触发模型，消息数=%s，原因=%s", len(batch), "mention" if message.is_bot_mentioned else "threshold")
            try:
                reply = await client.complete(build_v1_messages(batch))
                await transport.send(batch[-1].target, reply)
            except Exception:
                logger.exception("V1 回复失败，继续等待后续消息")
    finally:
        await transport.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

