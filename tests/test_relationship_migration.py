"""关系刻度迁移：旧 0..3 → 新刻度，可重跑、幂等。

用户 2026-10-20 的映射（逐条照抄）：

| 旧 | 好感 | 防备 |
| --- | --- | --- |
| 0 | 10 | 0 |
| 1 | 35 | 10 |
| 2 | 65 | 20 |
| 3 | 90 | 30 |

**这次不动生产库**：迁移写成可反复调用的函数，靠 `meta.relationship_scale_version`
判断跑没跑过；测试全在临时库上。真正执行由主会话点。

版本号丢失时必须先扫表：任何一行 >3 就说明它已经是新刻度，默认拒绝（一行不改），
只有 `force=True` / `--force` 才继续并留 `relationship_scale_forced=1`。
"""
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from qq_roleplay_bot.memory_store import (
    DAY,
    RELATIONSHIP_SCALE_VERSION,
    SCALE_EPOCH_KEY,
    SCALE_VERSION_KEY,
    MemoryStore,
)
from qq_roleplay_bot.relationship_migration import (
    CLOSENESS_MAP,
    GUARDEDNESS_MAP,
    MIGRATION_REJECTED_LOOKS_LIKE_NEW_SCALE,
    MIGRATION_SOURCE,
    SCALE_FORCED_KEY,
    main,
    migrate_relationship_scale,
)

USER = "900000001"
GROUP = "717151356"
NOW = 1_700_000_000.0
EVIDENCE = {"quote": "到了这一步才用得上原话", "who_alias": "1", "seq": 1}


def make_store(tmp: str, clock) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", clock=clock)


def seed_rows(store: MemoryStore, rows: list[tuple[str, int, int]]) -> None:
    """直接写旧刻度的行（迁移的输入本来就不是模型写的）。"""

    with store.connection() as db:
        for user_id, closeness, guardedness in rows:
            db.execute(
                "INSERT INTO relationships VALUES (?,?,?,?,?)",
                (user_id, closeness, guardedness, NOW - DAY, NOW - DAY),
            )


def rows(store: MemoryStore) -> dict[str, tuple[int, int]]:
    with store.connection() as db:
        return {row["user_id"]: (int(row["closeness"]), int(row["guardedness"]))
                for row in db.execute("SELECT user_id,closeness,guardedness FROM relationships")}


def the_fourteen() -> list[tuple[str, int, int]]:
    """真库那 14 行的旧值（2026-10-20 只读查出来的），按人编号。"""

    return [
        ("3278689325", 2, 0), ("2575311563", 2, 1), ("2639157559", 1, 0),
        ("1583844388", 1, 0), ("780078268", 1, 0), ("1185745728", 1, 1),
        ("3830005926", 0, 1), ("2873467761", 0, 1), ("1912600950", 0, 1),
        ("242003347", 0, 1), ("3432744809", 0, 1), ("3360593263", 0, 1),
        ("2595055768", 0, 1), ("1744326850", 0, 2),
    ]


def test_the_mapping_is_exactly_what_the_user_approved() -> None:
    assert CLOSENESS_MAP == {0: 10, 1: 35, 2: 65, 3: 90}
    assert GUARDEDNESS_MAP == {0: 0, 1: 10, 2: 20, 3: 30}


def test_migrates_the_fourteen_rows_and_leaves_a_trace() -> None:
    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, the_fourteen())
        report = migrate_relationship_scale(store)

        assert report["converted"] == 14 and report["skipped"] is False
        got = rows(store)
        assert got["3278689325"] == (65, 0)
        assert got["2575311563"] == (65, 10)
        assert got["2639157559"] == (35, 0)
        assert got["3830005926"] == (10, 10)
        assert got["1744326850"] == (10, 20)
        for user_id, old_closeness, old_guardedness in the_fourteen():
            assert got[user_id] == (CLOSENESS_MAP[old_closeness],
                                    GUARDEDNESS_MAP[old_guardedness]), user_id

        # 每一次数值变化都留一条"刻度升级"流水，超管看得到"上一次为什么变了"。
        # 好感映射里 0→10 也算变化，所以 14 行全有 closeness 流水；防备 0→0 的没有。
        with store.connection() as db:
            log = [dict(row) for row in db.execute(
                "SELECT * FROM relationship_log ORDER BY rowid")]
        assert len(log) == 14 + sum(1 for _, _, guard in the_fourteen() if guard != 0)
        assert {row["source"] for row in log} == {MIGRATION_SOURCE}
        assert all("刻度升级" in row["reason"] for row in log)


def test_meta_records_the_version_and_the_epoch() -> None:
    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, [("100", 3, 3)])
        migrate_relationship_scale(store, now=NOW)
        with store.connection() as db:
            meta = {row["key"]: row["value"] for row in db.execute("SELECT * FROM meta")}
        assert meta[SCALE_VERSION_KEY] == str(RELATIONSHIP_SCALE_VERSION)
        assert float(meta[SCALE_EPOCH_KEY]) == NOW


def test_second_run_is_a_no_op() -> None:
    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, the_fourteen())
        first = migrate_relationship_scale(store, now=NOW)
        assert first["converted"] == 14
        before = rows(store)
        with store.connection() as db:
            log_count = db.execute("SELECT COUNT(*) FROM relationship_log").fetchone()[0]

        clock[0] += 10 * DAY
        second = migrate_relationship_scale(store, now=clock[0])
        assert second["skipped"] is True and second["converted"] == 0
        assert rows(store) == before, "第二遍一行都不许动"
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM relationship_log").fetchone()[0] == log_count


