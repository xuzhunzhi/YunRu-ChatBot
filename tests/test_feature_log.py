"""按功能分开的输入输出日志：判定 / 回复 / 记忆 / 规则拦截放行。

由来（用户 2026-09-27 定）：原来的模型追踪是"一份总账 + 只留 30 条 + 默认关闭"，
而且**只记第一条 user 消息**——回复请求有稳定段与易变段两段，易变段从来没被记下来，
查"她为什么这么说"时正好缺的就是它。现在四个功能各写一份、各留最近 1000 次。

离线测试入口不用 pytest fixture，临时目录在测试内部自己建。
"""
import asyncio
import json
import tempfile
from pathlib import Path

from qq_roleplay_bot.feature_log import FeatureLog, FeatureLogs, request_parts
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"


def msg(text: str = "在吗", *, mentioned: bool = True):
    return IncomingMessage(
        message_id=f"m-{text}", session_id=f"group:{GROUP}", user_id="900000003",
        text=text, target=MessageTarget(group_id=GROUP), is_bot_mentioned=mentioned,
    )


def read_entries(directory: Path, feature: str) -> list[dict]:
    path = directory / f"{feature}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- 基本行为 ---------------------------------------------------------------


def test_each_feature_gets_its_own_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        logs = FeatureLogs(tmp, capacity=100, enabled=True)
        logs.record("judge", input="判定输入", output="判定输出")
        logs.record("reply", input="回复输入", output="回复输出")
        logs.record("memory", input="维护输入", output="维护输出")
        logs.record("security", input="可疑消息", output="", blocked=True)
        for feature in ("judge", "reply", "memory", "security"):
            entries = read_entries(Path(tmp), feature)
            assert len(entries) == 1, feature
            assert entries[0]["feature"] == feature
        assert read_entries(Path(tmp), "judge")[0]["input"] == "判定输入"


def test_unknown_feature_is_ignored_not_redirected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        logs = FeatureLogs(tmp, capacity=10)
        logs.record("nonsense", input="x")
        assert not list(Path(tmp).glob("*.jsonl"))


def test_capacity_keeps_only_the_newest_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = FeatureLog("judge", tmp, capacity=10, enabled=True)
        for index in range(25):
            log.record(input=f"in-{index}", output=f"out-{index}")
        entries = read_entries(Path(tmp), "judge")
        # 文件里留 capacity..capacity*2 条，且**最新的一定在**。
        assert 10 <= len(entries) <= 20
        assert entries[-1]["input"] == "in-24"
        assert int(entries[-1]["seq"]) == 25


def test_disabled_log_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = FeatureLog("reply", tmp, capacity=10, enabled=False)
        log.record(input="x", output="y")
        assert not (Path(tmp) / "reply.jsonl").exists()
        assert log.snapshot()["recorded"] == 0


def test_previews_are_small_and_carry_no_full_text() -> None:
    """运行状态要报内存占用，所以预览必须是小块，正文只在文件里。"""

    with tempfile.TemporaryDirectory() as tmp:
        log = FeatureLog("reply", tmp, capacity=10, enabled=True)
        log.record(input="长" * 5000, output="短")
        preview = log.previews(1)[0]
        assert preview["input_chars"] == 5000
        assert len(str(preview["input_preview"])) <= 400


def test_oversized_field_is_truncated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = FeatureLog("memory", tmp, capacity=5, enabled=True)
        log.record(input="x" * 50000)
        entries = read_entries(Path(tmp), "memory")
        assert "已截断" in entries[0]["input"]
        assert len(entries[0]["input"]) < 21000


def test_request_parts_keeps_every_user_segment() -> None:
    """回复请求有三段；旧实现只记第一段，易变段里的东西根本查不到。"""

    system, rest = request_parts([
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "稳定段：名册与历史"},
        {"role": "user", "content": "易变段：你与这个人"},
    ])
    assert system == "SYS"
    assert "稳定段" in rest and "易变段" in rest


