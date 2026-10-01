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
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    return Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"


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

    from .operator_config import SETTINGS

    masked: dict[str, str] = {}
    for key, value in values.items():
        spec = SETTINGS.get(str(key))
        if spec is not None and spec.get("kind") == "secret":
            masked[str(key)] = mask_secret(value)
        else:
            masked[str(key)] = str(value)
    return masked


# --- 引擎状态 ---------------------------------------------------------------


def _state_from_engine(engine) -> dict[str, object]:
    """活快照：会话、启停、计数（与 `/super status` 同一口径）。"""

    snapshot = engine.snapshot()
    metrics = snapshot.metrics or {}
    latency = metrics.get("model_latency") or {} if isinstance(metrics, dict) else {}
    store = getattr(engine, "usage_store", None)
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


def read_state(engine, root: Path | None = None) -> dict[str, object]:
    """状态：bot 在跑就用活快照，否则用文件快照（并标明来源）。"""

    if engine is not None:
        try:
            return _state_from_engine(engine)
        except Exception:  # noqa: BLE001 - 快照失败就退到文件，不让整页挂掉
            logger.warning("webui_engine_snapshot_failed", exc_info=True)
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

    from .stage3_main import APICHECK_ROLES

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
        for key, value in (filters or {}).items():
            column = allowed.get(str(key))
            if column is None or value in (None, ""):
                continue
            where.append(f"{column} = ?")
            params.append(str(value))
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


def read_health(engine, *, started_at: float = 0.0) -> dict[str, object]:
    """面板自己的健康 + bot 进程是否活着（**用 bind 探法**，不产生半截连接）。"""

    from .stage3_main import _port_in_use, ONEBOT_WS_HOST, ONEBOT_WS_PORT

    live = False
    try:
        live = bool(_port_in_use(ONEBOT_WS_HOST, ONEBOT_WS_PORT))
    except Exception:  # noqa: BLE001 - 探不到就报"不知道"，不猜
        live = False
    return {
        "panel_started_at": started_at,
        "panel_uptime_seconds": round(max(0.0, time.time() - started_at), 1) if started_at else 0.0,
        "bot_port": ONEBOT_WS_PORT,
        "bot_live": live,
        "engine_attached": engine is not None,
        "data_dir": str(_data_root()),
    }
