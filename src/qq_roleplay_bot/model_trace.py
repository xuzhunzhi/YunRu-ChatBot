"""模型原始输入输出追踪（默认关闭）。

为什么需要它：Stage 3 只把解析后的摘要写进日志，模型原始的 `<context>` 字段
（intent / tone / target / pending_question）和原始 `<reply>` 都被丢弃。做提示词
注入审计时，恰恰是这些被丢弃的字段最能说明"模型是否已经被说服"：

- `<context>` 能看出它对当前请求的定性有没有松动；
- 原始 `<reply>` 与最终出站正文对比，能发现模型吐了协议标签、又被
  `sanitize_reply_text` 清掉的情况——QQ 里看着干净，但那是被掩盖的信号。

安全约束（这是默认关闭的原因）：

1. 追踪记录**完整聊天内容与完整 system prompt**，因此只在显式设置
   `QQBOT_DEBUG_MODEL_IO=1` 时启用，绝不默认开启；
2. 内容只留在**内存里的有界环形缓冲**（默认 30 条），不自动落盘；
3. 需要落盘时另外显式设置 `QQBOT_DEBUG_MODEL_IO_FILE`，写入项目内的临时目录；
4. `snapshot()` 返回的是**脱敏**视图，只给长度与前缀，便于判断而不复制全文。
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 30
MAX_RAW_CHARS = 20000
PREVIEW_CHARS = 600


def trace_enabled() -> bool:
    """原始 I/O 追踪是否启用；默认关闭。"""

    return os.environ.get("QQBOT_DEBUG_MODEL_IO", "").strip().lower() in {"1", "true", "yes", "on"}


def trace_file_path() -> Path | None:
    """可选的落盘路径；未配置则只留在内存。

    相对路径会解析成基于项目根目录的绝对路径。这一点很重要：如果留着相对路径，
    进程的工作目录一旦不同，文件就会写到别处；而且文件被外部删除后，进程仍握着
    已删除的句柄继续写，数据静默丢失、谁也看不到。
    """

    value = os.environ.get("QQBOT_DEBUG_MODEL_IO_FILE", "").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        # 项目根 = 本文件的上两级（src/qq_roleplay_bot/ → 项目根）
        path = Path(__file__).resolve().parents[2] / path
    return path


@dataclass(frozen=True, slots=True)
class ModelTraceEntry:
    """一次模型调用的原始记录。"""

    sequence: int
    session_id: str
    trigger: str
    system_prompt: str
    user_content: str
    raw_output: str
    error: str = ""

    def redacted(self) -> dict[str, object]:
        """脱敏视图：只给长度与前缀，便于人工判断而不再复制全文。"""

        return {
            "sequence": self.sequence,
            "session_id": self.session_id,
            "trigger": self.trigger,
            "system_chars": len(self.system_prompt),
            "user_chars": len(self.user_content),
            "raw_chars": len(self.raw_output),
            "raw_preview": self.raw_output[:PREVIEW_CHARS],
            "error": self.error,
        }


class ModelTrace:
    """有界环形缓冲，保存最近若干次模型交互。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, *, file_path: Path | None = None) -> None:
        self.capacity = max(1, capacity)
        self.file_path = file_path
        self._entries: deque[ModelTraceEntry] = deque(maxlen=self.capacity)
        self._sequence = 0
        self.enabled = trace_enabled()
        self.write_failures = 0

    def __len__(self) -> int:
        return len(self._entries)

    def record(
        self,
        request: list[dict[str, str]],
        raw_output: str,
        *,
        session_id: str,
        trigger: str,
        error: str = "",
    ) -> ModelTraceEntry | None:
        """记录一次调用；未启用时是空操作。"""

        if not self.enabled:
            return None
        self._sequence += 1
        system_prompt, user_content = _split_request(request)
        entry = ModelTraceEntry(
            sequence=self._sequence,
            session_id=session_id,
            trigger=trigger,
            system_prompt=system_prompt[:MAX_RAW_CHARS],
            user_content=user_content[:MAX_RAW_CHARS],
            raw_output=(raw_output or "")[:MAX_RAW_CHARS],
            error=error,
        )
        self._entries.append(entry)
        if self.file_path is not None:
            self._append_to_file(entry)
        return entry

    def entries(self) -> tuple[ModelTraceEntry, ...]:
        return tuple(self._entries)

    def snapshot(self) -> dict[str, object]:
        """脱敏摘要，可安全放进运行快照或日志。"""

        return {
            "enabled": self.enabled,
            "capacity": self.capacity,
            "recorded": self._sequence,
            "kept": len(self._entries),
            "writing_to_file": self.file_path is not None,
            "write_failures": self.write_failures,
        }

    def recent_redacted(self, count: int = 5) -> list[dict[str, object]]:
        return [entry.redacted() for entry in list(self._entries)[-max(1, count):]]

    def clear(self) -> None:
        self._entries.clear()

    def _append_to_file(self, entry: ModelTraceEntry) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            with self.file_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "sequence": entry.sequence,
                    "session_id": entry.session_id,
                    "trigger": entry.trigger,
                    "system_prompt": entry.system_prompt,
                    "user_content": entry.user_content,
                    "raw_output": entry.raw_output,
                    "error": entry.error,
                }, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.write_failures += 1
            logger.warning("model_trace_write_failed category=%s", type(exc).__name__)


def _split_request(request: object) -> tuple[str, str]:
    """从模型请求里取出 system 与 user 正文。"""

    if not isinstance(request, list):
        return "", ""
    system_prompt = ""
    user_content = ""
    for item in request:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if not isinstance(content, str):
            continue
        if role == "system" and not system_prompt:
            system_prompt = content
        elif role == "user" and not user_content:
            user_content = content
    return system_prompt, user_content
