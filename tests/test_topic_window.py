"""她**实际看得见多少条**：视图不再按话题切，只剩压缩这一个变动点。

## 现在生效的口径（2026-10-15，用户原话）

> 「**我还要说几次，不切话题**」

意思是：给模型的历史**一刀都不切**。2026-10-02 定的那条"话题锚只裁剪视图"
**作废**——`ConversationState` 里不再有 `topic_start_seq`，
`topic_history()` / `advance_topic_start()` / `drop_summary_if_stale()` 三个成员
一起删掉，回复段与判定段都直接读 `live_history()`（摘要之后累积的全部消息）。
所以**视图与窗口是同一段**：她看得见的条数就是活窗口的条数。

活窗口的**下限**仍是压缩保留的 50 条（用户另一条口径「最短 50 条」，因此自动满足）。

⚠️ **但不要说成"唯一还会动历史的地方只剩满 500 条压缩"**（本文件原来第 14 行就是
那么写的，2026-10-15 复核实测指出**说过头了**）。按话题切的那条路确实已删，可是
还有三条会动历史：① 焦点交接的强制压缩（不看 500 门槛）；② 压缩关掉/连续失败时
`deque(maxlen=HISTORY_LIMIT=2000)` 静默丢最旧的消息（且它们从未进过摘要），
另有会话 LRU 整段丢；③ 压缩那一轮里判定比回复早看到历史。逐条见
`ARCHITECTURE.md` 的"话题锚已停用"。

⚠️ **本文件测不到第 ③ 条，也测不到"引擎到底喂了什么"**：下面所有断言都是
直接对 `ConversationState.live_history()` 自己下断言。**"引擎实际传给 prompt 构造器
的 history"由 `tests/test_history_wiring.py` 抓**（它包住两个构造函数取实参）。
两份文件职责不同，别把这一份当成接线证明——它曾经就是这样变成假绿的
（`test_the_visible_window_is_the_whole_live_window` 在"取数口换成最近 N 条"的
旧代码上照样绿）。

## 设计参数（文档规定；不是从代码行为反推的）

* `ARCHITECTURE.md:160`：短期窗口 `LIVE_TARGET = 500`（压缩阈值）、
  `COMPACT_KEEP = 50`（压缩后保留）、`HISTORY_LIMIT = 2000`（deque 安全上限）。
* `LONG_TERM_MEMORY_PLAN.md:27`：活窗口 = 一段压缩摘要 + 摘要之后累积的消息
  （**超过 500 条时压缩并保留 50 条**）。
* `docs/MULTI_GROUP_FOCUS.md:79`（那一节现在标题带"已停用"）：压缩在 **500 条**触发；
  压缩后进入回复段的是**摘要 + 最新 50 条**。（"回复段 = 从话题起点之后的消息"
  一句已作废。）

## 旧口径的实测依据（保留，不删）

那套话题锚不是拍脑袋来的，它当时要解释的现象是真的（2026-10-02 复核）：

| 量 | 旧实测（`run/data/logs/judge.jsonl` 1464 条判定） |
| --- | --- |
| 判定想往后退 | **0 次（0.0%）** |
| 原地确认 / 前进 | 1288 / **187** |
| "前进"落点离"最新那条" | 中位 **2**；**72/187 正好指在最新那条** |
| 判定那一刻窗口宽度 | 中位 **16**，p10 3，p90 48，**61% ≤20 条** |

也就是说：当时**62% 的"前进"等于把视图清到只剩最新一两句**，窗口永远长不到
设计的 500（`run/data/logs/reply.jsonl` 854 条样本里可见条数中位 **13**）。
用户 2026-10-15 的裁决是**把裁切本身去掉**，而不是再去调那个起点——
所以上面这些数字现在只用来解释"为什么曾经有过那套机制"，
**不再有对应的代码**。

## 这个文件干什么

钉住四件事：

1. **视图 = 活窗口**：`live_history()` 就是交给模型的那一段，谁也不按话题裁它。
2. **压缩之间视图只追加、长度单调不减**（同一批消息逐条加进来时是前缀关系）。
3. **消息本体只在满 500 条时被压缩收走**（进摘要、留最近 50 条）；
   "话题换了"**不再**丢摘要（那条判据随起点一起没了）。
4. **老状态兼容**：磁盘上还带着 `topic_start_seq` / `anchor_floor_migrated` 的
   状态文件必须能载入、不报错，且视图仍是整个载荷。
"""
import asyncio

