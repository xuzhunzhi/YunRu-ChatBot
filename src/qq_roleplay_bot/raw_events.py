"""原始入站事件日志：**核心没认出来的那些事件**，原样落一行。

为什么要有它（2026-10-06）：用户想做"用群里的表情回应定位她说过的好句子"（她自己的话
被点了表情 = 优质样本），但**没人知道 NapCat 到底推不推这种事件**。传输层目前只认
`post_type=message`：`parse_message_event` 对别的一律返回 `None`，`onebot_ws._handle_payload`
于是**直接把那一帧丢掉**——丢在哪、长什么样，事后一条痕迹都没有。实测（2026-10-06）：
`data/logs/chat.jsonl` 里 `reaction|emoji_like` 0 行，而"没记"与"没发生"从日志里分不出来。

所以这一份只干一件事：**把没被处理的那条事件原样记下来**，让人能拿真实数据回答
"NapCat 报不报"。它**不是功能**：不解析、不判据、不喂模型、不改任何既有行为。

与另外两份日志并列、互不影响：

| 文件 | 记什么 | 谁写 |
| --- | --- | --- |
| `data/logs/chat.jsonl` | **实际收发**的消息（拆开的每一段各一条） | 传输层（`onebot_ws.py`） |
| `data/logs/raw_events.jsonl` | **没被处理**的入站事件（原样 JSON） | 传输层（`raw_events.py`） |
| `data/logs/{judge,reply,memory,security,mail}.jsonl` | 模型请求与原始输出（模型日志） | 引擎（`feature_log.py`） |

三条纪律：

1. **原样**。落盘的是**整个 payload 对象**（逐字段、逐值、照原顺序），**不挑字段、不截断、
   不归一化**——要看清真实形状，就不能先按想象裁剪。外面只加一个 `at`（接收时刻），
   payload 原封不动放在 `payload` 键下：`{"at": 1759734.5, "payload": {...原始...}}`。
2. **不花一分钱**。这里**绝不发模型调用**，也不进核心（`raw_events.jsonl` 有行 ≠ 她收到了
   事件）。它只落盘，不产生任何行为。
3. **不打断收发**。写盘失败只累加 `write_failures` 并告警（照 `ChatLog` 那条做法），
   绝不让"日志写不进去"变成"消息收不到 / 发不出"。

轮转**复用 `feature_log.RollingJsonlFile`**（不另写第二套）：按条滚动、留一倍余量、
超过 `capacity × 2` 就重写成最近 `capacity` 条。容量见 `dev_config.RAW_EVENTS_MAX`
（默认 5000），开关 `QQBOT_RAW_EVENTS=0`（**默认开启**，一键关掉）。

**心跳不算**：`meta_event/heartbeat` 每几十秒一条、纯噪音，排除（`is_heartbeat`）；
其它 `meta_event`（例如 `lifecycle` 上下线）照记——它同样是"现在被丢掉的事件"。
带 `echo` 的**迟到回执**（配对已经超时，`_pending` 里没有它）也会落一条：它不是事件，
但"对面回了一个我们不认识的回执"正是以前**一声不响丢掉**的那类东西，留个痕迹比丢掉强；
正常情况下极少（匹配上的回执在 `_handle_payload` 开头就返回了，走不到这里）。

正文可能含聊天内容，所以只落 `data/`（已进 `.gitignore`）：不进 git、不该被贴到任何
仓库或工单里；排查时按时间读文件。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from . import dev_config
from .feature_log import RollingJsonlFile, logs_directory

logger = logging.getLogger(__name__)

RAW_EVENTS_FILE = "raw_events.jsonl"
# 容量的兜底范围（与 `chat_log` 同一套口径）：0 或负数会让"不会无限增长"这条保证失效。
MIN_CAPACITY = 10
MAX_CAPACITY = 1_000_000


def raw_events_enabled() -> bool:
    """原始事件日志开不开。**默认启用**——它就是用来事后查"到底推了什么"的。"""

    value = os.environ.get("QQBOT_RAW_EVENTS", "").strip()
    if not value:
        return bool(dev_config.RAW_EVENTS_ENABLED)
    return value.lower() not in {"0", "false", "no", "off"}


def raw_events_capacity() -> int:
    """保留多少条（按条滚动，不是按天）。范围兜在 [10, 1000000]。"""

    raw = os.environ.get("QQBOT_RAW_EVENTS_MAX", "").strip()
    try:
        value = int(raw) if raw else int(dev_config.RAW_EVENTS_MAX)
    except ValueError:
        value = int(dev_config.RAW_EVENTS_MAX)
    return max(MIN_CAPACITY, min(MAX_CAPACITY, value))


def raw_events_directory() -> Path:
    """日志目录：默认与模型日志、对话日志同一个 `data/logs/`（并列的三份），可以单独搬走。"""

    value = os.environ.get("QQBOT_RAW_EVENTS_DIR", "").strip() or dev_config.RAW_EVENTS_DIR
    if value:
        path = Path(value)
        if not path.is_absolute():
            # 相对路径按项目根解析：不然进程工作目录不同就写到别处去了。
            path = Path(__file__).resolve().parents[2] / path
        return path
    return logs_directory()


def is_heartbeat(event: object) -> bool:
    """是不是心跳。**只排心跳**，别的 `meta_event`（例如 lifecycle）不排。"""

    if not isinstance(event, dict):
        return False
    return (event.get("post_type") == "meta_event"
            and event.get("meta_event_type") == "heartbeat")


def should_record(event: object) -> bool:
    """这条入站事件该不该落进 `raw_events.jsonl`。

    调用点只有一处（`onebot_ws._handle_payload` 里"没被当成消息收下"的那个分支），
    所以判据就一句：**心跳不记、其余都记**。不按 `post_type` / `notice_type` 挑——
    "哪种事件值得记"正是现在还不知道的事，先照单全收才看得清。解析失败的消息事件
    也落在这里（那同样是"被丢掉的事件"，而且是最难查的一种）；**解析成功的消息不进这里**
    （它走 `chat.jsonl`，那边记的是核心真正拿去理解的那一份）。
    """

    if not isinstance(event, dict):
        return False
    return not is_heartbeat(event)


class RawEventLog:
    """没被处理的入站事件的落盘口：一个进程一个，挂在传输层上。

    纪律与 `ChatLog` / `FeatureLog` 同一套（写盘失败只计数、绝不打断调用方、正文不进内存），
    但它是**自己的一份**：只碰 `raw_events.jsonl` 这一个文件，只加一个 `at`。
    """

    def __init__(self, directory: Path | str | None = None, *, capacity: int | None = None,
                 enabled: bool | None = None, clock=time.time) -> None:
        self.enabled = raw_events_enabled() if enabled is None else bool(enabled)
        self.clock = clock
        self.capacity = (raw_events_capacity() if capacity is None
                         else max(MIN_CAPACITY, int(capacity)))
        self.directory = Path(directory) if directory is not None else raw_events_directory()
        self.path = self.directory / RAW_EVENTS_FILE
        self.recorded = 0
        self._file = RollingJsonlFile(self.path, capacity=self.capacity,
                                      enabled=self.enabled, label="raw_events")

    @property
    def write_failures(self) -> int:
        return self._file.write_failures

    # --- 唯一入口 ---------------------------------------------------------

    def record(self, payload: object) -> bool:
        """把一条**没被处理**的入站事件原样落盘；返回有没有真的走到落盘那一步。

        只加 `at`，payload 一个字段不改、不裁（见模块头部第 1 条）。未启用、或传进来的
        不是对象时是空操作（返回 False）——调用方拿它写日志，拿不到就什么都不做。
        """

        if not self.enabled or not isinstance(payload, dict):
            return False
        self.recorded += 1
        self._file.append({"at": round(self.clock(), 3), "payload": payload})
        return True

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
