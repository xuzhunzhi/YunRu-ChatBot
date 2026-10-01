from __future__ import annotations

import asyncio
import logging
import os

from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from .prompt import build_messages

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未设置 API_KEY")

    client = OpenAICompatibleClient(
        base_url=os.environ.get("API_BASE_URL", "https://api.huanyan.ltd/v1"),
        api_key=api_key,
        model=os.environ.get("API_MODEL", "gpt-5.6-luna"),
    )
    transport = OneBotWebSocketTransport(
        host=os.getenv("ONEBOT_WS_HOST", "127.0.0.1"),
        port=int(os.getenv("ONEBOT_WS_PORT", "8080")),
        access_token=os.getenv("ONEBOT_ACCESS_TOKEN", ""),
    )
    await transport.start()
    logger.info("模型客户端已启用，model=%s", client.model)
    try:
        while True:
            message = await transport.receive()
            try:
                reply = await client.complete(build_messages(message.text))
                await transport.send(message.target, reply)
            except Exception:
                logger.exception("处理 QQ 消息失败，继续等待后续消息")
    finally:
        await transport.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

