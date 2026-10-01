"""记忆的**人工操作接缝**：只删、不加。

由来（2026-10-01 用户）："不允许手动添加记忆，只允许手动删除有问题的记忆"。
这条与 AGENTS 2.1 是一致的方向——记忆的正确性与边界是 Stage 3 的核心指标，
而"谁能往记忆里写"越窄越好：写入路径只有一个（记忆维护 agent 按批次批），
人工只能在**发现有问题时把那条拿掉**。

所以这个模块**故意只有三个方法**，而且没有一个是"新增记录"：

| 方法 | 作用 | 依据 |
| --- | --- | --- |
| `purge(ids, reason)` | 删除指定活跃记忆 | `MemoryStore.purge_records`：**先归档快照再清空**（保留 30 天） |
| `affinity(user_id, axis, delta)` | 调关系两轴 ±1 | `MemoryStore.apply_affinity`，写既有 `relationship_log` |
| `reset_affinity(user_id)` | 把关系按回默认档 | `MemoryStore.reset_relationship` |

关系两轴不是"记忆内容"，是"她跟这个人的关系现状"（`docs/AFFINITY_DESIGN.md`），
所以它不算"添加记忆"；但仍要求 `delta` 只能是 -1 / 0 / +1——那是既有设计里
"变化要慢"的硬约束，人工操作也不该破例。

**删除必须先归档**：`purge_records` 内部会 `_archive` 再清空，且 `tombstones` 只记
键与时间、不含内容。这个模块不去绕过它（不去直接 UPDATE `records`）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 关系两轴的名字（与 `memory_store.AFFINITY_AXES` 的口径一致）。
AXES = ("closeness", "guardedness")
#: 单次人工调整的允许增量。**只有这三档**，与判定的 AFFINITY_DELTAS 同源。
DELTAS = (-1, 0, 1)


class MemoryOpRejected(ValueError):
    """参数不合法。消息是给人看的固定说法。"""


class MemoryOps:
    """记忆人工操作的唯一入口。`store` 是 `MemoryStore`（核心持有）。"""

    def __init__(self, store, *, source: str = "webui") -> None:
        self.store = store
        self.source = source

    @property
    def available(self) -> bool:
        return self.store is not None

    def purge(self, record_ids, *, reason: str = "") -> int:
        """删除若干活跃记忆。返回真正清掉的条数。

        空列表直接返回 0（不报错：UI 上"没勾任何一条"不该是错误）。
        """

        if self.store is None:
            raise MemoryOpRejected("记忆库这次没启用，删不了")
        ids = [str(item).strip() for item in (record_ids or []) if str(item).strip()]
        if not ids:
            return 0
        if len(ids) > 100:
            raise MemoryOpRejected("一次最多删 100 条")
        note = (reason or "").strip() or "面板人工清理"
        removed = int(self.store.purge_records(ids, reason=note[:200]) or 0)
        logger.warning("memory_purge_by_hand requested=%s removed=%s reason=%s",
                       len(ids), removed, note[:60])
        return removed

    def affinity(self, user_id: str, axis: str, delta: int) -> dict[str, int]:
        """按一档调关系。返回调整后的两轴值。"""

        if self.store is None:
            raise MemoryOpRejected("记忆库这次没启用，调不了关系")
        target = str(user_id or "").strip()
        if not target.isdigit():
            raise MemoryOpRejected("要给出这个人的 QQ 号（纯数字）")
        name = str(axis or "").strip()
        if name not in AXES:
            raise MemoryOpRejected("轴只有 closeness 与 guardedness 两个")
        try:
            step = int(delta)
        except (TypeError, ValueError):
            raise MemoryOpRejected("增量只能填 -1、0 或 1") from None
        if step not in DELTAS:
            raise MemoryOpRejected("增量只能填 -1、0 或 1")
        # `apply_affinity` 是**唯一的写入路径**，它返回空串表示生效，否则是拒绝码。
        # 这里把拒绝码原样抛出去让人看到（不做"部分生效"，也不静默改小）。
        refusal = self.store.apply_affinity(
            target, closeness=step if name == "closeness" else 0,
            guardedness=step if name == "guardedness" else 0,
            reason="面板人工调整", source=self.source,
        )
        if refusal:
            raise MemoryOpRejected(f"这次调整被拒（{refusal}）")
        closeness, guardedness = self.store.relationship(target)
        return {"closeness": int(closeness), "guardedness": int(guardedness)}

    def reset_affinity(self, user_id: str) -> bool:
        if self.store is None:
            raise MemoryOpRejected("记忆库这次没启用，调不了关系")
        target = str(user_id or "").strip()
        if not target.isdigit():
            raise MemoryOpRejected("要给出这个人的 QQ 号（纯数字）")
        changed = bool(self.store.reset_relationship(target, reason="面板人工重置"))
        logger.warning("affinity_reset_by_hand user=%s changed=%s", target, changed)
        return changed


#: 公开名白名单：`tests/test_memory_ops.py` 用它钉住"这里没有任何新增记忆的入口"。
#: 加新方法时必须同步更新，且**不许**出现 add/append/insert/write/note/save 之类名字。
PUBLIC_API = ("available", "purge", "affinity", "reset_affinity")
FORBIDDEN_NAME_PARTS = ("add", "append", "insert", "write", "note", "save", "create", "put")
