"""关系刻度 0..3 → 新刻度（好感 -100..100 / 防备 -30..30）的**可重跑换算**。

## 为什么单独一个模块、而且不自动跑

用户 2026-10-20 拍板换刻度，同时明确：**这次不要动生产库**。所以换算写成
可以反复调用、也可以先只看不写的函数；真正执行由主会话来点。
`MemoryStore` 打开旧库时**不会**自动换算——表结构版本（`SCHEMA_VERSION`）与
数据版本是两件事，混在一起就会"谁启动一次谁改一次关系数据"。

## 幂等靠什么

`meta` 表里的 `relationship_scale_version`。写入与版本号在**同一个事务**里
（`MemoryStore.connection()` 出错就整批回滚），所以不会出现"改了一半、版本号
却已经是新的"。第二次调用直接返回 `skipped=True`，一行都不写。

### 为什么还要"看起来已经是新刻度"的硬拒绝（2026-10-20 补）

只靠版本号不够：有人删了 `meta`、或者恢复了一份旧 dump，版本号就不见了。那时
整张表里只要有一行落在旧刻度之外（`closeness > 3` 或 `guardedness > 3`），就说明
这份库其实已经是新刻度；再跑一次会把仍是 `0..3` 的行（在新刻度里也合法）误当成
旧档位换算掉。所以在写入前先扫整张 `relationships`：版本号缺失、但存在这样的行时，
**拒绝迁移**并返回 `migration_rejected_looks_like_new_scale`，一行都不改。

`--force` 才允许越过这道检查（给"确实是旧库、但有人手工留了新值"的救援场景）；
绕过时在 `meta.relationship_scale_forced=1` 留痕，事后能看出这次是强制的。

### 旧值的处理

仍然只换算**能在旧映射里找到的行**：force 场景下混在里面的新刻度行原样跳过，
不当成旧档位再套一次。

`meta.relationship_scale_at` 记下换算那一刻：24 小时净变化与 7 天放松上限
都按流水求和，**换算之前的旧刻度流水不能算进新窗口**（见
`MemoryStore._scale_epoch`）。换算本身不是一次真实的关系变化。

## 映射（用户 2026-10-20 给的，逐条照抄）

| 旧 | 好感 | 防备 |
| --- | --- | --- |
| 0 | 10 | 0 |
| 1 | 35 | 10 |
| 2 | 65 | 20 |
| 3 | 90 | 30 |

**只换算仍然在旧刻度里的行**（两个轴都能在表里找到）：万一有人先手工改过某一行，
不按映射硬套——宁可跳过它，也不能把 90 当成旧档位再换算一次。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .memory_store import (RELATIONSHIP_SCALE_VERSION, SCALE_EPOCH_KEY,
                           SCALE_VERSION_KEY, MemoryStore)

#: 旧档位 → 新刻度。用户 2026-10-20 在 `data/relationship-tiers.md` 第 75 行定的。
CLOSENESS_MAP: dict[int, int] = {0: 10, 1: 35, 2: 65, 3: 90}
GUARDEDNESS_MAP: dict[int, int] = {0: 0, 1: 10, 2: 20, 3: 30}

#: 换算流水写这个来源；`/super affinity` 里一眼能分出"这不是一次真实互动"。
MIGRATION_SOURCE = "scale_migration"
MIGRATION_REASON = "关系刻度升级（0–3 → 新刻度）"

#: 版本号缺失、库里却已经有 >3 的值：说明它已经是新刻度，拒绝二次换算。
MIGRATION_REJECTED_LOOKS_LIKE_NEW_SCALE = "migration_rejected_looks_like_new_scale"
#: `--force` 越过上面那道检查时留的痕（值固定为 1），事后能分清是不是强制的。
SCALE_FORCED_KEY = "relationship_scale_forced"


def migrate_relationship_scale(store: MemoryStore, *, now: float | None = None,
                               dry_run: bool = False, force: bool = False) -> dict[str, object]:
    """把 `relationships` 里的旧刻度换算成新刻度。返回一份可读的报告。

    `dry_run=True` 只读：返回同样的报告，但一行都不写、版本号也不动
    （给主会话先看一眼再决定）。真正的写入是一次事务，幂等。

    `force=True` 只用于"版本号丢了、但库里已经有 >3 的新刻度值"的救援场景：
    默认会拒绝（`rejected` 字段给出拒绝码），传了它才继续跑，并在 `meta` 里记
    `relationship_scale_forced=1`。为什么默认拒绝：那种库已经被换算过，再跑一次
    会把仍然在 `0..3` 的行当成旧档位二次换算，数据就毁了（2026-10-20 补）。
    """

    moment = float(store.clock() if now is None else now)
    with store.connection() as db:
        recorded = _recorded_scale_version(db)
        if recorded == str(RELATIONSHIP_SCALE_VERSION):
            return _report(db, moment, dry_run=dry_run, skipped=True)

        # **扫描整张表**再决定写不写：版本号缺失 + 任何一行 >3 = 看起来已是新刻度。
        # 这个检查放在建 `meta` / 任何 UPDATE 之前，拒绝时一行都不改。
        looks_new = _looks_like_new_scale(db)
        if recorded is None and looks_new and not force:
            return _rejected_report(db, moment, dry_run=dry_run)

        if dry_run:
            return _report(db, moment, dry_run=True)

        # 老库没有 `meta`（它是 2026-10-20 才加的）：在这里幂等建表，
        # 不靠表结构版本，避免"打开库就自动改数据"。
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if recorded is None and looks_new:
            # 只有 force 能走到这里（没传 force 的话上面已经返回拒绝了）。
            # 留痕是给人看的：以后发现一段看不懂的换算时，先看这是不是强制跑的。
            db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCALE_FORCED_KEY, "1"),
            )

        converted = 0
        for record in db.execute(
                "SELECT user_id,closeness,guardedness FROM relationships ORDER BY user_id").fetchall():
            user_id = record["user_id"]
            old_closeness, old_guardedness = int(record["closeness"]), int(record["guardedness"])
            if old_closeness not in CLOSENESS_MAP or old_guardedness not in GUARDEDNESS_MAP:
                # 不是旧刻度（已经手工改过 / 已经迁过）——跳过，别二次换算。
                continue
            new_closeness = CLOSENESS_MAP[old_closeness]
            new_guardedness = GUARDEDNESS_MAP[old_guardedness]
            if (new_closeness, new_guardedness) == (old_closeness, old_guardedness):
                # 防备 0→0 这种"数值没变"的行不改、不留流水；它已经在目标刻度上。
                continue
            db.execute(
                "UPDATE relationships SET closeness=?,guardedness=?,updated_at=? WHERE user_id=?",
                (new_closeness, new_guardedness, moment, user_id),
            )
            for axis, before, after in (("closeness", old_closeness, new_closeness),
                                        ("guardedness", old_guardedness, new_guardedness)):
                if before == after:
                    continue
                # 换算本身留一条流水：以后看到的"上一次变化"不会是一串看不懂的旧数字。
                db.execute(
                    "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                    (user_id, axis, before, after, MIGRATION_REASON,
                     MIGRATION_SOURCE, moment),
                )
            converted += 1

        db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCALE_VERSION_KEY, str(RELATIONSHIP_SCALE_VERSION)),
        )
        db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCALE_EPOCH_KEY, repr(moment)),
        )
        # **必须用同一个连接出报告**：换一个连接会读不到本事务里尚未提交的值，
        # 报告就变成"旧值 → 旧值"（2026-10-20 自己写错过一次）。
        return _report(db, moment, dry_run=False, skipped=False, converted=converted)


def _recorded_scale_version(db) -> str | None:
    """`meta` 里记的刻度版本；`meta` 表本身不存在时也算"缺失"。

    不用 `CREATE TABLE` 探一下再说：拒绝迁移时要求**一行都不许改**，
    连空表都不该顺手建出来。
    """

    try:
        row = db.execute("SELECT value FROM meta WHERE key=?", (SCALE_VERSION_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row is not None else None


def _looks_like_new_scale(db) -> bool:
    """整张表扫一遍：旧刻度两轴都只到 3，出现 >3 就说明它已经是新刻度。"""

    row = db.execute(
        "SELECT 1 FROM relationships WHERE closeness>3 OR guardedness>3 LIMIT 1"
    ).fetchone()
    return row is not None


def _rejected_report(db, moment: float, *, dry_run: bool) -> dict[str, object]:
    """拒绝时的报告：`skipped` + 一个明确的码，调用方不必靠"converted=0"去猜。

    `mapped=False`：被拒绝时不要列出"本来会换算成什么"，否则报告看起来像
    "这些值要被改"，与"一行都不动"自相矛盾。
    """

    report = _report(db, moment, dry_run=dry_run, skipped=True, mapped=False)
    report["rejected"] = MIGRATION_REJECTED_LOOKS_LIKE_NEW_SCALE
    return report


def _report(db, moment: float, *, dry_run: bool,
            skipped: bool = False, converted: int = 0,
            mapped: bool = True) -> dict[str, object]:
    """把"每一行旧值 → 新值"列出来。只读查询，调用方给连接。"""

    rows = []
    for record in db.execute(
            "SELECT user_id,closeness,guardedness FROM relationships ORDER BY user_id").fetchall():
        old_closeness, old_guardedness = int(record["closeness"]), int(record["guardedness"])
        # 报告里的"旧值"只在确实是旧刻度时才可信；已经迁过的行按现状列出。
        rows.append({
            "user_id": record["user_id"],
            "old": [old_closeness, old_guardedness],
            "new": ([CLOSENESS_MAP.get(old_closeness, old_closeness),
                     GUARDEDNESS_MAP.get(old_guardedness, old_guardedness)]
                    if mapped else [old_closeness, old_guardedness]),
        })
    return {
        "scale_version": RELATIONSHIP_SCALE_VERSION,
        "at": moment,
        "dry_run": dry_run,
        "skipped": skipped,
        "converted": converted,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把关系两轴从旧 0..3 刻度换算到新刻度（可重跑、幂等）。")
    parser.add_argument("--db", required=True, help="memory.sqlite3 的路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印换算报告，不写库、不动版本号")
    parser.add_argument("--force", action="store_true",
                        help="版本号缺失但库看起来已是新刻度时仍然执行（会在 meta 留痕）")
    args = parser.parse_args(argv)
    store = MemoryStore(Path(args.db))
    report = migrate_relationship_scale(store, dry_run=args.dry_run, force=args.force)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 拒绝迁移要让脚本"失败"：否则调用方看退出码 0 会以为换算成功了。
    return 1 if report.get("rejected") else 0


if __name__ == "__main__":  # pragma: no cover - 由主会话手工调用
    raise SystemExit(main())
