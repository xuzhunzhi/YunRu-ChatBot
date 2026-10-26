"""关系变动的**证据句**（`relationship_evidence`，2026-10-16）。

由来：关系流水从前只存一句概括理由（旧版判定写的固定说法），事后回答不了
"到底哪句话触发的"。现在每次关系变动带一句原话，存进一张**旁表**。

**2026-10-24 拆权后，这张表只由维护 agent（海马体）那条路写**：用户口径
*"判定没有任何写权限，判定只能调起回复"*——判定报 UP 只挂内存缓存，
`relationship_evidence` 一行都不落。判定侧仍会核对自己抄回的原话
（`_verify_guard_quote`），但那只用来计数/记日志，不再落库；维护侧
（`MemoryStore.apply_affinity(evidence=...)`）的证据链一个字没动。

这一份钉住五件事（每一条都对应一种"看起来做了、其实没做到"的失败）：

1. **判定这条路不落证据、维护那条路照旧落**：判定报 UP 后旁表为空、关系轴不动；
   维护 `apply_affinity` 的逐字校验与两级脱敏照旧；
2. **两级脱敏不是照搬**：命中 `broad_terms` 的**片段抹除**（"这jb机器人" → "这jb〔略〕"），
   命中机制类七条的**整句不存**（只留规则码 + 长度 + 时间）；
3. **保留期 30 天、和流水（365 天）分开**；
4. **只在私聊显示**：群里敲 `/super affinity evidence` 一个字都不给；
5. **老库打开不报错、老行不回填**（v6 → v7）。

另外钉住"它进不了别的通道"：不进货真价实的写信白名单（`memory_readonly.CHANGE_FIELDS`）、
不进回复 prompt。
"""
import asyncio
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot import memory_readonly
from qq_roleplay_bot.memory_store import (
    DAY, GUARD_RAISE_MAX, SCHEMA_VERSION, MemoryStore)
from qq_roleplay_bot.relationship_tiers import guard_tier
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
USER = "900000001"
ADMIN = "999"
NOW = 1_700_000_000.0
TARGET = MessageTarget(group_id=GROUP)


# --- 最小可用的引擎 -----------------------------------------------------------


class NeverCalled:
    async def complete(self, request):
        raise AssertionError("这条路径不该调用模型")


class ScriptedReply:
    def __init__(self, output="<reply>嗯。</reply>") -> None:
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class ScriptedJudge:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return self.output


class _Service:
    """引擎只用到 `.store` 与 identity 那几样；这些用例不关心记忆材料。"""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def capture(self, message) -> None:
        return None

    async def retrieve(self, message, topic, mentioned=()):
        from qq_roleplay_bot.memory_model import MemoryMaterial

        return MemoryMaterial()

    async def identity_for(self, message):
        from qq_roleplay_bot.memory_model import MemoryMaterial

        return MemoryMaterial()

    async def care_note(self, message, *, topic_shifted, current_terms=()):
        return None

    async def profile_for(self, message) -> str:
        return ""

    async def mark_care_noted(self, record_id):
        return None

    async def own_notes(self, message, *, limit: int = 2):
        return ()

    async def mark_own_note_noted(self, record_id):
        return None

    def snapshot(self) -> dict:
        return {"queued_events": 0}


def _store(tmp: str, *, clock=None) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", clock=clock or (lambda: NOW))


def _engine(store: MemoryStore, *, judge_output: str, reply=None):
    engine = DialogueEngine(
        reply or ScriptedReply(), judge_client=ScriptedJudge(judge_output),
        super_admin_user_ids=frozenset({ADMIN}),
    )
    engine.memory_service = _Service(store)
    engine.clock = lambda: NOW
    return engine


def _msg(index: int, text: str = "在的", *, mentioned: bool = True) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=USER,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


def guard_up(quote: str) -> str:
    return (
        "<route>REPLY</route><topic>骂人</topic><related>YES</related>"
        "<guard>UP</guard><guard_reason>insult</guard_reason>"
        f"<guard_quote>{quote}</guard_quote>"
    )


def _quoted_text(request) -> str:
    """从**真正发出去的那一份请求**里抠出当前这条的正文（当作"原话"）。

    不手写那串字：判定的渲染会转义、去 @，手写的字面量未必与它逐字相同，
    那样测出来的就不是"校验通过"，而是"我的字面量刚好撞上了"。
    """

    user = request[-1]["content"]
    body = user.split("<current_message>")[1].split("</current_message>")[0]
    return body.split("<text>")[1].split("</text>")[0].strip()


