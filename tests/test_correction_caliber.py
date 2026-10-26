"""带教式纠正被读成嫌弃：判定侧与维护侧的两道机械闸门（2026-10-24）。

事故：主人在群里纠正她"你不了解的事情为什么要插嘴呢""为啥你就是记不住数码话题
没把握就别插嘴呢"。画像里明明写着"他说话直……那不是瞧不起你，是在带你；话难听也
未必是嫌你"，但 18:29 维护把 closeness 44→40、20:52 判定报 `belittle` 并把
guardedness 0→10。这里钉两件事：

* 判定侧：有纠正式词、没有贬损词的原话，**不得**留在 `belittle`；
* 维护侧：画像有"在带你"的口径时，没有贬损原话的负向关系变动**不得**生效；
  同一批里的 ADD/UPDATE 不受影响。
"""
import asyncio
import json
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.affinity_caliber import profile_caliber
from qq_roleplay_bot.dialogue_judge import (
    build_judge_messages,
    compose_identity_note,
    parse_judge_output,
)
from qq_roleplay_bot.memory_maintenance_agent import (
    MEMORY_SYSTEM_PROMPT,
    MemoryMaintenanceAgent,
)
from qq_roleplay_bot.memory_model import (
    InboxEvent,
    MemoryMaterial,
    MemoryMetrics,
)
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
USER = "900000001"
TARGET = MessageTarget(group_id=GROUP)
PROFILE = ("他说话直，常常当面说你哪一句不对、让你别插话、让你换个说法。"
           "那不是瞧不起你，是在带你；话难听也未必是嫌你。")
CORRECTION = "为啥你就是记不住数码话题没把握就别插嘴呢"


def msg(index, text="@YunRu 在吗", *, mentioned=True) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id=USER,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


# --- A. 判定侧：belittle 的机械闸门 -------------------------------------------


def test_correction_quote_is_not_belittle() -> None:
    """事故原话：有"记不住/没把握/插嘴"这类纠正式词，且没有贬损词 → 必须降级。"""

    verdict = parse_judge_output(
        "<route>REPLY</route><guard>UP</guard>"
        "<guard_reason>belittle</guard_reason>"
        f"<guard_quote>{CORRECTION}</guard_quote>"
    )
    assert verdict.guard_up is False
    assert verdict.guard_reason == ""
    assert verdict.guard_quote == ""


def test_real_belittle_still_reaches_belittle() -> None:
    """明确的贬损（"就你这水平"）仍然可以是 belittle：闸门不能把真冒犯一起放走。"""

    verdict = parse_judge_output(
        "<route>REPLY</route><guard>UP</guard>"
        "<guard_reason>belittle</guard_reason>"
        "<guard_quote>就你这水平也配说话</guard_quote>"
    )
    assert verdict.guard_up is True
    assert verdict.guard_reason == "belittle"


def test_caliber_line_is_one_short_line() -> None:
    line = profile_caliber(PROFILE)
    assert line.startswith("他对你说直话时：")
    assert "不是瞧不起你" in line or "在带你" in line
    assert len(line) <= 60


def test_identity_gets_the_caliber_line_but_nothing_when_absent() -> None:
    identity = compose_identity_note("称呼：主人；边界：别碰手机", PROFILE)
    line = profile_caliber(PROFILE)
    assert line in identity
    request = build_judge_messages(
        [msg(1)], current=msg(2), trigger="mention", addressed=True,
        identity=identity,
    )
    content = request[1]["content"]
    assert line in content
    # 画像没有这条口径时，identity 一个字都不多。
    assert compose_identity_note("称呼：主人", "他喜欢猫。") == "称呼：主人"
    assert profile_caliber("") == ""


class _ScriptedJudge:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return "<route>REPLY</route><related>YES</related><guard>HOLD</guard>"


class _ScriptedReply:
    async def complete(self, request):
        return "<reply>嗯。</reply>"


class _Service:
    """最小记忆服务：引擎只用到 `.store` 与下面这几个方法。"""

    def __init__(self, store: MemoryStore, profile: str = "") -> None:
        self.store = store
        self._profile = profile

    def capture(self, message) -> None:
        return None

    async def retrieve(self, message, topic, mentioned=()):
        return MemoryMaterial()

    async def identity_for(self, message):
        return MemoryMaterial()

    async def profile_for(self, message) -> str:
        return self._profile

    async def own_notes(self, message, *, limit: int = 2):
        return ()

    async def care_note(self, message, *, topic_shifted, current_terms=()):
        return None

    async def mark_care_noted(self, record_id):
        return None

    async def mark_own_note_noted(self, record_id):
        return None

    def snapshot(self) -> dict:
        return {}


