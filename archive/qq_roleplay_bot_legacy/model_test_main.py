from __future__ import annotations

import asyncio
import logging
import os

from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.transport import MessageTarget

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ["API_KEY"]
    client = OpenAICompatibleClient(
        base_url=os.environ.get("API_BASE_URL", "https://api.huanyan.ltd/v1"),
        api_key=api_key,
        model=os.environ.get("API_MODEL", "gpt-5.6-luna"),
    )
    transport = OneBotWebSocketTransport(port=int(os.getenv("ONEBOT_WS_PORT", "8080")))
    await transport.start()
    try:
        await asyncio.wait_for(transport._connection_ready.wait(), timeout=30)
        reply = await client.complete([
            {"role": "system", "content": "你是一个自然、简短、口语化的中文 QQ 语擦角色。只输出聊天正文。"},
            {"role": "user", "content": "这是一条连接测试，请用一句自然的话回复，不要解释测试过程。"},
        ])
        await transport.send(MessageTarget(group_id="717151356"), reply)
        logger.info("模型测试回复已发送到目标群")
    finally:
        await transport.close()


if __name__ == "__main__":
    asyncio.run(run())

