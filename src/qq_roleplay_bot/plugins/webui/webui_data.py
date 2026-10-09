"""面板的**只读数据层**：把文件、sqlite、引擎快照读成一份可直接渲染的 JSON。

三条纪律：

1. **读不动的一律降级**：文件缺失/损坏、sqlite 被锁、索引不在——都返回 `None` 并把原因
   攒进 `errors`，由页面显示"读不到"。面板绝不能因为"某个文件正在被写"就整页失败。
2. **sqlite 一律 `mode=ro`**：不起迁移、不建目录、不设 `journal_mode`（**绝不用
   `MemoryStore.connection()`**，那个会写 WAL 与建表）。跑完面板之后库文件的
   mtime 不该变，这条有测试钉着。
3. **凭据不回显**：`mask_secret` 只给前 4 后 4，其余一律 `*`。审计里连这个都没有。

正文**不做脱敏**（2026-10-01 用户："正文不用脱敏，只有我能看"）。这条只对**聊天/记忆/
知识正文**成立；凭据是另一回事——它不该出现在任何响应里。
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: 列表类接口的单页上限（与 `memory_store.list_records` 的 200 保持一致）。
MAX_LIST = 200
#: 一次读 jsonl 尾部最多读多少字节（judge/memory 那两个文件已经 20 MB+）。
TAIL_BYTES = 1024 * 1024
#: 日志白名单：只认这几个 feature（文件名拼路径之前先过一遍）。
LOG_FEATURES = ("judge", "reply", "memory", "security", "mail")


def _data_root() -> Path:
    """运行数据根目录。**只有一处算法**（`dev_config.data_dir`）：这个文件搬过一次家，
    自己数 `parents[N]` 会静默指到 `src/data`。"""

    from ...dev_config import data_dir

    return data_dir()


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --- 掩码 -------------------------------------------------------------------


def mask_secret(value: object) -> str:
    """凭据的展示形式：`sk-abc…wxyz`。空值显示"未设置"。"""

    text = str(value or "")
    if not text:
        return "未设置"
    if len(text) <= 12:
        return text[:2] + "*" * max(1, len(text) - 2)
    return f"{text[:4]}…{text[-4:]}"


def mask_settings(values: dict[str, str]) -> dict[str, str]:
    """按 `operator_config.SETTINGS` 的 kind 掩码。secret 只给首尾。"""

    from ...operator_config import SETTINGS

    masked: dict[str, str] = {}
    for key, value in values.items():
        spec = SETTINGS.get(str(key))
        if spec is not None and spec.get("kind") == "secret":
            masked[str(key)] = mask_secret(value)
        else:
            masked[str(key)] = str(value)
    return masked


# --- 引擎状态 ---------------------------------------------------------------


def _state_from_snapshot(snapshot, store) -> dict[str, object]:
    """活快照：会话、启停、计数（与 `/super status` 同一口径）。

    **参数是快照本身**，不是引擎：面板拿不到引擎（那条硬边界），它只拿得到
    装配点注入的取值闭包。这里因此只认"一个快照 + 一个可选账本"。
    """

    metrics = snapshot.metrics or {}
    latency = metrics.get("model_latency") or {} if isinstance(metrics, dict) else {}
    now = time.time()
    current = {
        "accepted_messages": snapshot.accepted_messages,
        "replies": snapshot.replies,
        "judge_calls": snapshot.judge_calls,
        "model_calls": snapshot.model_calls,
        "blocked_messages": snapshot.blocked_messages,
    }
    all_time = store.all_time(current) if store is not None else dict(current)
    uptime = max(0.0, now - snapshot.started_at) if snapshot.started_at else 0.0
    return {
        "source": "live",
        "enabled": snapshot.enabled,
        "enabled_group_ids": list(snapshot.enabled_group_ids),
        "target_group_id": snapshot.target_group_id,
        "uptime_seconds": round(uptime, 1),
        "started_at": snapshot.started_at,
        "memory_bytes": snapshot.memory_bytes,
        "memory_peak_bytes": snapshot.memory_peak_bytes,
        "counters": {**current, "reply_segments": snapshot.reply_segments,
                     "history_seeded": snapshot.history_seeded},
        "all_time": dict(all_time),
        "since": float(getattr(store, "since", 0.0) or 0.0) if store is not None else 0.0,
        "latency": latency,
        "sessions": [
            {"session_id": item.session_id, "mode": item.mode,
             "history_size": item.history_size, "topic": item.topic,
             "topic_status": item.topic_status}
            for item in snapshot.sessions
        ],
        "enabled_groups": list(snapshot.enabled_group_ids),
    }

def _state_from_file(root: Path) -> dict[str, object] | None:
    """bot 不在时的快照：只读 `runtime_state.json`（它按状态变化落盘）。"""

    data = _read_json(root / "runtime_state.json")
    if data is None:
        return None
    sessions = data.get("sessions") if isinstance(data.get("sessions"), list) else []
    rows = []
    for item in sessions:
        if not isinstance(item, dict):
            continue
        history = item.get("history") if isinstance(item.get("history"), list) else []
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        rows.append({
            "session_id": str(item.get("session_id") or ""),
            "mode": str(item.get("mode") or ""),
            "history_size": len(history),
            "topic": str(context.get("topic") or ""),
            "topic_status": str(context.get("topic_status") or ""),
        })
    try:
        written_at = (root / "runtime_state.json").stat().st_mtime
    except OSError:
        written_at = 0.0
    return {
        "source": "files",
        "enabled": bool(data.get("enabled", True)),
        "enabled_group_ids": [str(item) for item in (data.get("enabled_group_ids") or [])],
        "target_group_id": str(data.get("target_group_id") or ""),
        "group_admins": {str(k): [str(x) for x in (v or [])]
                         for k, v in (data.get("group_admins") or {}).items()},
        "sessions": rows,
        "file_written_at": written_at,
    }


def read_state(state=None, root: Path | None = None) -> dict[str, object]:
    """状态：bot 在跑就用活快照，否则用文件快照（并标明来源）。

    `state` 是**取值闭包**（`() -> {"snapshot":…, "usage_store":…} | None`），
    不是引擎——面板拿不到引擎（那条硬边界）。装配点注入的是核心的取值闭包。
    """

    if state is not None:
        try:
            live = state() if callable(state) else state
        except Exception:  # noqa: BLE001 - 快照失败就退到文件，不让整页挂掉
            logger.warning("webui_engine_snapshot_failed", exc_info=True)
        else:
            live = live or {}
            snapshot = live.get("snapshot") if isinstance(live, dict) else live
            if snapshot is not None:
                store = live.get("usage_store") if isinstance(live, dict) else None
                return _state_from_snapshot(snapshot, store)
    return _state_from_file(root or _data_root()) or {"source": "unavailable", "sessions": []}


def read_session_messages(root: Path, session_id: str, *, limit: int = 50) -> dict[str, object]:
    """某个会话最近的消息（只读状态文件里的历史，不在内存里再存一份）。"""

    capped = max(1, min(MAX_LIST, int(limit)))
    data = _read_json(root / "runtime_state.json") or {}
    sessions = data.get("sessions") if isinstance(data.get("sessions"), list) else []
    for item in sessions:
        if not isinstance(item, dict) or str(item.get("session_id")) != str(session_id):
            continue
        history = item.get("history") if isinstance(item.get("history"), list) else []
        rows = []
        for entry in history[-capped:]:
            if not isinstance(entry, dict):
                continue
            rows.append({
                "message_id": str(entry.get("message_id") or ""),
                "user_id": str(entry.get("user_id") or ""),
                "sender_name": str(entry.get("sender_name") or ""),
                "is_bot_message": bool(entry.get("is_bot_message")),
                "text": str(entry.get("text") or ""),
            })
        return {"session_id": session_id, "total": len(history), "messages": rows}
    return {"session_id": session_id, "total": 0, "messages": []}


# --- 用量 -------------------------------------------------------------------


def read_usage(root: Path | None = None) -> dict[str, object]:
    """账本（跨重启累计）+ 按 agent 的命中率表。角色名复用 `APICHECK_ROLES`。"""

    # `APICHECK_ROLES`（role → 中文名）现在住在核心入口 `stage3_main`：
    # `/super apicheck` 就是照它列 agent 的，面板再抄一份就成了第二个真相来源。
    # 这一段原来写的是 `from ...api_usage import APICHECK_ROLES`——本体重构时那个常量
    # 搬去了 `stage3_main`，这里没跟着改，于是 `/api/usage` 一直 500
    # （总览页与账号页的"用量"整块读不出来，实测）。
    from ...stage3_main import APICHECK_ROLES

    data = _read_json((root or _data_root()) / "api_usage.json") or {}
    roles: dict[str, dict[str, int]] = {}
    raw_roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    for role, values in raw_roles.items():
        if not isinstance(values, dict):
            continue
        hit = int(values.get("prompt_cache_hit_tokens", 0) or 0)
        miss = int(values.get("prompt_cache_miss_tokens", 0) or 0)
        roles[str(role)] = {
            "calls": int(values.get("_calls", 0) or 0),
            "hit_tokens": hit,
            "miss_tokens": miss,
            "prompt_tokens": int(values.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(values.get("completion_tokens", 0) or 0),
            "total_tokens": int(values.get("total_tokens", 0) or 0),
            "hit_rate": round(hit / (hit + miss), 4) if (hit + miss) else 0.0,
        }
    labels = list(APICHECK_ROLES) if isinstance(APICHECK_ROLES, tuple) else []
    known = [{"role": str(role), "label": str(label)} for role, label in labels]
    seen = {item["role"] for item in known}
    for role in sorted(roles):
        if role not in seen:
            known.append({"role": role, "label": role})
    return {
        "since": float(data.get("since") or 0.0),
        "updated_at": float(data.get("updated_at") or 0.0),
        "roles": roles,
        "role_list": known,
        "counters": {str(k): int(v) for k, v in (data.get("counters") or {}).items()
                     if isinstance(v, (int, float))},
        "available": bool(data),
    }


# --- 记忆（只读 sqlite） -----------------------------------------------------


def open_memory_readonly(path: Path):
    """只读连接。打不开返回 None。**不建目录、不起迁移、不设 journal_mode**。"""

    try:
        if not Path(path).exists():
            return None
        return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error as exc:
        logger.warning("webui_memory_open_failed category=%s", type(exc).__name__)
        return None


def _memory_path(root: Path) -> Path:
    configured = os.environ.get("QQBOT_MEMORY_DB", "").strip()
    return Path(configured) if configured else root / "memory" / "memory.sqlite3"


def _count(db, sql: str) -> int:
    try:
        return int(db.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return 0


def _attach_subjects(db, rows: list[dict[str, object]]) -> None:
    """给记忆行补 `subjects`（关联人 QQ）与 `about`（他们的显示名）。

    与提示词那边同一套口径（`MemoryMaterial._about`）：名字来源是 `people` 表，
    查不到就退回 QQ 号。**绝不猜**：QQ 号是权威身份，显示名只是给人看的。
    """

    ids = [str(row["id"]) for row in rows if row.get("id")]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    subjects: dict[str, list[str]] = {item: [] for item in ids}
    try:
        for row in db.execute(
            f"SELECT record_id, user_id FROM record_subjects WHERE record_id IN ({placeholders})",
            ids,
        ):
            subjects.setdefault(str(row["record_id"]), []).append(str(row["user_id"]))
    except sqlite3.Error as exc:
        # **不要在这里 return**：`record_subjects` 读不到时，归属人还有
        # `subject_user_id` 这条来源，`about` 不该整列消失（第一版就是这么错的，
        # 一条测试直接照出来）。记一句，继续往下走。
        logger.warning("webui_subjects_query_failed category=%s", type(exc).__name__)
    wanted = {uid for users in subjects.values() for uid in users}
    wanted.update(str(row.get("subject_user_id") or "") for row in rows)
    wanted.discard("")
    names: dict[str, str] = {}
    if wanted:
        try:
            marks = ",".join("?" for _ in wanted)
            for row in db.execute(
                f"SELECT user_id, name FROM people WHERE user_id IN ({marks})", tuple(wanted)
            ):
                names[str(row["user_id"])] = str(row["name"])
        except sqlite3.Error:
            names = {}
    for row in rows:
        key = str(row.get("id") or "")
        people = subjects.get(key) or []
        if not people:
            owner = str(row.get("subject_user_id") or "")
            people = [owner] if owner else []
        row["subjects"] = people
        row["about"] = "、".join(names.get(uid, uid) for uid in people)


def read_memory_overview(root: Path | None = None) -> dict[str, object]:
    """各表计数（与 `MemoryStore.counts()` 同一口径，但只读、不碰它的连接）。"""

    base = root or _data_root()
    db = open_memory_readonly(_memory_path(base))
    if db is None:
        return {"available": False}
    try:
        db.row_factory = sqlite3.Row
        counts = {
            "short": _count(db, "SELECT COUNT(*) FROM memory_short"),
            "long": _count(db, "SELECT COUNT(*) FROM memory_long"),
            "active": _count(db, "SELECT COUNT(*) FROM memory_short WHERE status='active'")
            + _count(db, "SELECT COUNT(*) FROM memory_long WHERE status='active'"),
            "archive": _count(db, "SELECT COUNT(*) FROM archive"),
            "inbox": _count(db, "SELECT COUNT(*) FROM inbox"),
            "audit": _count(db, "SELECT COUNT(*) FROM audit"),
            "people": _count(db, "SELECT COUNT(*) FROM people"),
            "relationships": _count(db, "SELECT COUNT(*) FROM relationships"),
            "tombstones": _count(db, "SELECT COUNT(*) FROM tombstones"),
        }
        return {"available": True, "counts": counts}
    finally:
        db.close()


_LIST_SQL: dict[str, str] = {
    "records": ("SELECT id, scope_type, scope_key, subject_user_id, kind, normalized_key, "
                "content, confidence, status, revision, durable, updated_at, tier FROM records"),
    "archive": ("SELECT id, scope_type, scope_key, subject_user_id, kind, normalized_key, "
                "content, confidence, deleted_at, delete_reason FROM archive"),
    "inbox": ("SELECT id, group_id, user_id, speaker, text, occurred_at FROM inbox"),
    "audit": ("SELECT batch_id, operation_index, op, scope_type, created_at FROM audit"),
    "relationships": ("SELECT user_id, closeness, guardedness, updated_at, last_seen_at "
                      "FROM relationships"),
    "people": ("SELECT group_id, user_id, name, updated_at FROM people"),
    "tombstones": ("SELECT scope_type, scope_key, normalized_key, deleted_at FROM tombstones"),
}

#: 允许的筛选参数（列名白名单，值一律参数化）。**不拼用户输入**。
_FILTERS: dict[str, dict[str, str]] = {
    "records": {"tier": "tier", "status": "status", "kind": "kind",
                "scope_type": "scope_type", "scope_key": "scope_key"},
    "archive": {"kind": "kind", "scope_type": "scope_type"},
    "inbox": {"group_id": "group_id", "user_id": "user_id"},
    "audit": {"op": "op", "scope_type": "scope_type"},
    "people": {"group_id": "group_id"},
}


def read_memory_table(table: str, *, root: Path | None = None, filters: dict[str, str] | None = None,
                      query: str = "", limit: int = 50, offset: int = 0,
                      kinds: tuple[str, ...] | None = None,
                      exclude_kinds: tuple[str, ...] = ()) -> dict[str, object]:
    """列某张表（`records` / `archive` / …）。筛选列走白名单，值参数化。

    `kinds` / `exclude_kinds` 用来把**人物画像**（`kind='profile'`）和普通旧事分开——
    面板上是两块（用户 2026-10-01："人物画像和记忆分两个面板"）。画像是一条人一条、
    跟着人走、不进按词检索，跟"你记得的旧事"本来就不是一层，混在一起看不出来。

    **`records` 默认只看生效的**（2026-10-09）：`status != 'active'` 那些行里，
    已删的**正文是故意清空的**（删除路径先归档、再把正文置空，物理行等 7 天保留期
    才清），面板把它们列出来就是用户看到的"**空正文的记录**"。所以：

    * **没提 `status`** 的调用方 → 一律按 `active` 取（绝大多数页面要的就是这个）；
    * 要看历史（「记忆」页那个"全部状态"页签）就**显式**给 `status=any`
      —— 那是"这一列不过滤"的哨兵（不能用空串：`parse_qs` 默认会把空值丢掉，
      `?status=` 根本到不了这里，2026-10-09 踩到过）；给具体状态值就按它取。
    """

    if table not in _LIST_SQL:
        raise ValueError("unknown_table")
    if kinds is not None and not kinds:
        # 空元组 = 这一类没有东西，**不是**"不过滤"（不过滤会把整库倒出来）。
        return {"available": True, "rows": [], "total": 0, "limit": 0, "offset": 0}
    capped = max(1, min(MAX_LIST, int(limit)))
    skip = max(0, int(offset))
    base = root or _data_root()
    db = open_memory_readonly(_memory_path(base))
    if db is None:
        return {"available": False, "rows": [], "total": 0}
    try:
        db.row_factory = sqlite3.Row
        where: list[str] = []
        params: list[object] = []
        allowed = _FILTERS.get(table, {})
        given: set[str] = set()
        for key, value in (filters or {}).items():
            column = allowed.get(str(key))
            if column is None:
                continue
            given.add(str(key))
            text = "" if value is None else str(value)
            if text == "":
                continue                      # 参数表里给空 = 这一列不过滤（老写法，留着）
            if str(key) == "status" and text.strip().casefold() in {"any", "*"}:
                continue                      # 显式哨兵：**这一列不过滤**（"全部状态"）
            where.append(f"{column} = ?")
            params.append(text)
        if table == "records" and "status" not in given:
            # 没提 status 的调用方 → **只看生效的**（见上面那段：已删的正文是空的）。
            where.append("status = ?")
            params.append("active")
        if kinds is not None:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if exclude_kinds:
            where.append(f"kind NOT IN ({','.join('?' for _ in exclude_kinds)})")
            params.extend(exclude_kinds)
        if query.strip():
            where.append("content LIKE ?" if table in {"records", "archive"} else "text LIKE ?"
                         if table == "inbox" else "normalized_key LIKE ?")
            params.append(f"%{query.strip()}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = _LIST_SQL[table] + clause
        order = {
            "records": " ORDER BY updated_at DESC",
            "archive": " ORDER BY deleted_at DESC",
            "inbox": " ORDER BY occurred_at DESC",
            "audit": " ORDER BY created_at DESC",
            "relationships": " ORDER BY closeness DESC, updated_at DESC",
            "people": " ORDER BY updated_at DESC",
            "tombstones": " ORDER BY deleted_at DESC",
        }[table]
        total = int(db.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0])
        rows = [dict(row) for row in db.execute(sql + order + " LIMIT ? OFFSET ?",
                                               params + [capped, skip])]
        if table in {"records", "archive"}:
            # 补上"这条记忆跟谁有关"——**这正是记忆里最重要的那一维**
            # （docs/MEMORY_PEOPLE.md：`record_subjects` 管归属与召回，
            # `subject_user_id` 管隔离）。原来这个接口不带它，等于让人看不出
            # "哪条记忆是谁的"，也就没法核对记忆有没有挂错人。
            # 归档也要带：删错了要看清删的是谁的事，才判断得出该不该放回去。
            _attach_subjects(db, rows)
        return {"available": True, "rows": rows, "total": total,
                "limit": capped, "offset": skip}
    except sqlite3.Error as exc:
        logger.warning("webui_memory_query_failed table=%s category=%s", table,
                       type(exc).__name__)
        return {"available": False, "rows": [], "total": 0}
    finally:
        db.close()


#: 「问题记忆」的筛选口径（面板用来挑"该删的"）。每条都是一个只读 SQL 条件。
PROBLEM_FILTERS: dict[str, str] = {
    "expired": "expires_at IS NOT NULL AND expires_at < ?",
    "stale": "last_used_at IS NOT NULL AND last_used_at < ?",
    "low_confidence": "confidence < 0.5",
    "agent_inferred": "origin = 'agent_inferred' AND revision = 1",
    "no_subject": "kind = 'status' AND (subject_user_id IS NULL OR subject_user_id = '')",
}


def read_problem_records(*, root: Path | None = None, which: str = "stale",
                         limit: int = 100) -> dict[str, object]:
    """只读地挑"看起来有问题"的活跃记忆，供人工决定删哪条。"""

    condition = PROBLEM_FILTERS.get(str(which))
    if condition is None:
        raise ValueError("unknown_filter")
    capped = max(1, min(MAX_LIST, int(limit)))
    now = time.time()
    base = root or _data_root()
    db = open_memory_readonly(_memory_path(base))
    if db is None:
        return {"available": False, "rows": [], "filter": which}
    try:
        db.row_factory = sqlite3.Row
        params: list[object] = []
        if "?" in condition:
            # 过期看当前时间；久未使用看 30 天前。
            params.append(now - (30 * 86400 if which == "stale" else 0))
        rows: list[dict[str, object]] = []
        for tier in ("memory_short", "memory_long"):
            sql = (f"SELECT id, scope_type, scope_key, kind, normalized_key, content, "
                   f"confidence, revision, origin, expires_at, last_used_at, updated_at "
                   f"FROM {tier} WHERE status='active' AND ({condition}) "
                   "ORDER BY updated_at ASC LIMIT ?")
            rows.extend(dict(row) for row in db.execute(sql, params + [capped]))
        return {"available": True, "rows": rows[:capped], "filter": which,
                "filters": sorted(PROBLEM_FILTERS)}
    except sqlite3.Error as exc:
        logger.warning("webui_problem_query_failed category=%s", type(exc).__name__)
        return {"available": False, "rows": [], "filter": which}
    finally:
        db.close()


# --- 日志尾部 ---------------------------------------------------------------


def tail_jsonl(path: Path, *, limit: int = 20, max_bytes: int = TAIL_BYTES) -> list[dict[str, object]]:
    """读 jsonl 的尾部若干条。文件已经 20 MB+，所以**不整份读**。"""

    capped = max(1, min(MAX_LIST, int(limit)))
    try:
        size = path.stat().st_size
    except OSError:
        return []
    start = max(0, size - max_bytes)
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            blob = handle.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # 第一行可能是被切断的半条
    rows: list[dict[str, object]] = []
    for line in lines[-capped:]:
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def read_logs(feature: str, *, root: Path | None = None, limit: int = 20) -> dict[str, object]:
    name = str(feature or "").strip()
    if name not in LOG_FEATURES:
        raise ValueError("unknown_feature")
    base = root or _data_root()
    directory = os.environ.get("QQBOT_FEATURE_LOG_DIR", "").strip()
    target = Path(directory) if directory else base / "logs"
    path = target / f"{name}.jsonl"
    rows = tail_jsonl(path, limit=limit)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {"feature": name, "rows": rows, "file": str(path), "size_bytes": size,
            "features": list(LOG_FEATURES)}


def _port_in_use(host: str, port: int) -> bool:
    """这个端口上已经有人监听了吗（面板的"bot 在不在跑"就靠它）。

    **用 bind 探，不用 connect 探**（与核心 `stage3_main._port_in_use` 同一手法）：
    连一下确实也能探出"有人在听"，但那半截连接会被对面当成一次失败的握手，
    在**现有实例**的日志里留一条异常堆栈（核心那边的注释记着这次踩坑）。
    bind 不产生连接：端口被占就直接 `WSAEADDRINUSE`。

    **探针留在这里，不再从核心取**（2026-10-06）：这段代码原来写的是
    `from ...onebot_ws import port_in_use`，而那个名字**从来没有存在过**
    （`onebot_ws.py` 里只有消息解析那几个函数）→ `/health` 与 `/api/overview`
    每次都 ImportError → 500；前端那道登录门正是拿 `/api/overview` 验令牌的，
    于是**令牌再对也进不去**（截图抓到：一直停在登录页）。
    核心那个同名函数是 `stage3_main._port_in_use`：私有，而且是"防重复启动"的
    内部细节，不是给插件用的接缝——面板自己探一次更干净，也不用顺着核心内部拿东西。
    """

    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def read_health(*, attached: bool = False, started_at: float = 0.0,
                address: object = None) -> dict[str, object]:
    """面板自己的健康 + bot 进程是否活着（**用 bind 探法**，不产生半截连接）。

    `attached` 由调用方给（面板自己知道核心那侧接上没有）——这里不该反过来去问引擎。
    `address` 也是调用方给的（装配点注入的 `panel_address`：**实际**绑上的 host/port）。
    两样都不给时退回配置值，并用 `panel_port_exact` 如实标出"这是配置里的、不是实测的"
    ——设备页据此换一句更保守的话（2026-10-06 截图核对抓到：夹具听在 8799、配置写 8790，
    照配置显示就是假话）。

    2026-10-06 设备页返工加的几项（`python` / `panel_host` / `panel_port`）都是**面板
    自己的东西**（本进程的 Python、我们监听的地址），不是核心的数据——所以这里读没有
    越过任何接缝；核心那侧的（版本 / 提交 / 依赖）面板**读不到就是读不到**，不编。
    """

    # 主机/端口本来就是 `dev_config` 的（2026-10-01：原来连 `stage3_main` 里的
    # `ONEBOT_WS_HOST` / `ONEBOT_WS_PORT` 都顺着拿，那两样不该从核心内部取）。
    # 端口探针见 `_port_in_use`。
    from ... import dev_config

    live = False
    try:
        live = bool(_port_in_use(dev_config.ONEBOT_WS_HOST, dev_config.ONEBOT_WS_PORT))
    except Exception:  # noqa: BLE001 - 探不到就报"不知道"，不猜
        live = False
    bound = address if isinstance(address, dict) else {}
    port = bound.get("port")
    exact = isinstance(port, int) and not isinstance(port, bool) and port > 0
    return {
        "panel_started_at": started_at,
        "panel_uptime_seconds": round(max(0.0, time.time() - started_at), 1) if started_at else 0.0,
        "bot_port": dev_config.ONEBOT_WS_PORT,
        "bot_live": live,
        "engine_attached": bool(attached),
        "data_dir": str(_data_root()),
        "python": platform.python_version(),
        "panel_host": str(bound.get("host") or dev_config.WEBUI_HOST),
        "panel_port": int(port) if exact else dev_config.WEBUI_PORT,
        "panel_port_exact": exact,
    }


#: 「占用空间」那张卡最多往下走几层、最多数多少个文件。
#: data/ 下的日志能有几万个小文件——数不完就如实说"至少这么多"，别把它当总数。
STORAGE_MAX_DEPTH = 3
STORAGE_MAX_ENTRIES = 20000


def _dir_size(path: Path, budget: int, *, depth: int) -> tuple[int, int, bool]:
    """递归求和：`(字节数, 数了几个文件, 有没有被上限截断)`。

    只数 `st_size`，**不打开任何文件**；符号链接一律跳过（data/ 里可能有指向外部的
    链接，跟着走会把别处的大小算进来）。
    """

    size = 0
    used = 0
    truncated = False
    try:
        children = list(os.scandir(path))
    except OSError:
        return 0, 0, False
    for entry in children:
        if used >= budget:
            return size, used, True
        try:
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                size += entry.stat(follow_symlinks=False).st_size
                used += 1
                continue
            if entry.is_dir(follow_symlinks=False) and depth < STORAGE_MAX_DEPTH:
                child_size, child_used, cut = _dir_size(
                    Path(entry.path), budget - used, depth=depth + 1)
                size += child_size
                used += child_used
                truncated = truncated or cut
        except OSError:
            continue
    return size, used, truncated


def read_storage(root: Path | None = None) -> dict[str, object]:
    """`data/` 下**每一项各占多少**（设备页那张「占用空间」卡）。

    这是**面板自己的落点**（与它的日志页、状态文件同一棵树），不是核心的数据面，
    所以这里直接读文件系统没有越过任何接缝。读不到就回 `available: false`。
    """

    base = Path(root) if root is not None else _data_root()
    try:
        children = sorted(base.iterdir(), key=lambda item: item.name)
    except (FileNotFoundError, OSError):
        return {"available": False, "entries": [], "total_bytes": 0,
                "truncated": False, "path": str(base),
                "limits": {"depth": STORAGE_MAX_DEPTH, "entries": STORAGE_MAX_ENTRIES}}
    entries: list[dict[str, object]] = []
    total = 0
    truncated = False
    budget = STORAGE_MAX_ENTRIES
    for child in children:
        try:
            if child.is_symlink():
                continue
            if child.is_file():
                size = child.stat().st_size
                entries.append({"name": child.name, "bytes": size, "kind": "file",
                                "files": 1})
                total += size
                continue
            if not child.is_dir():
                continue
            size, used, cut = _dir_size(child, budget, depth=1)
            budget -= used
            truncated = truncated or cut
            entries.append({"name": child.name, "bytes": size, "kind": "dir",
                            "files": used})
            total += size
        except OSError:
            continue
    entries.sort(key=lambda item: (-int(item["bytes"]), str(item["name"])))
    return {"available": True, "entries": entries, "total_bytes": total,
            "truncated": truncated, "path": str(base),
            "limits": {"depth": STORAGE_MAX_DEPTH, "entries": STORAGE_MAX_ENTRIES}}
