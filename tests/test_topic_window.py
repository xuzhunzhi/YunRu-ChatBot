"""话题窗口：她**实际看得见多少条**，与**设计规定的 500/50** 差多远。

## 设计（文档规定；不是我从代码行为反推的）

* `ARCHITECTURE.md:158`：短期窗口 `LIVE_TARGET = 500`（压缩阈值）、
  `COMPACT_KEEP = 50`（压缩后保留）、`HISTORY_LIMIT = 2000`（deque 安全上限）。
* `LONG_TERM_MEMORY_PLAN.md:27`：活窗口 = 一段压缩摘要 + 摘要之后累积的消息
  （**超过 500 条时压缩并保留 50 条**）。
* `docs/MULTI_GROUP_FOCUS.md:79`：压缩在 **500 条**触发；压缩后进入回复段的是
  **摘要 + 最新 50 条**。
* `docs/MULTI_GROUP_FOCUS.md:185`：话题锚点——话题内前缀只追加，**压缩后启用新起点**。

按设计：模型平时应当看得见**最多 500 条**活消息；压缩之后**至少约 50 条**。

## 实测（`run/data/logs/reply.jsonl` 854 条样本 + `bot.err.log`，2026-10-02 复核）

| 量 | 实测 | 设计 |
| --- | --- | --- |
| 可见条数 | 中位 **13**（min 1 / max 166） | 最多 500 |
| 回看跨度 | 中位 **12**；≤20 条占 **576/854（67%）** | — |
| 只有 1 条 | **61** 个样本（其中 55 个 index≥50，中位 1999 → 是老会话） | 压缩后至少 ~50 |
| `topic start advanced` | **486 次**（`group:800000001` 占 361 次） | 压缩时才换新起点 |

差了一个多数量级。

## 根因（2026-10-02 实测，`run/data/logs/judge.jsonl` 1464 条判定）

| 量 | 实测 |
| --- | --- |
| 判定想往后退 | **0 次（0.0%）** |
| 原地确认 / 前进 | 1288 / **187** |
| "前进"往前挪多少条 | 中位 **13**（p90 46） |
| "前进"落点离"最新那条" | 中位 **2**；**72/187 正好指在最新那条**、116/187 在 2 条以内 |
| 判定那一刻窗口宽度 | 中位 **16**，p10 3，p90 48；**61% ≤20 条** |

也就是说：**62% 的"前进"等于把视图清空到只剩最新一两句**——这正是文档里那句
"最新那条永远被当成话题起点"。窗口于是永远长不到设计的 500。

**改法**（用户 2026-10-02 定的，两处）：

1. **起点只裁剪视图，不销毁消息**。`topic_history()` 是给模型的视图；
   消息本体只在**满 500 条**时进摘要（`live_history()` 不按起点过滤）。
2. **话题换了、摘要与当前话题脱节 → 丢掉摘要**（而不是把旧话题拖进新话题）。

**没有采用**的两条，都写在这里免得以后有人再走一遍：

* "允许判定往后退"——实测 **0/1464**，纯空转，还会白送缓存风险（真退一次就换前缀）。
* "话题切换时先压缩那一段"——那等于给窗口加**第二个裁切入口**，
  与"上下文裁切只有满 500 条才会触发"冲突（用户 2026-10-02 原话）。

## 这个文件干什么

钉住四件事：

1. **起点只许前进**（`advance_topic_start`）——实测判定从不后退，所以没有代价。
2. **起点只裁剪视图，不销毁消息**：`topic_history()` 是视图，
   `live_history()`（压缩的取数口）不按起点过滤。
3. **消息本体只在满 500 条时被压缩收走**（进摘要、留最近 50 条）。
4. **话题换了、摘要脱节 → 丢掉摘要**（判据：话题起点在 `summary_through` 之后）。
"""
from qq_roleplay_bot.stage3_main import COMPACT_KEEP, HISTORY_LIMIT, LIVE_TARGET
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


