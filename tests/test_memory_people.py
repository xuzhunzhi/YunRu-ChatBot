"""记忆 ↔ 人：一条记忆怎么跟一个人/几个人对应（schema v4）。

设计见 `docs/MEMORY_PEOPLE.md`。这里锁四件事：

1. v3 库能迁到 v4，并按已有的 `subject_user_id` 给旧记录补上关联人；
2. 模型声明的 `subjects` 只能从**本群名册**里挑，编出来的丢掉；
3. 跨人召回**只借本群范围内的记录**——别人的 `user_global` 不外借；
4. 渲染时写名字（查不到才退回号码），称呼/边界永远单人。

离线测试入口不用 pytest fixture，临时目录在测试内部自己建。
"""
import sqlite3
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.memory_model import InboxEvent, MemoryMaterial, MemoryRecord
from qq_roleplay_bot.memory_store import SCHEMA_VERSION, MemoryStore

GROUP = "800000001"
OTHER_GROUP = "717151356"
ALICE = "1001"
BOB = "1002"


def make_store(tmp: str, **kwargs) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", **kwargs)


def seed_person(store: MemoryStore, group: str, user: str, name: str) -> None:
    """让这个人"出现过"：记进本群名册（不动 inbox，免得被后续 claim 认领走）。"""

    store.note_person(group, user, name)


def add_fact(store: MemoryStore, *, user: str, key: str, content: str, group: str = GROUP,
             kind: str = "preference", scope: str = "user_group", subjects=(),
             evidence: str = "e1") -> list:
    store.append(InboxEvent(evidence, group, user, "user", content, time.time()))
    batch = store.claim({group}, lease_seconds=60)
    assert batch is not None, "事件应当能被认领成一批"
    extra = ""
    if subjects:
        extra = ', "subjects": [%s]' % ", ".join(f'"{s}"' for s in subjects)
    raw = (
        '{"operations":[{"op":"ADD","scope_type":"%s","kind":"%s","normalized_key":"%s",'
        '"content":"%s","confidence":0.9,"ttl_days":null,"evidence_event_ids":["%s"],'
        '"targets":[]%s}]}' % (scope, kind, key, content, evidence, extra)
    )
    return list(store.commit(batch, raw))


def subjects_of(store: MemoryStore, key: str) -> list[str]:
    rows = []
    with store.connection() as db:
        for table in ("memory_short", "memory_long"):
            rows.extend(db.execute(
                f"SELECT s.user_id, s.group_id FROM record_subjects s "
                f"JOIN {table} r ON r.id = s.record_id WHERE r.normalized_key=?",
                (key,)).fetchall())
    return sorted({row["user_id"] for row in rows})


# --- 1. 迁移 -----------------------------------------------------------------


def test_v3_database_migrates_to_v5_and_backfills_subjects() -> None:
    """v3 库要能就地升到 v5，并按已有的归属人补出关联人行。"""

    with tempfile.TemporaryDirectory() as tmp:
        legacy = Path(tmp) / "legacy.sqlite3"
        con = sqlite3.connect(legacy)
        con.executescript("""
        CREATE TABLE records (
            id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
            subject_user_id TEXT, kind TEXT NOT NULL, normalized_key TEXT NOT NULL,
            content TEXT NOT NULL, confidence REAL NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
            revision INTEGER NOT NULL, visibility TEXT NOT NULL DEFAULT 'group_safe',
            status TEXT NOT NULL DEFAULT 'active', last_used_at REAL, last_reviewed_at REAL,
            origin TEXT NOT NULL DEFAULT 'agent_inferred', source_event_ids TEXT NOT NULL);
        CREATE TABLE people_placeholder (id INTEGER);
        INSERT INTO records VALUES
            ('r1','user_global','1001',1001,'preference','likes_tea','喜欢喝奶茶',0.9,
             1000.0,1000.0,NULL,1,'group_safe','active',NULL,NULL,'agent_inferred','[]');
        INSERT INTO records VALUES
            ('r2','user_group','800000001:1002',1002,'preference','likes_coffee','喜欢喝咖啡',0.9,
             1000.0,1000.0,NULL,1,'group_safe','active',NULL,NULL,'agent_inferred','[]');
        INSERT INTO records VALUES
            ('r3','group','800000001',NULL,'group_fact','weekly_meet','本群周末聚餐',0.9,
             1000.0,1000.0,NULL,1,'group_safe','active',NULL,NULL,'agent_inferred','[]');
        PRAGMA user_version=3;
        """)
        con.commit()
        con.close()

        store = MemoryStore(legacy)
        with store.connection() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            rows = db.execute("SELECT record_id, user_id, group_id FROM record_subjects "
                              "ORDER BY record_id").fetchall()
            tiers = {
                "short": db.execute("SELECT COUNT(*) FROM memory_short").fetchone()[0],
                "long": db.execute("SELECT COUNT(*) FROM memory_long").fetchone()[0],
            }
        # 两条用户记录各补一行；group 记录没有归属人，不补。
        assert [(r["record_id"], r["user_id"]) for r in rows] == [("r1", "1001"), ("r2", "1002")]
        # user_group 记录顺带反推出它属于哪个群。
        assert rows[1]["group_id"] == GROUP
        # v5 分层：这三条都是 preference/group_fact 且 revision=1，按强化规则**都不够格**，
        # 所以全部留在中短期（称呼/边界与被再确认过的才会搬去长期，见两级那个测试）。
        assert tiers == {"short": 3, "long": 0}, tiers
        # 迁移不丢数据：旧记录仍可检索（本人记录在前，本群公共事实人人可见）。
        assert [r.normalized_key for r in store.retrieve(GROUP, ALICE, "")] == \
            ["likes_tea", "weekly_meet"]


