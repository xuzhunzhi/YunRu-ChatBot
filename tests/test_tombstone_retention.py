"""墓碑（`tombstones`）有保留期，而且清得**保守**（隔离增长审计 [9]）。

墓碑只记**键与时间判据**、不含内容（AGENTS §2.1），它是"删掉的东西不许被写回来"的
凭据——`_cleanup` 里删了 inbox / receipts / replays / leases / audit / archive，
唯独没删它，于是也只增不减。

现在按保留期清**旧行**：`QQBOT_TOMBSTONE_RETENTION_DAYS`（默认 365 天，夹 30..3650，
**调用时现读**）。只删 `deleted_at` 严格早于界限的行；写入语义一个字不改
（墓碑记什么、什么时候记，全都没动）。

这一份钉住：

1. 超过保留期的旧墓碑被清掉、**近期墓碑一行不动**；
2. 保留期是**调用时现读**的配置；
3. 条数可见：`pruned` 与 `counts()`；
4. 默认值与夹取。

突变（还原）：去掉那条 `DELETE FROM tombstones` → 第 1 条红。
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


def _add_tombstone(store: MemoryStore, deleted_at: float, key: str) -> None:
    with store.connection() as db:
        db.execute("INSERT INTO tombstones VALUES (?,?,?,?)", ("group", "717151356", key, deleted_at))


def _tombstone_rows(store: MemoryStore) -> list[tuple[str, float]]:
    with store.connection() as db:
        return [(str(row[0]), float(row[1])) for row in db.execute(
            "SELECT normalized_key, deleted_at FROM tombstones ORDER BY deleted_at").fetchall()]


# --- 1. 保守清理 -------------------------------------------------------------


def test_old_tombstones_are_pruned_and_recent_ones_are_kept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _add_tombstone(store, NOW - 400 * DAY, "old")
        _add_tombstone(store, NOW - 10 * DAY, "recent")
        _add_tombstone(store, NOW, "now")

        store.cleanup()

        assert _tombstone_rows(store) == [("recent", NOW - 10 * DAY), ("now", NOW)], \
            "近期墓碑被动了"
        assert store.pruned["tombstones"] == 1
        assert store.counts()["pruned_tombstones"] == 1


def test_the_retention_is_read_when_called() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _add_tombstone(store, NOW - 40 * DAY, "old")
        _add_tombstone(store, NOW - 5 * DAY, "recent")

        with patch.dict(os.environ, {"QQBOT_TOMBSTONE_RETENTION_DAYS": "30"}):
            store.cleanup()

        assert _tombstone_rows(store) == [("recent", NOW - 5 * DAY)]
        assert store.pruned["tombstones"] == 1


def test_a_second_cleanup_prunes_nothing_more() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _add_tombstone(store, NOW - 400 * DAY, "old")
        _add_tombstone(store, NOW - 1 * DAY, "recent")
        store.cleanup()
        store.cleanup()
        assert _tombstone_rows(store) == [("recent", NOW - 1 * DAY)]
        assert store.pruned["tombstones"] == 1


# --- 2. 配置 -----------------------------------------------------------------


def test_the_retention_defaults_and_is_clamped() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_TOMBSTONE_RETENTION_DAYS", None)
        assert dev_config.tombstone_retention_days() == 365
    with patch.dict(os.environ, {"QQBOT_TOMBSTONE_RETENTION_DAYS": "90"}):
        assert dev_config.tombstone_retention_days() == 90
    for text, expected in (("", 365), ("不是数", 365), ("1", 30), ("99999", 3650)):
        with patch.dict(os.environ, {"QQBOT_TOMBSTONE_RETENTION_DAYS": text}):
            assert dev_config.tombstone_retention_days() == expected, text


if __name__ == "__main__":
    import unittest

    unittest.main()
