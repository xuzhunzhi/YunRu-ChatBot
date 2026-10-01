"""一个人可以有多个 QQ 号：别名登记与账号合并（schema v6）。

由来（2026-10-01 用户报的实例）："在不在不在"和"兔子狗可爱喵"是同一个人，
两个号各自攒了一半记忆，换号说话时调不到。

设计见 `docs/MEMORY_PEOPLE.md` 与 `MemoryStore.merge_alias` 的说明。这里锁六件事：

1. 没登记别名时，两个号就是两个人（**不做任何模糊匹配**）；
2. 登记之后，两个号读写同一份记忆——换号说话取得到，新学的也落在规范号名下；
3. 合并会把旧号名下的记录、关联人、名册、归档整体搬过去，**正文一个字不改**；
4. 墓碑跟着搬：删掉的键在规范号下同样算删过，旧证据不能把它写回来；
5. 同一把键两边都活着时**拒绝合并**（不猜谁对谁错），且不留半成品；
6. 关系档位、画像按规范号算，`profile_record` 用旧号也查得到那一份。

离线测试入口不用 pytest fixture，临时目录在测试内部自己建
（`run_offline.py` 用 `unittest.FunctionTestCase` 直接调用，**不给参数**，
所以下面的测试都套了 `with_tmp`）。
"""
import functools
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore, MemoryValidationError

GROUP = "800000001"
OTHER_GROUP = "717151356"
ALICE = "475842590"      # 规范号
ALICE_ALT = "430523284"  # 别名（她另一个号）
BOB = "1002"


def with_tmp(fn):
    """给测试一个临时目录，同时对外保持**零参数**（离线 runner 不给参数）。

    `functools.wraps` 会把 `__name__` 留成 `test_...`，所以 `run_offline.py`
    那句 `name.startswith("test_")` 仍然认得它。
    """

    @functools.wraps(fn)
    def wrapper():
        tmp = tempfile.mkdtemp(prefix="alias-")
        try:
            fn(tmp)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    wrapper.__wrapped__ = fn
    return wrapper


def make_store(tmp: str) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3")


def add_fact(store: MemoryStore, *, user: str, key: str, content: str, group: str = GROUP,
             kind: str = "preference", scope: str = "user_group", evidence: str = "e1",
             ttl_days=None) -> list:
    """走真实路径塞一条记忆：进收件箱 → 认领成批 → 提交模型输出。"""

    store.append(InboxEvent(evidence, group, user, "user", content, time.time()))
    batch = store.claim({group}, lease_seconds=60)
    assert batch is not None, "事件应当能被认领成一批"
    raw = (
        '{"operations":[{"op":"ADD","scope_type":"%s","kind":"%s","normalized_key":"%s",'
        '"content":"%s","confidence":0.9,"ttl_days":%s,"evidence_event_ids":["%s"],'
        '"targets":[]}]}' % (scope, kind, key, content,
                             "null" if ttl_days is None else ttl_days, evidence)
    )
    return list(store.commit(batch, raw))


def keys_seen(store: MemoryStore, group: str, user: str) -> set:
    return {r.normalized_key for r in store.retrieve(group, user, "")}


@with_tmp
def test_without_alias_two_ids_are_two_people(tmp: str) -> None:
    """没登记就是两个人——这是"不做模糊匹配"的底线断言。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE, key="likes_cookies", content="喜欢奶味曲奇")
    assert keys_seen(store, GROUP, ALICE) == {"likes_cookies"}
    assert keys_seen(store, GROUP, ALICE_ALT) == set()
    assert store.canonical_user(ALICE_ALT) == ALICE_ALT


@with_tmp
def test_alias_makes_both_ids_read_and_write_one_memory(tmp: str) -> None:
    """登记之后：旧号取得到规范号的记忆，旧号说的话也落在规范号名下。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE, key="likes_cookies", content="喜欢奶味曲奇")
    store.merge_alias(ALICE_ALT, ALICE, note="同一个人")

    assert store.canonical_user(ALICE_ALT) == ALICE
    assert keys_seen(store, GROUP, ALICE_ALT) == {"likes_cookies"}

    # 从旧号说一句新的：必须落在规范号名下（否则下次换回来又丢了）。
    add_fact(store, user=ALICE_ALT, key="poor_sense_of_direction", content="不认路",
             evidence="e2")
    assert keys_seen(store, GROUP, ALICE) == {"likes_cookies", "poor_sense_of_direction"}
    with store.connection() as db:
        owners = {row[0] for row in db.execute(
            "SELECT DISTINCT subject_user_id FROM memory_short WHERE normalized_key=?",
            ("poor_sense_of_direction",))}
    assert owners == {ALICE}, f"新记忆应当挂在规范号名下，实际 {owners}"


