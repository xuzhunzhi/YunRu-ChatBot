"""按功能分开的输入输出日志。

为什么单独做一个：模型 I/O 追踪原来是"一份总账"（`model_trace.py`），只留最近
30 条、默认关闭，而且**只记 system + 第一条 user 消息**——回复请求有三段
（system / 稳定段 / 易变段），易变段从来没被记下来过，查问题时正好缺的就是它。

现在每个功能各写一份，各留最近 1000 次：

| 功能 | 文件 | 记什么 |
| --- | --- | --- |
| `judge` | `data/logs/judge.jsonl` | 判定 agent 的完整请求与原始输出 |
| `reply` | `data/logs/reply.jsonl` | 回复 agent 的完整请求与原始输出 |
| `memory` | `data/logs/memory.jsonl` | 记忆维护 agent 的完整请求与原始输出 |
| `security` | `data/logs/security.jsonl` | 规则拦截/放行的判定（不是模型调用） |
| `mail` | `data/logs/mail.jsonl` | 每日汇报那次写信的完整请求与原始输出 |

设计取舍：

1. **正文只落盘，不进内存。** 1000 条 × 4 个功能 × 每条几十 KB，全放内存就是
   上百 MB——运行状态里还要报内存占用，不能自己先把内存吃满。内存里只留计数与
   最后几条预览（给 `/super status` 看）。
2. **文件最多留 2×容量行，超过就重写成最近 1000 条。** 每写一条都重写 1000 行
   太贵；留一倍余量之后，摊到每条记录的成本可以忽略。
3. **内容含完整 system prompt 与聊天正文**，所以目录在 `data/`（已被 .gitignore
   忽略），并且可以用 `QQBOT_FEATURE_LOG=0` 整体关掉。
4. 单字段上限 20000 字符：一条请求撑不到这个数，但真撑到了也不该让日志无限长。
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

FEATURES = ("judge", "reply", "memory", "security", "mail")
# 每个功能保留多少次。"最近 1000 次"是用户 2026-09-27 定的口径。
DEFAULT_CAPACITY = 1000
# 单字段上限。
MAX_FIELD_CHARS = 20000
# 内存里留几条预览（只为运行状态好看，正文在文件里）。
PREVIEW_ENTRIES = 20
PREVIEW_CHARS = 400
# 文件行数超过 capacity × 这个倍数才重写。
TRIM_SLACK = 2


def logs_enabled() -> bool:
    """按功能日志是否启用。**默认启用**——它就是用来事后查问题的。"""

    return os.environ.get("QQBOT_FEATURE_LOG", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def logs_directory() -> Path:
    """日志目录。相对路径按项目根解析，避免进程工作目录不同就写到别处去。"""

    value = os.environ.get("QQBOT_FEATURE_LOG_DIR", "").strip()
    if value:
        path = Path(value)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        return path
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "logs"


def log_capacity() -> int:
    try:
        value = int(os.environ.get("QQBOT_FEATURE_LOG_CAPACITY", str(DEFAULT_CAPACITY)))
    except ValueError:
        value = DEFAULT_CAPACITY
    return max(10, min(100000, value))


def _trim(text: object) -> str:
    if not isinstance(text, str):
        return ""
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + f"\n…（已截断，原长 {len(text)}）"


class FeatureLog:
    """一个功能的日志：文件是正文所在，内存只留计数与预览。"""

    def __init__(
        self,
        feature: str,
        directory: Path | str,
        *,
        capacity: int = DEFAULT_CAPACITY,
        enabled: bool = True,
        clock=time.time,
    ) -> None:
        self.feature = feature
        self.directory = Path(directory)
        self.capacity = max(10, int(capacity))
        self.enabled = bool(enabled)
        self.clock = clock
        self.path = self.directory / f"{feature}.jsonl"
        self.recorded = 0
        self.write_failures = 0
        self._lines_in_file = 0
        self._bytes = 0
        self._previews: deque[dict[str, object]] = deque(maxlen=PREVIEW_ENTRIES)
        if self.enabled:
            self._measure_existing()

    def record(self, *, input: str = "", output: str = "", system: str = "",
               error: str = "", **meta: object) -> None:
        """记一次。未启用时是空操作；写盘失败只计数，绝不打断调用方。"""

        if not self.enabled:
            return
        self.recorded += 1
        entry = {
            "seq": self.recorded,
            "at": round(self.clock(), 3),
            "feature": self.feature,
            **{key: value for key, value in meta.items() if value not in (None, "")},
            "system": _trim(system),
            "input": _trim(input),
            "output": _trim(output),
        }
        if error:
            entry["error"] = _trim(error)
        self._previews.append({
            "seq": entry["seq"],
            "at": entry["at"],
            **{key: value for key, value in meta.items() if value not in (None, "")},
            "system_chars": len(entry["system"]),
            "input_chars": len(entry["input"]),
            "output_chars": len(entry["output"]),
            "error": error,
            "input_preview": entry["input"][:PREVIEW_CHARS],
            "output_preview": entry["output"][:PREVIEW_CHARS],
        })
        self._write(entry)

    def snapshot(self) -> dict[str, object]:
        """运行快照里用的摘要：计数、大小、路径，不含正文。"""

        return {
            "enabled": self.enabled,
            "capacity": self.capacity,
            "recorded": self.recorded,
            "lines": self._lines_in_file,
            "bytes": self._bytes,
            "file": str(self.path),
            "write_failures": self.write_failures,
        }

    def previews(self, count: int = 5) -> list[dict[str, object]]:
        return list(self._previews)[-max(1, count):]

    def clear(self) -> None:
        """清空文件与计数（运维用；不删文件本身）。"""

        self._previews.clear()
        self.recorded = 0
        self._lines_in_file = 0
        self._bytes = 0
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError:
            self.write_failures += 1

    # --- 内部 -------------------------------------------------------------

    def _measure_existing(self) -> None:
        """启动时数一次现有行数与字节数，供运行状态显示。"""

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                self._lines_in_file = sum(1 for _ in handle)
            self._bytes = self.path.stat().st_size
        except FileNotFoundError:
            return
        except OSError:
            self.write_failures += 1

    def _write(self, entry: dict[str, object]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._lines_in_file += 1
            self._bytes = self.path.stat().st_size
        except OSError as exc:
            self.write_failures += 1
            logger.warning("feature_log_write_failed feature=%s category=%s",
                           self.feature, type(exc).__name__)
            return
        if self._lines_in_file > self.capacity * TRIM_SLACK:
            self._trim_file()

    def _trim_file(self) -> None:
        """把文件重写成最近 capacity 条。顺序读一遍，内存里只留尾部若干行。"""

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                tail = deque(handle, maxlen=self.capacity)
            temp = self.path.with_suffix(".jsonl.trim")
            with temp.open("w", encoding="utf-8") as handle:
                handle.writelines(tail)
            os.replace(temp, self.path)
            self._lines_in_file = len(tail)
            self._bytes = self.path.stat().st_size
            logger.info("feature_log_trimmed feature=%s kept=%s", self.feature, len(tail))
        except OSError as exc:
            self.write_failures += 1
            logger.warning("feature_log_trim_failed feature=%s category=%s",
                           self.feature, type(exc).__name__)


class FeatureLogs:
    """四个功能各一份日志，共用开关与容量。"""

    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        capacity: int | None = None,
        enabled: bool | None = None,
        clock=time.time,
    ) -> None:
        self.enabled = logs_enabled() if enabled is None else bool(enabled)
        self.directory = Path(directory) if directory is not None else logs_directory()
        size = log_capacity() if capacity is None else int(capacity)
        self.logs: dict[str, FeatureLog] = {
            feature: FeatureLog(feature, self.directory, capacity=size,
                                enabled=self.enabled, clock=clock)
            for feature in FEATURES
        }

    def __getitem__(self, feature: str) -> FeatureLog:
        return self.logs[feature]

    def record(self, feature: str, **kwargs: object) -> None:
        """按功能名记一条；功能名不认识就忽略（不让日志写错地方）。"""

        target = self.logs.get(feature)
        if target is None:
            logger.warning("feature_log_unknown feature=%s", feature)
            return
        target.record(**kwargs)  # type: ignore[arg-type]

    def snapshot(self) -> dict[str, object]:
        return {feature: log.snapshot() for feature, log in self.logs.items()}

    def total_bytes(self) -> int:
        return sum(int(log._bytes) for log in self.logs.values())

    def total_entries(self) -> int:
        return sum(log.recorded for log in self.logs.values())


def request_parts(request: object) -> tuple[str, str]:
    """把一条模型请求拆成 (system, 其余全部)。

    **这里必须把三段都收进来**：回复请求是 system / 稳定段 / 易变段，
    旧实现只取第一条 user 消息，于是"你与这个人"这类后来加进易变段的东西
    在日志里永远看不到——查了半天才发现日志里本来就没有。
    """

    if not isinstance(request, list):
        return "", ""
    system = ""
    parts: list[str] = []
    for item in request:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        role = item.get("role")
        if role == "system":
            system = f"{system}\n{content}" if system else content
        else:
            parts.append(f"===== {role} =====\n{content}")
    return system, "\n\n".join(parts)