def visible_seqs(state: ConversationState) -> list[int]:
    return [state.seq_of(item) for item in state.topic_history()]


def roster_aliases(state: ConversationState) -> set[int]:
    return {int(line.split("=", 1)[0]) for line in state.roster_lines()}


def who_aliases(state: ConversationState) -> set[int]:
    rendered = _format_history(state.topic_history(), seq_of=state.seq_of,
                               alias_of=state.alias_of, aliases=state.aliases)
    return {int(chunk.split('"', 1)[0]) for chunk in rendered.split('who="')[1:]}


# --- 设计：短期窗口的参数（与你说的 500/50 对齐） ------------------------------

def test_the_documented_short_term_window_parameters() -> None:
    """`ARCHITECTURE.md:158` 的三个数：500 触发压缩、压缩后留 50、deque 安全上限 2000。"""

    assert (LIVE_TARGET, COMPACT_KEEP, HISTORY_LIMIT) == (500, 50, 2000), (
        "这三个数是**文档写明的设计**：改任何一处都要同时改 ARCHITECTURE.md:158、"
        "LONG_TERM_MEMORY_PLAN.md:27、docs/MULTI_GROUP_FOCUS.md:79"
    )


def test_the_window_grows_to_the_designed_size_before_compaction() -> None:
    """没有锚点时，窗口就该长到设计上限——**不是长到 13 条**。

    这条钉住"她本应看得见多少"：`ConversationState()` 的默认 50 只是 dataclass 默认值，
    生产用 `history_limit=LIVE_TARGET 的那套 2000`，锚点不动时应当看得见整窗。
    """

    assert ConversationState().history_limit == 50, "默认值确实是 50（但生产不这么建）"
    state = build_state(500)
    assert len(state.topic_history()) == 500, "满窗口的 500 条都该在 prompt 里"


# --- 设计：锚点切出来的是一整段，且"谁是谁"仍然成立 ---------------------------

def test_the_visible_window_is_a_contiguous_slice_from_the_topic_start() -> None:
    state = build_state(200)
    assert visible_seqs(state) == list(range(200))

    state.advance_topic_start(150)
    got = visible_seqs(state)
    assert got == list(range(150, 200)), f"应当只留 150..199，实际 {got[:5]}…"


def test_the_roster_covers_every_visible_speaker_so_who_refs_always_resolve() -> None:
    """名册里必须查得到每一条可见消息的 `who=`（真实日志 15263 条引用 0 条查不到）。"""

    state = build_state(120)
    state.advance_topic_start(100)
    missing = who_aliases(state) - roster_aliases(state)
    assert not missing, f"这些编号在名册里查不到：{sorted(missing)}"


def test_the_roster_can_list_someone_who_has_left_the_visible_window() -> None:
    """名册是**整个窗口**的人，不随选择收缩——有意的不对称，为了让前缀稳定。"""

    state = build_state(60)
    early = state.aliases[SPEAKERS[0]]
    state.advance_topic_start(59)
    assert len(visible_seqs(state)) == 1
    assert early in roster_aliases(state), "早期那个人仍在名册里"
    assert state.alias_of(message(0)) not in who_aliases(state), "但他已经不在可见历史里"


def test_the_topic_start_only_moves_forward() -> None:
    """起点只许前进——**实测判定从不后退，所以这条限制没有代价**。

    2026-10-02 从 `run/data/logs/judge.jsonl` 的 1464 条判定里数出来：
    88% 原地确认、12% 前进、**0% 后退**。因为每轮都把当前起点告诉它
    （"这段谈话目前的起点：第 N 条"），它照着确认就行。
    所以单向棘轮挡掉的那个动作**根本不发生**；而放开后退会白送一份缓存风险。
    """

    state = build_state(200)
    lengths = []
    for start in (20, 60, 120, 199):
        assert state.advance_topic_start(start) is True
        lengths.append(len(state.topic_history()))
    assert lengths == sorted(lengths, reverse=True), f"视图应当只减不增：{lengths}"

    assert state.advance_topic_start(60) is False, "后退必须是无效的（判定也从没请求过）"
    assert state.topic_start_seq == 199
    assert state.advance_topic_start(199) is False, "同一个编号不算变化"
    assert state.advance_topic_start(None) is False, "没报就不动"


