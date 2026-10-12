"""只读记忆口（`memory_readonly.py`）的规矩。

要治的病（2026-10-12 用户原话）：

    记忆要完整开放给写信agent来读取，又不是强制一次输入就必须得到一封信。

改之前那条接缝递过去的是真 `MemoryService`——顺着 `service.store` 就能拿到可写的
`MemoryStore`（`purge_records` / `merge_alias` / `retrieve` 会写 `last_used_at`）。
这里钉住新口径：

1. 只有 `group_safe` + `active` + `confidence >= 0.5` 的记录能出来；
2. 字段白名单之外的东西（`source_event_ids` / `origin` / `last_used_at`）一个都不出；
3. **翻看不改状态**：读多少次，`last_used_at` 都不动；连接本身拒绝写
   （`file:…?mode=ro` + `PRAGMA query_only=1`，结构性的，不靠自觉）；
4. 库不在、表不在、查询炸了，一律空元组 + 一条日志，绝不往上抛；
5. 关系那两句素材（跟谁说过话 / 谁让她改了看法）也走同一条只读路。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from qq_roleplay_bot import memory_readonly
from qq_roleplay_bot.memory_store import MemoryStore

NOW = 1_791_800_000.0
COLUMNS = ("id, scope_type, scope_key, subject_user_id, kind, normalized_key, content, "
           "confidence, created_at, updated_at, expires_at, revision, visibility, status, "
           "last_used_at, last_reviewed_at, origin, source_event_ids, durable, reminded_at")


def _record(**over):
    row = {
        "id": "r1", "scope_type": "user_global", "scope_key": "123456789",
        "subject_user_id": "123456789", "kind": "preference", "normalized_key": "",
        "content": "上周考试的事，他说考完再说。", "confidence": 0.8,
        "created_at": NOW - 3600, "updated_at": NOW - 60, "expires_at": None,
        "revision": 1, "visibility": "group_safe", "status": "active",
        "last_used_at": None, "last_reviewed_at": None, "origin": "agent_inferred",
        "source_event_ids": "[]", "durable": 0, "reminded_at": None,
    }
    row.update(over)
    # 去重键是 (scope_type, scope_key, normalized_key) 上的唯一约束：默认按 id 造一个，
    # 免得测试里插两条不同的事却撞在同一个键上。
    row["normalized_key"] = row["normalized_key"] or f"k-{row['id']}"
    return row


class MemoryReadonlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "memory.sqlite3"
        # 用**真的** `MemoryStore` 建库：口径一致，也能证明只读口读得到它写的东西。
        self.store = MemoryStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add(self, *rows, tier: str = "memory_short") -> None:
        with self.store.connection() as db:
            for row in rows:
                db.execute(f"INSERT INTO {tier} ({COLUMNS}) VALUES "
                           f"({','.join('?' * len(row))})",
                           tuple(row[name] for name in COLUMNS.split(", ")))

    def test_finds_a_record_by_word(self) -> None:
        self.add(_record())

        rows = memory_readonly.lookup("考试", db_path=self.path)

        assert len(rows) == 1, rows
        assert rows[0]["content"].startswith("上周考试")
        assert rows[0]["kind"] == "preference"

    def test_finds_a_person_by_id_and_not_someone_else(self) -> None:
        self.add(
            _record(id="mine", scope_type="user_group", scope_key="717151356:123456789",
                    subject_user_id=None, content="他喜欢短句。"),
            _record(id="other", scope_type="user_group", scope_key="717151356:999999999",
                    subject_user_id=None, content="别人喜欢长句。"),
        )

        rows = memory_readonly.lookup("123456789", db_path=self.path)

        assert [row["content"] for row in rows] == ["他喜欢短句。"], rows

    def test_only_group_safe_active_confident_records_come_out(self) -> None:
        self.add(
            _record(id="ok", content="可以外露的。"),
            _record(id="hidden", visibility="private", content="不许外露的。"),
            _record(id="dead", status="expired", content="过期了。"),
            _record(id="weak", confidence=0.3, content="没把握的。"),
            _record(id="old", expires_at=NOW - 1, content="早失效了。"),
        )

        rows = memory_readonly.lookup("的", db_path=self.path, limit=10)

        assert [row["content"] for row in rows] == ["可以外露的。"], rows

    def test_the_whitelist_keeps_evidence_and_usage_inside(self) -> None:
        self.add(_record(source_event_ids='["e1"]', origin="agent_inferred",
                         last_used_at=NOW - 5))

        rows = memory_readonly.lookup("考试", db_path=self.path)

        assert tuple(rows[0]) == memory_readonly.READONLY_FIELDS, tuple(rows[0])
        for banned in ("source_event_ids", "origin", "last_used_at", "id", "status"):
            assert banned not in rows[0], banned

    def test_looking_things_up_never_touches_last_used_at(self) -> None:
        self.add(_record(last_used_at=None))

        memory_readonly.lookup("考试", db_path=self.path)
        memory_readonly.MemoryRead(db_path=self.path, clock=lambda: NOW).search("考试")

        with self.store.connection() as db:
            row = db.execute("SELECT last_used_at FROM memory_short WHERE id='r1'").fetchone()
        assert row["last_used_at"] is None, dict(row)

    def test_the_connection_itself_refuses_writes(self) -> None:
        self.add(_record())

        conn = memory_readonly._connect(self.path)
        try:
            for sql in ("UPDATE memory_short SET last_used_at=1",
                        "DELETE FROM memory_short",
                        "INSERT INTO memory_short (id) VALUES ('x')"):
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute(sql)
        finally:
            conn.close()

    def test_a_read_only_connection_cannot_create_a_database(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            memory_readonly._connect(self.tmp / "nope.sqlite3")

    def test_missing_database_is_empty_and_says_why(self) -> None:
        with self.assertLogs("qq_roleplay_bot.memory_readonly", level="WARNING") as logs:
            rows = memory_readonly.lookup("考试", db_path=self.tmp / "nope.sqlite3")

        assert rows == ()
        assert "memory_readonly_unavailable" in "\n".join(logs.output)

    def test_a_database_without_our_tables_is_empty_too(self) -> None:
        bare = self.tmp / "bare.sqlite3"
        sqlite3.connect(bare).close()

        with self.assertLogs("qq_roleplay_bot.memory_readonly", level="WARNING") as logs:
            rows = memory_readonly.lookup("考试", db_path=bare)

        assert rows == ()
        assert "memory_readonly_failed" in "\n".join(logs.output)

    def test_an_empty_query_asks_nothing(self) -> None:
        self.add(_record())

        assert memory_readonly.lookup("   ", db_path=self.path) == ()
        assert memory_readonly.lookup(None, db_path=self.path) == ()

    def test_like_wildcards_are_not_wildcards(self) -> None:
        self.add(
            _record(id="pct", content="他说过 100% 不行。"),
            _record(id="plain", content="没有百分号的一句。"),
        )

        rows = memory_readonly.lookup("%", db_path=self.path)

        assert [row["content"] for row in rows] == ["他说过 100% 不行。"], rows

    def test_render_wraps_and_escapes(self) -> None:
        text = memory_readonly.render([
            {"kind": "boundary", "scope_type": "group", "scope_key": "717151356",
             "subject_user_id": None, "content": "<b>别问 & 别提</b>"},
        ])

        assert text.startswith('<memory scope="group" subject="717151356" kind="boundary">')
        assert "&lt;b&gt;别问 &amp; 别提&lt;/b&gt;" in text, text
        assert memory_readonly.render([]) == ""

    def test_recall_returns_text_and_says_when_nothing_was_found(self) -> None:
        self.add(_record())
        reader = memory_readonly.MemoryRead(db_path=self.path)

        found = reader.recall("考试")
        assert 'kind="preference"' in found, found
        assert reader.recall("查不到的词") == "（翻了一下，没找到跟这个有关的旧事。）"

    def test_people_and_changes_read_the_relationship_tables(self) -> None:
        with self.store.connection() as db:
            db.execute("INSERT INTO relationships VALUES (?,?,?,?,?)",
                       ("123456789", 3, 1, NOW - 10, NOW - 10))
            db.execute("INSERT INTO relationships VALUES (?,?,?,?,?)",
                       ("999999999", 0, 0, NOW - 999_999, NOW - 999_999))
            db.execute("INSERT INTO relationship_log "
                       "(user_id, axis, before_value, after_value, reason, source, occurred_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       ("123456789", "closeness", 1, 3, "他连着几天在问同一件事", "judge",
                        NOW - 30))

        reader = memory_readonly.MemoryRead(db_path=self.path)
        people = reader.people(NOW - 100)
        changes = reader.changes(NOW - 100)

        assert [row["user_id"] for row in people] == ["123456789"], people
        assert tuple(people[0]) == memory_readonly.RELATIONSHIP_FIELDS
        assert changes[0]["reason"] == "他连着几天在问同一件事"
        assert "source" not in changes[0], changes[0]

    def test_the_reader_only_exposes_queries(self) -> None:
        """递出去的那一个对象上，不许有写、不许有 store/service。"""

        reader = memory_readonly.MemoryRead(db_path=self.path)
        for banned in ("store", "service", "memory_service", "commit", "purge_records",
                       "merge_alias", "retrieve", "connection"):
            assert not hasattr(reader, banned), banned
        assert not hasattr(memory_readonly, "commit")


if __name__ == "__main__":
    unittest.main()
