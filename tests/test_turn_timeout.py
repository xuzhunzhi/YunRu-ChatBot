"""一轮对话有**总预算**：模型"拖着不回"不许把整个 bot 卡住（失败降级审计 2）。

修之前：接收循环是**串行**的（`_handle_one` 逐条 await），一条卡住的腿会把后面所有
消息一起挡住，而 focus / outbox 还在跑，日志看起来像"进程活着"。
（2026-10-13 更正：这里原来写"单次 `complete()` 最坏 ≈ 181 秒（60 秒 × 3 次 + 退避）、
一轮约 9 分钟"——随"不重试 + 超时只管首字"作废；现在每一腿只是**停顿网**，
相加仍可超过预算，所以这一层继续留着。）

现在 `runtime.handle_turn_within_budget` 用 `asyncio.wait_for` 包住
`engine.handle(item)`：预算 `QQBOT_TURN_TIMEOUT_SECONDS`（默认 90，夹 5..600，
**调用时现读**）。超时的结果与"模型抛异常"完全相同——这一条没有回复（返回 None）、
计数 + 一行 ERROR；**那条消息留在历史里**（记历史发生在任何模型调用之前，
她该知道群里发生过那句话）。

这一份钉住：

1. 故意不返回的客户端 → 到点就收尾（返回 None）、计数 +1、**消息仍在历史里**；
2. 正常一轮不受影响（照旧拿到回复、计数为 0）；
3. 预算的默认值 / 现读 / 夹取；
4. 计数进 snapshot 与 `/super status`；
5. **被砍在哪一段**（2026-10-13 夜间复盘）：判定/回复/压缩/识图四条腿在被砍的那一刻
   各自把自己的名字留在 `ContextVar` 里，`turn timeout` 那一行照实印出来——
   历史上这条网只响过 3 次（10-08/09/10），响得少的时候"砍在哪一段"就是唯一想知道的数。

突变（各自还原）：去掉总预算（直接 `await engine.handle`）→ 第 1 条会挂死/红；
超时后把消息从历史里删掉 → 第 1 条红；把 `set_turn_stage` 那几行删掉 → 第 5 条红。
"""
import asyncio
import logging
import os
import time
from unittest.mock import patch

from qq_roleplay_bot import dev_config
from qq_roleplay_bot.runtime import handle_turn_within_budget
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
SESSION = f"group:{GROUP}"
TARGET = MessageTarget(group_id=GROUP)
BUDGET = 5.0          # `QQBOT_TURN_TIMEOUT_SECONDS` 的下界就是 5


class _HangingClient:
    """故意不返回：模拟"服务端拖着不回"（不是拒绝，是拖）。"""

    async def complete(self, request):
        await asyncio.sleep(3600)


class _ScriptedClient:
    def __init__(self, output: str) -> None:
        self.output = output

    async def complete(self, request):
        return self.output


def _message(index: int = 1) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"t{index}", session_id=SESSION, user_id="100",
        text="@YunRu 在吗", target=TARGET, sender_name="某人", is_bot_mentioned=True,
    )


# --- 1. 拖着不回就有上界 -----------------------------------------------------


def test_a_hanging_turn_is_cut_by_the_budget_and_keeps_the_history() -> None:
    engine = DialogueEngine(
        _HangingClient(), judge_client=_HangingClient(),
        enabled_group_ids=frozenset({GROUP}))
    message = _message()
    with patch.dict(os.environ, {"QQBOT_TURN_TIMEOUT_SECONDS": str(BUDGET)}):
        started = time.monotonic()
        reply = asyncio.run(handle_turn_within_budget(engine, message))
        elapsed = time.monotonic() - started

    assert reply is None, "超时该按「这一条没有回复」收尾"
    assert elapsed < BUDGET + 3.0, elapsed
    assert engine.snapshot().turn_timeouts == 1
    # **不许**把那条消息从历史里删掉：她该知道群里发生过那句话。
    history = engine.sessions.state(SESSION).history
    assert [item.message_id for item in history] == [message.message_id], \
        "超时把消息从历史里删掉了"
    # 守卫（巡检锁）也要还回去，否则后面所有群都被"延迟"。
    assert engine.focus.sweeping is False