# --- 选择不销毁消息；只有满 500 条压缩才动消息 ------------------------------

def test_narrowing_the_view_never_removes_a_message_from_the_window() -> None:
    """起点只裁剪**视图**：缩到只剩 1 条，消息本体一条都没少。

    这是这一轮改动里最要紧的一条。我一度把"起点前移"讲成"消息丢了"——**那是错的**：
    `live_history()`（压缩的取数口）不按起点过滤，所以被排除在视图之外的消息
    仍然在窗口里，下一次满 500 条压缩时会照旧进摘要。
    """

    state = build_state(500)
    state.advance_topic_start(state.seq_range()[1])        # 视图缩到最新 1 条
    assert len(state.topic_history()) == 1, "视图只剩 1 条"
    assert len(state.live_history()) == 500, "但窗口里 500 条一条没少"
    assert state.seq_range() == (0, 499)
    assert state.dropped == 0, "没有任何消息被丢掉"


def test_a_narrow_selection_does_not_starve_the_next_compaction() -> None:
    """视图很窄时，压缩照样覆盖整个窗口——不会因为"没给她看"就压不到。

    注意触发条件是"**超过** 500 条"（`len(live) <= target` 返回 None），
    所以 500 条整不压、501 条才压——这是 `LONG_TERM_MEMORY_PLAN.md:27`
    写的"超过 500 条时压缩"。
    """

    state = build_state(501)
    state.advance_topic_start(state.seq_range()[1])
    pending = state.compaction_pending(target=LIVE_TARGET, keep=COMPACT_KEEP)
    assert pending is not None, "超过 500 条就该能压"
    messages, boundary = pending
    assert len(messages) == 501 - COMPACT_KEEP
    assert boundary == 501 - COMPACT_KEEP - 1


# --- 话题换了 → 丢摘要（代码里原来完全没有这一条） ---------------------------

def test_a_stale_summary_is_dropped_when_the_topic_moves_past_it() -> None:
    """话题起点挪到摘要覆盖范围**之后** = 摘要里全是旧话题 → 丢掉。

    用户 2026-10-02 的设计："话题换了是清空压缩"、"只要摘要里有跟当前话题
    一脉相承的东西就不丢"。判据就是话题起点与 `summary_through` 的先后。
    """

    state = build_state(300)
    state.summary = "旧话题的一段摘要。"
    state.summary_through = 199
    assert state.drop_summary_if_stale(250) is True, "起点在摘要之后 → 脱节 → 丢"
    assert state.summary == ""
    assert state.summary_through is None


def test_a_summary_that_still_covers_the_topic_is_kept() -> None:
    """话题起点落在摘要覆盖范围之内 = 这个话题跨过压缩边界延续过来 → 留着。"""

    state = build_state(300)
    state.summary = "这个话题的前文也在里面。"
    state.summary_through = 199
    assert state.drop_summary_if_stale(120) is False, "起点在摘要里 → 一脉相承 → 留"
    assert state.summary == "这个话题的前文也在里面。"
    assert state.summary_through == 199


def test_dropping_a_summary_happens_at_most_once_per_compaction_cycle() -> None:
    """丢过一次之后，没有新摘要可丢——所以不会每轮都丢。"""

    state = build_state(300)
    state.summary = "旧摘要。"
    state.summary_through = 199
    assert state.drop_summary_if_stale(250) is True
    assert state.drop_summary_if_stale(260) is False, "已经空了，没什么可丢"
    assert state.drop_summary_if_stale(None) is False, "没报起点也不丢"
