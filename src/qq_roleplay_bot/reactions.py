"""表情回应（`group_msg_emoji_like`）→ 干净一行，记进**对话日志** `data/logs/chat.jsonl`。

由来（2026-10-06）：用户想做"用群里的表情回应定位她说过的好句子"（她自己的话被点了表情
= 优质样本）。第一步只做**传输层**：上一批的 `raw_events.jsonl` 已经证明 **NapCat 确实推**
这种帧（`run/data/logs/raw_events.jsonl` 里有真实帧，字段清单见下），这一份把它**解析成一行**
并列写进对话日志——与 in/out 记录并列，用 `"kind": "reaction"` 区分。**不做判据**：
不判金句、不做 few-shot、不进核心、不碰对话/记忆/人格（那是后面的步骤）。

**真实帧的字段**（2026-10-06 从 `raw_events.jsonl` 读出来的原文，不是照谁给的样例猜的）::

    {"post_type": "notice", "notice_type": "group_msg_emoji_like", "sub_type": "add",
     "time": 1791223346, "self_id": 3655414729, "group_id": 1084401296,
     "user_id": 242003347, "operator_id": 242003347,
     "message_id": 1852979907, "message_seq": 372985,
     "likes": [{"emoji_id": "424", "count": 2}]}

实测到的两点（有反例就照反例改，不要照这段注释信）：

- `message_id` **实测出现过负数**（`-1269837352`）与正整数（`1852979907`）两种；
  作为**字符串**处理（`str(...)`），不假定符号、不假定量级；
- `likes` 里的 `emoji_id` 实测是**字符串**（`"424"`、`"476"`），有 `count`（实测 1 或 2）；
  4 条实测帧里 `likes` 恰好都只有一个元素——**没有**"一帧多个表情"的实测样本，
  所以按 `likes` 的每个元素各写一行（真出现了就这么记，不丢信息）。

**落盘判据与字段**（一行 = 一个 `likes` 元素；`at` 由 `ChatLog` 加）::

    {"at": …, "direction": "in", "kind": "reaction", "session_id": "group:…",
     "group_id": "…", "message_id": "…", "user_id": "…", "operator_id": "…",
     "emoji_id": "424", "count": 2, "sub_type": "add"}

- `user_id` / `operator_id`：**两个都记、都照帧里的原值**。实测 4 条帧里两者恒等
  （谁点的、操作者就是谁），但**没有**"两者不同"的样本，所以不合并、不推断它们的语义；
- `message_seq` / `self_id` / `time` 不记：`message_seq` 没有消费者，`self_id` 是 bot 自己、
  与"谁给哪条消息点了什么"无关，`time` 与本行的 `at`（接收时刻）区分不开也不冲突。
  要原始形状去 `raw_events.jsonl`（那一份**照旧原样**留着，这里不碰它）。

**三条纪律**（与 `raw_events.py` 同一套）：

1. **不花一分钱**。纯本地解析与落盘，**绝不**发模型调用、不进核心队列
   （`chat.jsonl` 有行 ≠ 她收到了这个事件）。本任务也**不用** `get_msg_emoji_likes`
   那个拉取接口——推送这一条就够了。
2. **不打断收发**。`ChatLog` 自己就"写盘失败只计数、绝不抛出"；这里再兜一层异常
   （见 `RawEventLog` 与 `onebot_ws._note_*` 的做法），日志坏了不能让接收循环出问题。
3. **照能记的记，剩下的丢并计数**。`message_id`（被反应的那条消息）或 `emoji_id` 缺失时
   **不写半条记录**：那种行落下去只会让人以为"有个查不到目标的表情回应"。
   丢掉的条数记在 `dropped` 里，**不编字段**。

**轮转与开关都复用对话日志**：这一份**不自带文件、不带容量、不带开关**——它拿
`onebot_ws` 上那个**同一个 `ChatLog`** 落盘，于是 `RollingJsonlFile` 的按条滚动与
`chat_log_capacity()` 的口径自动生效（**不另写一套**），`QQBOT_CHAT_LOG=0` 一起关掉
（`ChatLog` 的既有默认；不需要为它单独开一个开关，它本来就是"对话日志里的一类记录"）。
这份日志只记 `data/`（已进 `.gitignore`）：不进 git、不该贴到任何仓库或工单里。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型标注：运行时只要那个对象有 record_reaction + recorded
    from .chat_log import ChatLog

logger = logging.getLogger(__name__)

# 这一种 notice 的名字。**判据只认它**——别的 notice（poke / 撤回 / 名片变更…）不归这里，
# 它们照旧只走 `raw_events.jsonl`。
EMOJI_LIKE_NOTICE = "group_msg_emoji_like"
# 记录种类的值：与 in/out 记录并列，读日志的人一眼分得出这是表情回应、不是消息。
REACTION_KIND = "reaction"

# 一行记录里必须有的两个字段：没有"被反应的那条消息"或"哪个表情"，
# 这一行就查不出任何东西（见模块头部第 3 条纪律）——缺了就不写，只计数。
REQUIRED_KEYS = ("message_id", "emoji_id")


def is_reaction_notice(payload: object) -> bool:
    """这一帧是不是表情回应通知。判据就两个字段，**不按 post_type 之外的形状猜**。"""

    if not isinstance(payload, dict):
        return False
    return (payload.get("post_type") == "notice"
            and payload.get("notice_type") == EMOJI_LIKE_NOTICE)


def _text(value: object) -> str:
    """取一个**标量**字段并转成字符串；取不到（None / 容器 / 空串）就是空串。

    为什么容器也算取不到：`str({"a": 1})` 会写出 `"{'a': 1}"` 这种既不是原文、
    也不好解析的东西——那种"半个字段"比留空更坏（照 `receipt_message_id` 那条纪律）。
    """

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""  # bool 是 int 的子类：True 当 id 只会写出误导性的 "True"。
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _count(value: object) -> int | None:
    """`count` 取正整数；取不到就返回 None（**不写这个键，也不编一个 1**）。

    实测它是整数（`2` / `1`）。字符串数字（`"2"`）也收——对面换个形状不该让整行丢掉；
    别的类型一律当"没这个字段"。
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def parse_reaction(payload: object) -> tuple[dict[str, object], ...]:
    """把一帧表情回应解析成 0..N 个**干净记录**（一行一个 `likes` 元素）。

    返回空元组有三种情况，调用方一视同仁（丢并计数）：不是这种 notice、
    关键字段（`message_id` / `emoji_id`）缺失、`likes` 不是非空列表。
    记录里**没有 `at`**——那是 `ChatLog` 落盘时统一加的。
    """

    if not is_reaction_notice(payload):
        return ()
    assert isinstance(payload, dict)  # `is_reaction_notice` 已经保证了

    message_id = _text(payload.get("message_id"))
    if not message_id:
        return ()

    likes = payload.get("likes")
    if not isinstance(likes, list) or not likes:
        return ()

    group_id = _text(payload.get("group_id"))
    user_id = _text(payload.get("user_id"))
    operator_id = _text(payload.get("operator_id"))
    sub_type = _text(payload.get("sub_type"))

    records: list[dict[str, object]] = []
    for like in likes:
        if not isinstance(like, dict):
            continue
        emoji_id = _text(like.get("emoji_id"))
        if not emoji_id:
            # 缺表情 id 的这条**不写**（照模块头部第 3 条）：写出来也查不出是哪个表情。
            continue
        record: dict[str, object] = {
            "direction": "in",
            "kind": REACTION_KIND,
            "group_id": group_id,
            "message_id": message_id,
            "user_id": user_id,
            "operator_id": operator_id,
            "emoji_id": emoji_id,
            "count": _count(like.get("count")),
            "sub_type": sub_type,
        }
        session_id = f"group:{group_id}" if group_id else ""
        if session_id:
            record["session_id"] = session_id
        records.append(record)
    return tuple(records)


