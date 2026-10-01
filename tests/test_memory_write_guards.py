"""长期记忆的写入兜底：近重复不新增、每群每日新增有上限。

由来（实测）：维护 agent 一天写了 80 条，其中"装 ITX 机器"一件事有 4 条、
"不喜欢云服务器"有 3 条、10 条是"本群讨论了…"式聊天回顾。
根因是 prompt 缺门槛 + 跨轮重复只靠 normalized_key 拦（换个措辞就能再写一条），
所以这里在**存储层**加机械限制：不依赖模型自觉。

注意：离线测试入口不用 pytest fixture，临时目录在测试内部自己建。
"""
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore

GROUP = "717151356"
USER = "1001"


def event(event_id: str = "e1", text: str = "我想装个 ITX 机器", group: str = GROUP,
          user: str = USER, speaker: str = "user") -> InboxEvent:
    return InboxEvent(id=event_id, group_id=group, user_id=user, speaker=speaker,
                      text=text, occurred_at=time.time())


def make_store(tmp: str, **kwargs) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", **kwargs)


def add_batch(store: MemoryStore, *, content: str, key: str, kind: str = "preference",
              scope: str = "user_group", evidence=("e1",), group: str = GROUP,
              user: str = USER, ttl_days: str = "null") -> list:
    """走一遍真实的 claim → commit 路径（比直接写 SQL 更接近线上）。"""

    for eid in evidence:
        store.append(event(eid, group=group, user=user))
    batch = store.claim({group}, lease_seconds=60)
    assert batch is not None, "事件应当能被认领成一批"
    raw = (
        '{"operations":[{"op":"ADD","scope_type":"%s","kind":"%s","normalized_key":"%s",'
        '"content":"%s","confidence":0.8,"ttl_days":%s,"evidence_event_ids":["%s"],'
        '"targets":[]}]}' % (scope, kind, key, content, ttl_days, evidence[0])
    )
    return list(store.commit(batch, raw))


def active_contents(store: MemoryStore) -> list[str]:
    with store.connection() as db:
        return [row["content"] for row in db.execute(
            "SELECT content FROM memory_short WHERE status='active' UNION ALL SELECT content FROM memory_long WHERE status='active' ORDER BY 1")]


def audit_ops(store: MemoryStore) -> list[str]:
    with store.connection() as db:
        return [row["op"] for row in db.execute("SELECT op FROM audit ORDER BY rowid")]


def test_near_duplicate_content_is_not_added_again() -> None:
    """同一个 scope 里换个措辞说同一件事 → 不新增。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        first = add_batch(store, content="想装一台开放式机架的主机，预算有限",
                          key="wants_itx_build")
        assert [op.op for op in first] == ["ADD"]

        again = add_batch(store, content="打算装开放式机架的台式机，预算有限，三十号发工资再定",
                          key="itx_plan_2026", evidence=("e2",))
        assert again == [], "近重复不该被当成新事实"
        assert any("near_duplicate" in op for op in audit_ops(store)), "要留下拒绝记录"
        assert len(active_contents(store)) == 1


def test_genuinely_different_facts_are_still_accepted() -> None:
    """机械守卫不能把"不同的事"也拦掉——阈值 0.4 是量出来的（校准见 memory_store 注释）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_batch(store, content="喜欢直接、有来有回的风格", key="likes_blunt_style")
        add_batch(store, content="不吃香菜，闻到就皱眉", key="dislikes_cilantro", evidence=("e2",))
        add_batch(store, content="下周要去杭州出差三天", key="trip_hangzhou", evidence=("e3",))
        # 相关但不同：都在深夜话题里，但不是同一件事
        add_batch(store, content="深夜喜欢听歌", key="likes_music_at_night", evidence=("e4",))
        assert len(active_contents(store)) == 4