def test_dry_run_writes_nothing() -> None:
    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, the_fourteen())
        before = rows(store)
        report = migrate_relationship_scale(store, dry_run=True)
        assert report["dry_run"] is True and report["converted"] == 0
        assert rows(store) == before
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0


def drop_meta(store: MemoryStore) -> None:
    """模拟"meta 被删了 / 恢复的是旧 dump"：版本号不见了，但关系行还在。"""

    with store.connection() as db:
        db.execute("DROP TABLE IF EXISTS meta")


def meta_rows(store: MemoryStore) -> dict[str, str]:
    with store.connection() as db:
        return {row["key"]: row["value"] for row in db.execute("SELECT * FROM meta")}


def test_new_scale_without_meta_is_rejected_and_untouched() -> None:
    """混着新刻度行、又没有版本号：默认整次拒绝，一行都不许二次换算。"""

    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, [("100", 3, 0), ("200", 65, 20)])
        drop_meta(store)
        before = rows(store)
        report = migrate_relationship_scale(store)
        assert report["rejected"] == MIGRATION_REJECTED_LOOKS_LIKE_NEW_SCALE
        assert report["converted"] == 0 and report["skipped"] is True
        assert all(row["new"] == row["old"] for row in report["rows"]), "拒绝时不许列假想的换算"
        assert rows(store) == before, "拒绝时一行都不许动"
        with store.connection() as db:
            meta_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        assert meta_exists is None, "拒绝连 meta 表都不建"


def test_force_migrates_new_scale_without_meta_and_leaves_a_trace() -> None:
    """`--force` 才越过拒绝：旧档位行照常换算，新刻度行原样跳过。"""

    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, [("100", 65, 20), ("200", 2, 1)])
        drop_meta(store)
        report = migrate_relationship_scale(store, now=NOW, force=True)
        assert "rejected" not in report and report["converted"] == 1
        got = rows(store)
        assert got["100"] == (65, 20), "已经是新刻度的行不许再换算"
        assert got["200"] == (65, 10), "旧档位行照常换算"
        meta = meta_rows(store)
        assert meta[SCALE_VERSION_KEY] == str(RELATIONSHIP_SCALE_VERSION)
        assert meta[SCALE_FORCED_KEY] == "1"


def test_old_scale_without_meta_still_migrates_without_force() -> None:
    """旧刻度（两轴都 ≤3）+ 没有版本号：这是正常的首跑，不需要 force。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: NOW)
        seed_rows(store, [("100", 2, 1), ("200", 3, 3)])
        drop_meta(store)
        report = migrate_relationship_scale(store, now=NOW)
        assert "rejected" not in report and report["converted"] == 2
        assert rows(store) == {"100": (65, 10), "200": (90, 30)}
        assert SCALE_FORCED_KEY not in meta_rows(store), "没强制就不留强制的痕"


def test_version_present_but_rows_look_new_is_a_no_op() -> None:
    """有版本号就听版本号的：已经是新刻度，即便值 >3 也只是 no-op。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: NOW)
        seed_rows(store, [("100", 65, 20)])
        with store.connection() as db:
            db.execute("INSERT INTO meta(key,value) VALUES(?,?)",
                       (SCALE_VERSION_KEY, str(RELATIONSHIP_SCALE_VERSION)))
        before = rows(store)
        report = migrate_relationship_scale(store, now=NOW)
        assert report["skipped"] is True and report.get("rejected") is None
        assert rows(store) == before


def test_cli_force_flag_is_wired_to_the_function() -> None:
    """命令行开关必须真的传进 `migrate_relationship_scale`，不能只在帮助里。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: NOW)
        seed_rows(store, [("100", 65, 20)])
        path = str(store.path)
        drop_meta(store)
        output = io.StringIO()
        with redirect_stdout(output):
            assert main(["--db", path]) == 1, "默认拒绝要以非零退出"
        assert MIGRATION_REJECTED_LOOKS_LIKE_NEW_SCALE in output.getvalue()
        with redirect_stdout(io.StringIO()):
            assert main(["--db", path, "--force"]) == 0
        assert meta_rows(make_store(tmp, lambda: NOW))[SCALE_FORCED_KEY] == "1"


def test_the_epoch_keeps_old_scale_logs_out_of_the_new_daily_cap() -> None:
    """迁移前的流水按旧刻度记，不能算进新的 24 小时窗口。

    构造：迁移前 `0→1` 的防备变化（旧刻度 +1）。迁移后同一天 +8：
    如果旧流水被算进来，净变化 9 > 8 会被拒；排除之后 8 正好放行。
    """

    clock = [NOW]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, lambda: clock[0])
        seed_rows(store, [("100", 0, 1)])
        with store.connection() as db:
            db.execute(
                "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                ("100", "guardedness", 0, 1, "旧刻度的一次变化", "judge", NOW - 60),
            )
        migrate_relationship_scale(store, now=NOW)
        assert rows(store)["100"] == (10, 10)

        # 迁移那一刻的"刻度升级"流水也不许算进来：在同一秒里再写一次。
        refusal = store.apply_affinity(
            "100", guardedness=8, reason="迁移后的一次提升", source="test",
            evidence=EVIDENCE,
        )
        assert refusal == "", refusal
        assert rows(store)["100"] == (10, 18)


def test_it_runs_on_a_store_whose_meta_table_does_not_exist_yet() -> None:
    """真库现在没有 `meta`（它是这次新加的）：迁移自己幂等建表。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memory.sqlite3"
        store = MemoryStore(path, clock=lambda: NOW)
        with store.connection() as db:
            db.execute("DROP TABLE IF EXISTS meta")
        seed_rows(store, [("100", 0, 0)])
        report = migrate_relationship_scale(store, now=NOW)
        assert report["skipped"] is False
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 2
