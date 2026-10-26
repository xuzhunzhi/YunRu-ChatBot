"""公开的 `/yunru affinity` / `/yunru profile`：只看自己，无需超管。

2026-10-25 用户口径：*"affinity和profile只看自己的无需super权限，只需输入
/yunru affinity或者/yunru profile即可"*、*"私聊必须要对方先私信啊，群里发就群里回，
私聊发就私聊回吧"*。

这里锁死四件事：
1. 普通群友（非超管）能用，回复目标就是发命令的那个会话；
2. 命令里带 @ 或 QQ 号 → 拒绝，且绝不输出别人的数据；
3. 渲染与 `/super` 同口径（关系两轴 + 最近一次；画像走 `_profile_block`）；
4. `/super` 的权限一个字都没变，`/yunru ping` 与 `/help` 不受影响。
"""
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.builtin_commands import build_help_text
from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.relationship_tiers import guard_tier, likes_tier
from qq_roleplay_bot.stage3_main import (
    SELF_ONLY_REFUSAL,
    DialogueEngine,
    SelfAction,
    SuperAction,
    parse_self_command,
    parse_super_command,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ME = "900000001"
OTHER = "9999999999"
PROFILE_TEXT = "他叫老明，做后端的，喜欢把话说清楚。"
OTHER_PROFILE_TEXT = "这是别人的画像，不该出现在回复里。"
EVIDENCE = {"quote": "测试里的原话", "who_alias": "1", "seq": 1}


class NeverCalled:
    async def complete(self, request):
        raise AssertionError("公开的只看自己命令不应调用模型")


class _Service:
    """最小可用的记忆服务：命令路径只需要 `.store`。"""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def snapshot(self) -> dict:
        return {"queued_events": 0}


def _engine(*, super_admin: str | None = None):
    directory = Path(tempfile.mkdtemp(prefix="self-cmd-"))
    store = MemoryStore(directory / "memory.sqlite3")
    engine = DialogueEngine(
        NeverCalled(),
        super_admin_user_ids=frozenset({super_admin}) if super_admin else frozenset(),
    )
    engine.memory_service = _Service(store)  # type: ignore[assignment]
    return engine, store, (lambda: shutil.rmtree(directory, ignore_errors=True))


def _message(text: str, *, user_id: str = ME, mentioned: tuple[str, ...] = ()) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m:{text}:{user_id}:{mentioned}",
        session_id=f"group:{GROUP}",
        user_id=user_id,
        text=text,
        target=MessageTarget(group_id=GROUP),
        mentioned_user_ids=mentioned,
    )


def _private(text: str, *, user_id: str = ME) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"p:{text}:{user_id}",
        session_id=f"private:{user_id}",
        user_id=user_id,
        text=text,
        target=MessageTarget(user_id=user_id),
    )


def _seed_profile(store: MemoryStore, *, user_id: str, content: str, key: str = "profile") -> None:
    """走真实校验路径写一片画像，不直接插表。"""

    event_id = f"e-profile-{user_id}-{key}"
    store.append(InboxEvent(event_id, GROUP, user_id, "user", "我叫老明", time.time()))
    batch = store.claim({GROUP})
    assert batch is not None
    evidence = [event.id for event in batch.events if event.speaker == "user"]
    store.commit(batch, json.dumps({"operations": [{
        "op": "ADD", "scope_type": "user_global", "kind": "profile",
        "normalized_key": key, "content": content, "confidence": 0.9,
        "ttl_days": None, "evidence_event_ids": evidence, "targets": [],
        "subjects": [], "durable": True,
    }]}, ensure_ascii=False))


# --- 匹配 -------------------------------------------------------------------


def test_public_self_command_matching() -> None:
    for text in ("/yunru affinity", "#yunru affinity", "yunru affinity",
                 "  /YUNRU AFFINITY  ", "/yunru 关系", "/yunru 好感", "/yunru 好感度"):
        assert parse_self_command(text) == (SelfAction.AFFINITY, ""), text
    for text in ("/yunru profile", "#yunru profile", "yunru profile",
                 "  /YUNRU PORTRAIT  ", "/yunru 画像", "/yunru 人物画像"):
        assert parse_self_command(text) == (SelfAction.PROFILE, ""), text
    for text in ("/yunru", "/yunru ping", "/help", "/super affinity", "yunru affinities"):
        assert parse_self_command(text) is None, text
    # 带参数的形状仍要认出来（交给调用方拒绝），不能掉进普通聊天。
    assert parse_self_command(f"/yunru profile {OTHER}") == (SelfAction.PROFILE, f" {OTHER}")


def test_public_self_commands_are_treated_as_commands() -> None:
    engine, _store, cleanup = _engine()
    try:
        assert engine.handles_command(_message("/yunru affinity")) is True
        assert engine.handles_command(_message("/yunru profile")) is True
    finally:
        cleanup()


# --- 普通群友可用、只看自己 -------------------------------------------------


