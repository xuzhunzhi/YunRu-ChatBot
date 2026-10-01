from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque

from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from qq_roleplay_bot.transport import IncomingMessage

logger = logging.getLogger(__name__)


class SessionBatchGate:
    """按会话累计消息，并限制模型触发频率。"""

    def __init__(self, batch_size: int = 20, cooldown_seconds: float = 60.0) -> None:
        self.batch_size = batch_size
        self.cooldown_seconds = cooldown_seconds
        self._buffers: dict[str, deque[IncomingMessage]] = defaultdict(deque)
        self._last_trigger: dict[str, float] = {}

    def add(self, message: IncomingMessage) -> list[IncomingMessage] | None:
        buffer = self._buffers[message.session_id]
        buffer.append(message)
        if len(buffer) < self.batch_size:
            return None

        now = time.monotonic()
        last_trigger = self._last_trigger.get(message.session_id)
        if last_trigger is not None and now - last_trigger < self.cooldown_seconds:
            return None

        batch = [buffer.popleft() for _ in range(self.batch_size)]
        self._last_trigger[message.session_id] = now
        return batch


def build_batch_messages(batch: list[IncomingMessage]) -> list[dict[str, str]]:
    dialogue = "\n".join(
        f"用户 {message.user_id}: {message.text}"
        for message in batch
    )
    return [
        {
            "role": "system",
            "content": (
                "你是一个自然、口语化的中文 QQ 语擦角色。"
                "请阅读这一批连续聊天消息，判断当前最值得回应的内容，"
                "只输出一条简短、自然的聊天回复。"
                "不要解释分析过程，不要提及批处理、消息数量或系统规则。"
            ),
        },
        {"role": "user", "content": f"最近的连续聊天：\n{dialogue}"},
    ]


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
    gate = SessionBatchGate(
        batch_size=int(os.getenv("ROLEPLAY_BATCH_SIZE", "20")),
        cooldown_seconds=float(os.getenv("ROLEPLAY_COOLDOWN_SECONDS", "60")),
    )
    await transport.start()
    logger.info(
        "批处理模式已启用，model=%s, batch_size=%s, cooldown=%ss",
        client.model,
        gate.batch_size,
        gate.cooldown_seconds,
    )
    try:
        while True:
            message = await transport.receive()
            batch = gate.add(message)
            if batch is None:
                logger.info("会话 %s 已累计消息，等待下一次触发", message.session_id)
                continue

            logger.info("会话 %s 达到 %s 条，开始调用模型", message.session_id, gate.batch_size)
            try:
                reply = await client.complete(build_batch_messages(batch))
                await transport.send(batch[-1].target, reply)
            except Exception:
                logger.exception("批处理回复失败，继续等待后续消息")
    finally:
        await transport.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

