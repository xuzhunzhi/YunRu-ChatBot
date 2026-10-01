"""记忆的人工操作接缝：**只删不加**、删除必先归档、关系只走一档。

由来（2026-10-01 用户）："不允许手动添加记忆，只允许手动删除有问题的记忆"。
这个文件先钉住"这里没有任何新增入口"（名字层面扫一遍），再看行为：
删掉的原文必须能在 `archive` 里查到（AGENTS 2.1：删除不得销毁内容）。
"""
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_ops import (
    AXES, DELTAS, FORBIDDEN_NAME_PARTS, PUBLIC_API, MemoryOpRejected, MemoryOps,
)
from qq_roleplay_bot.memory_store import MemoryStore

USER = "900000001"
GROUP = "717151356"


def _store(tmp: str) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", daily_add_limit=0,
                       near_duplicate_ratio=9.9)


def _add_fact(store: MemoryStore, *, key: str = "likes_tea", content: str = "喜欢喝奶茶",
              kind: str = "preference") -> list:
    """按既有测试的写法造一条真实记录（走 append → claim → commit 全流程）。"""

    store.append(InboxEvent("e1", GROUP, USER, "user", content, time.time()))
    batch = store.claim({GROUP}, lease_seconds=60)
    assert batch is not None, "claim 该拿到一批"
    operations = (
        '{"operations":[{"op":"ADD","scope_type":"user_group","kind":"%s",'
        '"normalized_key":"%s","content":"%s","confidence":0.9,"ttl_days":null,'
        '"evidence_event_ids":["e1"],"subjects":[],"durable":false,"targets":[]}]}'
        % (kind, key, content)
    )
    return list(store.commit(batch, operations))


def _first_active_id(store: MemoryStore) -> str:
    """取一条活跃记录的 id。

    **不能用 `list_records`**：它走 `_public_record`，刻意不带 `id`（脱敏口径）。
    面板列表用那份"不给内部标识"的字段集是对的，但测试要删东西就得自己查。
    """

    with store.connection() as db:
        row = db.execute(
            "SELECT id FROM memory_short WHERE status='active' LIMIT 1").fetchone()
    assert row is not None, "该有一条活跃记录"
    return str(row[0])


def test_no_new_memory_entry_points_exist() -> None:
    """公开名白名单：不许出现 add/append/insert/note/write 之类"新增记忆"的名字。"""

    public = [name for name in dir(MemoryOps) if not name.startswith("_")]
    for expected in PUBLIC_API:
        assert expected in public, expected
    for name in public:
        assert not any(part in name.casefold() for part in FORBIDDEN_NAME_PARTS), name


def test_memory_ops_only_touches_three_store_methods() -> None:
    """除 purge / affinity / reset 之外，`MemoryOps` 不该碰 store 的写方法。"""

    class _Spy:
        def __init__(self):
            self.called: list[str] = []

        def purge_records(self, ids, *, reason=""):
            self.called.append("purge_records")
            return len(list(ids))

        def apply_affinity(self, user_id, **kwargs):
            self.called.append("apply_affinity")
            return ""

        def reset_relationship(self, user_id, *, reason=""):
            self.called.append("reset_relationship")
            return True

        def relationship(self, user_id):
            self.called.append("relationship")
            return (1, 0)

    spy = _Spy()
    ops = MemoryOps(spy, source="test")
    ops.purge(["a"], reason="x")
    ops.affinity(USER, "closeness", 1)
    ops.reset_affinity(USER)
    assert set(spy.called) <= {"purge_records", "apply_affinity", "reset_relationship",
                              "relationship"}


def test_purge_archives_before_deleting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        created = _add_fact(store)
        assert created, "得先造一条记录出来"
        victim_id = _first_active_id(store)
        live = store.list_records(limit=50)[0]
        victim_content = next(row["content"] for row in live if row["normalized_key"] == "likes_tea")
        ops = MemoryOps(store, source="test")
        removed = ops.purge([victim_id], reason="测试清理")
        assert removed == 1
        # 活跃名单里没有了。`list_records` 不带 status 时会**把已删的也列出来**
        # （面板要看归档），所以这里显式只要 active。
        active, _total = store.list_records(limit=50, status="active")
        assert "likes_tea" not in {row["normalized_key"] for row in active}
        # 但原文进了 archive ——"删除不得销毁内容"
        archived, total = store.list_archive(limit=10)
        assert total >= 1
        assert any(str(victim_content)[:10] in str(item.get("content", ""))
                   for item in archived)


def test_purge_rejects_absurd_batch_and_allows_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ops = MemoryOps(_store(tmp))
        try:
            ops.purge([str(i) for i in range(200)])
        except MemoryOpRejected:
            pass
        else:
            raise AssertionError("一次删太多该被拒")
        assert ops.purge([]) == 0        # 空列表不是错误


def test_affinity_only_accepts_one_step() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ops = MemoryOps(_store(tmp), source="test")
        assert AXES == ("closeness", "guardedness")
        assert DELTAS == (-1, 0, 1)
        for delta in (2, -3, 10):
            try:
                ops.affinity(USER, "closeness", delta)
            except MemoryOpRejected:
                continue
            raise AssertionError(f"{delta} 该被拒")
        for axis in ("trust", "", "CLOSENESS"):
            try:
                ops.affinity(USER, axis, 1)
            except MemoryOpRejected:
                continue
            raise AssertionError(f"{axis!r} 该被拒")
        try:
            ops.affinity("not-a-qq", "closeness", 1)
        except MemoryOpRejected:
            pass
        else:
            raise AssertionError("非数字 QQ 号该被拒")


def test_affinity_writes_a_log_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        ops = MemoryOps(store, source="test")
        values = ops.affinity(USER, "closeness", 1)
        assert set(values) == {"closeness", "guardedness"}
        rows = store.recent_relationship_changes(0.0, limit=10)
        assert any(row.get("source") == "test" for row in rows), rows


def test_ops_without_store_fails_closed() -> None:
    ops = MemoryOps(None)
    assert ops.available is False
    for call in (lambda: ops.purge(["a"]), lambda: ops.affinity(USER, "closeness", 1),
                 lambda: ops.reset_affinity(USER)):
        try:
            call()
        except MemoryOpRejected:
            continue
        raise AssertionError("没有记忆库时该明确拒绝")
