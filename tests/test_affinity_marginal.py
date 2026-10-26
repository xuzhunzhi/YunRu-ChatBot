"""好感上升的边际效益（2026-10-25，用户原话：*"越往上提升越慢才是对的"*）。

背景（实测，`run/data/memory/memory.sqlite3` 近 7 天）：3278689325 升 5 次 / +73、
1744326850 升 4 次 / +69、2575311563 升 3 次 / +68、主人 2639157559 升 6 次 / +47；
三天从初始值 10 爬到 70+，是线性上涨。用户只批准了这一条，"高位不拿值得聊当判据"
那个方案**没批准**，所以这里只锁"正增量打折"：判定、下降、防备、证据链都不动。

档位（实现见 `memory_store.closeness_marginal`）：
`<30` 100%、`30–49` 75%、`50–69` 50%、`70–89` 25%、`90–94` 10% 且保底 +1、
`≥95` 0。`manual=True`（面板/超管）优先，不打折。

不用 pytest fixture：临时目录在测试内部自己建。
"""
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.memory_store import (
    AFFINITY_RAISE_MAX,
    MemoryStore,
    closeness_marginal,
)

USER = "900000001"
DAY = 86400
EVIDENCE = {"quote": "测试里的原话", "who_alias": "1", "seq": 1}


def make_store(tmp: str, *, clock=None) -> MemoryStore:
    return MemoryStore(Path(tmp) / "memory.sqlite3", clock=clock or time.time)


def write(store: MemoryStore, **kwargs) -> str:
    kwargs.setdefault("reason", "测试推进")
    kwargs.setdefault("source", "test")
    kwargs.setdefault("evidence", EVIDENCE)
    return store.apply_affinity(USER, **kwargs)


def seed(store: MemoryStore, *, closeness: int, guardedness: int = 0, now: float) -> None:
    with store.connection() as db:
        db.execute("INSERT INTO relationships VALUES (?,?,?,?,?)",
                   (USER, closeness, guardedness, now, now))


# --- 纯函数：逐档 -----------------------------------------------------------


def test_marginal_curve_per_band() -> None:
    """每一档的因子都要钉死，防止以后被悄悄改成线性。"""

    assert closeness_marginal(8, 28) == 8
    assert closeness_marginal(8, 30) == 6
    assert closeness_marginal(8, 45) == 6
    assert closeness_marginal(8, 50) == 4
    assert closeness_marginal(8, 60) == 4
    assert closeness_marginal(8, 70) == 2
    assert closeness_marginal(8, 80) == 2
    # 8 × 10% = 0，但 90–94 必须保底 +1，否则永远到不了 95。
    assert closeness_marginal(8, 90) == 1
    assert closeness_marginal(8, 95) == 0
    assert closeness_marginal(8, 100) == 0
    assert closeness_marginal(AFFINITY_RAISE_MAX, 94) == 1, "到 95 的最后一步只走 1"


def test_non_positive_deltas_are_never_discounted() -> None:
    """下降不是"上升太快"，一个数都不改；0 也原样。"""

    assert closeness_marginal(-4, 80) == -4
    assert closeness_marginal(-16, 99) == -16
    assert closeness_marginal(0, 80) == 0


# --- 写路径：manual、下降、防备 ---------------------------------------------


def test_manual_path_ignores_the_curve_at_eighty() -> None:
    """面板/超管是人工覆盖，优先于曲线：80 分时仍然 +8。"""

    clock = [1_000_000.0]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, clock=lambda: clock[0])
        seed(store, closeness=80, now=clock[0])
        assert write(store, closeness=8, manual=True) == ""
        assert store.relationship(USER)[0] == 88


def test_drops_and_guardedness_are_not_discounted() -> None:
    """同一份存储层里，-4 与防备 +10 都按原值走（只有好感的正增量打折）。"""

    clock = [1_000_000.0]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, clock=lambda: clock[0])
        seed(store, closeness=80, now=clock[0])
        assert write(store, closeness=-4) == ""
        assert store.relationship(USER) == (76, 0), "下降不打折"
        assert write(store, guardedness=10) == ""
        assert store.relationship(USER) == (76, 10), "防备不打折"


# --- 写路径：顺序（先压 8，再打折，最后每日上限） ---------------------------


def test_discount_happens_before_the_daily_cap() -> None:
    """每日上限必须按**实际写入**的 2 算，而不是原始 8。

    70 分时窗口内已有净 +18：+8 折成 +2 后刚好 20，允许；若按原始 +8 算成 26，
    这条写入会被每日上限误拒。这就是"打折在上限之前"的机械证据。
    """

    clock = [1_000_000.0]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, clock=lambda: clock[0])
        seed(store, closeness=70, now=clock[0])
        with store.connection() as db:
            db.execute("INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                       (USER, "closeness", 70, 88, "窗口内已涨", "test", clock[0] - 60))
        assert write(store, closeness=8) == ""
        assert store.relationship(USER)[0] == 72, "70 + (8×25% = 2)"


# --- 轨迹回归 ---------------------------------------------------------------


def test_trajectory_from_ten_reaches_ninety_five_in_twenty_five_raises() -> None:
    """从 10 开始每天 +8：第 25 次才到 95；第 26 次不涨（边界）。

    这个次数是刻意写死的：以后任何一次改档位/改上限，要么更新这里，要么就是
    把曲线悄悄改快了。轨迹（每次的落点）：
    18,26,34,40,46,52,56,60,64,68,72,74,76,78,80,82,84,86,88,90,
    91,92,93,94,95。
    """

    clock = [1_000_000.0]
    with tempfile.TemporaryDirectory() as tmp:
        store = make_store(tmp, clock=lambda: clock[0])
        landed = []
        for _ in range(25):
            clock[0] += DAY + 60
            assert write(store, closeness=8) == ""
            landed.append(store.relationship(USER)[0])
        assert landed == [
            18, 26, 34, 40, 46, 52, 56, 60, 64, 68, 72, 74, 76, 78,
            80, 82, 84, 86, 88, 90, 91, 92, 93, 94, 95,
        ]
        clock[0] += DAY + 60
        assert write(store, closeness=8) == "affinity_rejected_at_boundary"
        assert store.relationship(USER)[0] == 95
