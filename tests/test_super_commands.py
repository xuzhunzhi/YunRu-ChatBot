"""超管/管理员分层命令与记忆查看的离线测试。"""
import asyncio
import json
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.memory_view import (
    ADMIN_HELP,
    ADMIN_HELP_GUEST_NOTE,
    MAX_REPLY_CHARS,
    SUPER_HELP,
    build_archive_view,
    build_audit_view,
    build_inbox_view,
    build_overview,
    build_records_view,
)
from qq_roleplay_bot.stage3_main import (
    DialogueEngine,
    SuperAction,
    is_admin_help_command,
    parse_super_command,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ADMIN = "900000001"
OTHER_ADMIN = "1111111111"
NOISE = "9999999999"


@contextmanager
def _temp_dir():
    import tempfile
    path = tempfile.mkdtemp(prefix="memview-")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _now() -> float:
    """InboxEvent 必须带近期时间戳，否则会被保留期检查拒绝。"""

    return time.time()


def _message(text: str, *, user_id: str = NOISE, group: str = GROUP) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m:{text}:{user_id}",
        session_id=f"group:{group}",
        user_id=user_id,
        text=text,
        target=MessageTarget(group_id=group),
    )


def _private(text: str, *, user_id: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"p:{text}:{user_id}",
        session_id=f"private:{user_id}",
        user_id=user_id,
        text=text,
        target=MessageTarget(user_id=user_id),
    )


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("帮助/记忆命令不应调用模型")


def _engine_with_memory(*, super_admin: str = ADMIN):
    """构造带真实（临时）记忆库的引擎，用于验证记忆查看。

    返回 (engine, store, cleanup)。调用方在 finally 里执行 cleanup。
    """

    import tempfile
    directory = Path(tempfile.mkdtemp(prefix="memview-"))
    store = MemoryStore(directory / "memory.sqlite3")

    class _Service:
        def __init__(self) -> None:
            self.store = store

        def snapshot(self) -> dict:
            return {"queued_events": 0}

    engine = DialogueEngine(
        _NeverCalled(),
        super_admin_user_ids=frozenset({super_admin}),
        admin_user_ids=frozenset({super_admin}),
    )
    engine.memory_service = _Service()  # type: ignore[assignment]
    return engine, store, (lambda: shutil.rmtree(directory, ignore_errors=True))


# --- 命令解析 -------------------------------------------------------------

def test_super_help_matching() -> None:
    for text in ("/super help", "/super", "#super help", "  /SUPER HELP  ", "/super 帮助"):
        assert parse_super_command(text) is SuperAction.HELP, text
    for text in ("/super memory", "/super 记忆"):
        assert parse_super_command(text) is SuperAction.MEMORY, text
    for text in ("/super memory records", "/super 记忆 记忆"):
        assert parse_super_command(text) is SuperAction.MEMORY_RECORDS, text
    for text in ("/super memory inbox", "/super 记忆 收件"):
        assert parse_super_command(text) is SuperAction.MEMORY_INBOX, text
    for text in ("/super memory audit", "/super 记忆 审计"):
        assert parse_super_command(text) is SuperAction.MEMORY_AUDIT, text
    for text in ("/super processes", "/super 进程", "/super process"):
        assert parse_super_command(text) is SuperAction.PROCESSES, text
    for text in ("/super status", "/super 状态"):
        assert parse_super_command(text) is SuperAction.STATUS, text
    for text in ("/super restart", "/super 重启", "  /SUPER RESTART  "):
        assert parse_super_command(text) is SuperAction.RESTART, text


def test_profile_command_matching() -> None:
    """`/super profile @某人` = 看她对这个人的完整印象（2026-09-30 用户要求）。"""

    for text in ("/super profile", "/super profile @某人", "/super 画像", "/super 人物画像",
                 "/super portrait 900000001", "  /SUPER PROFILE @x  "):
        assert parse_super_command(text) is SuperAction.PROFILE, text
    # 不能被通用模式吃掉，也不能让组合词变成合法命令
    assert parse_super_command("/super restart profile") is None
    assert parse_super_command("/super memory") is SuperAction.MEMORY