def test_affinity_command_is_public_and_reads_only_self() -> None:
    engine, store, cleanup = _engine()
    try:
        assert store.apply_affinity(
            ME, closeness=8, guardedness=8, reason="聊得来", source="test",
            evidence=EVIDENCE) == ""
        result = asyncio.run(engine.handle(_message("/yunru affinity", user_id=ME)))
        assert result is not None
        assert result.target == MessageTarget(group_id=GROUP)
        assert "你：好感 18" in result.text
        assert f"（{likes_tier(18).name}" in result.text
        assert f"防备 8（{guard_tier(8).name}" in result.text
        assert "最近一次" in result.text and "聊得来" in result.text
        # 公开这条不提供 reset / evidence 两支。
        assert "reset" not in result.text and "evidence" not in result.text
        assert engine.snapshot().model_calls == 0
    finally:
        cleanup()


def test_profile_command_is_public_and_reads_only_self() -> None:
    engine, store, cleanup = _engine()
    try:
        _seed_profile(store, user_id=ME, content=PROFILE_TEXT)
        _seed_profile(store, user_id=OTHER, content=OTHER_PROFILE_TEXT)
        result = asyncio.run(engine.handle(_message("/yunru profile", user_id=ME)))
        assert result is not None
        assert result.target == MessageTarget(group_id=GROUP)
        assert "【人物画像】" in result.text and ME in result.text
        assert PROFILE_TEXT in result.text
        assert OTHER_PROFILE_TEXT not in result.text
        assert OTHER not in result.text
        assert engine.snapshot().model_calls == 0
    finally:
        cleanup()


def test_profile_without_a_profile_says_it_plainly() -> None:
    engine, _store, cleanup = _engine()
    try:
        result = asyncio.run(engine.handle(_message("/yunru profile", user_id=ME)))
        assert result is not None
        assert "还没有画像" in result.text
        assert "更新于" not in result.text
    finally:
        cleanup()


def test_self_commands_without_memory_service_say_so() -> None:
    engine = DialogueEngine(NeverCalled())
    for index, text in enumerate(("/yunru affinity", "/yunru profile")):
        result = asyncio.run(engine.handle(_message(text, user_id=ME)))
        assert result is not None
        assert "当前没有接入长期记忆" in result.text, text
    assert engine.snapshot().model_calls == 0


# --- 拒绝别人的数据 ---------------------------------------------------------


def test_affinity_command_refuses_mentions_and_qq_numbers() -> None:
    engine, store, cleanup = _engine()
    try:
        store.apply_affinity(OTHER, closeness=8, guardedness=8, reason="别人的事",
                             source="test", evidence=EVIDENCE)
        for text, mentioned in (("/yunru affinity", (OTHER,)),
                                (f"/yunru affinity {OTHER}", ())):
            result = asyncio.run(engine.handle(
                _message(text, user_id=ME, mentioned=mentioned)))
            assert result is not None
            assert result.text == SELF_ONLY_REFUSAL, text
            assert "别人的事" not in result.text
            assert OTHER not in result.text
        # 拒绝不等于执行：别人的关系值没被动过。
        assert store.relationship(OTHER)[0] == 18
    finally:
        cleanup()


def test_profile_command_refuses_mentions_and_qq_numbers() -> None:
    engine, store, cleanup = _engine()
    try:
        _seed_profile(store, user_id=OTHER, content=OTHER_PROFILE_TEXT)
        for text, mentioned in (("/yunru profile", (OTHER,)),
                                (f"/yunru profile {OTHER}", ())):
            result = asyncio.run(engine.handle(
                _message(text, user_id=ME, mentioned=mentioned)))
            assert result is not None
            assert result.text == SELF_ONLY_REFUSAL, text
            assert OTHER_PROFILE_TEXT not in result.text
            assert OTHER not in result.text
    finally:
        cleanup()


# --- 在哪儿发就在哪儿回 -----------------------------------------------------


def test_self_commands_reply_where_they_were_sent() -> None:
    engine, _store, cleanup = _engine()
    try:
        group_result = asyncio.run(engine.handle(_message("/yunru affinity", user_id=ME)))
        assert group_result is not None
        assert group_result.target == MessageTarget(group_id=GROUP)

        private_result = asyncio.run(engine.handle(_private("/yunru profile", user_id=ME)))
        assert private_result is not None
        assert private_result.target == MessageTarget(user_id=ME)
    finally:
        cleanup()


# --- 既有命令不受影响 -------------------------------------------------------


def test_super_affinity_and_profile_stay_super_admin_only() -> None:
    engine, store, cleanup = _engine()
    try:
        _seed_profile(store, user_id=OTHER, content=OTHER_PROFILE_TEXT)
        assert asyncio.run(engine.handle(
            _message("/super affinity", user_id=ME, mentioned=(OTHER,)))) is None
        assert asyncio.run(engine.handle(
            _message("/super profile", user_id=ME, mentioned=(OTHER,)))) is None
    finally:
        cleanup()