from qq_roleplay_bot.stage3_main import COMPACT_KEEP, HISTORY_LIMIT, LIVE_TARGET, DialogueEngine
from qq_roleplay_bot.stage3_runtime import ConversationState, _format_history
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"

SPEAKERS = ("1001", "1002", "1003", "1004")


def message(index: int, *, speaker: int = 0) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}",
        session_id=SESSION,
        user_id=SPEAKERS[speaker % len(SPEAKERS)],
        text=f"第{index:04d}句",
        target=TARGET,
        sender_name=f"某人{speaker % len(SPEAKERS)}",
    )


def build_state(count: int, *, limit: int = HISTORY_LIMIT, speakers: int = 4) -> ConversationState:
    state = ConversationState(history_limit=limit)
    for index in range(count):
        state.add(message(index, speaker=index % speakers), 0.0)
    return state


class ScriptedCompactor:
    """压缩用的替身：记下请求原文，回一段固定摘要（离线、不发请求）。"""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def complete(self, request):
        self.requests.append(request[1]["content"])
        return f"第{len(self.requests)}批摘要。"


def compact(state: ConversationState) -> ScriptedCompactor:
    """跑一次真压缩（`DialogueEngine._maybe_compact`），返回那个替身以便核对。"""

    compactor = ScriptedCompactor()
    engine = DialogueEngine(compactor, judge_client=compactor)
    asyncio.run(engine._maybe_compact(state, session_id=SESSION))
    return compactor


def visible_seqs(state: ConversationState) -> list[int]:
    """交给模型的那一段的绝对序号。**取数口就是 `live_history()`**——
    视图与窗口是同一段，这里不再有第二个（按话题裁的）取数口。

    刻意建一个 `message_id → 绝对序号` 的表，而不是 `seq_of(message(i))`：
    `seq_of` 认的是**对象身份**（`item is message`），另造一个新对象永远查不到，
    会静默退化成"全是 None"——那样断言就全是假绿（这个坑我在这轮踩过一次）。
    """

    return [seq_by_id(state)[item.message_id] for item in state.live_history()]


def seq_by_id(state: ConversationState) -> dict[str, int]:
    return {item.message_id: state.dropped + offset
            for offset, item in enumerate(state.history)}


def roster_aliases(state: ConversationState) -> set[int]:
    return {int(line.split("=", 1)[0]) for line in state.roster_lines()}


def who_aliases(state: ConversationState) -> set[int]:
    """渲染结果里出现过的 `who="N"` 编号集合。**必须传 `aliases`**：不传时
    `_format_history` 会按本段现场重编一套临时编号，读出来的就不是名册编号。"""

    rendered = _format_history(state.live_history(), seq_of=state.seq_of,
                               alias_of=state.alias_of, aliases=state.aliases)
    return {int(chunk.split('"', 1)[0]) for chunk in rendered.split('who="')[1:]}


# --- 设计：短期窗口的参数（与你说的 500/50 对齐） ------------------------------

def test_the_documented_short_term_window_parameters() -> None:
    """`ARCHITECTURE.md:160` 的三个数：500 触发压缩、压缩后留 50、deque 安全上限 2000。"""

    assert (LIVE_TARGET, COMPACT_KEEP, HISTORY_LIMIT) == (500, 50, 2000), (
        "这三个数是**文档写明的设计**：改任何一处都要同时改 ARCHITECTURE.md:160、"
        "LONG_TERM_MEMORY_PLAN.md:27、docs/MULTI_GROUP_FOCUS.md:79"
    )


def test_the_window_grows_to_the_designed_size_before_compaction() -> None:
    """没有裁切之后，窗口就该长到设计上限——**不是长到 13 条**。

    这条钉住"她本应看得见多少"：`ConversationState()` 的默认 50 只是 dataclass 默认值，
    生产用 `history_limit=LIVE_TARGET 的那套 2000`，而**没有任何东西按话题裁它**，
    所以满窗就是 500 条。
    """

    assert ConversationState().history_limit == 50, "默认值确实是 50（但生产不这么建）"
    state = build_state(500)
    assert len(state.live_history()) == 500, "满窗口的 500 条都该在 prompt 里"