def test_super_parser_rejects_non_super_text() -> None:
    for text in ("super help", "/supers help", "/super 未知", "/super memory unknown",
                 "你好", "/help", "/admin help", "", "/superhelp",
                 "/super restart now", "/super restart 1"):
        assert parse_super_command(text) is None, text


def test_admin_help_matching() -> None:
    for text in ("/admin help", "/admin", "#admin help", "  /ADMIN 帮助 "):
        assert is_admin_help_command(text), text
    for text in ("admin help", "/admins help", "/admin help me", "/help"):
        assert not is_admin_help_command(text), text


# --- 权限分层（核心安全属性） ---------------------------------------------

def test_super_command_ignored_for_non_super_admin() -> None:
    """非超管发 /super help 不应拿到帮助，而是走普通流程（不回复）。"""

    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        assert await engine.handle(_message("/super help", user_id=NOISE)) is None

    asyncio.run(run())


def test_super_command_works_outside_the_enabled_groups() -> None:
    """超管是**全局身份**：跟"在哪个群""这个群开没开"都没关系。

    从前这里按群过滤，结果在没打开的群里发 `/super help` 一点反应都没有。
    """

    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        result = await engine.handle(_message("/super help", user_id=ADMIN, group="888888888"))
        assert result is not None and result.text == SUPER_HELP

    asyncio.run(run())


def test_admin_help_menu_is_visible_to_everyone() -> None:
    """菜单公开、执行要权限（用户 2026-09-28 要求"普通成员可以呼叫 /admin help 调起菜单"）。

    管理员的回执就是菜单本身；普通成员多读一段"请超管用 /super permit 放行一次"。
    `/super help` 不在这条规则里：那一层不是菜单，是这台 bot 自己的操作面。
    """

    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), admin_user_ids=frozenset({ADMIN}),
                              super_admin_user_ids=frozenset({ADMIN}))
        allowed = await engine.handle(_message("/admin help", user_id=ADMIN))
        assert allowed is not None and allowed.text == ADMIN_HELP

        guest = await engine.handle(_message("/admin help", user_id=NOISE))
        assert guest is not None
        assert guest.text.startswith(ADMIN_HELP)
        assert ADMIN_HELP_GUEST_NOTE in guest.text
        assert "/super permit" in guest.text
        # 没权限的人还是拿不到超管那一层。
        assert await engine.handle(_message("/super help", user_id=NOISE)) is None
        # 看菜单不会给他任何权限：真正的命令照样被拒。
        still_denied = await engine.handle(_message("/admin status", user_id=NOISE))
        assert still_denied is not None and "权限" in still_denied.text

    asyncio.run(run())


def test_super_help_via_private_chat() -> None:
    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        result = await engine.handle(_private("/super help", user_id=ADMIN))
        assert result is not None and result.text == SUPER_HELP

    asyncio.run(run())


# --- 人物画像查看（/super profile） ---------------------------------------

PROFILE_TEXT = "他叫老明，做后端的，喜欢把话说清楚，讨厌被追问家里的事。"


def _seed_profile(store, *, user_id: str = NOISE, content: str = PROFILE_TEXT) -> None:
    """往临时库里写一条画像（走真实校验路径，不直接插表）。"""

    store.append(InboxEvent("e-profile", GROUP, user_id, "user", "我叫老明", _now()))
    batch = store.claim({GROUP})
    assert batch is not None
    evidence = [event.id for event in batch.events if event.speaker == "user"]
    store.commit(batch, json.dumps({"operations": [{
        "op": "ADD", "scope_type": "user_global", "kind": "profile",
        "normalized_key": "profile", "content": content, "confidence": 0.9,
        "ttl_days": None, "evidence_event_ids": evidence, "targets": [],
        "subjects": [], "durable": True,
    }]}, ensure_ascii=False))


def _mention(text: str, user_id: str, *, sender: str = ADMIN) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"pf:{text}:{user_id}",
        session_id=f"group:{GROUP}",
        user_id=sender,
        text=text,
        target=MessageTarget(group_id=GROUP),
        mentioned_user_ids=(user_id,),
    )