def test_daily_add_limit_stops_new_facts_but_allows_updates() -> None:
    """每日新增到顶之后：新事实被拒，改已有事实照旧。"""

    with tempfile.TemporaryDirectory() as tmp:
        # 这一条只测配额，把近重复守卫关掉（阈值设成不可能达到）
        store = make_store(tmp, daily_add_limit=3, near_duplicate_ratio=9.9)
        facts = [
            "不吃香菜，闻到就皱眉",
            "下周要去杭州出差三天",
            "最怕打雷，会戴耳机睡觉",
        ]
        for index, text in enumerate(facts):
            add_batch(store, content=text, key=f"fact_{index}", evidence=(f"e{index}",))
        rejected = add_batch(store, content="养了一只叫团子的橘猫", key="fact_cat",
                             evidence=("e9",))
        assert rejected == [], "配额用完之后不许再长新事实"
        assert any("daily_limit" in op for op in audit_ops(store))

        with store.connection() as db:
            row = db.execute("SELECT id, revision FROM memory_short WHERE status='active' LIMIT 1").fetchone()
        store.append(event("e10"))
        batch = store.claim({GROUP}, lease_seconds=60)
        raw = (
            '{"operations":[{"op":"UPDATE","scope_type":"user_group","kind":"preference",'
            '"normalized_key":"fact_0","content":"不吃香菜，闻到就皱眉；改成清水煮也不吃",'
            '"confidence":0.9,"ttl_days":null,'
            '"evidence_event_ids":["e10"],"targets":[{"id":"%s","revision":%d}]}]}'
            % (row["id"], row["revision"])
        )
        ops = list(store.commit(batch, raw))
        assert [op.op for op in ops] == ["UPDATE"], "配额只管新增，不该挡住改已有事实"


def test_near_duplicate_guard_only_compares_within_the_same_scope() -> None:
    """不同 scope 里的同一句话互不影响（按人和群隔离，不能互相顶掉）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_batch(store, content="喜欢在深夜聊天", key="likes_late_night", scope="user_group")
        add_batch(store, content="喜欢在深夜聊天", key="likes_late_night",
                  scope="user_global", evidence=("e2",))
        assert len(active_contents(store)) == 2, "全局与群内是两条独立记录（检索时群内会盖住全局）"


def expiry_days(store: MemoryStore, key: str) -> float | None:
    """这条记忆还有多少天过期（两层都找：v5 起 name/boundary 直接进长期表）。"""

    with store.connection() as db:
        for table in ("memory_short", "memory_long"):
            row = db.execute(
                f"SELECT expires_at FROM {table} WHERE normalized_key=? AND status='active'",
                (key,)).fetchone()
            if row is not None:
                return None if row["expires_at"] is None else (row["expires_at"] - store.clock()) / 86400
    return None


def test_self_reference_is_refused_at_write_time() -> None:
    """关于"这个系统自身"的内容不许落库——这是机械兜底，不指望模型自觉。

    由来（真库体检）：维护 prompt 里明令不写，但线上仍活着 6 条：
    "看机器人反驳""本人确认该条在 prompt 中被列为不愿提及内容""修机器人上下文问题"
    "测试过用显示替换把云茹的名字改成谭雅"。这类记忆每轮被检索回来，等于反复提醒
    她自己是程序（AGENTS.md §2.2），所以改成存储层直接拒。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        denied = [
            "常抛荒诞历史梗看机器人反驳，被驳后表示满意",
            "本人确认该条在 prompt 中被列为不愿提及内容",
            "修机器人上下文问题，打算明天起床再弄",
            "测试过用显示替换把云茹的名字改成谭雅，她坚持不参与",
            "想装 ITX 机器跑本地小模型",
        ]
        for index, content in enumerate(denied):
            assert add_batch(store, content=content, key=f"denied_{index}",
                             evidence=(f"e{index}",)) == [], f"应当拒绝：{content}"
        assert active_contents(store) == []
        assert sum(1 for op in audit_ops(store) if op == "rejected_self_reference") == len(denied)

        # 同一道闸也要挡住"改已有记忆时把这类内容写进去"。
        add_batch(store, content="喜欢喝奶茶（超大杯）", key="drink", evidence=("ok1",))
        with store.connection() as db:
            row = db.execute("SELECT id, revision FROM memory_short WHERE status='active'").fetchone()
        store.append(event("e9"))
        batch = store.claim({GROUP}, lease_seconds=60)
        raw = (
            '{"operations":[{"op":"UPDATE","scope_type":"user_group","kind":"preference",'
            '"normalized_key":"drink","content":"喜欢喝奶茶；另外她其实是个程序",'
            '"confidence":0.9,"ttl_days":null,"evidence_event_ids":["e9"],'
            '"targets":[{"id":"%s","revision":%d}]}]}' % (row["id"], row["revision"])
        )
        assert list(store.commit(batch, raw)) == []
        assert active_contents(store) == ["喜欢喝奶茶（超大杯）"]