class ReactionLog:
    """表情回应的解析与落盘口：挂在传输层上，**借对话日志那个 `ChatLog` 落盘**。

    它自己**不建文件、不管容量、不看开关**（见模块头部最后一段）：`ChatLog` 关掉时
    这里也是空操作（`recorded` 不动），`ChatLog` 的轮转照旧管着 `chat.jsonl`。
    这样一个进程只多了一个对象，不多一份需要对齐的配置。
    """

    def __init__(self, chat_log: "ChatLog", clock=None) -> None:
        self.chat_log = chat_log
        self.clock = clock
        # 计数只给自己看（`snapshot()`）：`recorded` 是"写出去的条数"，
        # `dropped` 是"这一帧没能变成一条记录"的次数——**不写半条**，丢就照实计数。
        self.recorded = 0
        self.dropped = 0

    def record(self, payload: object) -> bool:
        """解析并落盘；返回这次有没有写出记录。**绝不抛给调用方**。

        非这种 notice 直接返回 False 且**不计 dropped**——"不是我的帧"不是丢弃，
        它们照旧走 `raw_events.jsonl`（调用方负责）。
        """

        if not is_reaction_notice(payload):
            return False
        try:
            records = parse_reaction(payload)
            if not records:
                self.dropped += 1
                return False
            for record in records:
                self.chat_log.record_reaction(record)
                self.recorded += 1
        except Exception:  # noqa: BLE001 - 日志坏了不能让接收循环出问题
            logger.warning("reaction_log_failed", exc_info=True)
            return False
        return True

    @property
    def write_failures(self) -> int:
        """落盘失败次数（直接取对话日志的计数：这一份没有自己的文件）。"""

        return self.chat_log.write_failures

    def snapshot(self) -> dict[str, object]:
        """运行状态用的摘要：计数与落点，**不含正文**（与另外两份日志同一口径）。"""

        return {
            "kind": REACTION_KIND,
            "recorded": self.recorded,
            "dropped": self.dropped,
            "file": str(self.chat_log.path),
            "enabled": self.chat_log.enabled,
        }
