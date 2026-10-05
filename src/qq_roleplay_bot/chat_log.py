"""对话日志：**实际收发**的消息（传输层），与模型日志分开。

为什么必须有它（用户 2026-10-05 原话："日志属于底层设施，不属于stage3。我的建议是
**对话日志和模型日志分开**"）：`feature_log.py` 那一份记的是**模型看到与生成的原文**，
不是**群里实际收到的东西**。真出过事——要查"她到底发出去过什么"，`reply.jsonl` 里
只有模型生成的那一整段，而群里实际收到的是被拆成几条发出去的分段：那几条在日志里
一条都没有，于是查不出来。

所以两份日志并列，各管一半：

| 文件 | 记什么 | 谁写 |
| --- | --- | --- |
| `data/logs/chat.jsonl` | **实际收发**的每条消息（拆开的每一段各一条） | 传输层（`onebot_ws.py` 的收发边界） |
| `data/logs/raw_events.jsonl` | **没被处理**的入站事件（通知/请求/其它，原样 JSON） | 传输层（`raw_events.py`） |
| `data/logs/{judge,reply,memory,security,mail}.jsonl` | 模型请求与原始输出（**模型日志**） | 引擎（`feature_log.py`） |

**为什么落在传输层**：只有那里看得见"真正出了门的那一条"——引擎只知道模型写了什么，
插件发的图/文更是根本不经过引擎。在这一层记，收到与发出**两头才都全**。

**两条线互不影响**：文件、开关（`QQBOT_CHAT_LOG` / `QQBOT_FEATURE_LOG`）、容量
（`QQBOT_CHAT_LOG_MAX` / `QQBOT_FEATURE_LOG_CAPACITY`）、写入路径各自独立。
写对话日志不会往模型日志里写一个字，反过来也一样（`tests/test_chat_log.py` 钉住了这条）。
第三份 `raw_events.jsonl`（没被处理的入站事件，`raw_events.py`）同样是独立的一份：
写它不会碰这两份，写这两份也不会碰它（`tests/test_raw_events.py` 钉住了那条）。

**正文是聊天内容，所以只落 `data/`**（已进 `.gitignore`）——不进 git，也不该被贴到
任何仓库/工单里；排查时按会话与时间读文件。

保留期：最多留 `capacity × 2` 条，超过就重写成最近 `capacity` 条（默认 20000 条，
理由见 `dev_config.CHAT_LOG_MAX` 那一行）。轮转用的是 `feature_log.RollingJsonlFile`
——**与模型日志共用同一份实现**，不另写一套（"共用模块的改动必须同时服务两边"）。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import dev_config
from .feature_log import RollingJsonlFile, logs_directory, truncate_field

if TYPE_CHECKING:  # 只为类型标注：这个模块不需要在运行时认识传输层
    from .transport import IncomingMessage, MessageTarget

logger = logging.getLogger(__name__)

CHAT_LOG_FILE = "chat.jsonl"
DIRECTION_IN = "in"
DIRECTION_OUT = "out"
# 容量的兜底范围。默认值在 `dev_config.CHAT_LOG_MAX`；这里只拦离谱的配置
# （0 或负数会让"保留期"这条保证失效，所以要兜住）。**不允许无限增长。**
MIN_CAPACITY = 10
MAX_CAPACITY = 1_000_000


def chat_log_enabled() -> bool:
    """对话日志开不开。**默认启用**——它就是用来事后查"她到底发出去过什么"的。"""

    value = os.environ.get("QQBOT_CHAT_LOG", "").strip()
    if not value:
        return bool(dev_config.CHAT_LOG_ENABLED)
    return value.lower() not in {"0", "false", "no", "off"}


def chat_log_capacity() -> int:
    """保留多少条（按条滚动，不是按天）。范围兜在 [10, 1000000]。"""

    raw = os.environ.get("QQBOT_CHAT_LOG_MAX", "").strip()
    try:
        value = int(raw) if raw else int(dev_config.CHAT_LOG_MAX)
    except ValueError:
        value = int(dev_config.CHAT_LOG_MAX)
    return max(MIN_CAPACITY, min(MAX_CAPACITY, value))


def chat_log_directory() -> Path:
    """日志目录：默认与模型日志同一个 `data/logs/`（并列的两份），可以单独搬走。"""

    value = os.environ.get("QQBOT_CHAT_LOG_DIR", "").strip() or dev_config.CHAT_LOG_DIR
    if value:
        path = Path(value)
        if not path.is_absolute():
            # 相对路径按项目根解析：不然进程工作目录不同就写到别处去了。
            path = Path(__file__).resolve().parents[2] / path
        return path
    return logs_directory()


def session_id_of(target: "MessageTarget") -> str:
    """出站会话 id 的口径，与入站那两行（`parse_message_event`）保持同一套。"""

    if target.group_id is not None:
        return f"group:{target.group_id}"
    return f"private:{target.user_id or ''}"


def _clean(entry: dict[str, object]) -> dict[str, object]:
    """去掉空字段（"不适用"的键不写），但 `message_id` 例外。

    `message_id` 的空串是**有意义的**："回执里没拿到"。它不能因为"空"就被抹掉，
    否则读日志的人分不清"没记这个字段"和"这条没有 id"。
    """

    return {
        key: value for key, value in entry.items()
        if value not in (None, "") or key == "message_id"
    }


class ChatLog:
    """实收发消息的落盘口：一个进程一个，挂在传输层上。

    纪律与 `FeatureLog` 同一套（写盘失败只计数、绝不打断调用方、单字段有上限、
    内存里不留正文），但它是**自己的一份**：只碰 `chat.jsonl` 这一个文件。
    """

    def __init__(self, directory: Path | str | None = None, *, capacity: int | None = None,
                 enabled: bool | None = None, clock=time.time) -> None:
        self.enabled = chat_log_enabled() if enabled is None else bool(enabled)
        self.clock = clock
        self.capacity = (chat_log_capacity() if capacity is None
                         else max(MIN_CAPACITY, int(capacity)))
        self.directory = Path(directory) if directory is not None else chat_log_directory()
        self.path = self.directory / CHAT_LOG_FILE
        self.recorded = 0
        self._file = RollingJsonlFile(self.path, capacity=self.capacity,
                                      enabled=self.enabled, label="chat")

    @property
    def write_failures(self) -> int:
        return self._file.write_failures

    # --- 两个入口：收到 / 发出 ---------------------------------------------

    def record_incoming(self, message: "IncomingMessage") -> None:
        """记一条**收到的**消息。

        正文用 `IncomingMessage.text`——它是核心真正拿去理解的那份：媒体段在里面是
        **本地占位描述**（`[图片]` / `[表情]`），不是 base64、也不下载任何资源。
        """

        self._write({
            "direction": DIRECTION_IN,
            "session_id": message.session_id,
            "group_id": message.target.group_id or "",
            "user_id": message.user_id,
            "sender_name": message.sender_name,
            "sender_card": message.sender_card,
            "message_id": message.message_id,
            "reply_to": message.reply_to_message_id,
            "body": truncate_field(message.text),
            "has_media": bool(message.has_media),
        })

    def record_outgoing(self, target: "MessageTarget", *, body: str, kind: str = "text",
                        reply_to: str = "", message_id: str = "", part: int | None = None,
                        total: int | None = None, origin: str = "", outcome: str = "ok",
                        error: str = "", error_detail: str = "") -> None:
        """记一条**实际发出去的**消息。

        判据是"真的调了发送、对面回了回执"：成功记 `outcome="ok"` 与回执里的
        `message_id`；失败**照实记** `outcome="failed"` + 错误类别，绝不假装成功。

        `part` / `total` 是"第几段 / 共几段"：**只有调用方知道**（传输层看到的是一条
        独立的消息），所以由调用方传进来；拿不到就不写这两个键——**不猜**。
        """

        entry: dict[str, object] = {
            "direction": DIRECTION_OUT,
            "session_id": session_id_of(target),
            "group_id": target.group_id or "",
            "user_id": target.user_id or "",
            "kind": kind,
            "body": truncate_field(body),
            # 回执里没有就留空串（**空串 = 没拿到**，也不拿 echo 冒充 message_id）。
            "message_id": str(message_id or ""),
            "part": part,
            "total": total,
            "reply_to": reply_to,
            "outcome": outcome,
            "error": error,
            "error_detail": truncate_field(error_detail),
            "origin": origin,
        }
        if outcome == "ok" and not message_id:
            entry["message_id_missing"] = True
        self._write(entry)

    # --- 运维 -------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """运行状态用的摘要：计数、大小、路径，**不含正文**。"""

        return {
            "enabled": self.enabled,
            "capacity": self.capacity,
            "recorded": self.recorded,
            "lines": self._file.lines,
            "bytes": self._file.bytes,
            "file": str(self.path),
            "write_failures": self._file.write_failures,
        }

    def clear(self) -> None:
        """清空文件与计数（运维用；不删文件本身）。"""

        self.recorded = 0
        self._file.clear()

    # --- 内部 -------------------------------------------------------------

    def _write(self, entry: dict[str, object]) -> None:
        """落盘入口：未启用时是空操作；`at` 一律在最前面。"""

        if not self.enabled:
            return
        self.recorded += 1
        stamped: dict[str, object] = {"at": round(self.clock(), 3)}
        stamped.update(_clean(entry))
        self._file.append(stamped)
