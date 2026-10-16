"""**引擎实际喂给 prompt 构造器的是哪一段历史**——按接口接线断言。

## 为什么需要这个文件（2026-10-15 复核实测，这是补假绿）

`tests/test_topic_window.py:174`
（`test_the_visible_window_is_the_whole_live_window`）自己调 `live_history()` 再断言
它等于整个窗口——**它测的是 `live_history()` 自己**，在"取数口换成最近 N 条"的旧代码上
照样绿。`tests/test_dialogue_judge.py:165`
（`test_judge_sees_the_whole_topic_not_a_sliding_tail`）那一条只证明**渲染器不截断**
它收到的那一段。两者都没有碰"引擎实际传进去的实参"。

复核做过的变异实验（把 `stage3_main.py` 两处取数口换成 `state.recent()[-12:]`，
名字全部保留）：**全量 1622 条里只有 3 条既有用例变红**，而且都在
`tests/test_dual_agent_comparison.py`（它们比的是相邻两轮 prompt 的前缀比例，
被那 12 条的截断顺带带红），**没有一条**是冲着"这段历史是不是整窗"来的。

本文件补的就是这条接线：**包住引擎真正调用的那两个构造函数**，把它的 `history` 形参抓下来，
再断言它等于调用那一刻的 `live_history()`。

## 口径依据（用户原话，逐字）

> 「**我还要说几次，不切话题**」（2026-10-15）

给模型的历史**一刀都不切**：回复段（`stage3_main.py` 的 `build_dialogue_messages` 调用）
与判定段（`dialogue_judge.build_judge_messages`）看的都是摘要之后累积的**全部**消息。

## 怎么保证它真的会红

两条断言都必须成立，缺一条就是一个新的假绿：

1. `captured == live_at_call`——**逐条**比（对象身份，见下面 `_same`）。
2. `len(captured) >= floor`——**地板**。只比"等于那一刻的 `live_history()`"是不够的：
   变异可能**同时**把取数口与那个"那一刻的快照"改成 `recent()[-12:]`，两边一起错、
   等式照旧成立。地板钉住"整窗必须真的在实参里"，而这种变异会让长度掉到 12。

所以：**任何"只喂最近 N 条"的写法都会红**（N 只要小于下面的地板就会被抓）。
"""
import asyncio
import os

from contextlib import contextmanager
from unittest.mock import patch

from qq_roleplay_bot import runtime_flags, stage3_main
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"

#: 造多少条历史。远小于 `LIVE_TARGET = 500`（所以不会触发压缩），
#: 又**远大于 12**——"只喂最近 12 条"的变异必须被地板抓住。
HISTORY_COUNT = 200


def message(index: int) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}",
        session_id=SESSION,
        user_id=str(1000 + index % 4),
        text=f"第{index:04d}句",
        target=TARGET,
        sender_name=f"某人{index % 4}",
        is_bot_mentioned=True,
    )


class ScriptedClient:
    """两用 client：判定答 REPLY、回复答一句固定正文、压缩答一段摘要。

    判定必须真跑（否则走不到 `_judge_on` 那一段，判定的实参就抓不到），
    所以本文件的用例都会显式装一份 `judge_enabled=True` 的运行期开关——见
    `_judge_on_engine_flags`。
    """

    async def complete(self, request):
        system = request[0]["content"]
        if "回应判定器" in system:
            return "<route>REPLY</route><topic>话题</topic>"
        if "压缩存档" in system:
            return "早先聊了不少事。"
        return ("<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>收到。</reply>")


