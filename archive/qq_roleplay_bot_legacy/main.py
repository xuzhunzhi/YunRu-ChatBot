from __future__ import annotations

import asyncio
import logging
import os

from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport

logger = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    transport = OneBotWebSocketTransport(
        host=os.getenv("ONEBOT_WS_HOST", "127.0.0.1"),
        port=int(os.getenv("ONEBOT_WS_PORT", "8080")),
        access_token=os.getenv("ONEBOT_ACCESS_TOKEN", ""),
    )
    await transport.start()
    try:
        while True:
            message = await transport.receive()
            if message is None:
                # 传输层已关闭；继续循环会变成忙等待。
                break
            try:
                await transport.send(message.target, f"收到：{message.text}")
            except Exception:
                logger.exception("发送 QQ 消息失败，继续等待后续消息")
    finally:
        await transport.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