def _evidence(store: MemoryStore, user_id: str = USER) -> tuple[dict, ...]:
    return store.relationship_evidence(user_id, limit=10)


# --- 1. 判定不落证据；维护那条路照旧落 ----------------------------------------


def test_the_judge_path_stores_no_evidence_but_maintenance_still_does() -> None:
    """判定报 UP：旁表**一行都不写**；维护那条真路照旧逐字存证。

    2026-10-24 用户口径：*"判定没有任何写权限，判定只能调起回复"*、
    *"人物画像、记忆、世界知识属于海马体"*。所以这里先钉判定这条路（原话核得过
    也不落库、关系轴不动），再钉维护那条路没被这次拆权弄坏（逐字校验/截断/定位
    都还在 `MemoryStore.apply_affinity(evidence=...)` 里）。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        judge = ScriptedJudge(guard_up("占位"))
        engine = _engine(store, judge_output="占位")
        engine.judge_client = judge
        # 先跑一轮"不报 UP"的，只为拿到**真实渲染出来的正文**。
        judge.output = "<route>REPLY</route><related>YES</related><guard>HOLD</guard>"
        asyncio.run(engine.handle(_msg(1, "@YunRu 你就是个没用的废物")))
        quote = _quoted_text(judge.requests[0])
        assert quote, "渲染里该有正文"

        judge.output = guard_up(quote)
        asyncio.run(engine.handle(_msg(2, "@YunRu 你就是个没用的废物")))

        assert _evidence(store) == (), "判定这条路一个证据行都不许写"
        assert store.relationship(USER) == (10, 0), "判定这条路连关系轴都不许写"
        assert store.recent_relationship_changes(0.0, limit=10) == ()
        assert engine.snapshot().evidence_stored == 0
        assert engine.snapshot().quote_unverified == 0, "原话核得过，只是不落库"
        assert engine.snapshot().guard_raised == 1, "冷感照旧挂在缓存里"

        # 维护那条真路：证据链不许动（source 不是 judge，`may_lower_guard` 不参与）。
        assert store.apply_affinity(
            USER, guardedness=GUARD_RAISE_MAX, reason="他当面贬损羞辱",
            source="maintenance", evidence={"quote": quote, "who_alias": "1", "seq": 5},
        ) == ""
        rows = _evidence(store)
        assert len(rows) == 1, rows
        assert rows[0]["quote"] == quote
        assert rows[0]["verify"] == "verbatim"
        assert rows[0]["rule"] == ""
        assert int(rows[0]["quote_chars"]) == len(quote)
        assert int(rows[0]["quote_truncated"]) == 0
        assert int(rows[0]["seq"]) == 5
        assert rows[0]["who_alias"] == "1"


def test_a_quote_that_is_not_in_the_input_is_counted_but_stores_nothing() -> None:
    """模型编一句/改写一句 → 不落证据、只加计数；冷感照旧（判定本来就不写库）。

    2026-10-20 用户口径"拿不到原话就拒绝这次写入"针对的是**关系轴写入**；
    2026-10-24 拆权后判定没有写入可拒（用户原话：*"判定没有任何写权限，
    判定只能调起回复"*）。所以这里钉的是：核不过的原话不落旁表、`quote_unverified`
    计数 +1，而这一轮的冷感（内存缓存）与关系轴（一个字不写）各自照旧。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        engine = _engine(store, judge_output=guard_up("你这人向来就是这样，没救了"))
        asyncio.run(engine.handle(_msg(1, "@YunRu 你就是个没用的废物")))

        assert _evidence(store) == (), "核不过的句子一句都不许进库"
        snapshot = engine.snapshot()
        assert snapshot.quote_unverified == 1
        assert snapshot.evidence_stored == 0
        assert snapshot.guard_raised == 1, "判定只挂缓存，不涉及'这次写入'"
        assert snapshot.guard_rejected == 0
        assert store.relationship(USER)[1] == 0, "判定一个字都不写"
        assert store.recent_relationship_changes(0.0, limit=10) == ()


