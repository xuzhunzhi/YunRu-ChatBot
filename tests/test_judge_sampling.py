"""判定采样：群消息来得太快时，攒几条再让判定 agent 看一眼（端到端判据）。

规则来源（用户 2026-10-10 原话）：

- *"如果短时间内入栈消息太多则修改判定 agent 调起频率，达到一定量变成 5 条一次，
  达到一定量变成 10 条一次，达到一定量变成 20 条一次"*；
- *"五条跑一次指判定这五条有没有回复的必要而不是 5 条取一条剩下的不管"*。

档位表（默认配置，`QQBOT_JUDGE_RATE_*` 全部调用时现读）：

    速率（条 / 30 秒）    判定行为
    < 8                  每条各判一次（现状）
    >= 8                 攒满 5 条判一次
    >= 16                攒满 10 条判一次
    >= 30                攒满 20 条判一次

这一份钉住：

1. 慢速（5 条/30s → 档 0）→ 每条都判，行为与改动前一致；
2. 中速（12 条/30s → 档 1）→ 档 1 稳态下判定调用数 = 消息数 ÷ 5（不足一批的
   攒着等下一批，不取上整也不丢）；
3. 高速（35 条/30s → 档 3）→ 档 3 稳态下判定调用数 = 消息数 ÷ 20；
4. 任意速率下 @ 她的那条**立刻触发判定**（不等攒满），积攒清零；
5. 速率回落 → 档位回落（窗口滚动），下一批回到更小的攒数；
6. 跳过的消息**全部在历史里**（判定下一次看得到），跳过时不产生任何模型调用；
7. 私聊不走采样；冷群原有的"攒 20 条巡检"路径不受影响（闸不再加一层）；
8. 全部参数调用时现读（改环境变量立刻生效）+ 默认值 / 夹取 / 坏值回落；
9. `/super status` 印出"判定采样：当前档 X / 本群攒了 Y 条 / 跳过 Z 次"；
10. 跳过时记的是 DEBUG（不刷 INFO）。

突变（各自还原，`tests/run_single.py` 单文件验）：

- 把采样闸整个去掉（`admit` 恒放行）→ 第 2/3 条红（高频下判定调用量不降）；
- 去掉 @ 豁免（`mentioned` 当 False 传进闸）→ 第 4 条红；
- 档位查表写死不读环境变量 → 第 8 条红。
"""
import asyncio
import contextlib
import logging
import os
from unittest.mock import patch

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SESSION = f"group:{GROUP}"
TARGET = MessageTarget(group_id=GROUP)

_JUDGE_RATE_KEYS = (
    "QQBOT_JUDGE_RATE_WINDOW_SECONDS",
    "QQBOT_JUDGE_RATE_TIER1",
    "QQBOT_JUDGE_RATE_TIER2",
    "QQBOT_JUDGE_RATE_TIER3",
    "QQBOT_JUDGE_RATE_BATCH_SIZES",
)


@contextlib.contextmanager
def _default_judge_rate_env():
    """确保这组用例跑在默认档位表上（环境里就算有旧值也不影响）。"""

    with patch.dict(os.environ):
        for key in _JUDGE_RATE_KEYS:
            os.environ.pop(key, None)
        yield


class _Clock:
    """可拨的假单调钟：窗口（30 秒滚动）全靠它控制。"""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _ScriptedJudge:
    """记录判定请求；第一次回 REPLY（热身让会话进入活跃），其余一律 NO_REPLY。"""

    def __init__(self, *, first_replies: bool = True) -> None:
        self.requests = []
        self._first = first_replies

    async def complete(self, request):
        self.requests.append(request)
        if self._first:
            self._first = False
            return "<route>REPLY</route><from>none</from><topic>话题</topic>"
        return "<route>NO_REPLY</route><from>none</from>"


class _ScriptedReply:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return "<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>嗯。</reply>"


def _msg(index, text="闲话", *, mentioned=False, user_id="100") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id=SESSION, user_id=user_id,
        text=text, target=TARGET, sender_name="某人", is_bot_mentioned=mentioned,
    )


def _private_msg(index, user_id="100") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"p{index}", session_id=f"private:{user_id}", user_id=user_id,
        text="私聊的话", target=MessageTarget(user_id=user_id), sender_name="某人",
    )


