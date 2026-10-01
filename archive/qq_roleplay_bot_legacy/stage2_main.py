from __future__ import annotations

import asyncio
import logging
import time

from qq_roleplay_bot.dev_config import BATCH_SIZE, COOLDOWN_SECONDS, API_KEY, API_BASE_URL, API_MODEL, TARGET_GROUP_ID
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.onebot_ws import OneBotWebSocketTransport
from .stage2_runtime import ConversationMode, ConversationState, DecisionKind, build_stage2_messages, parse_stage2_output
from .trigger import IdleTrigger, MessageDeduplicator

HISTORY_LIMIT = 50
ACTIVE_TIMEOUT_SECONDS = 180.0
logger = logging.getLogger(__name__)


class Stage2Sessions:
    def __init__(self):
        self.states: dict[str, ConversationState] = {}
        self.triggers: dict[str, IdleTrigger] = {}

    def state(self, session_id: str) -> ConversationState:
        return self.states.setdefault(session_id, ConversationState(history_limit=HISTORY_LIMIT))

    def trigger(self, session_id: str) -> IdleTrigger:
        return self.triggers.setdefault(session_id, IdleTrigger(BATCH_SIZE, 60.0, COOLDOWN_SECONDS))

    def active(self, state: ConversationState) -> bool:
        if state.mode is not ConversationMode.ACTIVE:
            return False
        if state.last_activity_at is not None and time.monotonic() - state.last_activity_at > ACTIVE_TIMEOUT_SECONDS:
            state.exit()
            return False
        return True

    def leave(self, session_id: str) -> None:
        self.state(session_id).exit()
        self.trigger(session_id).reset()


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = OpenAICompatibleClient(API_BASE_URL, API_KEY, API_MODEL)
    transport = OneBotWebSocketTransport(port=8080)
    sessions = Stage2Sessions()
    deduplicator = MessageDeduplicator()
    await transport.start()
    logger.info("Stage 2 started: group=%s batch=%s cooldown=%s model=%s", TARGET_GROUP_ID, BATCH_SIZE, COOLDOWN_SECONDS, client.model)
    try:
        while True:
            message = await transport.receive()
            if message is None:
                break
            if message.target.group_id != TARGET_GROUP_ID or not deduplicator.accept(message.message_id):
                continue
            state = sessions.state(message.session_id)
            active = sessions.active(state)
            state.add(message)
            if active:
                mode, reason = ConversationMode.ACTIVE, "active_message"
            else:
                mode = ConversationMode.IDLE
                if sessions.trigger(message.session_id).add(message, force=message.is_bot_mentioned) is None:
                    continue
                reason = "mention" if message.is_bot_mentioned else "threshold"
            try:
                result = await client.complete(build_stage2_messages(state.recent(), mode=mode, trigger=reason))
                decision = parse_stage2_output(result)
            except Exception:
                logger.exception("Stage 2 model check failed")
                continue
            if decision.kind is DecisionKind.EXIT:
                sessions.leave(message.session_id)
            elif decision.kind is DecisionKind.REPLY:
                state.enter()
                try:
                    await transport.send(message.target, decision.text)
                except Exception:
                    logger.exception("Stage 2 send failed")
    finally:
        await transport.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