def test_a_hold_never_stores_a_quote_even_if_the_model_fills_one() -> None:
    """`HOLD` 时模型学舌填了这一段：清空，不落库。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        engine = _engine(
            store,
            judge_output=(
                "<route>REPLY</route><related>YES</related>"
                "<guard>HOLD</guard><guard_quote>你就是个没用的废物</guard_quote>"
            ),
        )
        asyncio.run(engine.handle(_msg(1, "@YunRu 你就是个没用的废物")))
        assert _evidence(store) == ()


# --- 2. 两级脱敏 --------------------------------------------------------------


def test_broad_terms_are_masked_not_dropped() -> None:
    """实测反例：`这jb机器人` 命中 `broad_terms`，照搬 reason 那套会整句被抹。

    改后应当存成 `这jb〔略〕`——抹掉的是命中的那个词，不是整句。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.apply_affinity(
            USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
            evidence={"quote": "这jb机器人", "who_alias": "4", "seq": 12},
        )
        rows = _evidence(store)
        assert len(rows) == 1
        assert rows[0]["quote"] == "这jb〔略〕"
        assert rows[0]["rule"] == "broad_terms"
        assert rows[0]["verify"] == "verbatim_masked"
        assert int(rows[0]["quote_chars"]) == len("这jb机器人"), "原长要留住"
        assert int(rows[0]["seq"]) == 12


def test_masking_too_short_drops_the_whole_sentence() -> None:
    """抹完不足 6 个字：整句不存（剩下的碎片说明不了"哪句话"）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.apply_affinity(
            USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
            evidence={"quote": "你这bot", "who_alias": "4", "seq": 3},
        )
        rows = _evidence(store)
        assert rows and rows[0]["quote"] == ""
        assert rows[0]["rule"] == "broad_terms"
        assert rows[0]["verify"] == "dropped"


def test_system_self_quotes_are_dropped_whole() -> None:
    """讲这个系统自己怎么来的：**整句不存**，只留规则码 + 长度 + 时间。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.apply_affinity(
            USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
            evidence={"quote": "认得。做云茹的那个。", "who_alias": "4", "seq": 7},
        )
        rows = _evidence(store)
        assert rows and rows[0]["quote"] == "", "机制类句子一个片段都不许留"
        assert rows[0]["rule"] == "maker_claim"
        assert rows[0]["verify"] == "dropped"
        assert int(rows[0]["quote_chars"]) == len("认得。做云茹的那个。")
        assert float(rows[0]["occurred_at"]) == NOW, "时间要留着"


def test_long_quotes_keep_120_chars_and_the_original_length() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        long_text = "你" * 300
        store.apply_affinity(
            USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
            evidence={"quote": long_text, "who_alias": "4", "seq": 9},
        )
        rows = _evidence(store)
        saved = str(rows[0]["quote"])
        assert saved.endswith("…")
        assert len(saved) == 121, "前 120 + 一个省略号"
        assert int(rows[0]["quote_truncated"]) == 1
        assert int(rows[0]["quote_chars"]) == 300, "原长不许按截断后的算"


# --- 3. 保留期：30 天，和流水（365 天）分开 -----------------------------------


def test_evidence_expires_after_30_days_while_the_log_keeps_365() -> None:
    """同一次变动留下的**证据** 30 天就清，**流水**照旧按 365 天留着。"""

    clock = [NOW - 40 * DAY]
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, clock=lambda: clock[0])
        store.apply_affinity(
            USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
            evidence={"quote": "总算说人话了", "who_alias": "4", "seq": 5},
        )
        assert len(_evidence(store)) == 1

        clock[0] = NOW
        store.cleanup()

        assert _evidence(store) == (), "40 天前的证据该被清掉"
        assert store.recent_relationship_changes(0.0, limit=10), "流水按 365 天留着，不该动"
        assert store.pruned["relationship_evidence"] == 1
        assert store.counts()["pruned_relationship_evidence"] == 1


def test_recent_evidence_is_never_pruned() -> None:
    clock = [NOW - 29 * DAY]
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp, clock=lambda: clock[0])
        store.apply_affinity(
            USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
            evidence={"quote": "总算说人话了", "who_alias": "4", "seq": 5},
        )
        clock[0] = NOW
        store.cleanup()
        assert len(_evidence(store)) == 1, "近期一行不动"


def test_the_evidence_retention_is_thirty_days_and_configurable() -> None:
    from qq_roleplay_bot import dev_config

    with patch.dict(os.environ):
        os.environ.pop("QQBOT_RELATIONSHIP_EVIDENCE_RETENTION_DAYS", None)
        assert dev_config.relationship_evidence_retention_days() == 30
        assert dev_config.relationship_log_retention_days() == 365, "两条期限不是同一个"
    for text, expected in (("10", 10), ("1", 7), ("99999", 365), ("不是数", 30)):
        with patch.dict(os.environ, {"QQBOT_RELATIONSHIP_EVIDENCE_RETENTION_DAYS": text}):
            assert dev_config.relationship_evidence_retention_days() == expected, text