@contextmanager
def _judge_on_engine_flags():
    """装一份"判定开着、其它都关"的运行期开关，跑完**还回原来那份**。

    为什么必须装：`DialogueEngine._flags()` 优先读 `runtime_flags.shared()` 这份**进程级
    单例**，而 `tests/test_memory.py` 那次真装配会把 `DUAL_AGENT_ENABLED` patch 成 False，
    把 `judge_enabled=False` 留在进程里（`runtime_flags.install` 的说明里记着这个坑）。
    不显式装一份，本文件就会随"谁先跑过"在"判定跑了"和"判定没跑"之间摇摆——
    而"判定没跑"的表现是断言 key 缺失，看起来像测试自己坏了。

    关掉 `review_enabled` / `vision_enabled` 是为了让这一轮只有"判定 + 回复"两次调用，
    实参抓取不被审核那条重写路径搅进来。

    还回**原来那份**（可能是 None）：随便留一份新的在进程里，后面靠
    `engine._default_flags` 的用例就全失效（`test_data_boundary.py` 里记着同一条）。
    """

    previous = runtime_flags.shared()
    # 采样闸（`judge_sampling.admit`）按**窗口秒数**数条数，而这一轮要把几百条历史
    # 一次性灌进状态。把窗口压到 0 秒，档位就不会因为"条数多"升上去而拦掉这一轮判定
    # ——本文件要测的是"喂了什么"，不是采样。
    with patch.dict(os.environ, {"QQBOT_JUDGE_RATE_WINDOW_SECONDS": "0"}):
        try:
            runtime_flags.install(runtime_flags.RuntimeFlags(
                judge_enabled=True, review_enabled=False, vision_enabled=False,
            ))
            yield
        finally:
            runtime_flags.install(previous)


def _same(captured: list[IncomingMessage], live: list[IncomingMessage]) -> bool:
    """逐条比**对象身份**。

    不能用 `==`：`IncomingMessage` 是 dataclass，两条内容一样的消息会相等，
    于是"喂了另一批同样内容的消息"也会通过——那正是这条测试要挡的事之一。
    也不能用 message_id：它比对象身份更容易被人为对齐。
    """

    return len(captured) == len(live) and all(a is b for a, b in zip(captured, live))


def run_engine(history_count: int = HISTORY_COUNT):
    """把 `history_count` 条历史灌进一个会话，再走一轮完整处理（判定 + 回复）。

    返回 `(实时快照表, 两个构造函数观察到的实参)`：

    * `realtime`：`{"judge": [...], "reply": [...]}`——**每次构造函数被调用的那一刻**，
      当场从引擎的会话状态上取的 `live_history()`。它必须与实参分头取，否则就是
      "拿同一份东西跟自己比"（那正是被复核点名的假绿形状）。
    * `captured`：`{"judge": [...], "reply": [...]}`——构造函数**实际收到**的 `history` 实参。
    """

    captured: dict[str, list[IncomingMessage]] = {}
    realtime: dict[str, list[IncomingMessage]] = {}
    # 引擎放进 dict 而不是普通局部变量：两个 spy 是闭包，
    # 直接闭包捕获 `engine` 会在"它还没被赋值"时报
    # `NameError: cannot access free variable`（spy 先于赋值执行）。
    box: dict[str, DialogueEngine] = {}
    # **两处都在 `stage3_main` 的命名空间里**：判定那个构造函数是 `from
    # .dialogue_judge import (..., build_judge_messages, ...)` 直接引进来的
    # （`stage3_main.py:46`），所以引擎调用走的是 `stage3_main.build_judge_messages`。
    # 第一版我 patch 的是 `dialogue_judge.build_judge_messages`——**那样一个都抓不到**
    # （spy 列表空、断言以 KeyError 的形式报"判定没跑"），而判定其实跑了 200 条。
    real_judge = stage3_main.build_judge_messages
    real_reply = stage3_main.build_dialogue_messages

    def judge_spy(history, *args, **kwargs):
        captured["judge"] = list(history)
        realtime["judge"] = list(box["engine"].sessions.state(SESSION).live_history())
        return real_judge(history, *args, **kwargs)

    def reply_spy(history, *args, **kwargs):
        captured["reply"] = list(history)
        realtime["reply"] = list(box["engine"].sessions.state(SESSION).live_history())
        return real_reply(history, *args, **kwargs)

    async def main():
        engine = DialogueEngine(ScriptedClient(), judge_client=ScriptedClient())
        box["engine"] = engine
        state = engine.sessions.state(SESSION)
        for index in range(history_count):
            state.add(message(index), 0.0)
        # 补进来的历史必须真的在整窗里（否则下面两条断言就成了同义反复）。
        assert len(state.live_history()) == history_count
        await engine.handle(message(history_count))
        return engine

    # **patched 的必须是引擎查名字的那个命名空间**：两个构造函数都在
    # `stage3_main` 的模块全局里（见上面 `real_judge` 的说明）。
    with _judge_on_engine_flags(), \
            patch.object(stage3_main, "build_judge_messages", judge_spy), \
            patch.object(stage3_main, "build_dialogue_messages", reply_spy):
        engine = asyncio.run(main())

    return realtime, captured, engine


