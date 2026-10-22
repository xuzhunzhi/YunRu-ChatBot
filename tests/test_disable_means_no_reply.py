"""`/admin disable 群` 只表示"不回复"；记忆是**另一张按群的名单**。

用户 2026-10-08 的原话（三句，合起来就是这个文件的判据）：

* *"**只是不回复，不要影响上下文和记忆入栈**"*
* *"我要的是**记忆**不是对话，我的意思是**这几个群开记忆**"*
* *"我要的是**在哪个群说 enable/disable 就开启/关闭哪个群的**"*

背景：维护每 tick 调的 `memory_store.discard_disallowed` 会按"哪些群开对话"把
其它群的待处理行**静默删掉**，体检实测因此丢过 249 条普通聊天
（`docs/audits/isolation-growth-2026-10-08.md` 与那份体检的"摄取"一节）。
收窄之后，两张名单各管各的：

| 那张名单 | 怎么改 | 管什么 |
| --- | --- | --- |
| `enabled_group_ids`（对话） | `/admin enable|disable`（在本群说） | 回不回；**不影响记不记** |
| `memory_group_ids`（记忆） | `/super enable|disable memory`（在本群说） | 收不收新记忆；**不影响回不回** |

这个文件把两边都钉住：未启用对话的群照记（只要它开了记忆）、没开记忆的群一条都不收、
关掉记忆**不删**已有待处理行、开关在重启后仍在、非超管用不了。
"""
import asyncio
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qq_roleplay_bot.memory_service import MemoryService
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.memory_view import SUPER_HELP
from qq_roleplay_bot.control_audit import ControlAudit
from qq_roleplay_bot.stage3_main import DialogueEngine, SuperAction, parse_super_command
from qq_roleplay_bot.state_store import RuntimeStateStore
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"   # A 群
OTHER = "888888888"   # B 群
ADMIN = "900000001"   # 超管
NOISE = "100"

IGNORE = json.dumps({"operations": [{"op": "IGNORE"}]}, ensure_ascii=False)


class _NeverCalled:
    """走到模型就是错：这里没有任何一条消息该被回复。"""

    async def complete(self, request):
        raise AssertionError("这条消息不该走到模型")


class _IgnoreClient:
    """维护用的假 client：老实说"这条不用记"（一次真实的处理，不是删除）。"""

    last_usage = {"prompt_tokens": 7, "completion_tokens": 2}

    async def complete(self, request):
        return IGNORE


class _BrokenClient:
    """维护调用直接失败：那一轮不许提交，也**不许删**那条材料。"""

    async def complete(self, request):
        raise RuntimeError("model down")


def group_message(message_id, text, *, group=GROUP, user=NOISE, mentioned=False):
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{group}",
        user_id=user,
        text=text,
        target=MessageTarget(group_id=group),
        is_bot_mentioned=mentioned,
        sender_name="某人",
    )


def command(text, *, group=GROUP, user=ADMIN):
    return group_message(f"cmd:{text}:{group}:{user}", text, group=group, user=user)


def private_command(text, *, user=ADMIN):
    return IncomingMessage(
        message_id=f"pcmd:{text}", session_id=f"private:{user}", user_id=user,
        text=text, target=MessageTarget(user_id=user),
    )


class Case:
    """一套真实（临时）记忆库 + 引擎 + 可注入的状态库与审计。"""

    def __init__(self, engine, store, service, directory):
        self.engine = engine
        self.store = store
        self.service = service
        self.directory = directory

    def inbox_rows(self):
        with self.store.connection() as db:
            return db.execute("SELECT group_id,text FROM inbox ORDER BY occurred_at").fetchall()

    def audit_ops(self):
        with self.store.connection() as db:
            return [row[0] for row in db.execute("SELECT op FROM audit")]

    def history(self, session_id):
        return list(self.engine.sessions.state(session_id).history)

    async def deliver(self, message):
        """把一条消息走完引擎，并把收件箱排空（写库那一步是后台任务）。"""
        result = await self.engine.handle(message)
        writer = asyncio.create_task(self.service.inbox.run())
        try:
            await self.service.inbox.flush()
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        if result is not None and not isinstance(result, str):
            self.engine.record_sent_reply(message, result.text)
        return result