# --- 4. 可见性：只在私聊 ------------------------------------------------------


def _super_msg(text: str, *, group: bool = True) -> IncomingMessage:
    target = TARGET if group else MessageTarget(user_id=ADMIN)
    return IncomingMessage(
        message_id=f"s:{text}", session_id=f"group:{GROUP}" if group else f"private:{ADMIN}",
        user_id=ADMIN, text=text, target=target, mentioned_user_ids=(USER,),
    )


def _prepared_store(tmp: str) -> MemoryStore:
    store = _store(tmp)
    store.apply_affinity(
        USER, guardedness=8, reason="他当面贬损羞辱", source="judge",
        evidence={"quote": "总算说人话了", "who_alias": "4", "seq": 5},
    )
    return store


def test_the_group_command_never_shows_the_evidence() -> None:
    """群里敲 `/super affinity ... evidence`：**一个字都不给**。

    证据句是别人的原话（往往正是那句难听的），当众回一遍等于再发一次给全群看。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = _prepared_store(tmp)
        engine = _engine(store, judge_output="<guard>HOLD</guard>")
        reply = asyncio.run(engine.handle(_super_msg("/super affinity evidence", group=True)))
        assert reply is not None
        assert "总算说人话了" not in reply.text, "群里不许出现证据句"
        assert "私聊" in reply.text, "要说清去哪儿看"


def test_the_private_command_shows_the_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = _prepared_store(tmp)
        engine = _engine(store, judge_output="<guard>HOLD</guard>")
        reply = asyncio.run(engine.handle(_super_msg("/super affinity evidence", group=False)))
        assert reply is not None
        assert "总算说人话了" in reply.text
        assert "第 5 条" in reply.text, "定位信息要一起给"


def test_the_plain_affinity_command_still_works_in_a_group() -> None:
    """私聊闸只加在 `evidence` 那一支；原来的"看档位"不许被这次改动弄坏。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = _prepared_store(tmp)
        engine = _engine(store, judge_output="<guard>HOLD</guard>")
        reply = asyncio.run(engine.handle(_super_msg("/super affinity", group=True)))
        assert reply is not None and "防备" in reply.text
        assert "总算说人话了" not in reply.text, "证据句不进普通那一支"


# --- 5. 迁移：v6 老库打开不报错、老行不回填 ------------------------------------


def test_a_v6_database_opens_and_gets_the_table_without_backfill() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "memory.sqlite3"
        first = MemoryStore(path, clock=lambda: NOW)
        first.apply_affinity(USER, closeness=8, reason="聊得来", source="test",
                             evidence={"quote": "聊得来", "who_alias": "1", "seq": 1})
        assert SCHEMA_VERSION == 7

        # 造一个"真的是 v6"的库：把新表拆掉、版本号退回 6，再存一行老流水。
        # 连接必须**关掉**再交给下一个实例（Windows 上不关就删不掉临时目录）。
        with closing(sqlite3.connect(path)) as raw:
            raw.execute("DROP TABLE relationship_evidence")
            raw.execute("PRAGMA user_version=6")
            raw.execute(
                "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                (USER, "closeness", 1, 2, "老依据", "judge", NOW - 10 * DAY),
            )
            raw.commit()

        reopened = MemoryStore(path, clock=lambda: NOW)
        assert reopened.counts()["records"] >= 0, "打开不报错、查询照常"
        with closing(sqlite3.connect(path)) as raw:
            version = raw.execute("PRAGMA user_version").fetchone()[0]
            count = raw.execute("SELECT COUNT(*) FROM relationship_evidence").fetchone()[0]
            rows = raw.execute(
                "SELECT user_id,axis,before_value,after_value,reason "
                "FROM relationship_log ORDER BY rowid").fetchall()
        assert version == SCHEMA_VERSION
        assert count == 0, "老行不许回填——编一条出来等于伪造当时那句话"
        assert [row[4] for row in rows] == ["聊得来", "老依据"], "老流水一行不动"


# --- 6. 它进不了别的通道 ------------------------------------------------------