def _engine(clock: _Clock, **kwargs):
    judge = _ScriptedJudge()
    reply = _ScriptedReply()
    engine = DialogueEngine(
        reply, judge_client=judge, clock=clock,
        enabled_group_ids=frozenset({GROUP}), **kwargs,
    )
    return engine, judge, reply


def _warm_up(engine: DialogueEngine, clock: _Clock) -> None:
    """@ 她一句让会话进入活跃（此后每条群消息都走判定路径，正是采样闸的管辖地）。"""

    asyncio.run(engine.handle(_msg(1, "@YunRu 在吗", mentioned=True)))


# --- 1. 慢速：档 0，每条都判（与现状一致）--------------------------------------


def test_slow_rate_judges_every_message() -> None:
    clock = _Clock(1000.0)
    with _default_judge_rate_env():
        engine, judge, _ = _engine(clock)
        _warm_up(engine, clock)
        # 慢速：此后 5 条 / 30 秒（间隔 6 秒），窗口内任何时刻都 < 8 条 → 档 0。
        for i, offset in enumerate((6.0, 12.0, 18.0, 24.0, 30.0), start=2):
            clock.now = 1000.0 + offset
            asyncio.run(engine.handle(_msg(i, f"慢速{i}")))
        # 每条都判：热身 1 次 + 5 条各 1 次；零跳过。
        assert len(judge.requests) == 6, "慢速下每条消息都必须各判一次（现状不变）"
        assert engine.snapshot().judge_calls == 6
        info = engine.snapshot().judge_sampling["sessions"][SESSION]
        assert (info["tier"], info["batch"]) == (0, 1)
        assert info["skipped"] == 0
        assert engine.judge_sampling.skipped_total == 0


# --- 2. 中速：档 1，攒 5 条判一次 -----------------------------------------------


def test_medium_rate_batches_five_messages_per_judge_call() -> None:
    clock = _Clock(2000.0)
    with _default_judge_rate_env():
        engine, judge, reply = _engine(clock)
        _warm_up(engine, clock)
        # 12 条 / 30 秒（间隔 1.5 秒）：第 8 条起窗口内 >= 8 条 → 档 1（攒 5）。
        counts = []
        for i in range(2, 14):
            clock.now = 2000.0 + (i - 1) * 1.5
            asyncio.run(engine.handle(_msg(i, f"中速{i}")))
            counts.append(len(judge.requests))
        # 第 2..7 条速率还没到 8 → 各判一次（计数 2..7）；第 8..11 条攒着（计数停在 7，
        # 共五个 7：第七条自己那次 + 四次攒）；第 12 条攒满 5 判一次；第 13 条重新攒
        # （等下一批，不取上整也不丢）。
        assert counts == [2, 3, 4, 5, 6, 7, 7, 7, 7, 7, 8, 8], counts
        # 档 1 稳态段（第 8..12 条，5 条）恰好只判一次 = 消息数 ÷ 5。
        info = engine.snapshot().judge_sampling["sessions"][SESSION]
        assert (info["tier"], info["batch"]) == (1, 5)
        assert info["pending"] == 1, "没攒满的那条在等下一批"
        assert info["skipped"] == 5
        # 被跳过的消息全部在历史里（判定下一次看得到，一条不丢）。
        history_texts = [m.text for m in engine.sessions.state(SESSION).history]
        for i in (8, 9, 10, 11, 13):
            assert f"中速{i}" in history_texts
        # 跳过期间没有任何模型调用：判定调用数不动（见 counts），回复只热身过一次。
        assert len(reply.requests) == 1


# --- 3. 高速：档 3，攒 20 条判一次 ----------------------------------------------


def test_high_rate_batches_twenty_messages_per_judge_call() -> None:
    clock = _Clock(3000.0)
    with _default_judge_rate_env():
        engine, judge, reply = _engine(clock)
        _warm_up(engine, clock)
        # 间隔 0.5 秒连发 48 条：窗口内速率一路涨过 8 / 16 / 30 → 档 1 → 2 → 3。
        checkpoints = {}
        for i in range(2, 50):
            clock.now = 3000.0 + (i - 1) * 0.5
            asyncio.run(engine.handle(_msg(i, f"高速{i}")))
            if i in (7, 12, 22, 41, 42, 49):
                checkpoints[i] = len(judge.requests)
        # 低速段（到第 7 条）各判一次；之后档 1 攒 5、档 2 攒 10、档 3 攒 20。
        assert checkpoints == {7: 7, 12: 8, 22: 9, 41: 9, 42: 10, 49: 10}, checkpoints
        # 档 3 稳态段（第 30..49 条，20 条）恰好只判一次 = 消息数 ÷ 20。
        info = engine.snapshot().judge_sampling["sessions"][SESSION]
        assert (info["tier"], info["batch"]) == (3, 20)
        assert len(judge.requests) == 10, "49 条消息只许判 10 次"
        assert info["skipped"] == 39
        # 全部 49 条都在历史里（热身 + 48 条），跳过不等于丢。
        history_texts = {m.text for m in engine.sessions.state(SESSION).history}
        assert all(f"高速{i}" in history_texts for i in range(2, 50))
        assert len(reply.requests) == 1, "判定 NO_REPLY 之外，跳过的连判定都不调"