def test_the_engine_sends_the_caliber_line_with_the_judge_request() -> None:
    """端到端：画像有口径时，真正发给判定的那一份里必须出现那一行。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        judge = _ScriptedJudge()
        engine = DialogueEngine(_ScriptedReply(), judge_client=judge)
        engine.memory_service = _Service(store, PROFILE)
        asyncio.run(engine.handle(msg(1, "@YunRu 你不了解的事情为什么要插嘴呢")))
        content = judge.requests[0][1]["content"]
        line = profile_caliber(PROFILE)
        assert line and line in content
        assert len(line) <= 60


# --- B. 维护侧：口径压过语气 -------------------------------------------------


def _seed_profile(store: MemoryStore, content: str) -> None:
    event = InboxEvent("p1", GROUP, USER, "user", "我今天有点累", time.time())
    store.append(event)
    batch = store.claim({GROUP}, lease_seconds=60)
    assert batch is not None
    raw = json.dumps({"operations": [{
        "op": "ADD", "scope_type": "user_global", "kind": "profile",
        "normalized_key": "profile/temper", "content": content,
        "confidence": 0.9, "ttl_days": None, "evidence_event_ids": ["p1"],
        "targets": [], "subjects": [], "durable": True,
    }]}, ensure_ascii=False)
    assert [op.op for op in store.commit(batch, raw)] == ["ADD"]


class _Client:
    last_usage = {}

    def __init__(self, output: str) -> None:
        self.output = output

    async def complete(self, request):
        return self.output


def _affinity_output(*, with_add: bool = False) -> str:
    operations = []
    if with_add:
        operations.append({
            "op": "ADD", "scope_type": "user_global", "kind": "preference",
            "normalized_key": "likes_digital", "content": "他喜欢聊数码产品",
            "confidence": 0.8, "ttl_days": None, "evidence_event_ids": ["e2"],
            "targets": [], "subjects": [], "durable": True,
        })
    operations.append({
        "op": "AFFINITY", "user_id": USER, "closeness": "-4", "guardedness": "0",
        "evidence_event_ids": ["e2"], "reason": "他明显在气头上，先小幅降一点",
    })
    return json.dumps({"operations": operations}, ensure_ascii=False)


def _run_agent(store: MemoryStore, output: str) -> MemoryMetrics:
    metrics = MemoryMetrics()
    agent = MemoryMaintenanceAgent(
        _Client(output), store, lambda: {GROUP}, metrics, max_batches=1)
    asyncio.run(agent.run_once())
    return metrics


def test_profile_caliber_rejects_a_downward_affinity_but_keeps_add() -> None:
    """画像说"在带你" + 证据是纠正 → AFFINITY 被拒、关系不变；同批 ADD 照常。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        _seed_profile(store, PROFILE)
        store.append(InboxEvent("e2", GROUP, USER, "user", CORRECTION, time.time()))

        metrics = _run_agent(store, _affinity_output(with_add=True))

        assert store.relationship(USER) == (10, 0)
        assert metrics.affinity_rejected == 1
        assert metrics.operations["AFFINITY"] == 0
        assert metrics.operations["ADD"] == 1, "同一批里的 ADD 不许被一起拒掉"


def test_without_the_caliber_the_affinity_still_applies() -> None:
    """画像没有这条口径时照常生效：闸门不能把正常的关系维护一起拦掉。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        _seed_profile(store, "他喜欢猫，也喜欢跟我聊数码。")
        store.append(InboxEvent("e2", GROUP, USER, "user", CORRECTION, time.time()))

        metrics = _run_agent(store, _affinity_output())

        assert store.relationship(USER) == (6, 0)
        assert metrics.affinity_rejected == 0


def test_maintenance_prompt_requires_reading_the_profile_caliber() -> None:
    assert "做 AFFINITY 决策前" in MEMORY_SYSTEM_PROMPT
    assert "这不是嫌你" in MEMORY_SYSTEM_PROMPT or "不是嫌你" in MEMORY_SYSTEM_PROMPT