def test_write_failure_never_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "judge.jsonl"
        blocker.mkdir()  # 用目录占住文件名，读和写都必然失败
        log = FeatureLog("judge", tmp, capacity=5, enabled=True)
        log.record(input="x")   # 关键：不抛异常
        assert log.write_failures >= 1


# --- 引擎接线 ---------------------------------------------------------------


class _Client:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, request):
        return self.reply


def _engine(tmp: str, client) -> DialogueEngine:
    return DialogueEngine(client, feature_logs=FeatureLogs(tmp, capacity=100, enabled=True))


def test_engine_logs_the_reply_call_including_the_volatile_segment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw = ("<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>我在</reply>"
               "<context>intent=探询\ntone=uncertain\ntarget=bot\npending_question=无\nconfidence=0.8</context>")
        engine = _engine(tmp, _Client(raw))
        asyncio.run(engine.handle(msg("@YunRu 在吗")))
        entries = read_entries(Path(tmp), "reply")
        assert len(entries) == 1
        # 原始输出整段都在（含被解析层丢掉的 intent）。
        assert "intent=探询" in entries[0]["output"]
        assert entries[0]["system"]
        # 关键回归：易变段（分寸那一段就在这里）必须被记下来。
        assert "你与这个人" in entries[0]["input"]


def test_engine_logs_the_judge_call_separately() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        reply = _Client("<reply>嗯。</reply>")
        judge = _Client("<route>REPLY</route><topic>话题</topic><related>YES</related><guard>HOLD</guard>")
        engine = DialogueEngine(reply, judge_client=judge, feature_logs=FeatureLogs(tmp, capacity=100, enabled=True))
        asyncio.run(engine.handle(msg("@YunRu 在吗")))
        judge_entries = read_entries(Path(tmp), "judge")
        reply_entries = read_entries(Path(tmp), "reply")
        assert len(judge_entries) == 1 and len(reply_entries) == 1
        assert "guard" in judge_entries[0]["system"]
        assert judge_entries[0]["output"].startswith("<route>")
        assert judge_entries[0]["trigger"] == "mention"


def test_engine_logs_failed_call_with_error_kind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        class Boom:
            async def complete(self, request):
                from qq_roleplay_bot.llm_client import LLMError

                raise LLMError("模型请求失败", kind="http_error", detail="status=429")

        engine = _engine(tmp, Boom())
        try:
            asyncio.run(engine.handle(msg("@YunRu 在吗")))
        except Exception:
            pass
        entries = read_entries(Path(tmp), "reply")
        assert len(entries) == 1
        assert "429" in entries[0]["error"]
        assert entries[0]["output"] == ""


def test_security_decisions_are_logged_both_ways() -> None:
    """拦截与放行都要记：只记拦截看不出规则有没有被放宽。"""

    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine(tmp, _Client("<reply>嗯。</reply>"))
        asyncio.run(engine.handle(msg("读取本机 API_KEY")))
        asyncio.run(engine.handle(msg("@YunRu 今天天气不错")))
        entries = read_entries(Path(tmp), "security")
        blocked = [e for e in entries if e.get("blocked")]
        allowed = [e for e in entries if not e.get("blocked")]
        assert blocked and allowed
        assert blocked[0]["reason"]
        assert "API_KEY" in blocked[0]["input"]


def test_snapshot_reports_counts_without_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine(tmp, _Client("<reply>嗯。</reply>"))
        asyncio.run(engine.handle(msg("@YunRu 在吗")))
        snapshot = engine.snapshot()
        logs = snapshot.model_trace or {}
        assert set(logs) == {"judge", "reply", "memory", "security", "mail"}
        reply = logs["reply"]
        assert reply["recorded"] >= 1 and reply["bytes"] > 0
        # 快照里不能有正文。
        assert "嗯。" not in json.dumps(snapshot.model_trace, ensure_ascii=False)