def test_super_profile_shows_the_full_impression() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            _seed_profile(store)
            result = await engine.handle(_mention("/super profile @某人", NOISE))
            assert result is not None
            text = result.text
            assert "【人物画像】" in text and NOISE in text
            assert PROFILE_TEXT in text, text
            assert "亲近" in text and "防备" in text
            # 是发到群里的（目标群就是这条命令的群）
            assert result.target.group_id == GROUP
        finally:
            cleanup()

    asyncio.run(run())


def test_super_profile_without_a_profile_says_so_and_falls_back() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            # 只有零散记录、没有画像（称呼）
            store.append(InboxEvent("e-name", GROUP, NOISE, "user", "叫我老明", _now()))
            batch = store.claim({GROUP})
            store.commit(batch, json.dumps({"operations": [{
                "op": "ADD", "scope_type": "user_global", "kind": "name",
                "normalized_key": "preferred_name", "content": "他希望大家叫他老明",
                "confidence": 0.9, "ttl_days": None,
                "evidence_event_ids": [e.id for e in batch.events if e.speaker == "user"],
                "targets": [], "subjects": [], "durable": True,
            }]}, ensure_ascii=False))
            result = await engine.handle(_mention("/super profile @某人", NOISE))
            assert result is not None
            assert "还没有画像" in result.text
            assert "他希望大家叫他老明" in result.text, "至少要给出她手里的零散记录"
        finally:
            cleanup()

    asyncio.run(run())


def test_super_profile_accepts_a_plain_qq_number() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            _seed_profile(store, user_id=NOISE)
            result = await engine.handle(_message(f"/super profile {NOISE}", user_id=ADMIN))
            assert result is not None and PROFILE_TEXT in result.text
        finally:
            cleanup()

    asyncio.run(run())


def test_super_profile_is_still_super_admin_only() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            _seed_profile(store, user_id=NOISE)
            # 普通成员发同样的命令：什么都不给（不暴露这一层）
            assert await engine.handle(_mention("/super profile @某人", NOISE, sender=NOISE)) is None
        finally:
            cleanup()

    asyncio.run(run())


def test_super_help_takes_precedence_and_is_not_admin_help() -> None:
    """/super help 不能被 /admin 规则抢走。"""

    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), admin_user_ids=frozenset({ADMIN}),
                              super_admin_user_ids=frozenset({ADMIN}))
        result = await engine.handle(_message("/super help", user_id=ADMIN))
        assert result is not None
        assert "超管命令" in result.text
        assert result.text != ADMIN_HELP

    asyncio.run(run())


def test_public_help_still_works_for_anyone() -> None:
    async def run() -> None:
        engine = DialogueEngine(_NeverCalled())
        result = await engine.handle(_message("/help", user_id=NOISE))
        assert result is not None and "使用说明" in result.text

    asyncio.run(run())


# --- 超管状态 -------------------------------------------------------------

def test_super_status_reports_counts_without_model_call() -> None:
    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        result = await engine.handle(_message("/super status", user_id=ADMIN))
        assert result is not None
        assert "运行状态" in result.text
        assert "模型调用" in result.text

    asyncio.run(run())


# --- 重启 ---------------------------------------------------------------


def test_restart_only_sets_an_intent_flag() -> None:
    """`/super restart` 在引擎里只**记一个意图**，不真的动进程。

    真正的重启在 runtime 里做：先让这条回复发出去、再关掉传输层，
    最后才换进程。所以这里能安全地断言"标记被立起来了"。
    """

    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        assert engine.consume_restart_request() is False
        result = await engine.handle(_message("/super restart", user_id=ADMIN))
        assert result is not None and "重启" in result.text
        assert engine.consume_restart_request() is True
        # 读过一次就清掉：不能因为一条旧指令反复重启。
        assert engine.consume_restart_request() is False

    asyncio.run(run())


def test_restart_flag_is_not_set_for_other_commands_or_other_people() -> None:
    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        assert await engine.handle(_message("/super status", user_id=ADMIN)) is not None
        assert engine.consume_restart_request() is False
        # 非超管：静默丢弃，也不该留下重启意图。
        assert await engine.handle(_message("/super restart", user_id=NOISE)) is None
        assert engine.consume_restart_request() is False

    asyncio.run(run())


