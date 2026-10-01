from __future__ import annotations

import asyncio
import logging
import time

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
AT_REPLY_PROBABILITY = 1.0


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = OpenAICompatibleClient(API_BASE_URL, API_KEY, API_MODEL)
    transport = OneBotWebSocketTransport(port=8080)
    gate = SessionBatchGate(BATCH_SIZE, COOLDOWN_SECONDS)
    last_trigger: dict[str, float] = {}
    await transport.start()
    logger.info(
        "开发批处理模式已启用，model=%s, target_group=%s, batch_size=%s, cooldown=%ss, at_probability=100%%",
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

            now = time.monotonic()
            cooldown_active = (
                message.session_id in last_trigger
                and now - last_trigger[message.session_id] < COOLDOWN_SECONDS
            )
            if message.is_bot_mentioned and not cooldown_active:
                batch = [message]
                last_trigger[message.session_id] = now
                logger.info("目标群消息艾特 Bot，100%% 概率立即触发模型")
            else:
                batch = gate.add(message)
                if batch is not None:
                    last_trigger[message.session_id] = now

            if batch is None:
                logger.info("目标群已累计消息，等待下一次触发")
                continue

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

