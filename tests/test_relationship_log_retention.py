"""关系流水（`relationship_log`）有保留期，而且清得**保守**（隔离增长审计 [8]）。

它是好感/关系的每一次变动，从前**没有任何保留期**：`_cleanup` 里删了 inbox / receipts /
replays / leases / audit / archive，唯独没删它，于是只增不减。

现在按保留期清**旧行**：`QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS`（默认 365 天，
夹 30..3650，**调用时现读**）。清理保守：只删严格早于界限的行（近期一行不动），
写入语义一个字不改；清掉的条数记在 `MemoryStore.pruned` 与 `counts()` 里，
`/super status` 的记忆那行会印出来。

这一份钉住：

1. 超过保留期的旧行被清掉、**近期行一行不动**；
2. 保留期是**调用时现读**的配置（改环境变量，下一次 `_cleanup` 就按新值走）；
3. 条数可见：`pruned` 与 `counts()`；
4. 默认值与夹取。

突变（还原）：去掉那条 `DELETE FROM relationship_log` → 第 1 条红。
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.memory_store import DAY, MemoryStore

NOW = 1_700_000_000.0


def _store(tmp: str) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", clock=lambda: NOW)


def _add_relationship_row(store: MemoryStore, occurred_at: float, user_id: str = "100") -> None:
    with store.connection() as db:
        db.execute(
            "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
            (user_id, "closeness", 0, 1, "chat", "judge", occurred_at),
        )


def _relationship_rows(store: MemoryStore) -> list[float]:
    with store.connection() as db:
        return [float(row[0]) for row in db.execute(
            "SELECT occurred_at FROM relationship_log ORDER BY occurred_at").fetchall()]


# --- 1. 保守清理 -------------------------------------------------------------


def test_old_rows_are_pruned_and_recent_ones_are_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _add_relationship_row(store, NOW - 400 * DAY)      # 超过 365 天
        _add_relationship_row(store, NOW - 10 * DAY)       # 近期
        _add_relationship_row(store, NOW)                  # 刚刚

        store.cleanup()

        assert _relationship_rows(store) == [NOW - 10 * DAY, NOW], "近期行被动了"
        assert store.pruned["relationship_log"] == 1
        assert store.counts()["pruned_relationship_log"] == 1


def test_the_retention_is_read_when_called() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _add_relationship_row(store, NOW - 40 * DAY)
        _add_relationship_row(store, NOW - 5 * DAY)

        with patch.dict(os.environ, {"QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS": "30"}):
            store.cleanup()

        assert _relationship_rows(store) == [NOW - 5 * DAY]
        assert store.pruned["relationship_log"] == 1


def test_a_second_cleanup_prunes_nothing_more() -> None:
    """清过一次就不再重复计数，也不会把近期行越清越少。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _add_relationship_row(store, NOW - 400 * DAY)
        _add_relationship_row(store, NOW - 1 * DAY)
        store.cleanup()
        store.cleanup()
        assert _relationship_rows(store) == [NOW - 1 * DAY]
        assert store.pruned["relationship_log"] == 1


# --- 2. 配置 -----------------------------------------------------------------


def test_the_retention_defaults_and_is_clamped() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS", None)
        assert dev_config.relationship_log_retention_days() == 365
    with patch.dict(os.environ, {"QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS": "90"}):
        assert dev_config.relationship_log_retention_days() == 90
    for text, expected in (("", 365), ("不是数", 365), ("1", 30), ("99999", 3650)):
        with patch.dict(os.environ, {"QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS": text}):
            assert dev_config.relationship_log_retention_days() == expected, text


if __name__ == "__main__":
    import unittest

    unittest.main()
