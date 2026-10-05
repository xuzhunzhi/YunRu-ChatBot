"""跨重启的累计账本：API 用量（按角色）与引擎计数。

由来（2026-09-30 用户）："super apicheck 最好改一下，默认展示从开始使用到现在的，
而不是重启后的"，随后补一句"status 同理"。

以前这些数字都在**进程内存**里（`OpenAICompatibleClient.usage_totals`、`engine._stats`），
一重启就归零——于是"这套东西一共花了多少、一共回了多少条"永远看不到，只能看到"这次
重启之后"。这个模块把它们落一份到 `data/api_usage.json`。

两块内容、两种写法（刻意不同）：

- **API 用量**（calls / hit / miss / prompt / completion，按 role）：每次调用都往这里加一笔，
  节流落盘。它是"只增不减"的账，所以直接累加。
- **引擎计数**（收到消息、回复、判定调用…）：引擎自己那套是**本次进程**的计数，
  所以这里存的是"**之前几轮进程的累计**"（baseline），展示时 `baseline + 本次`。
  这样就不用去改引擎里每一处 `_stats[...] += 1`。

落盘与 `mail_state` 同一套写法：临时文件 + `os.replace`，失败只记日志；
`QQBOT_STATE_PERSIST=0`（测试与干跑）时整个账本不落盘，只在内存里待着。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STORE_VERSION = 1
# 落盘节流：账本是"只增不减"的，最多丢最近几秒的数字，不值得每次调用都写盘。
SAVE_INTERVAL_SECONDS = 5.0
# 计数名单（引擎侧）。写成常量是为了"账本里有什么"一眼可见。
COUNTER_NAMES = (
    "accepted_messages", "ignored_messages", "blocked_messages", "deferred_messages",
    "model_calls", "replies", "reply_segments", "judge_calls", "history_seeded",
    "guard_raised", "media_described", "reply_reviewed", "reply_review_rejected_final",
    "empty_forced_reply",
)


def default_path() -> Path:
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "api_usage.json"


class ApiUsageStore:
    """跨重启累计。`enabled=False` 时纯内存（测试与干跑）。"""

    def __init__(self, path: Path | str | None = None, *, enabled: bool = True,
                 clock=time.time) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.enabled = bool(enabled)
        self.clock = clock
        self.since = 0.0
        self._roles: dict[str, dict[str, int]] = {}
        self._counters: dict[str, int] = {}
        self._last_save = 0.0
        self.last_error = ""
        if self.enabled:
            self._load()
        if not self.since:
            # 全新账本：起点就是这次启动的时刻。"从开始用到现在"要的是这个时间，
            # 不能等到第一次落盘才算（那会晚几分钟，看起来像"从今天几点几分开始"）。
            # 落地仍由第一次 `_save` 完成，这里只是先把时间定下来。
            self.since = self.clock()

    # --- 读盘 / 写盘 -------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("api_usage_read_failed category=%s", type(exc).__name__)
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("api_usage_invalid_json; 从零开始记")
            return
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return
        roles = data.get("roles")
        if isinstance(roles, dict):
            for role, values in roles.items():
                if isinstance(values, dict):
                    self._roles[str(role)] = {
                        str(key): int(value) for key, value in values.items()
                        if isinstance(value, (int, float))
                    }
        counters = data.get("counters")
        if isinstance(counters, dict):
            self._counters = {
                str(key): int(value) for key, value in counters.items()
                if isinstance(value, (int, float))
            }
        self.since = float(data.get("since") or 0.0)

    def _save(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = self.clock()
        if not force and now - self._last_save < SAVE_INTERVAL_SECONDS:
            return
        self._last_save = now
        if not self.since:
            self.since = now
        body = {
            "version": STORE_VERSION,
            "since": round(self.since, 3),
            "updated_at": round(now, 3),
            "roles": {role: dict(values) for role, values in self._roles.items()},
            "counters": dict(self._counters),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("api_usage_write_failed category=%s", type(exc).__name__)
            return
        self.last_error = ""

    # --- API 用量 -----------------------------------------------------------

    def add_usage(self, role: str, usage) -> None:
        """记一次调用（只增不减）。字段与 `llm_client._USAGE_FIELDS` 对齐。"""

        name = str(role or "unknown")
        bucket = self._roles.setdefault(name, {})
        # 调用次数单独记一个内部键（usage 里没有"次数"这个字段）。
        bucket["_calls"] = int(bucket.get("_calls", 0)) + 1
        for key, value in dict(usage or {}).items():
            if isinstance(value, (int, float)):
                bucket[str(key)] = int(bucket.get(str(key), 0)) + int(value)
        self._save()

    def role_stats(self, role: str) -> dict[str, int]:
        return dict(self._roles.get(str(role), {}))

    def roles(self) -> dict[str, dict[str, int]]:
        return {role: dict(values) for role, values in self._roles.items()}

    def usage_report(self, roles) -> dict[str, dict[str, int]]:
        """把若干角色的累计合成一张表（缺失的角色给空）。

        与 `/super apicheck` 的展示口径一致：命中率 = 命中 /（命中+未命中）。
        """

        report: dict[str, dict[str, int]] = {}
        for role in roles:
            stats = self.role_stats(role)
            hit = int(stats.get("prompt_cache_hit_tokens", 0))
            miss = int(stats.get("prompt_cache_miss_tokens", 0))
            report[role] = {
                "calls": int(stats.get("_calls", 0)),
                "hit_tokens": hit,
                "miss_tokens": miss,
                "prompt_tokens": int(stats.get("prompt_tokens", 0)),
                "completion_tokens": int(stats.get("completion_tokens", 0)),
            }
        return report

    # --- 引擎计数 -----------------------------------------------------------

    def commit_counters(self, counts: dict[str, int]) -> None:
        """把"到这一刻为止的全时总量"写下来（调用方给的是 baseline + 本次）。"""

        merged: dict[str, int] = {}
        for name in COUNTER_NAMES:
            merged[name] = int(counts.get(name, self._counters.get(name, 0)))
        self._counters = merged
        self._save()

    def counters(self) -> dict[str, int]:
        return {name: int(self._counters.get(name, 0)) for name in COUNTER_NAMES}

    def all_time(self, current: dict[str, int] | None = None) -> dict[str, int]:
        """全时计数 = 账本里的（之前几轮进程） + 本次进程的。"""

        now = {name: int((current or {}).get(name, 0) or 0) for name in COUNTER_NAMES}
        return {name: int(self._counters.get(name, 0)) + now[name] for name in COUNTER_NAMES}

    def stats(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "path": str(self.path),
            "since": self.since,
            "roles": sorted(self._roles),
            "counters": self.counters(),
            "last_error": self.last_error,
        }