# --- 视图 = 活窗口；压缩之间只追加 -------------------------------------------

def test_the_visible_window_is_the_whole_live_window() -> None:
    """`live_history()` 返回**整个**活窗口——它自己这一层的性质。

    ⚠️ **这一条不是接线证明，别当它是**（2026-10-15 复核点名过）：
    它自己调 `live_history()` 再断言它等于整个窗口，中间没有任何代码能改那个结果。
    所以"引擎实际喂给 prompt 构造器的 history 是不是整窗"它一个字节都没验——
    在"把取数口换成 `state.recent()[-12:]`"的旧代码上它**照样绿**（假绿）。

    真正的接线断言在 `tests/test_history_wiring.py`
    （`test_the_reply_engine_is_handed_the_whole_live_window` /
    `test_the_judge_is_handed_the_whole_live_window`）：它包住构造函数取实参。

    这一条留着的价值是它测的那件事本身（`live_history()` 不截尾）——它是接线那条
    断言的**前置**：取数口自己就截尾的话，接线断言与它一起红。

    2026-10-15 之前这里断言的是"从话题起点切出来的连续切片"；用户
    「*我还要说几次，不切话题*」之后，正确的期望是**切片就是整段**。
    """

    state = build_state(200)
    assert visible_seqs(state) == list(range(200))


def test_the_view_only_appends_and_never_shrinks_between_compactions() -> None:
    """**压缩之间视图只追加、长度随消息单调增**（用户「不切话题」的机械表述）。

    做法：在同一个会话上一条一条加消息，每一步都断言
    ① 这一步的视图是上一步视图的**前缀**（没有任何旧消息被挤掉或藏起来），
    ② 长度**不减**。
    """

    state = build_state(3)
    previous = visible_seqs(state)
    lengths = [len(previous)]
    for index in range(3, 120):
        state.add(message(index, speaker=index), 0.0)
        current = visible_seqs(state)
        assert len(current) >= len(previous), (
            f"第 {index} 条之后视图变短了：{len(previous)} → {len(current)}"
        )
        assert current[:len(previous)] == previous, (
            f"第 {index} 条之后视图的头变了（不再是只追加）："
            f"{current[:4]}… vs {previous[:4]}…"
        )
        previous = current
        lengths.append(len(current))
    assert lengths == sorted(lengths), f"长度必须单调不减：{lengths[:3]}…{lengths[-3:]}"
    assert lengths[-1] == 120 == len(state.live_history()), "加了 117 条 → 视图就是 120 条"


def test_threshold_compaction_shortens_the_view() -> None:
    """**阈值这一条**：满 500 条压缩会把视图收到保留条数，然后重新只追加。

    ⚠️ 原来这条叫 `test_only_compaction_shortens_the_view`、docstring 写"只有满 500 条
    压缩会动历史"——**那句话说过头了**（2026-10-15 复核实测）。焦点交接的强制压缩
    （`_force_compact` → `_maybe_compact(force=True)` → `compaction_pending_all`，
    **不看 500 门槛**）不走这条路径，所以这个用例从名字到说明都只说"阈值这一条"。
    另外两条会动历史的情形（压缩关掉/失败后的 deque 静默丢弃、会话 LRU；以及压缩轮
    里判定与回复不同刻）见 `ARCHITECTURE.md` 的"话题锚已停用"与
    `tests/test_history_wiring.py`。
    """

    state = build_state(501)
    assert len(state.live_history()) == 501
    compactor = compact(state)
    assert compactor.requests, "501 条必须真的触发压缩"
    assert len(state.live_history()) == COMPACT_KEEP, "压缩后留 50 条"
    assert state.summary_through is not None, "摘要边界要落下来"

    previous = visible_seqs(state)
    for index in range(501, 505):
        state.add(message(index), 0.0)
        current = visible_seqs(state)
        assert current[:len(previous)] == previous, "压缩之后照旧只追加"
        assert len(current) == len(previous) + 1
        previous = current