@with_tmp
def test_merge_moves_records_and_leaves_no_trace_of_alias(tmp: str) -> None:
    """合并把旧号名下的东西整体搬走，并且正文不被改写。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE_ALT, key="profile_like", content="原样正文：奶味曲奇",
             evidence="e1")
    store.note_person(GROUP, ALICE_ALT, "兔子狗可爱喵")
    store.note_person(GROUP, ALICE, "在不在不在")

    summary = store.merge_alias(ALICE_ALT, ALICE, note="同一个人")
    assert summary["canonical"] == ALICE
    assert summary["moved"]["memory_short.subject"] == 1

    with store.connection() as db:
        left = db.execute("SELECT COUNT(*) FROM memory_short WHERE subject_user_id=?",
                          (ALICE_ALT,)).fetchone()[0]
        content = db.execute("SELECT content FROM memory_short WHERE normalized_key=?",
                             ("profile_like",)).fetchone()[0]
        roster = db.execute("SELECT COUNT(*) FROM people WHERE user_id=?",
                            (ALICE_ALT,)).fetchone()[0]
        moved_scope = db.execute(
            "SELECT scope_key FROM memory_short WHERE normalized_key=?",
            ("profile_like",)).fetchone()[0]
    assert left == 0, "旧号名下不该再有记录"
    assert content == "原样正文：奶味曲奇", "正文一个字都不该改"
    assert roster == 0, "名册里不该再有旧号那一行"
    assert moved_scope == f"{GROUP}:{ALICE}", f"本群那份应当搬到规范号，实际 {moved_scope}"


@with_tmp
def test_tombstone_follows_the_merge(tmp: str) -> None:
    """删掉的键在规范号下同样算删过：旧证据不能把它写回来。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE_ALT, key="deleted_before", content="这条后来被删了",
             evidence="old-evidence")
    with store.connection() as db:
        rid = db.execute("SELECT id FROM memory_short WHERE normalized_key=?",
                         ("deleted_before",)).fetchone()[0]
    assert store.purge_records([rid], reason="测试删除") == 1

    store.merge_alias(ALICE_ALT, ALICE, note="同一个人")

    # 用一条**新的、时间更早**的证据再写一次：墓碑搬过去了，应当以 deleted_evidence 拒绝。
    # （不能重用上面那条证据 id：`receipts` 会把它当重复消息挡在收件箱外，
    #  那样 claim 直接返回 None，测的就不是墓碑了。）
    store.append(InboxEvent("late-evidence", GROUP, ALICE, "user", "又说了一次",
                            time.time() - 120))
    batch = store.claim({GROUP}, lease_seconds=60)
    assert batch is not None
    raw = ('{"operations":[{"op":"ADD","scope_type":"user_group","kind":"preference",'
           '"normalized_key":"deleted_before","content":"想复活","confidence":0.9,'
           '"ttl_days":null,"evidence_event_ids":["late-evidence"],"targets":[]}]}')
    try:
        store.commit(batch, raw)
    except MemoryValidationError as exc:
        # `deleted_evidence` 是整批拒绝（不是逐条跳过）：与 memory_store 里的语义一致。
        assert str(exc) == "deleted_evidence", f"应当是墓碑拦下的，实际 {exc}"
    else:
        raise AssertionError("旧证据不该能把删掉的键写回来")
    assert "deleted_before" not in keys_seen(store, GROUP, ALICE)
    with store.connection() as db:
        # 只看活跃行：purge 会留下 `status='deleted'` 的空壳（内容已清），那是删除的痕迹，
        # 不是"被写回来了"。这里要断言的是**没有新的活跃记录**。
        rows = [tuple(r) for r in db.execute(
            "SELECT status,scope_key,content FROM memory_short "
            "WHERE normalized_key=? AND status='active'", ("deleted_before",))]
    assert rows == [], f"被墓碑拦下的写入不该留下活跃记录（rows={rows}）"


