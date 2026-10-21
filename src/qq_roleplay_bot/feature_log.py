"""按功能分开的输入输出日志（**模型日志**）。

为什么单独做一个：模型 I/O 追踪原来是"一份总账"（`model_trace.py`），只留最近
30 条、默认关闭，而且**只记 system + 第一条 user 消息**——回复请求有三段
（system / 稳定段 / 易变段），易变段从来没被记下来过，查问题时正好缺的就是它。

**这一份记的是"模型看到与生成了什么"，不是"群里实际收到什么"**（用户 2026-10-05
定的边界："对话日志和模型日志分开"）。实际收发那一半在 `chat_log.py`（`chat.jsonl`），
落在传输层；两边**文件、开关、容量、写入路径都各自独立**，互不覆盖。

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
2. **活文件最多留 2×容量行，超过就重写成最近 1000 条。** 每写一条都重写 1000 行
   太贵；留一倍余量之后，摊到每条记录的成本可以忽略。**这段轮转逻辑现在只有一份**
   （`RollingJsonlFile`）：对话日志（`chat_log.py`）要的是同一个做法，两边共用，
   不会出现"模型日志的轮转修了、对话日志还是老样子"。
   **被裁掉的旧行不再丢弃**：追加到同目录的 `archive/<label>-YYYYMM.jsonl`，
   于是活文件仍然只有 capacity..capacity*2 行，但半个月以上的历史还能在归档里查到
   （2026-10-21 用户要求"所有落盘日志都能查到半个月以上"）。归档保留天数由
   `QQBOT_LOG_ARCHIVE_DAYS` 控制（默认 30，0 = 不归档、完全回到旧行为）。
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
# 归档保留多少天。默认 30：用户 2026-10-21 要求"所有落盘日志都能查到半个月以上"。
# 0 = 不归档（回退用，行为与改动前完全一致），坏值回落默认值。
DEFAULT_ARCHIVE_DAYS = 30
MAX_ARCHIVE_DAYS = 3650


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


def log_archive_days() -> int:
    """归档保留天数。**0 = 不归档**（回退到"裁掉就丢"的旧行为）；坏值回落默认值。

    每次都读环境变量（不在 `RollingJsonlFile.__init__` 里缓存）：这样运维改配置
    不用重启进程就能生效，测试也能在构造之后再切换。范围夹在 0..3650。
    """

    raw = os.environ.get("QQBOT_LOG_ARCHIVE_DAYS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_ARCHIVE_DAYS
    except ValueError:
        value = DEFAULT_ARCHIVE_DAYS
    return max(0, min(MAX_ARCHIVE_DAYS, value))


def truncate_field(text: object) -> str:
    """单字段上限：**模型日志与对话日志共用这一份口径**（见 `MAX_FIELD_CHARS`）。"""

    if not isinstance(text, str):
        return ""
    if len(text) <= MAX_FIELD_CHARS:
        return text
    return text[:MAX_FIELD_CHARS] + f"\n…（已截断，原长 {len(text)}）"


class RollingJsonlFile:
    """按条滚动的 JSONL 落盘口：**模型日志与对话日志共用这一份实现**。

    为什么要把它从 `FeatureLog` 里提出来：轮转只能有一份算法。原来"写一条 → 行数
    超过 `capacity × TRIM_SLACK` 就把文件重写成最近 `capacity` 条"是长在 `FeatureLog`
    身上的，而对话日志（`chat_log.py`）要的是**同一个做法**（按条滚动、留一倍余量、
    写盘失败只计数）。提出来之后改一处两边一起生效。

    共用纪律：

    - **绝不让日志打断调用方**：写盘/轮转/归档失败只累加 `write_failures` 并告警，不抛；
    - 内存里**不留正文**：这里只数行数与字节数（正文在文件里），
      所以文件再大也不吃内存；
    - 被裁掉的旧行落进同目录 `archive/<label>-YYYYMM.jsonl`（`QQBOT_LOG_ARCHIVE_DAYS`
      默认 30 天，0 = 不归档），活文件的容量口径不变。

    `capacity` 下限只有 1（`FeatureLog` 自己在传进来之前已经按 10 兜住了）；
    滚动判据是"行数 > capacity × slack"，所以文件里通常有 capacity..capacity×slack 条。
    """

    def __init__(self, path: Path | str, *, capacity: int, slack: int = TRIM_SLACK,
                 enabled: bool = True, label: str = "") -> None:
        self.path = Path(path)
        self.capacity = max(1, int(capacity))
        self.slack = max(1, int(slack))
        self.enabled = bool(enabled)
        # 告警行里用它指认是谁：`judge` / `chat`。只用于日志，不参与任何判据。
        self.label = label or self.path.stem
        self.lines = 0
        self.bytes = 0
        self.write_failures = 0
        if self.enabled:
            self.measure()

    def measure(self) -> None:
        """启动时数一次现有行数与字节数，供运行状态显示。"""

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                self.lines = sum(1 for _ in handle)
            self.bytes = self.path.stat().st_size
        except FileNotFoundError:
            return
        except OSError:
            self.write_failures += 1

    def append(self, entry: dict[str, object]) -> None:
        """追加一条；超过上限就把文件重写成最近 `capacity` 条。"""

        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self.lines += 1
            self.bytes = self.path.stat().st_size
        except OSError as exc:
            self.write_failures += 1
            logger.warning("rolling_log_write_failed label=%s category=%s",
                           self.label, type(exc).__name__)
            return
        if self.lines > self.capacity * self.slack:
            self.trim()

    def trim(self) -> None:
        """把文件重写成最近 capacity 条；被裁掉的旧行先追加到归档。

        **顺序必须是先写归档、再替换活文件。** 如果反过来（先 `os.replace` 再归档），
        进程在两步之间崩掉时，那批已经从活文件里消失、又还没进归档的行就**永久没了**；
        按现在这个顺序崩，最坏是归档里多出下一轮会再写一遍的重复行——重复可以接受，
        丢数据不行。这也是"宁可少归档，不许日志弄炸主流程"的落点：归档写不进去时只
        计数告警，活文件照样裁到 capacity。

        内存仍然只留尾部 capacity 行：顺序读一遍，deque 满了就说明最左那行马上要被
        挤掉，当场写进归档。**不把整个文件读进内存。**
        """

        days = log_archive_days()
        archive_path = self._archive_path() if days > 0 else None
        archive = None
        archive_broken = False

        def archive_line(line: str) -> None:
            nonlocal archive, archive_broken
            if archive_path is None or archive_broken:
                return
            if archive is None:
                try:
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    archive = archive_path.open("a", encoding="utf-8")
                except OSError as exc:
                    # 一次失败就不再重试：archive 可能是同名普通文件，每行都试一遍
                    # 只会把同一条告警刷满、把 write_failures 刷爆。
                    archive_broken = True
                    self._note_failure("rolling_log_archive_failed", exc)
                    return
            try:
                archive.write(line)
            except OSError as exc:
                archive_broken = True
                self._note_failure("rolling_log_archive_failed", exc)
                self._close(archive)
                archive = None

        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                tail = deque(maxlen=self.capacity)
                for line in handle:
                    if len(tail) == self.capacity:
                        archive_line(tail[0])
                    tail.append(line)
            # 归档先落盘并关闭，之后才动活文件（见上面的顺序说明）。
            # close 的 flush 失败同样是归档写失败，要计数，不能悄悄吞掉。
            self._close(archive, count=True)
            archive = None
            if archive_path is not None:
                self._prune_archive(archive_path, days)
            temp = self.path.with_suffix(".jsonl.trim")
            with temp.open("w", encoding="utf-8") as handle:
                handle.writelines(tail)
            os.replace(temp, self.path)
            self.lines = len(tail)
            self.bytes = self.path.stat().st_size
            logger.info("rolling_log_trimmed label=%s kept=%s", self.label, len(tail))
        except OSError as exc:
            self._note_failure("rolling_log_trim_failed", exc)
        finally:
            if archive is not None:
                self._close(archive)

    def _archive_path(self) -> Path:
        """当月归档文件：`archive/<label>-YYYYMM.jsonl`（YYYYMM 取归档那一刻的本地年月）。"""

        stamp = time.strftime("%Y%m", time.localtime())
        return self.path.parent / "archive" / f"{self.label}-{stamp}.jsonl"

    def _prune_archive(self, keep: Path, days: int) -> None:
        """删归档目录里 mtime 超过 days 天的 `*.jsonl`；**刚写的那个不删**。

        只 glob 这个目录自己的 `*.jsonl`（不递归、不碰活日志）。清理失败同样不打断
        调用方——它是顺手做的事，不是 trim 成功的前提。
        """

        directory = keep.parent
        cutoff = time.time() - days * 86400
        try:
            candidates = list(directory.glob("*.jsonl"))
        except OSError as exc:
            # `archive` 被同名普通文件占住时这里会失败；不能因此让 trim 失败。
            self._note_failure("rolling_log_archive_prune_failed", exc)
            return
        keep_key = os.path.normcase(os.path.abspath(str(keep)))
        for candidate in candidates:
            if os.path.normcase(os.path.abspath(str(candidate))) == keep_key:
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._note_failure("rolling_log_archive_prune_failed", exc)

    def _note_failure(self, event: str, exc: OSError) -> None:
        self.write_failures += 1
        logger.warning("%s label=%s category=%s", event, self.label, type(exc).__name__)

    def _close(self, handle, *, count: bool = False) -> None:
        if handle is None:
            return
        try:
            handle.close()
        except OSError as exc:
            if count:
                self._note_failure("rolling_log_archive_failed", exc)

    def clear(self) -> None:
        """清空文件与计数（不删文件本身）。"""

        self.lines = 0
        self.bytes = 0
        try:
            self.path.write_text("", encoding="utf-8")
        except OSError:
            self.write_failures += 1


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
        self._previews: deque[dict[str, object]] = deque(maxlen=PREVIEW_ENTRIES)
        self._file = RollingJsonlFile(self.path, capacity=self.capacity,
                                      enabled=self.enabled, label=feature)

    @property
    def write_failures(self) -> int:
        return self._file.write_failures

    @property
    def _lines_in_file(self) -> int:
        return self._file.lines

    @property
    def _bytes(self) -> int:
        return self._file.bytes

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
            "system": truncate_field(system),
            "input": truncate_field(input),
            "output": truncate_field(output),
        }
        if error:
            entry["error"] = truncate_field(error)
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
        self._file.append(entry)

    def snapshot(self) -> dict[str, object]:
        """运行快照里用的摘要：计数、大小、路径，不含正文。"""

        return {
            "enabled": self.enabled,
            "capacity": self.capacity,
            "recorded": self.recorded,
            "lines": self._file.lines,
            "bytes": self._file.bytes,
            "file": str(self.path),
            "write_failures": self._file.write_failures,
        }

    def previews(self, count: int = 5) -> list[dict[str, object]]:
        return list(self._previews)[-max(1, count):]

    def clear(self) -> None:
        """清空文件与计数（运维用；不删文件本身）。"""

        self._previews.clear()
        self.recorded = 0
        self._file.clear()


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