# --- 两处取数口：实参就是那一刻的整窗 -----------------------------------------


def test_the_judge_is_handed_the_whole_live_window() -> None:
    """判定段的实参 = 调用那一刻的整窗（不是"最近 N 条"）。"""

    realtime, captured, _ = run_engine()
    assert "judge" in captured, "判定那一次调用没发生——引擎没走判定段？"
    assert _same(captured["judge"], realtime["judge"]), (
        "判定段收到的 history 不是那一刻的整窗："
        f"实参 {len(captured['judge'])} 条 vs 整窗 {len(realtime['judge'])} 条"
    )
    # 地板：等式可以被"两边一起截"绕过，长度不行。
    assert len(captured["judge"]) >= HISTORY_COUNT, (
        f"判定段只拿到 {len(captured['judge'])} 条，整窗是 {HISTORY_COUNT}+1 条——"
        "有人把历史截短了（用户 2026-10-15：*我还要说几次，不切话题*）"
    )


def test_the_reply_engine_is_handed_the_whole_live_window() -> None:
    """回复段的实参 = 调用那一刻的整窗（不是"最近 N 条"）。"""

    realtime, captured, _ = run_engine()
    assert "reply" in captured, "回复那一次调用没发生——判定没答 REPLY？"
    assert _same(captured["reply"], realtime["reply"]), (
        "回复段收到的 history 不是那一刻的整窗："
        f"实参 {len(captured['reply'])} 条 vs 整窗 {len(realtime['reply'])} 条"
    )
    assert len(captured["reply"]) >= HISTORY_COUNT, (
        f"回复段只拿到 {len(captured['reply'])} 条，整窗是 {HISTORY_COUNT}+1 条——"
        "有人把历史截短了（用户 2026-10-15：*我还要说几次，不切话题*）"
    )


def test_both_call_sites_see_the_same_window_in_a_plain_turn() -> None:
    """一轮里没有压缩时，两处实参是**同一段**（同一份列表内容）。

    这条守的是"回复段与判定段读同一份历史"这个形状本身；它**不是**"永远一致"的断言
    ——压缩轮里两处看到的历史会不同刻，见下面那条对照用例。
    """

    realtime, captured, _ = run_engine()
    assert _same(captured["judge"], captured["reply"]), (
        "同一轮里判定段与回复段拿到的历史不一样（这一轮没有压缩，两处应当同源）"
    )
    assert len(captured["reply"]) >= HISTORY_COUNT


# --- 对照：压缩轮里两处**不同刻** ---------------------------------------------
#
# 这一条是上面那条的边界说明，不是"历史被切了"。顺序（`stage3_main.py`）：
# 判定 `_judge`（约 :5132）→ `_maybe_compact`（约 :5171）→ 回复取数（约 :5197）。
# 压缩发生在判定与回复之间，所以**判定看的是压缩前的整窗、回复看的是压缩后的保留段**。
# 这正是 `stage3_main.py:5952` 那句"判定与回复看到的历史**永远一致**"不成立的原因
# （那一句已经改准，见那里的注释）。


def test_compaction_between_judge_and_reply_makes_the_two_windows_differ() -> None:
    """压缩轮里判定看到的是压缩前的整窗，回复看到的是压缩后的保留段。

    构造：先灌满 `LIVE_TARGET + 1` 条，再走一轮——这一轮 `_maybe_compact` 会真的压缩，
    于是回复段那一次拿到的条数**小于**判定那一次。
    """

    from qq_roleplay_bot.stage3_main import COMPACT_KEEP, LIVE_TARGET

    count = LIVE_TARGET + 1
    realtime, captured, engine = run_engine(count)
    assert "judge" in captured and "reply" in captured
    assert _same(captured["judge"], realtime["judge"])
    assert _same(captured["reply"], realtime["reply"])
    assert len(captured["judge"]) > len(captured["reply"]), (
        "压缩轮里两处应当不同刻：判定看压缩前整窗、回复看压缩后保留段"
    )
    assert len(captured["reply"]) <= COMPACT_KEEP + 1, (
        f"压缩后回复段应当回落到保留条数附近，实际 {len(captured['reply'])} 条"
    )
    assert engine.sessions.state(SESSION).summary_through is not None