def test_evidence_never_reaches_the_letter_side() -> None:
    """写信/日报那条只读白名单里**没有它**：不在 `CHANGE_FIELDS`，接缝上也没有入口。"""

    assert "relationship_evidence" not in memory_readonly.CHANGE_FIELDS
    assert memory_readonly.CHANGE_FIELDS == (
        "user_id", "axis", "before_value", "after_value", "reason", "occurred_at")
    assert not hasattr(memory_readonly.MemoryRead, "evidence")
    rows = memory_readonly.changes(0.0, db_path=Path(tempfile.gettempdir()) / "no_such.sqlite3")
    assert rows == (), "查不到库时返回空，不抛"


def _reply_prompt_blob(store: MemoryStore, judge_output: str, text: str) -> str:
    """跑一轮，把她真正看到的那一份材料拼成一个串。

    两处与本次要验的事无关、而且**每轮都不一样**，先抹掉再比：
    "现在是什么时候"那一行按分钟走；不可信数据的记号（`‹…›`）是每季/每轮现取的。
    """

    reply = ScriptedReply()
    engine = _engine(store, judge_output=judge_output, reply=reply)
    asyncio.run(engine.handle(_msg(1, text)))
    lines = []
    for request in reply.requests:
        for part in request:
            lines.extend(str(part.get("content") or "").splitlines())
    kept = (line for line in lines if not line.startswith("现在是什么时候"))
    return re.sub(r"‹[^›]{1,12}›", "", "\n".join(kept))


def test_evidence_never_reaches_her_reply_prompt() -> None:
    """她只该知道"现在该收着"，不该看到"哪句话让我记下了"。

    2026-10-24 拆权后，证据只由维护那条路写（判定不落库）。判据仍是**差分**：
    同一段对话跑两遍，一遍库里已经有维护写下的证据（防备 +GUARD_RAISE_MAX），
    一遍没有证据、防备是默认值。

    2026-10-20 起 0 与 GUARD_RAISE_MAX **不再同档**（「正常分寸」vs「有点端着」），
    两份回复材料的档位文字本来就不同。所以先把那一行按**实际档位文字**
    （`guard_tier(0).text` / `guard_tier(GUARD_RAISE_MAX).text`）抹平，其余部分
    必须**逐字相同**；两边除了用户这条消息本身，也都不该出现证据原话——落库的
    证词若被拼进材料，那里就会多出一份。这条同时钉住"回复侧看不到数值"
    （否则两边的数字会不同）。
    """

    text = "@YunRu 你就是个没用的废话"
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        # 先拿"真实渲染出来的正文"当原话（不手写字面量）。
        probe = _engine(store, judge_output=(
            "<route>REPLY</route><related>YES</related><guard>HOLD</guard>"))
        asyncio.run(probe.handle(_msg(1, text)))
        quote = _quoted_text(probe.judge_client.requests[0])

    with tempfile.TemporaryDirectory() as stored_tmp, tempfile.TemporaryDirectory() as plain_tmp:
        stored = _store(stored_tmp)
        # 证据走**维护那条路**（判定那条路已经不写了，见上一条用例）。
        assert stored.apply_affinity(
            USER, guardedness=GUARD_RAISE_MAX, reason="他当面贬损羞辱",
            source="maintenance", evidence={"quote": quote, "who_alias": "1", "seq": 5},
        ) == ""
        with_text = _reply_prompt_blob(
            stored, "<route>REPLY</route><related>YES</related><guard>HOLD</guard>", text)
        assert len(_evidence(stored)) == 1, "这一步得真的存下了证据"
        assert stored.relationship(USER)[1] == GUARD_RAISE_MAX

        plain = _store(plain_tmp)
        without = _reply_prompt_blob(
            plain, "<route>REPLY</route><related>YES</related><guard>HOLD</guard>", text)
        assert _evidence(plain) == (), "这一轮不该有证据"
        assert plain.relationship(USER)[1] == 0

        with_tier = guard_tier(GUARD_RAISE_MAX)
        plain_tier = guard_tier(0)
        assert with_tier.text != plain_tier.text, "0 与 GUARD_RAISE_MAX 本该不同档"
        assert f"防备：{with_tier.name}——{with_tier.text}" in with_text
        assert f"防备：{plain_tier.name}——{plain_tier.text}" in without

        # 原话只来自用户这条消息；把它从材料里剔掉之后，哪儿都不该再有它。
        assert quote not in with_text.replace(text, "")
        assert quote not in without.replace(text, "")

        def _blank_guard(blob: str, tier) -> str:
            return blob.replace(f"防备：{tier.name}——{tier.text}", "防备：<档位>")

        assert _blank_guard(with_text, with_tier) == _blank_guard(without, plain_tier), (
            "证据句（或它的数值）漏进了她的 prompt")