# --- 4. @ 豁免：没攒满也立刻判 ---------------------------------------------------


def test_mentioned_message_judges_immediately_without_waiting_for_batch() -> None:
    clock = _Clock(4000.0)
    with _default_judge_rate_env():
        engine, judge, reply = _engine(clock)
        _warm_up(engine, clock)
        for i in range(2, 8):  # 第 2..7 条（速率 2..7）各判一次 → 共 7 次
            clock.now = 4000.0 + (i - 1) * 1.0
            asyncio.run(engine.handle(_msg(i)))
        assert len(judge.requests) == 7
        for i in (8, 9, 10):  # 速率 >= 8 → 档 1：攒到 3 条，还不够 5
            clock.now = 4000.0 + (i - 1) * 1.0
            asyncio.run(engine.handle(_msg(i)))
        assert len(judge.requests) == 7
        # 这条没攒满（4 < 5），但 @ 她 → 立刻判定；判定照旧决定回不回（NO_REPLY）。
        clock.now = 4010.0
        result = asyncio.run(engine.handle(_msg(11, "@YunRu 叫我", mentioned=True)))
        assert result is None
        assert len(judge.requests) == 8, "被 @ 的那条不许等攒满"
        assert len(reply.requests) == 1
        info = engine.snapshot().judge_sampling["sessions"][SESSION]
        assert info["pending"] == 0, "@ 触发之后积攒清零"
        # 清零之后从头攒：下一条不到 5 条不判。
        clock.now = 4011.0
        asyncio.run(engine.handle(_msg(12)))
        assert len(judge.requests) == 8


# --- 5. 速率回落：档位回落，下一批回到更小的攒数 ---------------------------------


def test_rate_falling_back_returns_to_smaller_batches() -> None:
    clock = _Clock(5000.0)
    with _default_judge_rate_env():
        engine, judge, _ = _engine(clock)
        _warm_up(engine, clock)
        for i in range(2, 36):  # 0.5 秒一条 → 速率 35 → 档 3（攒 20）
            clock.now = 5000.0 + (i - 1) * 0.5
            asyncio.run(engine.handle(_msg(i)))
        assert len(judge.requests) == 9
        assert engine.snapshot().judge_sampling["sessions"][SESSION]["tier"] == 3
        # 窗口滚空（35 秒 > 30 秒）再来一条：速率归 1 → 档 0 → 立刻判。
        clock.now += 35.0
        asyncio.run(engine.handle(_msg(36, "回落")))
        assert len(judge.requests) == 10
        info = engine.snapshot().judge_sampling["sessions"][SESSION]
        assert (info["tier"], info["pending"]) == (0, 0)
        # 再涨回中等速率（第 43 条起窗口内 >= 8 条 → 档 1）：下一批只要攒 5 条，
        # 不是 20 条——第 44..46 条攒着不判，第 47 条攒满判一次。
        for i in range(37, 48):
            clock.now += 0.5
            asyncio.run(engine.handle(_msg(i)))
            if i == 42:
                assert len(judge.requests) == 16  # 慢速段（速率 < 8）逐条判到此为止
            if i in (43, 44, 45, 46):
                assert len(judge.requests) == 16, f"第 {i} 条在攒（档 1，攒 5）"
        assert len(judge.requests) == 17, "攒满 5 条判一次（档 3 的 20 条没跟着回来）"
        assert engine.snapshot().judge_sampling["sessions"][SESSION]["tier"] == 1


# --- 6. 私聊不走采样 -------------------------------------------------------------