def test_the_roster_covers_every_visible_speaker_so_who_refs_always_resolve() -> None:
    """名册里必须查得到每一条可见消息的 `who=`（真实日志 15263 条引用 0 条查不到）。"""

    state = build_state(120)
    missing = who_aliases(state) - roster_aliases(state)
    assert not missing, f"这些编号在名册里查不到：{sorted(missing)}"


def test_a_compacted_away_speaker_keeps_his_alias_but_leaves_the_view() -> None:
    """**别名表跨压缩保留，名册按当前窗口现算**——两件事刻意不对称。

    `aliases`（QQ 号 → 编号）是会话状态里单调分配的，压缩收走老消息之后**仍留着**，
    这样同一个人重新出现时编号不变（编号漂移会让模型张冠李戴）。
    而 `<people>` 名册只列**当前窗口里还出现**的人（`roster_lines` 的 `present` 过滤），
    所以压缩之后那个人既不在可见历史里、也不在名册里——
    但 `alias_of` 照样能查到他的编号。

    构造上刻意**只让两个说话人各出现一次**：压缩收走最旧那条之后，
    别名表里那个人的编号**在渲染里不再出现**（每个说话人每 4 条复现一次的
    构造做不到这件事——挤掉第 0 条对他毫无影响）。
    """

    state = ConversationState(history_limit=10)
    state.add(message(0, speaker=0), 0.0)      # 说话人 1001
    state.add(message(1, speaker=1), 0.0)      # 说话人 1002
    first = state.history[0]
    early = state.aliases[first.user_id]
    state.mark_compacted(state.seq_range()[0])  # 压缩收走最旧那条（只动窗口）
    assert first.message_id not in {m.message_id for m in state.history}
    assert state.aliases[first.user_id] == early, "别名表跨压缩保留（编号不复用、不重排）"
    assert state.alias_of(first) == early, "别名表按 user_id 查，与他还在不在窗口无关"
    assert early not in who_aliases(state), "但他已经不在可见历史里"


# --- 消息本体只由压缩收走 ----------------------------------------------------

def test_the_next_compaction_still_covers_the_whole_window() -> None:
    """压缩触发条件是"**超过** 500 条"（`len(live) <= target` 返回 None），
    所以 500 条整不压、501 条才压——这是 `LONG_TERM_MEMORY_PLAN.md:27`
    写的"超过 500 条时压缩"。

    压缩覆盖的**永远是整段活窗口**（没有"没给她看"的那部分——这也是
    「不切话题」的直接推论）。
    """

    state = build_state(501)
    pending = state.compaction_pending(target=LIVE_TARGET, keep=COMPACT_KEEP)
    assert pending is not None, "超过 500 条就该能压"
    messages, boundary = pending
    assert len(messages) == 501 - COMPACT_KEEP
    assert boundary == 501 - COMPACT_KEEP - 1

    exactly = build_state(500)
    assert exactly.compaction_pending(target=LIVE_TARGET, keep=COMPACT_KEEP) is None, (
        "500 条整不触发压缩"
    )


# --- 老状态兼容：带话题锚的载荷必须还能载入 ----------------------------------
#
# 2026-10-15 删字段**不等于**能把磁盘上的老状态文件当坏的：`data/` 下那些文件里
# 还写着 `topic_start_seq` 与 `anchor_floor_migrated`。要求是"**兼容读入、不许报错**"，
# 并且视图仍是整个载荷（那些键不再有任何效果）。

def test_an_old_state_with_topic_anchor_keys_loads_and_shows_the_whole_payload() -> None:
    """老状态（带 `topic_start_seq` / `anchor_floor_migrated`）载入不报错，视图是整段。"""

    source = build_state(120)
    raw = source.as_state()
    assert "topic_start_seq" not in raw, "新写出去的状态里不该再有这个话题锚键"
    assert "anchor_floor_migrated" not in raw
    raw["topic_start_seq"] = 119        # 老代码留下的、切到只剩 1 条的锚点
    raw["anchor_floor_migrated"] = False
    restored = ConversationState.from_state(raw, history_limit=120)
    assert restored is not None, "老键不许让载入失败"
    assert not hasattr(restored, "topic_start_seq"), "字段本身必须已经删掉"
    assert [m.text for m in restored.live_history()] == [m.text for m in source.history], (
        "视图仍是整个载荷，锚点不再有任何效果"
    )


