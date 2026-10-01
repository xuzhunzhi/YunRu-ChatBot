from __future__ import annotations

import asyncio
import logging

from .batch_main import SessionBatchGate, build_batch_messages
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

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = OpenAICompatibleClient(API_BASE_URL, API_KEY, API_MODEL)
    transport = OneBotWebSocketTransport(port=8080)
    gate = SessionBatchGate(BATCH_SIZE, COOLDOWN_SECONDS)
    await transport.start()
    logger.info(
        "开发批处理模式已启用，model=%s, target_group=%s, batch_size=%s, cooldown=%ss",
        client.model,
        TARGET_GROUP_ID,
        gate.batch_size,
        gate.cooldown_seconds,
    )
    try:
        while True:
            message = await transport.receive()
            if message.target.group_id != TARGET_GROUP_ID:
                logger.debug("忽略非目标会话：%s", message.session_id)
                continue

            batch = gate.add(message)
            if batch is None:
                logger.info("目标群已累计消息，等待下一次触发")
                continue

            logger.info("目标群达到 %s 条，开始调用模型", gate.batch_size)
            try:
                reply = await client.complete(build_batch_messages(batch))
                await transport.send(batch[-1].target, reply)
            except Exception:
                logger.exception("目标群批处理回复失败，继续等待后续消息")
    finally:
        await transport.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