def test_private_chat_is_never_sampled() -> None:
    clock = _Clock(6000.0)
    with _default_judge_rate_env():
        engine, judge, _ = _engine(clock, private_debug_user_ids=frozenset({"100"}))
        # 12 条 / 15 秒——放群里已经是档 1（攒 5），私聊必须每条都判。
        for i in range(1, 13):
            clock.now = 6000.0 + (i - 1) * 0.5
            asyncio.run(engine.handle(_private_msg(i)))
        assert len(judge.requests) == 12, "私聊不走采样，每条都判（现状不变）"
        assert engine.judge_sampling.skipped_total == 0
        assert not (engine.snapshot().judge_sampling or {}).get("sessions")


# --- 7. 冷群原有的 20 条阈值路径不受影响 -----------------------------------------


def test_cold_group_threshold_path_still_judges_once_per_twenty() -> None:
    clock = _Clock(7000.0)
    with _default_judge_rate_env():
        engine, judge, _ = _engine(clock)
        judge._first = False  # 全程 NO_REPLY：这里只数判定调用
        for i in range(1, 21):  # 没人叫她 → 冷群攒 20 条阈值（改动前的行为）
            clock.now = 7000.0 + (i - 1) * 1.0
            result = asyncio.run(engine.handle(_msg(i, f"冷群{i}")))
            if i < 20:
                assert result is None
        # 前 19 条攒阈值不判；第 20 条阈值触发，采样闸不再加一层（pending 20 >= 任何档）。
        assert len(judge.requests) == 1, "冷群 20 条巡检一次的行为与改动前一致"
        assert engine.judge_sampling.skipped_total == 0


# --- 8. 配置：调用时现读 + 默认值 / 夹取 / 坏值回落 ------------------------------


def test_tier_thresholds_are_read_at_call_time() -> None:
    # TIER1=2：第 2 条消息就进档 1（攒 5）。默认（8）下前 7 条会各判一次——
    # 这个断言就是突变"档位查表写死"的判据。
    clock = _Clock(10000.0)
    with patch.dict(os.environ, {"QQBOT_JUDGE_RATE_TIER1": "2"}):
        engine, judge, _ = _engine(clock)
        _warm_up(engine, clock)  # 速率 1 < 2 → 档 0 → 判（第 1 次）
        for i in range(2, 7):
            clock.now += 0.5
            asyncio.run(engine.handle(_msg(i)))
        # 速率 2..6 全 >= 2 → 档 1：第 2..5 条攒，第 6 条攒满 5 判（第 2 次）。
        assert len(judge.requests) == 2, "TIER1=2 生效：攒 5 条只判了 2 次"


def test_batch_sizes_are_read_at_call_time() -> None:
    clock = _Clock(10500.0)
    with patch.dict(os.environ, {"QQBOT_JUDGE_RATE_TIER1": "2",
                                 "QQBOT_JUDGE_RATE_BATCH_SIZES": "3"}):
        engine, judge, _ = _engine(clock)
        _warm_up(engine, clock)  # 第 1 次判定
        for i in range(2, 8):
            clock.now += 0.5
            asyncio.run(engine.handle(_msg(i)))
        # 档 1 攒 3 条：第 4、7 条各判一次 → 共 3 次。默认攒 5 的话只会是 2 次。
        assert len(judge.requests) == 3, "BATCH_SIZES=3 生效：攒 3 条判一次"


def test_window_seconds_are_read_at_call_time() -> None:
    clock = _Clock(11000.0)
    with patch.dict(os.environ, {"QQBOT_JUDGE_RATE_WINDOW_SECONDS": "5"}):
        engine, judge, _ = _engine(clock)
        _warm_up(engine, clock)
        for i in range(2, 10):  # 到第 8 条速率 >= 8 → 档 1，第 8、9 条在攒
            clock.now += 0.5
            asyncio.run(engine.handle(_msg(i)))
        assert len(judge.requests) == 7
        # 窗口只有 5 秒：6 秒后旧消息全部滑出窗口 → 速率归 1 → 档 0 → 立刻判。
        clock.now += 6.0
        asyncio.run(engine.handle(_msg(10, "过了窗口")))
        assert len(judge.requests) == 8, "窗口滚空后回到档 0，立刻判"
        info = engine.snapshot().judge_sampling["sessions"][SESSION]
        assert info["tier"] == 0