@contextmanager
def harness(*, talk=(), memory=(), maintenance=None, state_store=None):
    """`talk` = 开对话的群，`memory` = 开记忆的群（两张名单，默认**都空**）。"""

    directory = Path(tempfile.mkdtemp(prefix="memory-groups-"))
    try:
        store = MemoryStore(directory / "memory.sqlite3")
        service = MemoryService(maintenance or _NeverCalled(),
                                lambda: set(engine.memory_group_ids),
                                store=store)
        engine = DialogueEngine(
            _NeverCalled(),
            memory_service=service,
            super_admin_user_ids=frozenset({ADMIN}),
            admin_user_ids=frozenset({ADMIN}),
            enabled_group_ids=frozenset(talk),
            memory_group_ids=frozenset(memory),
            state_store=state_store,
        )
        engine.control_audit = ControlAudit(directory / "control_audit.jsonl")
        yield Case(engine, store, service, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def run(coroutine):
    return asyncio.run(coroutine)


# --- 一、两张名单各管各的 -----------------------------------------------------


def test_memory_is_per_group_a_records_and_b_does_not() -> None:
    """A 群开了记忆、B 群没开：**只有 A 群进收件箱**。"""

    async def scenario():
        with harness(talk=(GROUP,), memory=(GROUP,)) as case:
            await case.deliver(group_message("a1", "A 群说的话", group=GROUP))
            await case.deliver(group_message("b1", "B 群说的话", group=OTHER))
            rows = [(r[0], r[1]) for r in case.inbox_rows()]
            assert rows == [(GROUP, "A 群说的话")], rows
            # 收件箱的入口（offer）自己也要判：否则 B 群那条会先入队、
            # 到写库那一步才被丢掉——那是"收了不用"，不是"不记"。
            assert case.service.metrics.enqueued == 1, "没开记忆的群连入队都不该发生"

    run(scenario())


def test_a_group_with_memory_off_gets_no_inbox_but_its_history_is_still_recorded() -> None:
    """没开记忆的群：不进收件箱。**上下文照记**——记忆名单只管记忆。

    （上下文不是记忆：她轮到这个群时要看得见"她不在的时候群里聊了什么"，
    这与"记不记这个群的事实"是两件事。）
    """

    async def scenario():
        with harness(talk=(GROUP,), memory=(GROUP,)) as case:
            result = await case.deliver(group_message("b1", "B 群说的", group=OTHER))
            assert result is None
            assert case.inbox_rows() == []
            assert [m.text for m in case.history(f"group:{OTHER}")] == ["B 群说的"]

    run(scenario())


def test_disable_memory_in_a_group_keeps_its_pending_rows() -> None:
    """A 群 disable memory：不再收新的，**已有的待处理行一条都不删**；重开就继续处理。"""

    async def scenario():
        with harness(talk=(GROUP,), memory=(GROUP,), maintenance=_IgnoreClient()) as case:
            await case.deliver(group_message("a1", "关之前收下的一条"))
            assert len(case.inbox_rows()) == 1
            off = await case.engine.handle(command("/super disable memory"))
            assert off is not None and "已关闭记忆" in off.text
            await case.deliver(group_message("a2", "关之后说的一条"))
            rows = [(r[0], r[1]) for r in case.inbox_rows()]
            assert rows == [(GROUP, "关之前收下的一条")], rows
            # 关着的时候维护一轮不认领它（但也不删它）。
            assert await case.service.agent.run_once() == 0
            assert len(case.inbox_rows()) == 1
            # 重新打开：那条留着的材料被处理掉（不是永远积压）。
            on = await case.engine.handle(command("/super enable memory"))
            assert on is not None and "已开启记忆" in on.text
            assert await case.service.agent.run_once() == 1
            assert case.audit_ops() == ["IGNORE"]
            assert case.inbox_rows() == []

    run(scenario())


def test_the_memory_switch_is_per_group_not_global() -> None:
    """在 A 群 disable、在 B 群 enable：两张名单里各只动一个群。"""

    async def scenario():
        with harness(talk=(GROUP, OTHER), memory=(GROUP,)) as case:
            await case.engine.handle(command("/super disable memory", group=GROUP))
            await case.engine.handle(command("/super enable memory", group=OTHER))
            assert case.engine.memory_group_ids == {OTHER}
            await case.deliver(group_message("a1", "A 群说的", group=GROUP))
            await case.deliver(group_message("b1", "B 群说的", group=OTHER))
            rows = [(r[0], r[1]) for r in case.inbox_rows()]
            assert rows == [(OTHER, "B 群说的")], rows

    run(scenario())


def test_disabling_the_chat_switch_does_not_touch_memory() -> None:
    """`/admin disable` 一个开了记忆的群：**不回复，但照记**（这才是用户要的收窄）。"""

    async def scenario():
        with harness(talk=(GROUP,), memory=(GROUP,)) as case:
            admin = group_message("adm", "/admin disable", group=GROUP, user=ADMIN)
            reply = await case.engine.handle(admin)
            assert reply is not None and "已关闭对话" in reply.text
            assert GROUP not in case.engine.enabled_group_ids
            assert GROUP in case.engine.memory_group_ids, "关对话不许顺手关掉记忆"
            assert await case.deliver(group_message("a1", "关了对话但我还记着")) is None
            rows = [(r[0], r[1]) for r in case.inbox_rows()]
            assert rows == [(GROUP, "关了对话但我还记着")], rows
            # 上下文也照记（只是不刷新活跃时间）。
            assert [m.text for m in case.history(f"group:{GROUP}")] == ["关了对话但我还记着"]

    run(scenario())


def test_a_group_with_memory_on_and_chat_off_still_gets_no_reply() -> None:
    """回归：记忆开着 ≠ 会回复。判定/注意力一个字都没动。"""

    async def scenario():
        with harness(talk=(OTHER,), memory=(GROUP,)) as case:
            result = await case.deliver(group_message("a1", "云茹在吗", mentioned=True))
            assert result is None
            snapshot = case.engine.snapshot()
            assert snapshot.replies == 0
            assert snapshot.model_calls == 0
            assert snapshot.judge_calls == 0
            assert len(case.inbox_rows()) == 1

    run(scenario())


def test_maintenance_processes_the_memory_groups_pending_row_instead_of_deleting_it() -> None:
    """核心：维护跑过一轮之后，那条材料**已被处理**，而不是消失。

    判据不是"库里还有没有这一行"（处理掉之后本来就没有），而是"它确实被认领、被处理过"
    ——`maintenance_calls` 与审计流水都在。删除路径不会留下这两样东西。
    """

    async def scenario():
        with harness(memory=(GROUP,), maintenance=_IgnoreClient()) as case:
            await case.deliver(group_message("a1", "这条会不会被偷偷删掉"))
            assert len(case.inbox_rows()) == 1
            assert await case.service.agent.run_once() == 1
            assert case.service.metrics.maintenance_calls == 1
            assert case.service.metrics.maintenance_runs == 1
            assert case.audit_ops() == ["IGNORE"], "要留下'处理过'的凭据，而不是静默消失"
            assert case.inbox_rows() == []

    run(scenario())


def test_a_failed_maintenance_round_keeps_the_row() -> None:
    """维护自己挂了也不能顺手删材料：那一行必须还在收件箱里等下一轮。"""

    async def scenario():
        with harness(memory=(GROUP,), maintenance=_BrokenClient()) as case:
            await case.deliver(group_message("a1", "维护挂掉时这条得留着"))
            assert await case.service.agent.run_once() == 0
            rows = [(r[0], r[1]) for r in case.inbox_rows()]
            assert rows == [(GROUP, "维护挂掉时这条得留着")], rows

    run(scenario())


def test_a_private_message_from_a_stranger_is_recorded_but_not_answered() -> None:
    """2026-10-22 用户口径「记所有人」：白名单外的**私聊**照进记忆。

    对话边界没变：**不回复、不进会话历史、不调模型**（fail-closed，见 AGENTS.md §2.1）。
    变的是记忆：私聊在记忆侧换算成伪群 `dm:<uid>`，收件箱里该有一条。
    这条原来断言的是"整条丢弃"——用户当天明确改成"任何人的私聊消息都进记忆"。
    """

    async def scenario():
        with harness(memory=(GROUP,)) as case:
            # 白名单故意留空，确保走的是"白名单外"那条闸，而不是本机 .env 的配置。
            case.engine.private_debug_user_ids = frozenset()
            stranger = IncomingMessage(
                message_id="p1", session_id="private:404", user_id="404",
                text="在吗", target=MessageTarget(user_id="404"),
            )
            assert await case.deliver(stranger) is None
            assert case.history("private:404") == []
            assert [(row[0], row[1]) for row in case.inbox_rows()] == [("dm:404", "在吗")]

    run(scenario())


# --- 二、命令：解析、按群、私聊、权限、落盘 -----------------------------------


def test_the_memory_switch_parses_like_the_chat_switch() -> None:
    assert parse_super_command("/super enable memory") is SuperAction.MEMORY_ON
    assert parse_super_command("/super disable memory") is SuperAction.MEMORY_OFF
    assert parse_super_command("/super 开启 记忆") is SuperAction.MEMORY_ON
    assert parse_super_command("/super 关闭 记忆") is SuperAction.MEMORY_OFF
    # 不许被别的 /super 分支吃掉，也不许把现有的查看命令挤掉。
    assert parse_super_command("/super memory") is SuperAction.MEMORY
    assert parse_super_command("/super memory inbox") is SuperAction.MEMORY_INBOX


def test_a_group_number_argument_is_rejected_like_the_admin_switch() -> None:
    """手滑带了群号：照 `/admin enable 群号` 那句拒绝，**并且一个群都不动**。"""

    async def scenario():
        with harness(memory=(GROUP,)) as case:
            reply = await case.engine.handle(command(f"/super disable memory {OTHER}"))
            assert reply is not None
            assert "群号参数已经去掉了" in reply.text
            assert case.engine.memory_group_ids == {GROUP}, "带了群号时不许动任何群"
            # 非数字的尾巴同样不带参数（措辞不同，但一样一个群都不动）。
            other = await case.engine.handle(command("/super disable memory please"))
            assert other is not None and "这条命令不带参数" in other.text
            assert case.engine.memory_group_ids == {GROUP}

    run(scenario())


def test_the_switch_in_a_private_chat_answers_clearly_and_changes_nothing() -> None:
    """私聊里说：回一句"这条要在群里发"，与 `/admin` 的处理一致，且不改任何群。"""

    async def scenario():
        with harness(memory=(GROUP,)) as case:
            reply = await case.engine.handle(private_command("/super disable memory"))
            assert reply is not None
            assert reply.text == "这条命令要在群里发：它管的就是你发命令的那个群。"
            assert case.engine.memory_group_ids == {GROUP}

    run(scenario())


def test_only_the_super_admin_can_use_the_memory_switch() -> None:
    async def scenario():
        with harness(memory=(GROUP,)) as case:
            assert await case.engine.handle(command("/super disable memory", user=NOISE)) is None
            assert case.engine.memory_group_ids == {GROUP}
            # 管理员也不行：这是超管那一层。
            engine_admin = DialogueEngine(
                _NeverCalled(), admin_user_ids=frozenset({"555"}), super_admin_user_ids=frozenset({ADMIN}),
                memory_group_ids=frozenset({GROUP}),
            )
            assert await engine_admin.handle(command("/super disable memory", user="555")) is None
            assert engine_admin.memory_group_ids == {GROUP}

    run(scenario())


def test_the_memory_list_survives_a_restart() -> None:
    """状态重启后仍在：与对话名单同一套落盘/恢复，但**各存各的键**（互不兜底）。"""

    async def scenario():
        with harness(talk=(GROUP,), memory=(GROUP,)) as case:
            path = case.directory / "runtime_state.json"
            case.engine.state_store = RuntimeStateStore(path)
            # 对话名单留 GROUP；记忆名单改成"只有 OTHER"——两张名单就此分叉。
            await case.engine.handle(command("/super enable memory", group=OTHER))
            await case.engine.handle(command("/super disable memory", group=GROUP))
            restarted = DialogueEngine(_NeverCalled(), state_store=RuntimeStateStore(path))
            restarted.restore_state()
            assert restarted.memory_group_ids == {OTHER}
            assert restarted.enabled_group_ids == {GROUP}, "对话名单照自己的键恢复"
            assert OTHER not in restarted.enabled_group_ids, "记忆名单没写脏对话名单"

    run(scenario())


def test_an_old_state_file_without_the_memory_key_means_memory_off() -> None:
    """旧状态文件里没有这个键 = 还没人开过记忆（空名单），**不沿用对话名单**。"""

    async def scenario():
        directory = Path(tempfile.mkdtemp(prefix="memory-groups-old-"))
        try:
            path = directory / "runtime_state.json"
            RuntimeStateStore(path).save({
                "enabled": True, "enabled_group_ids": [GROUP], "sessions": [],
            })
            engine = DialogueEngine(_NeverCalled(), state_store=RuntimeStateStore(path))
            engine.restore_state()
            assert engine.enabled_group_ids == {GROUP}
            assert engine.memory_group_ids == set(), "不许把对话名单当成记忆名单"
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    run(scenario())


def test_the_real_assembly_wires_memory_to_the_memory_list_not_the_chat_list() -> None:
    """装配点（`runtime._start_memory`）给记忆服务的那条 lambda 读的是**记忆名单**。

    上面那些用例用的是自己拼的 lambda，接不上就等于"新名单只活在测试里"。
    这里直接把装配点那条 lambda 取出来求值：两个名单给成不同的群，看它认哪一个。
    """

    from unittest.mock import patch

    import qq_roleplay_bot.runtime as runtime

    engine = DialogueEngine(
        _NeverCalled(),
        enabled_group_ids=frozenset({GROUP}),      # 开对话的是 A
        memory_group_ids=frozenset({OTHER}),       # 开记忆的是 B
    )

    class _Endpoint:
        # 真的 `TaskEndpoint` 也带 `provider`（装配点用它决定请求体发不发 DeepSeek
        # 专有字段），所以这个小替身要跟上，否则测的就不是真实形状。
        provider = "deepseek"

        def triple(self):
            return ("http://offline.invalid", "offline-key", "offline-model")

    captured = {}

    class _StubService:
        def __init__(inner, client, allowed, **kwargs):
            captured["allowed"] = allowed

    with patch.object(runtime, "_memory_endpoint", lambda: _Endpoint()), \
            patch.object(runtime, "OpenAICompatibleClient", lambda *a, **k: object()), \
            patch.object(runtime, "MemoryService", _StubService):
        runtime._start_memory(engine, object())
    assert captured["allowed"]() == {OTHER}, "装配点接错了名单"
    # 全局停用（engine.enabled=False）时一份都不给：那是"整个 bot 停了"。
    engine.enabled = False
    assert captured["allowed"]() == set()


def test_super_memory_says_whether_this_group_is_recorded() -> None:
    """`/super memory`：列出**当前这个群**记不记（待处理数就在那份概览里）。"""

    async def scenario():
        with harness(memory=(GROUP,), maintenance=_IgnoreClient()) as case:
            await case.deliver(group_message("a1", "一条待处理"))
            on = await case.engine.handle(command("/super memory"))
            assert on is not None
            assert "本群记忆：开" in on.text
            assert "待处理材料：1" in on.text
            off = await case.engine.handle(command("/super memory", group=OTHER))
            assert off is not None
            assert "本群记忆：关" in off.text

    run(scenario())


def test_help_lists_the_memory_switch() -> None:
    assert "/super enable memory" in SUPER_HELP
    assert "/super disable memory" in SUPER_HELP


def test_the_switch_is_audited() -> None:
    async def scenario():
        with harness(memory=(GROUP,)) as case:
            await case.engine.handle(command("/super disable memory"))
            entries = case.engine.control_audit.tail(10)
            assert len(entries) == 1, entries
            assert entries[0]["action"] == "memory_group_switch"
            assert entries[0]["detail"] == {"group_id": GROUP, "memory": "0"}

    run(scenario())
