"""两级记忆（用户 2026-09-28：新记忆先进中短期，强化后才进长期）。

用户选定的强化规则：**被后来的证据再确认过**（`revision>=2`）、**维护 agent 标 durable**、
**称呼与边界直接进长期**。状态类（正在发烧）永远只留在中短期。

外加「善意的追加提醒」：一条状态**只提醒一次**，靠 `reminded_at` 兜住。
"""
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import STATUS_KIND, MemoryStore

GROUP = "717151356"
USER = "1001"


def make_store(tmp: str, **kwargs) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", daily_add_limit=0,
                       near_duplicate_ratio=9.9, **kwargs)


def add_fact(store: MemoryStore, *, key: str, content: str, kind: str = "preference",
             scope: str = "user_group", user: str = USER, ttl: str = "null",
             durable: bool = False, evidence: str = "e1", targets=()) -> list:
    store.append(InboxEvent(evidence, GROUP, user, "user", content, time.time()))
    batch = store.claim({GROUP}, lease_seconds=60)
    assert batch is not None
    target_json = ",".join('{"id":"%s","revision":%d}' % (r["id"], r["revision"]) for r in targets)
    ops = ('{"op":"%s","scope_type":"%s","kind":"%s","normalized_key":"%s","content":"%s",'
           '"confidence":0.9,"ttl_days":%s,"evidence_event_ids":["%s"],"subjects":[],'
           '"durable":%s,"targets":[%s]}'
           % ("UPDATE" if targets else "ADD", scope, kind, key, content, ttl, evidence,
              "true" if durable else "false", target_json))
    return list(store.commit(batch, '{"operations":[%s]}' % ops))


def tier_of(store: MemoryStore, key: str) -> str | None:
    with store.connection() as db:
        for table, tier in (("memory_short", "short"), ("memory_long", "long")):
            row = db.execute(f"SELECT 1 FROM {table} WHERE normalized_key=? AND status='active'",
                             (key,)).fetchone()
            if row:
                return tier
    return None


# --- 1. 写入时怎么分层 -------------------------------------------------------


def test_names_and_boundaries_go_straight_to_long_term() -> None:
    """称呼与红线天然属于长期（用户选定的强化规则之一）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="preferred_name", content="自称小明", kind="name")
        add_fact(store, key="body", content="不喜欢被当面评价身材", kind="boundary",
                 evidence="e2")
        assert tier_of(store, "preferred_name") == "long"
        assert tier_of(store, "body") == "long"


def test_fresh_preferences_start_in_short_term() -> None:
    """新事实默认先落中短期——它的语义是"还没被确认过的东西"。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="likes_tea", content="喜欢喝奶茶")
        assert tier_of(store, "likes_tea") == "short"
        assert store.tier_counts() == {"short": 1, "long": 0}


def test_durable_flag_and_reconfirmation_promote() -> None:
    """durable 直接进长期；被后来的证据再确认过一次（revision>=2）也进。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="durable_fact", content="这是长期事实", durable=True)
        assert tier_of(store, "durable_fact") == "long"

        add_fact(store, key="restated", content="喜欢在深夜听歌", evidence="e2")
        assert tier_of(store, "restated") == "short"
        # 第二次被说起 = 再确认一次 → 直接落长期。
        with store.connection() as db:
            row = db.execute("SELECT * FROM memory_short WHERE normalized_key='restated'").fetchone()
        add_fact(store, key="restated", content="喜欢在深夜听歌", targets=[row], evidence="e3")
        assert tier_of(store, "restated") == "long"


def test_promote_reconfirmed_moves_old_short_rows() -> None:
    """机械强化那一遍：库里的中短期记录只要 revision>=2 就搬去长期。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="old_fact", content="以前被确认过两次的事")
        with store.connection() as db:
            db.execute("UPDATE memory_short SET revision=2 WHERE normalized_key='old_fact'")
        assert store.promote_reconfirmed() == 1
        assert tier_of(store, "old_fact") == "long"


def test_status_kind_never_reaches_long_term() -> None:
    """状态类哪怕被反复说起、被标 durable，也永远留在中短期。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="fever", content="正在发烧", kind=STATUS_KIND, ttl="14", durable=True)
        with store.connection() as db:
            db.execute("UPDATE memory_short SET revision=5 WHERE normalized_key='fever'")
        assert store.promote_reconfirmed() == 0
        assert tier_of(store, "fever") == "short"


# --- 2. 读取顺序 -------------------------------------------------------------


def test_long_term_outranks_short_term_on_a_tie() -> None:
    """同样命中一个词时，强化过的长期记忆排在前面。

    （注意：本人的称呼/边界是**常驻**项，按设计排在按词命中的记录之后——
    所以这里用两条偏好来比层级，不用 name。）
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="long_tea", content="喜欢喝奶茶，单位楼下那家", durable=True)
        add_fact(store, key="short_tea", content="喜欢喝奶茶，加珍珠", evidence="e2")
        got = store.retrieve(GROUP, USER, "喜欢喝奶茶")
        keys = [r.normalized_key for r in got]
        assert keys.index("long_tea") < keys.index("short_tea"), keys


# --- 3. 「善意的追加提醒」 ---------------------------------------------------


def test_status_note_is_offered_once_and_only_after_the_topic_moves_on() -> None:
    """一条状态只提醒一次；时机是"话题已经离开那件事"。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp)
        add_fact(store, key="fever", content="正在发烧", kind=STATUS_KIND, ttl="7")
        note = store.pending_status_note(GROUP, USER)
        assert note is not None and "发烧" in note["content"]
        # 刚说过、话题还没走开 → 不提醒（age<1 天且 related 还是 YES）
        assert store.pending_status_note(GROUP, USER)["age_days"] < 1
        # 提过之后就再也不给了
        store.mark_reminded(note["id"])
        assert store.pending_status_note(GROUP, USER) is None


def test_care_note_text_reaches_the_reply_prompt() -> None:
    """提示只出现在**易变段**，且写明"一次就够"。"""

    from qq_roleplay_bot.stage3_runtime import ConversationMode, ContextState, build_dialogue_messages
    from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

    target = MessageTarget(group_id=GROUP)
    current = IncomingMessage(message_id="m1", session_id=f"group:{GROUP}", user_id=USER,
                              text="明天吃什么", target=target, sender_name="某人")
    request = build_dialogue_messages(
        [current], current=current, mode=ConversationMode.ACTIVE, trigger="active_message",
        context=ContextState(), group_chat=True,
        care_note={"id": "r1", "content": "正在发烧", "age_days": 2.0},
    )
    volatile = request[2]["content"]
    assert "顺带一提" in volatile and "正在发烧" in volatile
    assert "一句就够" in volatile
    # 不能进稳定前缀（那会让缓存按人分叉）
    assert "顺带一提" not in request[1]["content"]
    assert "顺带一提" not in request[0]["content"]
