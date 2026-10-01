"""`/super status` 与 `/super apicheck` 的分工：概览归概览，对账归对账。

用户 2026-09-27 定的：status 看"现在正常吗"（本次重启之后、内存、计数、日志），
命中率与余额单独走 `/super apicheck`——而且**按 key 分开算**，不是一个账号一个数。
"""
import asyncio
import os
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.api_usage import ApiUsageStore
from qq_roleplay_bot.metrics import process_memory_bytes
from qq_roleplay_bot.stage3_main import (
    DialogueEngine,
    SuperAction,
    parse_super_command,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OWNER = "900000001"


class Counter:
    def __init__(self, hit: int = 0, miss: int = 0, calls: int = 0) -> None:
        self._stats = (hit, miss, calls)

    def cache_stats(self) -> dict[str, object]:
        hit, miss, calls = self._stats
        return {"calls": calls, "hit_tokens": hit, "miss_tokens": miss, "hit_rate": 0.0}

    async def complete(self, request):
        return "<reply>嗯。</reply>"


def msg(text: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m-{text}", session_id=f"group:{GROUP}", user_id=OWNER,
        text=text, target=MessageTarget(group_id=GROUP), is_bot_mentioned=True,
    )


def engine_with_keys() -> DialogueEngine:
    engine = DialogueEngine(
        Counter(0, 0, 0),
        client_factory=lambda sid: Counter(180_000, 20_000, 40),
        judge_client=Counter(90_000, 10_000, 30),
        judge_client_factory=lambda sid: Counter(90_000, 10_000, 30),
        super_admin_user_ids=frozenset({OWNER}),
    )
    engine.memory_client = Counter(30_000, 45_000, 12)
    engine._client_for("group:a")
    return engine


# --- 解析 -------------------------------------------------------------------


def test_apicheck_is_a_super_command() -> None:
    assert parse_super_command("/super apicheck") is SuperAction.API_CHECK
    assert parse_super_command("/super api") is SuperAction.API_CHECK
    assert parse_super_command("/super 接口对账") is SuperAction.API_CHECK
    # 别把别的命令吃掉
    assert parse_super_command("/super status") is SuperAction.STATUS


def test_apicheck_is_super_admin_only() -> None:
    engine = engine_with_keys()
    denied = asyncio.run(engine.handle(IncomingMessage(
        message_id="m1", session_id=f"group:{GROUP}", user_id="100",
        text="/super apicheck", target=MessageTarget(group_id=GROUP),
    )))
    assert denied is None


# --- 按 key 的口径 ----------------------------------------------------------


def test_cache_report_separates_the_three_keys() -> None:
    engine = engine_with_keys()
    report = engine.cache_report()
    assert report["dialogue"]["calls"] == 40
    assert report["judge"]["calls"] == 30
    assert report["memory"]["calls"] == 12
    # 每个 key 有自己的命中率，而不是一个总数糊在一起
    assert report["dialogue"]["hit_rate"] == 0.9
    assert report["judge"]["hit_rate"] == 0.9
    assert report["memory"]["hit_rate"] == 0.4
    assert report["total"]["calls"] == 82


def test_apicheck_renders_one_line_per_agent() -> None:
    engine = engine_with_keys()

    async def fake_balance() -> str:
        return "余额：CNY 42.00（赠送 2.00 / 充值 40.00）"

    engine._balance_line = fake_balance  # type: ignore[assignment]
    result = asyncio.run(engine.handle(msg("/super apicheck")))
    assert result is not None
    text = result.text
    assert "QQBOT_API_KEY" in text and "QQBOT_JUDGE_API_KEY" in text and "QQBOT_MEMORY_API_KEY" in text
    # **每个 agent 各一行**，包括 2026-09-30 用户点名的"审核 agent"
    # （"审核 agent 用量为什么不在 apicheck 里显示"）
    for marker in ("回复 agent", "判定 agent", "记忆维护", "风格审核", "识图", "写信 agent"):
        assert marker in text, marker
    assert "QQBOT_REVIEW_API_KEY" in text and "QQBOT_VISION_API_KEY" in text and "QQBOT_MAIL_API_KEY" in text
    assert "90.0%" in text and "40.0%" in text
    assert "余额：CNY 42.00" in text
    # 只有超管看得到，所以档位/余额可以出现在正文里
    assert engine.last_cache_report is not None
    assert engine.last_cache_report["reason"] == "apicheck"


def test_apicheck_tells_missing_agents_apart_from_unused_ones() -> None:
    """`没启用` 与 `还没有调用` 是两件事，不能混成一句。"""

    engine = DialogueEngine(Counter(), super_admin_user_ids=frozenset({OWNER}))

    async def fake_balance() -> str:
        return "余额：未配置查询凭据"

    engine._balance_line = fake_balance  # type: ignore[assignment]
    result = asyncio.run(engine.handle(msg("/super apicheck")))
    assert result is not None
    text = result.text
    # 回复 agent 有 client 但一次没调过；判定/记忆/审核/识图都没装配
    assert "回复 agent（QQBOT_API_KEY）：本次还没有调用" in text
    for marker in ("判定 agent", "记忆维护", "风格审核", "识图"):
        assert f"{marker}" in text and "没启用" in text
    assert text.count("没启用") == 4
    # 写信 agent 由后台插件装配，这里看不到 client——不假装知道，按"还没调用"报
    assert "写信 agent（QQBOT_MAIL_API_KEY，可回落主 key）：本次还没有调用" in text


def test_apicheck_shows_the_review_agent_usage() -> None:
    """审核 agent 的账要真的显示出来（用户点名的就是这条）。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = ApiUsageStore(Path(tmp) / "usage.json", clock=time.time)
        store.add_usage("review", {"prompt_tokens": 600, "completion_tokens": 30,
                                   "prompt_cache_hit_tokens": 400,
                                   "prompt_cache_miss_tokens": 100})
        store.add_usage("vision", {"prompt_tokens": 200, "prompt_cache_hit_tokens": 1_500,
                                   "prompt_cache_miss_tokens": 500})
        store._save(force=True)
        engine = engine_with_keys()
        engine.usage_store = store
        engine.style_reviewer = Counter(0, 0, 0)

        async def fake_balance() -> str:
            return "余额：未配置查询凭据"

        engine._balance_line = fake_balance  # type: ignore[assignment]
        result = asyncio.run(engine.handle(msg("/super apicheck")))
        assert result is not None
        text = result.text
        assert "风格审核（QQBOT_REVIEW_API_KEY，可回落主 key）：调用 1 次，命中率 80.0%" in text
        assert "（命中 400 / 未命中 100）" in text
        # 上千才缩成 k——小数目写成 0k 看着像没数据
        assert "（命中 2k / 未命中 500）" in text


# --- status 的分工 ----------------------------------------------------------


def test_status_shows_uptime_and_memory_but_not_hit_rate() -> None:
    engine = engine_with_keys()
    result = asyncio.run(engine.handle(msg("/super status")))
    assert result is not None
    text = result.text
    assert "本次重启之后" in text
    assert "已运行：" in text
    assert "内存：" in text
    assert "功能日志：" in text
    # 命中率与余额**不该**再出现在概览里
    assert "命中率" not in text
    assert "余额" not in text
    assert "提示词缓存" not in text


def test_memory_reading_is_available_on_this_platform() -> None:
    """`/super status` 的内存数字必须真的读得到，不能永远显示 `—`。

    踩过：没声明 ctypes 的 argtypes/restype 时 HANDLE 被当成 32 位传，
    函数返回 FALSE，取值永远是 0（界面上看起来像"没这个功能"）。
    """

    if os.name not in {"nt", "posix"}:
        return
    current, peak = process_memory_bytes()
    assert current > 0, "取不到本进程内存占用"
    assert peak >= 0


def test_dialogue_end_triggers_a_cache_check() -> None:
    """一场交谈结束时对一次账：这是"每次对话判定结束就检查命中率"的落点。

    注意 client 的选择：传了 `client_factory` 时回复走**会话 client**，
    所以这里把这个假 client 直接当兜底 client 用（生产里兜底 client 只服务记忆）。
    """

    class ExitingCounter(Counter):
        async def complete(self, request):
            return ("<decision>REPLY</decision><dialogue>EXIT</dialogue>"
                    "<reply>那就先这样。</reply>")

    engine = DialogueEngine(ExitingCounter(1, 1, 2))
    assert engine.last_cache_report is None
    asyncio.run(engine.handle(msg("@YunRu 拜拜")))
    assert engine.last_cache_report is not None
    assert engine.last_cache_report["reason"] == "dialogue_end"
    assert engine.last_cache_report["dialogue"]["calls"] == 2