def test_a_normal_turn_is_not_touched() -> None:
    judge = _ScriptedClient("<route>REPLY</route><topic>话题</topic>")
    reply_client = _ScriptedClient(
        "<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>我接一句。</reply>")
    engine = DialogueEngine(
        reply_client, judge_client=judge, enabled_group_ids=frozenset({GROUP}))
    with patch.dict(os.environ, {"QQBOT_TURN_TIMEOUT_SECONDS": str(BUDGET)}):
        reply = asyncio.run(handle_turn_within_budget(engine, _message()))

    assert reply is not None and reply.text == "我接一句。"
    assert engine.snapshot().turn_timeouts == 0


# --- 2. 预算本身 -------------------------------------------------------------


def test_the_budget_is_read_when_called_and_clamped() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_TURN_TIMEOUT_SECONDS", None)
        assert dev_config.turn_timeout_seconds() == 90.0
    with patch.dict(os.environ, {"QQBOT_TURN_TIMEOUT_SECONDS": "30"}):
        assert dev_config.turn_timeout_seconds() == 30.0
    for text, expected in (("", 90.0), ("不是数", 90.0), ("1", 5.0), ("99999", 600.0)):
        with patch.dict(os.environ, {"QQBOT_TURN_TIMEOUT_SECONDS": text}):
            assert dev_config.turn_timeout_seconds() == expected, text


# --- 3. 计数看得见 -----------------------------------------------------------


def test_the_timeout_count_shows_up_in_the_status() -> None:
    async def run() -> str:
        engine = DialogueEngine(_HangingClient(), super_admin_user_ids=frozenset({SUPER}))
        engine.note_turn_timeout()
        engine.note_turn_timeout()
        assert engine.snapshot().turn_timeouts == 2
        reply = await engine.handle(IncomingMessage(
            "m:status", SESSION, SUPER, "/super status", TARGET))
        return reply.text

    text = asyncio.run(run())
    assert "超过总预算 2 次" in text, text
    assert "历史" in text, text


# --- 4. 被砍在哪一段 ---------------------------------------------------------


def test_the_timeout_line_says_which_leg_was_cut() -> None:
    """拖着不回的是**判定**那一腿 → 日志里就该写 `stage=judging`。

    这一条同时钉住"取消**不会**影响这个值"：写在引擎对象上，而不是内层任务的
    `ContextVar` 上——`asyncio.wait_for` 给 `engine.handle` 的是**新任务**（context 是
    拷贝的），内层任务里写的 ContextVar 外层读出来是空的（当时实测过）。
    """

    engine = DialogueEngine(
        _HangingClient(), judge_client=_HangingClient(),
        enabled_group_ids=frozenset({GROUP}))

    class _Collect(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.messages.append(record.getMessage())

    collector = _Collect()
    log = logging.getLogger("qq_roleplay_bot.runtime")
    log.addHandler(collector)
    try:
        with patch.dict(os.environ, {"QQBOT_TURN_TIMEOUT_SECONDS": str(BUDGET)}):
            asyncio.run(handle_turn_within_budget(engine, _message()))
    finally:
        log.removeHandler(collector)

    cut = [m for m in collector.messages if "turn timeout" in m]
    assert len(cut) == 1, collector.messages
    assert "stage=judging" in cut[0], cut[0]


def test_the_stage_helper_only_takes_known_names() -> None:
    """写错阶段名当场炸：让日志印出一个没人认识的值，还不如不印。"""

    class _Engine:
        turn_stage = ""

    engine = _Engine()
    try:
        dev_config.set_turn_stage(engine, "replying")
        assert dev_config.current_turn_stage(engine) == "replying"
        dev_config.set_turn_stage(engine, "")
        assert dev_config.current_turn_stage(engine) == ""
        for bad in ("reply", "判定", "judge"):
            try:
                dev_config.set_turn_stage(engine, bad)
            except ValueError:
                continue
            raise AssertionError(f"未知阶段名被放过了: {bad!r}")
    finally:
        engine.turn_stage = ""


if __name__ == "__main__":
    import unittest

    unittest.main()