def test_ping_and_help_are_unaffected() -> None:
    engine, _store, cleanup = _engine()
    try:
        pong = asyncio.run(engine.handle(_message("/yunru ping", user_id=ME)))
        assert pong is not None and pong.text == "pong"
        help_text = build_help_text(())
        assert "/yunru affinity → 看你自己和我的关系（只有自己）" in help_text
        assert "/yunru profile → 看你自己的人物画像（只有自己）" in help_text
    finally:
        cleanup()

# --- 私聊申请制（2026-10-25）-------------------------------------------------


APPLICANT = "123456789"


def _with_allow_file(call):
    """把放行名单指到临时文件，别写进 dev/data。"""

    tmpdir = Path(tempfile.mkdtemp(prefix="allow-"))
    original = dev_config.private_allow_file
    dev_config.private_allow_file = lambda: tmpdir / "private_allow.json"
    try:
        return call(tmpdir / "private_allow.json")
    finally:
        dev_config.private_allow_file = original
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_private_command_is_matched() -> None:
    for text in ("/yunru private", "#yunru private", "yunru private", "/yunru 私聊", "/yunru 私信"):
        assert parse_self_command(text) == (SelfAction.PRIVATE, ""), text


def test_a_group_request_is_recorded_and_asks_the_owner_to_approve() -> None:
    engine, store, cleanup = _engine(super_admin=ME)
    try:
        result = asyncio.run(engine.handle(_message("/yunru private", user_id=APPLICANT)))
        assert result is not None
        assert "申请" in result.text and APPLICANT in result.text
        assert "/super enable" in result.text
        assert APPLICANT not in engine.private_debug_user_ids, "登记不等于放行"
    finally:
        cleanup()


def test_super_enable_opens_private_for_the_applicant_and_persists() -> None:
    def run(path):
        engine, store, cleanup = _engine(super_admin=ME)
        try:
            asyncio.run(engine.handle(_message("/yunru private", user_id=APPLICANT)))
            result = asyncio.run(engine.handle(_message("/super enable", user_id=ME)))
            assert result is not None and "已开通" in result.text
            assert APPLICANT in engine.private_debug_user_ids
            rows = json.loads(path.read_text(encoding="utf-8"))
            assert rows[0]["user_id"] == APPLICANT and rows[0]["by"] == ME
            return True
        finally:
            cleanup()

    assert _with_allow_file(run) is True


def test_only_a_super_admin_can_approve() -> None:
    def run(path):
        engine, store, cleanup = _engine()
        try:
            asyncio.run(engine.handle(_message("/yunru private", user_id=APPLICANT)))
            result = asyncio.run(engine.handle(_message("/super enable", user_id=OTHER)))
            assert APPLICANT not in engine.private_debug_user_ids
            assert not path.exists() or json.loads(path.read_text(encoding="utf-8")) == []
            return result
        finally:
            cleanup()

    _with_allow_file(run)


def test_enable_without_a_pending_request_says_how() -> None:
    def run(path):
        engine, store, cleanup = _engine(super_admin=ME)
        try:
            result = asyncio.run(engine.handle(_message("/super enable", user_id=ME)))
            assert result is not None and "/yunru private" in result.text
            assert APPLICANT not in engine.private_debug_user_ids
        finally:
            cleanup()

    _with_allow_file(run)


def test_a_previous_approval_survives_a_restart() -> None:
    def run(path):
        path.write_text(json.dumps([{"user_id": APPLICANT, "by": ME, "at": 0}],
                                   ensure_ascii=False), encoding="utf-8")
        engine, store, cleanup = _engine()
        try:
            assert APPLICANT in engine.private_debug_user_ids, "重启后要读回放行名单"
        finally:
            cleanup()

    _with_allow_file(run)


def test_a_dm_request_is_asked_to_apply_in_the_group() -> None:
    engine, store, cleanup = _engine(super_admin=ME)
    try:
        result = asyncio.run(engine.handle(_private("/yunru private", user_id=APPLICANT)))
        assert result is not None and "群里" in result.text
    finally:
        cleanup()

def test_super_command_grammar_is_feature_first() -> None:
    """2026-10-25 用户："enable 是对功能的响应，功能应该放在 enable 前面，比如 /super memory enable"。

    新语法是主用法，旧写法保留为兼容别名（用户先前发过 `/super enable memory`）。
    """

    assert parse_super_command("/super memory enable") is SuperAction.MEMORY_ON
    assert parse_super_command("/super memory disable") is SuperAction.MEMORY_OFF
    assert parse_super_command("/super 记忆 开启") is SuperAction.MEMORY_ON
    assert parse_super_command("/super enable memory") is SuperAction.MEMORY_ON  # 兼容
    assert parse_super_command("/super private enable") is SuperAction.PRIVATE_ON
    assert parse_super_command("/super 私聊 开启") is SuperAction.PRIVATE_ON
    assert parse_super_command("/super enable") is SuperAction.PRIVATE_ON  # 兼容