def test_judge_rate_config_defaults_clamps_and_fallbacks() -> None:
    from qq_roleplay_bot import dev_config

    with patch.dict(os.environ):
        for key in _JUDGE_RATE_KEYS:
            os.environ.pop(key, None)
        assert dev_config.judge_rate_window_seconds() == 30.0
        assert dev_config.judge_rate_tier1() == 8
        assert dev_config.judge_rate_tier2() == 16
        assert dev_config.judge_rate_tier3() == 30
        assert dev_config.judge_rate_batch_sizes() == (5, 10, 20)
    with patch.dict(os.environ, {
        "QQBOT_JUDGE_RATE_WINDOW_SECONDS": "1",
        "QQBOT_JUDGE_RATE_TIER1": "1",
        "QQBOT_JUDGE_RATE_TIER2": "9999",
        "QQBOT_JUDGE_RATE_TIER3": "bad",
        "QQBOT_JUDGE_RATE_BATCH_SIZES": "0,x,7",
    }):
        assert dev_config.judge_rate_window_seconds() == 5.0, "窗口夹 5..300"
        assert dev_config.judge_rate_tier1() == 2, "TIER1 夹 2..100"
        assert dev_config.judge_rate_tier2() == 200, "TIER2 夹 4..200"
        assert dev_config.judge_rate_tier3() == 30, "坏值回落默认"
        assert dev_config.judge_rate_batch_sizes() == (1, 7), "0 夹到 1、坏条目跳过"
    with patch.dict(os.environ, {"QQBOT_JUDGE_RATE_BATCH_SIZES": "x,y"}):
        assert dev_config.judge_rate_batch_sizes() == (5, 10, 20), "全坏回落默认"


# --- 9. /super status 的观测行 ----------------------------------------------------


def test_super_status_reports_sampling_tier_pending_and_skips() -> None:
    clock = _Clock(8000.0)
    with _default_judge_rate_env():
        engine, judge, _ = _engine(clock, super_admin_user_ids=frozenset({"100"}))
        _warm_up(engine, clock)
        for i in range(2, 14):  # 同中速夹具：档 1 / 攒 1 条 / 跳 5 次
            clock.now = 8000.0 + (i - 1) * 1.5
            asyncio.run(engine.handle(_msg(i, f"中速{i}")))
        assert len(judge.requests) == 8
        status = asyncio.run(engine.handle(_msg(90, "/super status")))
        text = status.text
        assert "判定采样：group:717151356 当前档 1（攒满 5 条判一次）" in text
        assert "本群攒了 1 条 / 跳过 5 次" in text
        assert "判定采样：累计跳过判定 5 次" in text


# --- 10. 跳过时记 DEBUG，不刷 INFO ------------------------------------------------


def test_skip_logs_at_debug_level_only() -> None:
    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    clock = _Clock(12000.0)
    with _default_judge_rate_env():
        engine, judge, _ = _engine(clock)
        _warm_up(engine, clock)
        for i in range(2, 8):
            clock.now += 1.0
            asyncio.run(engine.handle(_msg(i)))
        module_logger = logging.getLogger("qq_roleplay_bot.judge_sampling")
        capture = _Capture()
        original_level = module_logger.level
        module_logger.setLevel(logging.DEBUG)
        module_logger.addHandler(capture)
        try:
            clock.now += 1.0
            asyncio.run(engine.handle(_msg(8)))  # 速率 8 → 档 1：这一条被攒下
            clock.now += 1.0
            asyncio.run(engine.handle(_msg(9)))
        finally:
            module_logger.removeHandler(capture)
            module_logger.setLevel(original_level)
        skips = [r for r in capture.records if "judge sampling" in r.getMessage()]
        assert skips, "跳过必须留痕（DEBUG 一行）"
        assert all(r.levelno == logging.DEBUG for r in skips), "只许 DEBUG，不刷 INFO"
        assert len(judge.requests) == 7


# --- 11. 会话清理时采样状态跟着走 --------------------------------------------------


def test_leaving_a_session_drops_its_sampler_state() -> None:
    clock = _Clock(13000.0)
    with _default_judge_rate_env():
        engine, _, _ = _engine(clock)
        _warm_up(engine, clock)
        asyncio.run(engine.handle(_msg(2)))
        assert engine.snapshot().judge_sampling["sessions"]
        engine.leave_session(SESSION)
        assert not engine.snapshot().judge_sampling["sessions"], "会话没了，计数不留僵尸"
