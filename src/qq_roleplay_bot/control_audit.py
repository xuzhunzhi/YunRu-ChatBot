"""面板操作的审计流水：`data/control_audit.jsonl`。

为什么单独一份、不混进别处的日志：面板是**第二个能改配置的入口**（第一个是改 `.env`
再重启）。改了什么、什么时候改的、改了之后生效没有，需要一个**只增不删**的落盘记录，
而不是散在 `bot.err.log` 里的 INFO 行——那份日志会被重启截断，也没有结构。

纪律（与 `feature_log` 同一套）：

- **绝不记录凭据明文**：key 类的改动只记"改过哪个键"，不记值。来源只记 token 的
  sha256 前 8 位，不记 token 本身。
- **写失败只计数**，绝不打断面板的那次请求（面板本身已经改成功了，记不上账是次要问题）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 记进去会被截断的字段长度（参数摘要与结果都可能很长）。
MAX_TEXT = 600
#: 文件超过这个大小就整份裁到最近这么多行（避免长期跑成几百 MB）。
MAX_BYTES = 4 * 1024 * 1024
KEEP_LINES = 2000

#: **不许写进审计的键**：出现这些名字时只记键名，不记值。
SECRET_KEY_HINTS = ("key", "token", "password", "secret", "passwd")


def default_path() -> Path:
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "control_audit.jsonl"


def token_fingerprint(token: str) -> str:
    """token 的不可逆短指纹：够用来区分"哪把 token 干的"，不够用来还原它。"""

    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _clip(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def summarize(payload: dict[str, object]) -> dict[str, str]:
    """把一次写的参数压成可落盘的样子：**凭据类的值换成占位符**。"""

    summary: dict[str, str] = {}
    for key, value in (payload or {}).items():
        name = str(key)
        if any(hint in name.casefold() for hint in SECRET_KEY_HINTS):
            summary[name] = "[已改]"
        elif isinstance(value, (list, tuple)):
            summary[name] = f"[{len(value)} 项]"
        else:
            summary[name] = _clip(value)
    return summary


class ControlAudit:
    """只增的审计流水。`enabled=False`（测试）时是空操作。"""

    def __init__(self, path: Path | str | None = None, *, enabled: bool = True,
                 clock=time.time) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.enabled = bool(enabled)
        self.clock = clock
        self.recorded = 0
        self.write_failures = 0

    def record(self, action: str, *, detail: dict[str, object] | None = None,
               result: str = "", source: str = "", token: str = "") -> None:
        """记一次操作。任何失败都只计数。"""

        if not self.enabled:
            return
        self.recorded += 1
        entry = {
            "seq": self.recorded,
            "at": round(self.clock(), 3),
            "action": str(action),
            "result": _clip(result),
            "source": _clip(source),
            "token": token_fingerprint(token),
            "detail": summarize(detail or {}),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: PERF203 - 审计失败不能挡住操作
            self.write_failures += 1
            logger.warning("control_audit_write_failed category=%s", type(exc).__name__)
            return
        self._trim_if_needed()

    def _trim_if_needed(self) -> None:
        try:
            if self.path.stat().st_size <= MAX_BYTES:
                return
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            temp = self.path.with_suffix(".jsonl.trim")
            temp.write_text("".join(lines[-KEEP_LINES:]), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            self.write_failures += 1

    def tail(self, count: int = 50) -> list[dict[str, object]]:
        """最近几条（给面板的"操作历史"页用）。"""

        limit = max(1, min(200, int(count)))
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-limit:]
        except (FileNotFoundError, OSError):
            return []
        rows: list[dict[str, object]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def stats(self) -> dict[str, object]:
        return {"enabled": self.enabled, "path": str(self.path),
                "recorded": self.recorded, "write_failures": self.write_failures}