def test_restart_argv_uses_the_module_form_for_m_started_processes() -> None:
    """`-m` 启动的必须按模块名重启：直接重跑脚本路径会丢掉包上下文。"""

    from qq_roleplay_bot.runtime import restart_argv

    assert restart_argv("py.exe", "qq_roleplay_bot.stage3_main", "ignored.py") == [
        "py.exe", "-m", "qq_roleplay_bot.stage3_main",
    ]
    # 没有 spec（直接跑脚本）时退回脚本路径，并转成绝对路径。
    assert restart_argv("py.exe", None, "run.py") == [
        "py.exe", str(Path("run.py").resolve()),
    ]


# --- 记忆查看 -------------------------------------------------------------

def test_super_memory_overview_lists_counts() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            store.append(InboxEvent("e1", GROUP, "100", "user", "材料", _now()))
            result = await engine.handle(_message("/super memory", user_id=ADMIN))
            assert result is not None
            assert "记忆库概览" in result.text
            assert "待处理材料：1" in result.text
        finally:
            cleanup()

    asyncio.run(run())


def test_super_memory_records_view_shows_key_and_content() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            store.append(InboxEvent("e1", GROUP, "100", "user", "我喜欢喝茶", _now()))
            batch = store.claim({GROUP})
            store.commit(batch, '{"operations":[{"op":"ADD","scope_type":"user_global",'
                                '"kind":"preference","normalized_key":"tea","content":"用户喜欢喝茶",'
                                '"confidence":0.9,"ttl_days":null,"evidence_event_ids":["e1"],'
                                '"targets":[]}]}')
            result = await engine.handle(_message("/super memory records", user_id=ADMIN))
            assert result is not None
            assert "tea" in result.text
            assert "用户喜欢喝茶" in result.text
        finally:
            cleanup()

    asyncio.run(run())


def test_super_memory_records_does_not_leak_event_ids() -> None:
    """记忆查看不应暴露 source_event_ids 这类内部标识。"""

    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            store.append(InboxEvent("secret-event-id", GROUP, "100", "user", "内容", _now()))
            batch = store.claim({GROUP})
            store.commit(batch, '{"operations":[{"op":"ADD","scope_type":"user_global",'
                                '"kind":"preference","normalized_key":"k","content":"内容",'
                                '"confidence":0.9,"ttl_days":null,'
                                '"evidence_event_ids":["secret-event-id"],"targets":[]}]}')
            result = await engine.handle(_message("/super memory records", user_id=ADMIN))
            assert result is not None
            assert "secret-event-id" not in result.text
        finally:
            cleanup()

    asyncio.run(run())


def test_super_memory_without_service_degrades() -> None:
    async def run() -> None:
        engine = DialogueEngine(_NeverCalled(), super_admin_user_ids=frozenset({ADMIN}))
        result = await engine.handle(_message("/super memory", user_id=ADMIN))
        assert result is not None
        assert "记忆服务未启用" in result.text

    asyncio.run(run())


def test_super_memory_archive_lists_deleted_content() -> None:
    """归档命令必须显示被删内容的原文——否则"可恢复"只是说法。"""

    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            store.append(InboxEvent("e1", GROUP, "100", "user", "我喜欢喝茶", _now()))
            batch = store.claim({GROUP})
            store.commit(batch, json.dumps({"operations": [{
                "op": "ADD", "scope_type": "user_global", "kind": "preference",
                "normalized_key": "tea", "content": "用户喜欢喝茶", "confidence": 0.9,
                "ttl_days": None, "evidence_event_ids": ["e1"], "targets": [],
            }]}, ensure_ascii=False))

            store.append(InboxEvent("e2", GROUP, "100", "user", "忘掉这些", _now() + 1))
            delete_batch = store.claim({GROUP})
            target = delete_batch.records[0]
            store.commit(delete_batch, json.dumps({"operations": [{
                "op": "DELETE", "scope_type": "user_global", "evidence_event_ids": ["e2"],
                "targets": [{"id": target.id, "revision": target.revision}],
            }]}, ensure_ascii=False))

            result = await engine.handle(_message("/super memory archive", user_id=ADMIN))
            assert result is not None
            assert "归档" in result.text
            assert "tea" in result.text
            assert "用户喜欢喝茶" in result.text, "归档必须显示被删内容的原文"
        finally:
            cleanup()

    asyncio.run(run())