def test_an_old_state_with_the_migration_marker_also_loads() -> None:
    """带 `anchor_floor_migrated: True`（迁过的老状态）同样照常载入。"""

    raw = build_state(30).as_state()
    raw["topic_start_seq"] = 29
    raw["anchor_floor_migrated"] = True
    restored = ConversationState.from_state(raw, history_limit=30)
    assert restored is not None
    assert len(restored.live_history()) == 30


# --- 摘要：只在压缩时被替换（"话题换了丢摘要"已停用） ------------------------

def test_a_summary_is_never_dropped_on_a_topic_change() -> None:
    """摘要只在压缩时被替换；"话题换了就丢摘要"的判据随起点一起没了。

    ⚠️ **这一条原来是恒真的（2026-10-15 复核点名 `:322-326`）**：它自己把
    `state.summary` / `state.summary_through` 赋成两个值，紧接着断言它们还是那两个值
    ——中间**没有任何代码能改它们**，所以它在什么实现上都绿。现在改成**真断言**：
    让一段真的会走 `_maybe_compact` 的代码跑过去，再看摘要有没有被动过。

    2026-10-02 那条设计的判据是"话题起点跑到 `summary_through` 之后"。
    用户 2026-10-15 的「不切话题」之后**不存在话题起点**，判据无从计算；
    于是历史材料的变动点收敛成：阈值压缩 + 焦点交接的强制压缩（见
    `ARCHITECTURE.md` 的"话题锚已停用"）。
    （更早话题的内容由长期记忆负责，`docs/MULTI_GROUP_FOCUS.md:122` 同口径。）
    """

    # 反向断言先立：那条按话题丢摘要的 API 不存在（它回来就等于判据回来了）。
    assert not hasattr(ConversationState, "drop_summary_if_stale"), (
        "`drop_summary_if_stale` 2026-10-15 已随话题锚删掉；它回来就等于"
        "「摘要按话题被丢」这条行为也回来了"
    )

    state = build_state(300)
    state.summary = "旧话题的一段摘要。"
    state.summary_through = 199

    # **真的跑一次压缩的那条路**（活窗口 100 条 < 500，所以它什么都不压）。
    # 关键是这中间有真代码在跑，而不是"赋值完再读一遍"。
    compactor = compact(state)
    assert not compactor.requests, "100 条活窗口不该触发压缩"

    assert state.summary == "旧话题的一段摘要。", (
        "没有谁会在话题变化时动它（这一段跑过了 `_maybe_compact` 的真代码）"
    )
    assert state.summary_through == 199, "边界只由压缩推进"
    # 分两截写（不写 `... == 100 == len(state.history)` 那种链式比较）：
    # 「活窗口 = 摘要之后的那一段」与「`history` 仍是全部消息」是**两件事**，
    # 链起来读着像"它们必须相等"，实际它们本就不等（100 vs 300）。
    assert len(state.live_history()) == 100, (
        "活窗口应当是摘要边界之后的那 100 条，"
        f"实际 {len(state.live_history())} 条"
    )
    assert len(state.history) == 300, (
        "摘要边界之前那 200 条**仍在 `history` 里**——`live_history()` 只是按 "
        f"`summary_through` 过滤视图，不销毁消息；实际 {len(state.history)} 条"
    )


def test_the_removed_topic_anchor_api_stays_removed() -> None:
    """**反向断言**：三个话题锚成员一旦回来，就说明有人又接上了一个裁切入口。"""

    for name in ("topic_history", "advance_topic_start", "drop_summary_if_stale"):
        assert not hasattr(ConversationState, name), (
            f"`ConversationState.{name}` 已于 2026-10-15 停用（用户「我还要说几次，"
            f"不切话题」）；它回来就等于历史又多了一个裁切入口"
        )
    raw = build_state(5).as_state()
    assert "topic_start_seq" not in raw and "anchor_floor_migrated" not in raw
