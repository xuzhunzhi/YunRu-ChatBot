"""超短期层（inbox）的随机 TTL 与"到期续一次"的保险（2026-10-20 用户设计）。

用户口径：记忆是缓存层级——**超短期 = 内存、短期 = L3、中期 = L2、长期 = L1**。
这里只落超短期那一层：每条材料的寿命独立随机 1–3 小时，而且到期时如果
**从没被任何一次维护碰过**，先续一次再删。

为什么必须续（本文件锁的就是这条）：TTL 从 7 天压到 1–3 小时后，维护只要漏跑
一两拍（模型超时、进程重启、群被临时关掉），一条没人看过的消息就会被清掉——
那是**丢数据**，不是遗忘。续一次把"一趟维护失败"和"这条不需要"分开。
"""
from __future__ import annotations

import random
import sqlite3
import tempfile
import unittest
from pathlib import Path

from qq_roleplay_bot.api_usage import COUNTER_NAMES
from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import (INBOX_TTL_MAX_SECONDS, INBOX_TTL_MIN_SECONDS,
                                          MemoryStore)
from qq_roleplay_bot.stage3_main import DialogueEngine

GROUP = "717151356"


class InboxTtlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000_000.0
        self.path = Path(self.temp.name) / "memory.sqlite3"

    def make_store(self, *, seed=None, path: Path | None = None) -> MemoryStore:
        rng = random.Random(seed) if seed is not None else None
        return MemoryStore(path or self.path, clock=lambda: self.now,
                           inbox_ttl_random=rng)

    def append(self, store: MemoryStore, event_id: str) -> None:
        self.assertTrue(store.append(
            InboxEvent(event_id, GROUP, "100", "user", f"材料{event_id}", self.now)))

    def row(self, store: MemoryStore, event_id: str):
        with store.connection() as db:
            return db.execute("SELECT * FROM inbox WHERE id=?", (event_id,)).fetchone()

    def ttl_of(self, store: MemoryStore, event_id: str) -> float:
        row = self.row(store, event_id)
        return row["expires_at"] - row["occurred_at"]

    def test_ttl_is_random_between_one_and_three_hours(self):
        """同一批插入的行，TTL 全落在 3600–10800 秒，且**不是同一个值**。"""

        store = self.make_store()
        for index in range(20):
            self.append(store, str(index))
        ttls = [self.ttl_of(store, str(index)) for index in range(20)]
        for ttl in ttls:
            self.assertGreaterEqual(ttl, INBOX_TTL_MIN_SECONDS)
            self.assertLessEqual(ttl, INBOX_TTL_MAX_SECONDS)
        self.assertGreater(len(set(ttls)), 1, "每条独立随机，不该是同一个值")

    def test_fixed_seed_reproduces_exactly_the_same_ttls(self):
        """注入固定种子 → 两次跑出完全相同的 TTL（测试要能复现随机）。"""

        first = self.make_store(seed=20261020)
        second = self.make_store(seed=20261020, path=Path(self.temp.name) / "second.sqlite3")
        for index in range(5):
            self.append(first, str(index))
            self.append(second, str(index))
        self.assertEqual(
            [self.ttl_of(first, str(index)) for index in range(5)],
            [self.ttl_of(second, str(index)) for index in range(5)],
        )

    def test_unclaimed_expired_row_is_extended_once_then_deleted(self):
        """从没被认领过的行：第一次到期**续期不删**，第二次到期才删，并记账。"""

        store = self.make_store(seed=7)
        self.append(store, "m1")
        expires_at = self.row(store, "m1")["expires_at"]

        self.now = expires_at + 1
        store.cleanup()
        row = self.row(store, "m1")
        self.assertIsNotNone(row, "没人看过的消息不该被维护漏跑这一趟删掉")
        self.assertEqual(row["extended_once"], 1)
        self.assertLessEqual(row["expires_at"] - self.now, INBOX_TTL_MAX_SECONDS)
        self.assertGreaterEqual(row["expires_at"] - self.now, INBOX_TTL_MIN_SECONDS)
        self.assertEqual(store.inbox_maintenance_counts()["extended"], 1)
        self.assertEqual(store.inbox_maintenance_counts()["expired_deleted"], 0)

        self.now = row["expires_at"] + 1
        store.cleanup()
        self.assertIsNone(self.row(store, "m1"))
        self.assertEqual(store.inbox_maintenance_counts()["extended"], 1)
        self.assertEqual(store.inbox_maintenance_counts()["expired_deleted"], 1)

    def test_claimed_expired_row_is_deleted_without_extension(self):
        """已经被维护认领过的行：到期**不续期**，直接删（不算丢数据）。"""

        store = self.make_store(seed=8)
        self.append(store, "m1")
        self.assertIsNotNone(store.claim({GROUP}))
        expires_at = self.row(store, "m1")["expires_at"]

        self.now = expires_at + 1
        store.cleanup()
        self.assertIsNone(self.row(store, "m1"))
        self.assertEqual(store.inbox_maintenance_counts()["extended"], 0)
        self.assertEqual(store.inbox_maintenance_counts()["expired_deleted"], 1)

    def test_old_library_without_the_column_opens_and_works(self):
        """老库（v7，`inbox` 没有 `extended_once`）能直接打开：幂等补列后照常工作。"""

        self.make_store(seed=9).cleanup()  # 先按当前 schema 建库
        # 把 inbox 换回老形状（没有 extended_once），模拟升级前就存在的库。
        con = sqlite3.connect(self.path)
        con.executescript("""
        ALTER TABLE inbox RENAME TO inbox_old;
        CREATE TABLE inbox (
            id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
            speaker TEXT NOT NULL, text TEXT NOT NULL, occurred_at REAL NOT NULL,
            expires_at REAL NOT NULL, lease_id TEXT);
        INSERT INTO inbox SELECT id,group_id,user_id,speaker,text,occurred_at,expires_at,lease_id
            FROM inbox_old;
        DROP TABLE inbox_old;
        CREATE INDEX IF NOT EXISTS inbox_scope ON inbox(group_id, user_id, occurred_at);
        INSERT INTO inbox VALUES
            ('legacy', '717151356', '100', 'user', '老库里的材料', 1000000.0, 999999.0, NULL);
        """)
        con.commit()
        con.close()

        reopened = MemoryStore(self.path, clock=lambda: self.now)
        reopened.cleanup()  # 打开即补列；顺带把那条过期的老行续一次
        with reopened.connection() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(inbox)")}
        self.assertIn("extended_once", columns)
        self.assertEqual(self.row(reopened, "legacy")["extended_once"], 1)
        # 再打开一次也不报错（幂等），新写入照常带随机 TTL。
        again = MemoryStore(self.path, clock=lambda: self.now)
        self.append(again, "fresh")
        ttl = self.ttl_of(again, "fresh")
        self.assertGreaterEqual(ttl, INBOX_TTL_MIN_SECONDS)
        self.assertLessEqual(ttl, INBOX_TTL_MAX_SECONDS)


    def test_the_two_counts_reach_snapshot_and_ledger(self):
        """两个计数走既有出口：`COUNTER_NAMES` + `EngineSnapshot`（只记数，不含正文）。"""

        self.assertIn("memory_inbox_extended", COUNTER_NAMES)
        self.assertIn("memory_inbox_expired_deleted", COUNTER_NAMES)

        store = self.make_store(seed=10)

        class _Memory:
            def __init__(self, memory_store):
                self.store = memory_store

            def snapshot(self):
                return {}

        store.inbox_counts["extended"] = 3
        store.inbox_counts["expired_deleted"] = 2
        engine = DialogueEngine(None, memory_service=_Memory(store))
        snapshot = engine.snapshot()
        self.assertEqual(snapshot.memory_inbox_extended, 3)
        self.assertEqual(snapshot.memory_inbox_expired_deleted, 2)


if __name__ == "__main__":
    unittest.main()