def test_super_memory_archive_accepts_chinese_alias() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            result = await engine.handle(_message("/super memory 归档", user_id=ADMIN))
            assert result is not None
            assert "归档" in result.text
        finally:
            cleanup()

    asyncio.run(run())


def test_memory_view_failure_degrades_without_raising() -> None:
    async def run() -> None:
        engine, store, cleanup = _engine_with_memory()
        try:
            def boom():
                raise RuntimeError("db broken")

            store.counts = boom  # type: ignore[assignment]
            result = await engine.handle(_message("/super memory", user_id=ADMIN))
            assert result is not None
            assert "读取记忆库失败" in result.text
        finally:
            cleanup()

    asyncio.run(run())


# --- 渲染有界（群消息长度安全） -------------------------------------------

def test_all_views_are_bounded() -> None:
    huge = "长" * 5000

    def record(index):
        return {"scope_type": "user_group", "kind": "preference", "normalized_key": f"k{index}",
                "content": huge, "confidence": 0.9, "status": "active"}

    views = [
        build_overview({"records": 999, "active_records": 999, "inbox": 999, "audit": 999,
                        "tombstones": 1, "receipts": 2}),
        build_records_view([record(i) for i in range(50)], total=5000),
        build_inbox_view([{"speaker": "user", "user_id": "100", "text": huge} for _ in range(50)],
                         total=5000),
        build_audit_view([{"op": "ADD", "scope_type": "user_group"} for _ in range(50)], total=5000),
        build_archive_view(
            [{"normalized_key": f"k{i}", "scope_type": "user_global",
              "delete_reason": "agent_delete", "content": huge} for i in range(50)],
            total=5000),
    ]
    for view in views:
        rendered = view.render()
        assert len(rendered) <= MAX_REPLY_CHARS, len(rendered)


def test_views_report_hidden_remainder() -> None:
    records = [{"scope_type": "group", "kind": "group_fact", "normalized_key": f"k{i}",
                "content": "x", "confidence": 0.9, "status": "active"} for i in range(30)]
    assert "还有" in build_records_view(records, total=30).render()
    items = [{"speaker": "user", "user_id": "1", "text": "t"} for _ in range(30)]
    assert "还有" in build_inbox_view(items, total=30).render()


def test_empty_views_say_so() -> None:
    assert "暂无记录" in build_records_view([], total=0).render()
    assert "队列为空" in build_inbox_view([], total=0).render()
    assert "暂无记录" in build_audit_view([], total=0).render()
    assert "归档为空" in build_archive_view([], total=0).render()


def test_inbox_view_labels_bot_messages() -> None:
    view = build_inbox_view([{"speaker": "yunru", "user_id": "100", "text": "我说的"}], total=1)
    assert "云茹" in view.render()


# --- 记忆库只读查询 -------------------------------------------------------

def test_store_counts_and_lists_are_read_only() -> None:
    with _temp_dir() as directory:
        store = MemoryStore(directory / "m.sqlite3")
        store.append(InboxEvent("e1", GROUP, "100", "user", "hello", _now()))
        counts = store.counts()
        assert counts["inbox"] == 1
        assert counts["records"] == 0

        items, total = store.list_inbox(limit=5)
        assert total == 1 and len(items) == 1
        # 列表不带内部 id。
        assert "id" not in items[0]

        records, total = store.list_records(status="active")
        assert total == 0 and records == []

        rows, total = store.list_audit()
        assert total == 0 and rows == []

        # 只读：计数没有因为查询而变化。
        assert store.counts()["inbox"] == 1


def test_store_list_limit_is_clamped() -> None:
    with _temp_dir() as directory:
        store = MemoryStore(directory / "m.sqlite3")
        for index in range(5):
            store.append(InboxEvent(f"e{index}", GROUP, "100", "user", f"t{index}", _now()))
        items, _ = store.list_inbox(limit=10_000)
        assert len(items) <= 200
