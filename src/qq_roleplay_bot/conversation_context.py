"""Stage 3 对话上下文补全：群消息历史与参与者身份解析。

要解决的三个实际问题：

1. **Bot 只看得见自己启动之后的消息** —— 群里聊了半小时再 @ 它，它对之前
   一无所知，于是只能对每一句都给出泛泛的反应。用 `get_group_msg_history`
   把最近的群聊补进短期历史，对话才接得上。
2. **上下文里全是 QQ 数字** —— 模型分不清"谁在说"。用 `get_group_member_list`
   把 `user_id` 解析成群名片/昵称（只用于显示，身份依据仍是 QQ 号）。
3. **补上下文不能拖慢或拖垮对话** —— 每个群只补一次（可配刷新间隔），有超时，
   失败只记日志并降级为空，SnowLuma 不在时对话照常进行。

对应 action 都经过 `capabilities.py` 的 read 白名单校验。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .capabilities import CapabilityDenied, CapabilityRegistry
from .media_segments import media_label
from .onebot_client import SnowLumaHttpClient
from .transport import IncomingMessage, MessageTarget, display_name_from_sender

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_HISTORY_COUNT = 30
DEFAULT_REFRESH_INTERVAL_SECONDS = 180.0
MANAGED_ACTIONS = ("get_group_msg_history", "get_group_member_list")
HISTORY_PREFIX = "history:"


class ConversationContextProvider:
    """在会话开始时补齐群聊背景与参与者名字。"""

    def __init__(
        self,
        client: SnowLumaHttpClient | None,
        *,
        registry: CapabilityRegistry | None = None,
        history_count: int = DEFAULT_HISTORY_COUNT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.client = client
        self.registry = registry if registry is not None else CapabilityRegistry()
        self.history_count = max(1, min(100, history_count))
        self.timeout = max(0.1, timeout)
        self.refresh_interval = max(0.0, refresh_interval)
        self.clock = clock
        self._last_refresh: dict[str, float] = {}
        self._members: dict[str, dict[str, str]] = {}
        self._self_id = ""
        # 只问一次"我是谁"：查到了记下来，查不到（或对面不回这个）也别每轮再问。
        self._self_id_checked = False
        self.stats = {"refreshes": 0, "failures": 0, "denied": 0, "messages_seeded": 0}

    # --- 只读视图 ---------------------------------------------------------

    def display_name(self, session_id: str, user_id: str, fallback: str = "") -> str:
        """把 QQ 号解析成人名；未知时回退到调用方给的名字。"""

        if user_id == "yunru":
            return "YunRu"
        return (self._members.get(session_id) or {}).get(user_id) or fallback

    def cached_member_count(self, session_id: str) -> int:
        return len(self._members.get(session_id) or {})

    def self_id(self) -> str:
        return self._self_id

    # --- 补全 -------------------------------------------------------------

    async def collect_seed_messages(
        self, session_id: str, group_id: str | None, current: IncomingMessage
    ) -> tuple[IncomingMessage, ...]:
        """补一次上下文并返回可直接并入短期历史的消息（不含当前消息）。

        - 每个群按 `refresh_interval` 限流，不会每轮都打接口；
        - 任何失败都返回空元组，调用方按"没有背景"继续；
        - 返回的消息 message_id 带 `history:` 前缀，天然与实时事件不冲突。
        """

        if self.client is None or not group_id:
            return ()
        block = f"group:{group_id}"
        now = self.clock()
        if now - self._last_refresh.get(block, float("-inf")) < self.refresh_interval:
            return ()
        self._last_refresh[block] = now

        await self._learn_self_id()
        members, history = await self._fetch(group_id)
        if members is not None:
            self._members[session_id] = members
        self.stats["refreshes"] += 1

        seeded: list[IncomingMessage] = []
        for raw in history:
            message = self._to_message(raw, session_id, group_id)
            if message is None or message.message_id == current.message_id:
                continue
            seeded.append(message)
        seeded = seeded[-self.history_count:]
        self.stats["messages_seeded"] += len(seeded)
        if seeded:
            logger.info(
                "context_seeded group=%s messages=%s members=%s",
                group_id,
                len(seeded),
                self.cached_member_count(session_id),
            )
        return tuple(seeded)

    async def _learn_self_id(self) -> None:
        """问一次"我自己是谁"，好把群历史里**她自己发过的**消息认出来。

        由来（2026-09-29）：`_self_id` 从建立起就没有被赋过值（只有测试里手动设），
        于是 `_to_message` 里 `is_self` 恒为 False——补进来的历史里她自己说过的话
        既不算 `speaker=yunru`，也没被标成机器人发言，只能当成"另一个号码说的"。
        实测证据：`data/runtime_state.json` 里 `group:252576429` 的历史中，
        她自己的回复带着 QQ 号 900000002 以普通成员的身份躺着。

        查一次就缓存；失败、被拒或对面不回都只是"继续不知道"，不影响补上下文。
        """

        if self._self_id_checked or self.client is None:
            return
        self._self_id_checked = True
        try:
            self._check("get_login_info")
        except CapabilityDenied:
            self.stats["denied"] += 1
            return
        try:
            data = await self.client.call("get_login_info", {})
        except Exception as exc:  # noqa: BLE001 - 拿不到身份不该影响对话
            logger.warning("context_login_info_failed category=%s", type(exc).__name__)
            return
        if isinstance(data, dict):
            self._self_id = str(data.get("user_id") or "").strip()

    async def _fetch(self, group_id: str) -> tuple[dict[str, str] | None, tuple[dict, ...]]:
        try:
            self._check("get_group_msg_history")
            self._check("get_group_member_list")
        except CapabilityDenied:
            return None, ()

        members_task = asyncio.create_task(self.client.group_members(group_id))
        history_task = asyncio.create_task(
            self.client.group_message_history(group_id, count=self.history_count)
        )
        results = await asyncio.gather(members_task, history_task, return_exceptions=True)
        raw_members, raw_history = results[0], results[1]

        members: dict[str, str] | None = None
        if isinstance(raw_members, Exception):
            logger.warning("context_members_failed category=%s", type(raw_members).__name__)
        else:
            members = _index_members(raw_members)

        history: tuple[dict, ...] = ()
        if isinstance(raw_history, Exception):
            logger.warning("context_history_failed category=%s", type(raw_history).__name__)
        else:
            history = tuple(item for item in raw_history if isinstance(item, dict))

        return members, history

    async def fetch_history(
        self, group_id: str, *, count: int | None = None, message_seq: int | None = None
    ) -> tuple[dict, ...]:
        """直接取历史原始数据，供管理或调试用途。"""

        if self.client is None:
            return ()
        self._check("get_group_msg_history")
        return await self.client.group_message_history(
            group_id, count=count or self.history_count, message_seq=message_seq
        )

    def _check(self, action: str) -> None:
        try:
            self.registry.check(action, purpose="read")
        except CapabilityDenied:
            self.stats["denied"] += 1
            raise

    # --- 转换 -------------------------------------------------------------

    def _to_message(
        self, raw: dict, session_id: str, group_id: str
    ) -> IncomingMessage | None:
        user_id = str(raw.get("user_id") or "").strip()
        if not user_id:
            return None
        text = segments_to_text(raw.get("message"))
        is_self = bool(self._self_id) and user_id == self._self_id
        # 只含占位标记（纯图片/表情/转发，或只有 @）的历史条目对补上下文没有价值，
        # 塞进去只会挤占窗口、把背景变成一串 `[图片]`。机器人自己的发言例外，
        # 它对判断"我先前说过什么"是有意义的。
        if not is_self and not _has_readable_text(text):
            return None
        try:
            target = MessageTarget(group_id=group_id)
        except ValueError:
            return None
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        # 取名字的口径与实时消息一致（QQ 昵称优先，见 `display_name_from_sender`）：
        # 历史消息的 sender 里 `card` / `nickname` 都在，只读 `nickname` 会让
        # 同一批上下文里一个人有两个名字。
        nickname = display_name_from_sender(sender)
        seq = raw.get("message_seq") or raw.get("message_id")
        return IncomingMessage(
            message_id=f"{HISTORY_PREFIX}{seq}",
            session_id=session_id,
            user_id="yunru" if is_self else user_id,
            text=text,
            target=target,
            is_bot_mentioned=False,
            sender_role=str((sender or {}).get("role") or "unknown"),
            sender_name=self.display_name(
                session_id, "yunru" if is_self else user_id, nickname
            ),
            is_bot_message=is_self,
        )


_PLACEHOLDER_PATTERN = re.compile(r"\[[^\]]{1,12}\]")


def _has_readable_text(text: str) -> bool:
    """判断一段文本除了 `[图片]` 这类占位标记之外，是否还有真正的内容。"""

    if not text:
        return False
    return bool(_PLACEHOLDER_PATTERN.sub("", text).replace("@", "").strip())


def _index_members(raw: object) -> dict[str, str]:
    names: dict[str, str] = {}
    if not isinstance(raw, (list, tuple)):
        return names
    for item in raw:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        # QQ 昵称优先、群名片兜底（口径见 `display_name_from_sender`）：
        # 这个索引是"这个人叫什么"的权威答案，用群名片会让跨群显示漂移。
        name = display_name_from_sender(item)
        if name:
            names[user_id] = name
    return names


def segments_to_text(message: object) -> str:
    """把 OneBot 消息段转成可读文本；媒体段用占位标记。

    标记的来源是 `media_segments`——实时事件与补历史**必须用同一套判据**，
    否则补进来的历史里表情包又变回 `[图片]`（这正是 2026-09-30 用户报的问题）。
    """

    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        seg_type = str(segment.get("type") or "")
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        if seg_type == "text":
            value = data.get("text")
            if isinstance(value, str):
                parts.append(value)
        elif seg_type == "at":
            qq = data.get("qq")
            parts.append(f"@{qq}" if qq else "@")
        elif seg_type == "reply":
            # 引用关系在上下文里用标记表示，不解析被引用内容。
            parts.append("[回复]")
        else:
            label = media_label(segment)
            if label:
                parts.append(label)
    return "".join(parts).strip()