# --- 2. subjects 的来源与校验 -------------------------------------------------


def test_subjects_must_come_from_the_group_roster() -> None:
    """模型编出来的 QQ 号进不了关联人；真实存在的人进得去。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        seed_person(store, GROUP, BOB, "小B")
        add_fact(store, user=ALICE, key="trip_together", kind="group_fact",
                 scope="group", content="两人约好周末一起去杭州",
                 subjects=(BOB, "999999999999"))
        # group 作用域的记录是"本群公共事实"，不挂关联人（人人都看得到）。
        assert subjects_of(store, "trip_together") == []

        add_fact(store, user=ALICE, key="plans_together", content="和 BOB 约好一起去看电影",
                 subjects=(BOB, "999999999999"), evidence="e2")
        assert subjects_of(store, "plans_together") == [ALICE, BOB], "编出来的号码必须被丢掉"


def test_name_and_boundary_are_never_multi_subject() -> None:
    """称呼与边界机械地只挂归属人——把 A 的雷区算到 B 头上是最坏的错。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        seed_person(store, GROUP, BOB, "小B")
        add_fact(store, user=ALICE, key="preferred_name", kind="name", content="自称小明",
                 subjects=(BOB,))
        add_fact(store, user=ALICE, key="body_boundary", kind="boundary",
                 content="不喜欢被当面评价身材", subjects=(BOB,), evidence="e2")
        assert subjects_of(store, "preferred_name") == [ALICE]
        assert subjects_of(store, "body_boundary") == [ALICE]


# --- 3. 跨人召回（以及它的边界） ----------------------------------------------


def test_mentioned_person_is_recalled_from_group_memories() -> None:
    """别人提到 BOB 时，要能想起 BOB 在本群说过的事。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        seed_person(store, GROUP, BOB, "小B")
        add_fact(store, user=BOB, key="drink_preference", content="喜欢喝奶茶，超大杯那种")

        # BOB 自己问 → 命中
        assert [r.normalized_key for r in store.retrieve(GROUP, BOB, "我喜欢喝什么")] == ["drink_preference"]
        # 别人问 BOB 的事 → 提到他就能命中
        got = store.retrieve(GROUP, ALICE, "BOB 喜欢喝什么", (BOB,))
        assert [r.normalized_key for r in got] == ["drink_preference"]
        # 没提到他 → 不借（宁可不召回，也别乱认人）
        assert store.retrieve(GROUP, ALICE, "BOB 喜欢喝什么") == ()


def test_cross_person_recall_is_limited_to_the_origin_group() -> None:
    """借旧事只借**在本群学到的**：在 A 群知道的事，不会因为 B 群有人提到他而说出来。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        seed_person(store, GROUP, BOB, "小B")
        # BOB 的跨群全局偏好，是在 GROUP 里知道的。
        add_fact(store, user=BOB, key="global_tea", scope="user_global",
                 content="喜欢喝奶茶，超大杯那种")
        # 本群内可以借
        got = store.retrieve(GROUP, ALICE, "喜欢喝奶茶", (BOB,))
        assert [r.normalized_key for r in got] == ["global_tea"]
        # 别的群不借（哪怕同一条记录是 user_global）
        assert store.retrieve(OTHER_GROUP, ALICE, "喜欢喝奶茶", (BOB,)) == ()
        # 别的群里 BOB 自己说话时，照旧看得到自己的全局记录
        assert [r.normalized_key for r in store.retrieve(OTHER_GROUP, BOB, "")] == ["global_tea"]


def test_own_records_outrank_the_mentioned_persons() -> None:
    """两边都命中时，说话人自己的排在前面。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        seed_person(store, GROUP, BOB, "小B")
        add_fact(store, user=BOB, key="bob_tea", content="喜欢喝奶茶，超大杯")
        add_fact(store, user=ALICE, key="alice_tea", content="喜欢喝奶茶，加珍珠", evidence="e2")
        got = store.retrieve(GROUP, ALICE, "喜欢喝奶茶", (BOB,))
        assert [r.normalized_key for r in got][0] == "alice_tea"


# --- 4. 渲染 -----------------------------------------------------------------


def test_rendering_names_the_people_a_memory_is_about() -> None:
    """`about` 写名字，查不到才退回号码；转义照旧。"""

    from html import escape

    record = MemoryRecord(
        id="r1", scope_type="user_group", scope_key=f"{GROUP}:{BOB}", subject_user_id=BOB,
        kind="preference", normalized_key="plans", content="和<乐乐>约好一起买耳机",
        confidence=0.9, created_at=1.0, updated_at=1.0,
    )
    material = MemoryMaterial((record,), {"r1": (ALICE, BOB)}, {ALICE: "乐乐"})
    rendered = material.as_data(GROUP, BOB)
    assert 'about="乐乐, 1002"' in rendered, rendered  # 归属人排在最前，查不到名字就退回号码
    assert escape("和<乐乐>约好一起买耳机", quote=False) in rendered
    # 没有 subjects 信息时退回归属人号码
    assert 'about="1002"' in MemoryMaterial((record,)).as_data(GROUP, BOB)


def test_judge_note_still_only_covers_the_speaker() -> None:
    """判定拿到的常驻记忆仍只有当前说话人的称呼/边界。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        seed_person(store, GROUP, BOB, "小B")
        add_fact(store, user=BOB, key="bob_boundary", kind="boundary", content="不喜欢被拍肩膀")
        add_fact(store, user=ALICE, key="alice_name", kind="name", content="自称小明", evidence="e2")
        note = MemoryMaterial(store.speaker_identity(GROUP, ALICE)).as_judge_note(GROUP, ALICE)
        assert "小明" in note
        assert "拍肩膀" not in note