@with_tmp
def test_merge_refuses_on_key_clash(tmp: str) -> None:
    """同一把键两边都活着：拒绝合并，且不留半成品。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE, key="gaming_attitude", content="规范号的说法", evidence="e1")
    add_fact(store, user=ALICE_ALT, key="gaming_attitude", content="旧号的说法", evidence="e2")

    try:
        store.merge_alias(ALICE_ALT, ALICE, note="撞键")
    except MemoryValidationError as exc:
        assert str(exc) == "merge_key_clash"
    else:
        raise AssertionError("撞键时应当拒绝合并")

    with store.connection() as db:
        kept = db.execute("SELECT COUNT(*) FROM memory_short WHERE subject_user_id=?",
                          (ALICE_ALT,)).fetchone()[0]
        aliases = db.execute("SELECT COUNT(*) FROM person_aliases").fetchone()[0]
    assert kept == 1, "被拒绝的合并不该动数据"
    assert aliases == 0, "被拒绝的合并不该留下别名"
    assert store.canonical_user(ALICE_ALT) == ALICE_ALT


@with_tmp
def test_merge_is_refused_twice_and_same_id(tmp: str) -> None:
    """重复合并、以及把号并给自己，都要被挡住。"""

    store = make_store(tmp)
    store.merge_alias(ALICE_ALT, ALICE, note="第一次")
    for bad in ((ALICE_ALT, ALICE), (ALICE, ALICE), (BOB, BOB)):
        try:
            store.merge_alias(*bad)
        except MemoryValidationError:
            pass
        else:
            raise AssertionError(f"{bad} 应当被拒绝")


@with_tmp
def test_drop_profile_archives_instead_of_destroying(tmp: str) -> None:
    """清画像走归档：活跃那条没了，但 archive 里留得住。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE_ALT, key="profile", kind="profile",
             content="兔子狗可爱喵，自称乐乐", scope="user_global", evidence="e1")
    assert store.profile(ALICE_ALT) == "兔子狗可爱喵，自称乐乐"

    summary = store.merge_alias(ALICE_ALT, ALICE, note="同一个人", drop_profile=True)
    assert len(summary["dropped_profiles"]) == 1
    assert store.profile(ALICE) == "", "画像应当被清掉"
    with store.connection() as db:
        archived = db.execute(
            "SELECT content,delete_reason FROM archive WHERE normalized_key=?",
            ("profile",)).fetchone()
    assert archived is not None, "删画像必须先归档"
    assert archived[0] == "兔子狗可爱喵，自称乐乐"
    assert archived[1] == "operator_merge"


@with_tmp
def test_profile_record_follows_the_alias(tmp: str) -> None:
    """画像按人算：拿旧号查，也该查得到规范号那一份。"""

    store = make_store(tmp)
    add_fact(store, user=ALICE, key="profile", kind="profile",
             content="规范号的画像", scope="user_global", evidence="e1")
    store.merge_alias(ALICE_ALT, ALICE, note="同一个人")
    assert store.profile(ALICE_ALT) == "规范号的画像"
    record = store.profile_record(ALICE_ALT)
    assert record is not None and record.content == "规范号的画像"


@with_tmp
def test_affinity_and_roster_use_canonical_id(tmp: str) -> None:
    """关系档位按人算：从旧号加亲近，规范号那边读得到。"""

    store = make_store(tmp)
    store.merge_alias(ALICE_ALT, ALICE, note="同一个人")
    assert store.apply_affinity(ALICE_ALT, closeness=2, reason="聊得来", source="test") == ""
    assert store.relationship(ALICE) == store.relationship(ALICE_ALT)
    assert store.relationship(ALICE)[0] > 0


@with_tmp
def test_claim_reports_canonical_user(tmp: str) -> None:
    """换号说话时，整理 agent 看到的是规范号（否则会把一个人整理成两份）。"""

    store = make_store(tmp)
    store.merge_alias(ALICE_ALT, ALICE, note="同一个人")
    store.append(InboxEvent("e9", GROUP, ALICE_ALT, "user", "我今天买饼干了", time.time()))
    batch = store.claim({GROUP}, lease_seconds=60)
    assert batch is not None
    assert batch.user_id == ALICE, f"批次该挂在规范号上，实际 {batch.user_id}"
    # 认领到的仍然是**旧号那条消息**：别把号换了就找不到自己的话。
    assert [e.id for e in batch.events] == ["e9"]


@with_tmp
def test_alias_table_survives_schema_upgrade(tmp: str) -> None:
    """v5 库升到 v6：别名表建出来，且既有数据不动。"""

    path = Path(tmp) / "memory.sqlite3"
    store = MemoryStore(path)
    add_fact(store, user=ALICE, key="likes_cookies", content="喜欢奶味曲奇")
    with store.connection() as db:
        db.execute("DROP TABLE person_aliases")
        db.execute("PRAGMA user_version=5")

    again = MemoryStore(path)
    assert again.canonical_user(ALICE) == ALICE
    with again.connection() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        rows = db.execute("SELECT COUNT(*) FROM memory_short").fetchone()[0]
    assert version == 6
    assert rows == 1, "升级不该动记忆"


def _main() -> int:
    """不需要 pytest 的小跑法：直接 python tests/test_memory_alias.py。

    测试本体是零参数的（装饰器自己建临时目录），这里挨个调就行。
    """

    tests = [fn for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except BaseException as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
