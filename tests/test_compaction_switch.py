"""面板那个「对话压缩」开关**真的能拉闸**（2026-10-08 失败降级审计第 4 条 + 核查的加强版）。

修之前：`compaction_enabled` 全仓只有定义与说明，**零消费者**；而且它声明的环境变量
（`QQBOT_COMPACTION`）也没接进 `build_flags`（那里硬编码 `True`）。于是
"压缩模型开始退化、或一直超时烧钱"时，面板拉不动、环境变量也拉不动——真出事时没有闸门。

这一份钉三件事：

1. **关掉 → 一次压缩请求都不发**：`_maybe_compact` 在算待压缩段之前就返回，
   历史条数、`summary_through`、`compaction_calls` 全都不动（模型调用数不变）。
   把那条判断去掉（突变），第 1 条必红。
2. **开着 → 照常压**（默认行为不变）。
3. **环境变量真的接上了**：`QQBOT_COMPACTION=0` 让 `build_flags()` 造出来的开关为假，
   没设时为真。改回硬编码 `True`（突变），环境变量那几条必红。

`QQBOT_PERSONA_OFFSET_HOURS` 那种"调用时现读"的纪律同样适用于这里：
`build_flags()` 是在装配时调的，读的是**那一刻**的 `os.environ`。
"""
import asyncio
import os
from unittest.mock import patch

from qq_roleplay_bot import runtime_flags
from qq_roleplay_bot.runtime_flags import RuntimeFlags
from qq_roleplay_bot.stage3_main import COMPACT_KEEP, DialogueEngine
from qq_roleplay_bot.stage3_runtime import ConversationState
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"


def message(index: int) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}",
        session_id=SESSION,
        user_id=f"100{index % 3}",
        text=f"第{index:04d}句" + "字" * 36,
        target=TARGET,
        sender_name=f"某人{index % 3}",
    )


def build_state(count: int) -> ConversationState:
    state = ConversationState(history_limit=2000)
    for index in range(count):
        state.add(message(index), 0.0)
    return state


class ScriptedCompactor:
    """记录每一次压缩请求。`requests` 就是"模型调用数"。"""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def complete(self, request):
        self.requests.append(request[1]["content"])
        return f"第{len(self.requests)}批摘要。"


def _engine(compactor: ScriptedCompactor, *, compaction_enabled: bool) -> DialogueEngine:
    """实例级替身，不 install 到进程上（免得污染同进程里别的测试文件）。"""

    engine = DialogueEngine(compactor, judge_client=compactor)
    engine._flags = lambda: RuntimeFlags(compaction_enabled=compaction_enabled)
    return engine


# --- 1 / 2. 开关管着那两条压缩路 --------------------------------------------


def test_switch_off_sends_no_compaction_request() -> None:
    """关掉：攒满阈值也不发请求，历史原样留着。"""

    state = build_state(520)
    compactor = ScriptedCompactor()
    engine = _engine(compactor, compaction_enabled=False)
    asyncio.run(engine._maybe_compact(state, session_id=SESSION))

    assert compactor.requests == [], "关掉了还在调压缩模型"
    assert engine._stats["compaction_calls"] == 0, "关掉了还记了一次压缩调用"
    assert state.summary_through is None and not state.summary, "关掉了却改了摘要"
    assert len(state.history) == 520, "关掉了却动了窗口"


def test_switch_off_also_skips_the_forced_compaction() -> None:
    """切走当值群那条（`force=True`）同样受闸门管——它也是压缩。

    用 120 条：`force` 那条不看 500 的门槛，只要求"还有可压的"（> `COMPACT_KEEP`），
    所以它必须能在真的会压的量级上验"关掉就不压"。
    """

    state = build_state(120)
    compactor = ScriptedCompactor()
    engine = _engine(compactor, compaction_enabled=False)

    async def run() -> None:
        await engine._force_compact(state, SESSION)

    asyncio.run(run())
    assert compactor.requests == []
    assert state.summary_through is None
    assert len(state.history) == 120


def test_switch_on_keeps_compacting_as_before() -> None:
    """开着（默认）：行为与改动前逐字相同——照压、照推进边界、照留尾巴。"""

    state = build_state(520)
    compactor = ScriptedCompactor()
    engine = _engine(compactor, compaction_enabled=True)
    asyncio.run(engine._maybe_compact(state, session_id=SESSION))

    assert compactor.requests, "开着却不压了"
    assert engine._stats["compaction_calls"] == len(compactor.requests)
    assert state.summary_through is not None and state.summary
    assert len(state.history) == COMPACT_KEEP


# --- 3. 环境变量 -------------------------------------------------------------


def test_absent_env_var_keeps_the_switch_on() -> None:
    with patch.dict(os.environ):
        os.environ.pop("QQBOT_COMPACTION", None)
        assert runtime_flags.build_flags().compaction_enabled is True


def test_the_env_var_can_pull_the_switch_off() -> None:
    """`QQBOT_COMPACTION=0` 必须真的关掉它——修之前这一条必红（硬编码 True）。"""

    for text in ("0", "false", "off", "no", "FALSE"):
        with patch.dict(os.environ, {"QQBOT_COMPACTION": text}):
            assert runtime_flags.build_flags().compaction_enabled is False, text


def test_the_env_var_can_keep_the_switch_on() -> None:
    for text in ("1", "true", "on", "yes", "TRUE"):
        with patch.dict(os.environ, {"QQBOT_COMPACTION": text}):
            assert runtime_flags.build_flags().compaction_enabled is True, text


def test_a_blank_or_unreadable_value_falls_back_to_the_default() -> None:
    """空串与拼错的值都当"没配"：默认是开，不许因为一个手滑把功能关掉。"""

    for text in ("", "   ", "maybe", "2"):
        with patch.dict(os.environ, {"QQBOT_COMPACTION": text}):
            assert runtime_flags.build_flags().compaction_enabled is True, text


# --- 说明文字（面板上渲染的就是它） -----------------------------------------


def test_the_flag_note_says_what_happens_when_off() -> None:
    note = runtime_flags.FLAG_NOTES["compaction_enabled"]
    assert "关掉" in note and "不发" in note and "历史原样" in note, note
    assert "QQBOT_COMPACTION" in note, "说明里要写出环境变量那个口子"


if __name__ == "__main__":
    import unittest

    unittest.main()