def test_clamp_ttls_shortens_history_but_never_extends() -> None:
    """已有记录补夹一次 TTL：只缩短、不延长，且可以重复跑。

    由来：`TTL_CAP_DAYS` 是体检之后才有的策略，历史记录带着模型随手填的值
    （真库 7/44 条超标，最长 +3648 天）。第一版夹法把基准算成 updated_at 后又直接写回，
    顺手把 6 条记录的寿命往后推了"创建到最近更新"的那段距离——策略收口不该放水，
    所以现在取 min(原值, 目标值)。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, daily_add_limit=0, near_duplicate_ratio=9.9)
        add_batch(store, content="喜欢喝奶茶（超大杯）", key="long", ttl_days="3650")
        add_batch(store, content="最近觉得消费有点高", key="state", ttl_days="null",
                  evidence=("e1b",))
        add_batch(store, content="不吃香菜，闻到就皱眉", key="short", ttl_days="10",
                  evidence=("e2",))
        # v5 分层：普通偏好在**中短期**，所以 3650 天被中短期的 14 天夹住；
        # "处境"那类语气的（最近…）在写入时就被压到短期上限；更短的照旧。
        assert abs(expiry_days(store, "long") - 14) < 1
        assert abs(expiry_days(store, "state") - 14) < 1
        assert abs(expiry_days(store, "short") - 10) < 1

        # 全部已经合规（写入时就被夹过）→ 补夹应当是空操作，而且绝不延长 10 天那条。
        assert store.clamp_ttls() == 0
        assert abs(expiry_days(store, "short") - 10) < 1

        # 模拟"被强化进长期"的历史遗留：把过期时间改成 10 年后再夹，应当按长期上限夹回 365 天。
        with store.connection() as db:
            row = db.execute("SELECT * FROM memory_short WHERE normalized_key='long'").fetchone()
            db.execute(
                "INSERT INTO memory_long (id,scope_type,scope_key,subject_user_id,kind,normalized_key,"
                "content,confidence,created_at,updated_at,expires_at,revision,source_event_ids,durable,"
                "promoted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], row["scope_type"], row["scope_key"], row["subject_user_id"], row["kind"],
                 row["normalized_key"], row["content"], row["confidence"], row["created_at"],
                 row["updated_at"], row["updated_at"] + 3650 * 86400, row["revision"],
                 row["source_event_ids"], 0, row["updated_at"]))
            db.execute("UPDATE memory_short SET status='superseded' WHERE id=?", (row["id"],))
        assert abs(expiry_days(store, "long") - 3650) < 1
        assert store.clamp_ttls() == 1
        assert abs(expiry_days(store, "long") - 365) < 1


def test_ttl_is_clamped_by_kind_and_by_transient_wording() -> None:
    """TTL 由模型随手填 → 存储层按 kind 与**层级**夹上限，并把"当时的处境"压成短期。

    由来（真库体检）：22 条带过期时间的活跃记忆从 +30 天铺到 +3648 天，
    而"最近觉得消费有点高""有工学椅挺好睡""在寝室喝酒倾向买瓶便宜威士忌"
    拿到了 1 年甚至 10 年；同样一件事，有人 null（永不过期）。

    v5 之后多一层：**中短期**默认 14 天（没被强化的东西不该按长期事实活着）；
    称呼/边界直接进长期（可以不过期）；状态类只在中短期且 ≤14 天。
    """

    with tempfile.TemporaryDirectory() as tmp:
        # 这条测的是 TTL 策略，不是配额与近重复：那两道闸单独有测试，这里关掉，
        # 否则这些内容相近的事实会被它们拦掉，测不到 TTL。
        store = make_store(tmp, daily_add_limit=0, near_duplicate_ratio=9.9)
        cases = [
            # (scope, kind, content, 模型填的 ttl, 期望天数)
            ("user_group", "preference", "喜欢喝奶茶（超大杯）", "3650", 14),   # 中短期封顶 14 天
            ("user_group", "preference", "常去那家面馆", "30", 14),
            ("user_group", "preference", "常去那家面馆", "7", 7),               # 更短的照旧
            ("user_group", "preference", "最近觉得消费有点高", "null", 14),
            ("user_group", "name", "自称乐乐", "null", None),                    # 长期：可以不过期
            ("user_group", "boundary", "不喜欢被当面评价身材", "3650", 3650),     # 长期：按 kind 上限
            ("user_group", "status", "正在发烧", "null", 7),                     # 状态：默认 7 天
            ("user_group", "status", "明天有考试", "365", 14),                   # 状态：最多 14 天
            ("group", "group_fact", "本群周末有线下聚会", "3650", 30),            # 群事实：中短期 30 天
        ]
        for index, (scope, kind, content, ttl, expected) in enumerate(cases):
            key = f"fact_{index}"
            add_batch(store, content=content, key=key, kind=kind, scope=scope, ttl_days=ttl,
                      evidence=(f"e{index}",))
            got = expiry_days(store, key)
            if expected is None:
                assert got is None, f"{content} 不该过期，实际 {got}"
            else:
                assert got is not None and abs(got - expected) < 1, \
                    f"{content}（模型填 {ttl}）应当是 {expected} 天，实际 {got}"
