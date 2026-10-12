"""长期记忆的**只读口**：给写信那条路按需查，一个字都写不进去。

由来（2026-10-12 用户，原话）：

    记忆要完整开放给写信agent来读取，又不是强制一次输入就必须得到一封信。

在那之前，写信那一侧手上有两样东西：窗口计数（`chat_window`，只数不读）与
`ReportSeams.memory_service`——**那是真正的 `MemoryService`，顺着它就能拿到可写的
`MemoryStore`**（`purge_records` / `merge_alias` / `retrieve`（会写 `last_used_at`）
/ `commit` / `cleanup` 全都调得到）。也就是说"插件不许碰记忆"这条边界当时**不是接缝
保证的**，只是 `daily_report.py` 恰巧只挑了那两个只读方法，而且没有任何测试拦它。
这里把它换掉：接缝只递**查询函数**，实现落在这一侧。

## 为什么另开一条连接，而不是复用 `MemoryStore`

- `MemoryStore.connection()`（`memory_store.py:328-441`）每次取用都
  `BEGIN IMMEDIATE` + `commit()`：**纯 SELECT 也取库级写锁、也写盘**。
  写信那一刻正是记忆维护 agent 提交的时候，两边会互相等；
  `MemoryService` 的读一律 `asyncio.to_thread(..., timeout=0.3)` 就是为了躲这个。
- `MemoryStore.retrieve()` 会 `UPDATE … SET last_used_at=?`——那是"她在群里想起来过"，
  写一封信时翻看旧事不该算进去。
- 这里用 sqlite 自己的只读模式（`file:…?mode=ro`）+ `PRAGMA query_only=1`：
  **结构性只读**。以后谁往这个模块里加一句 SQL，也写不进去（SQLite 会拒绝），
  不靠"记得别写"。

## 读出来的是什么

只有 `group_safe`（唯一可外露档）、`active`、`confidence >= 0.5` 的记录，
字段白名单见 `READONLY_FIELDS`——**没有 `source_event_ids`、没有 `last_used_at`、
没有 `origin`**：写信不需要知道"这条是怎么来的"，那是审查要看的。

口径与 `memory_model.MemoryMaterial.as_data` 一样：往 DATA 段渲染，`<memory>` 包起来，
正文转义。**不按群过滤**：用户要的是"完整开放"，她自己在信里决定说什么——
每条都带 `scope`，她分得清哪件事发生在哪个地方。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable
import time
from html import escape
from pathlib import Path

from .security import sanitize_chat_text

logger = logging.getLogger(__name__)

#: 允许出给写信 agent 的字段（**白名单**，测试逐个钉住）。
READONLY_FIELDS = ("kind", "scope_type", "scope_key", "subject_user_id",
                   "content", "confidence", "updated_at")

#: 只有这一档记忆可以外露（`memory_model` 里的同一口径）。
VISIBILITY = "group_safe"
MIN_CONFIDENCE = 0.5
#: 一次最多给几条（模型问一轮，别把整个库倒给它）。
MAX_ROWS = 12
#: 单条正文截断长度（`memory_model.MAX_CONTENT` 是 300，这里再收一点：
#: 一封信里她问两三次，每次带回一堆长正文会把 system 之外的那段撑满）。
MAX_CONTENT_CHARS = 240
#: 一次查询词的长度上限。
MAX_QUERY_CHARS = 60

_COLUMNS = ("kind, scope_type, scope_key, subject_user_id, content, confidence, updated_at")
_WHERE = ("status = 'active' AND visibility = 'group_safe' AND confidence >= ? "
          "AND (expires_at IS NULL OR expires_at > ?)")
_USER_ID = re.compile(r"^\d{4,12}$")


def default_path() -> Path:
    """生产那份库的位置（与 `MemoryService` 用的同一个目录算式）。

    正常情况下由装配点把 `MemoryStore.path` 递进来；这个函数是"没拿到 store"
    时的兜底，也是测试的落点。
    """

    try:
        from .memory_config import MemorySettings

        return MemorySettings.from_environment().data_dir / "memory.sqlite3"
    except Exception:  # noqa: BLE001 - 配置读不出来就用默认目录，别在这里炸
        return Path(__file__).resolve().parents[2] / "data" / "memory" / "memory.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    """只读打开。`mode=ro` 保证连建库都不会发生（文件不在就是打不开）。"""

    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _select(where: str) -> str:
    """两张表（短期 / 长期）各查一次，按更新时间倒序合并。"""

    return (
        f"SELECT {_COLUMNS} FROM memory_short WHERE {_WHERE} AND {where} "
        f"UNION ALL "
        f"SELECT {_COLUMNS} FROM memory_long WHERE {_WHERE} AND {where} "
        f"ORDER BY updated_at DESC LIMIT ?"
    )


def _like(text: str) -> str:
    """把查询词包成 LIKE 模式，`\\` `%` `_` 都转义掉（否则问一句 "%" 会拉回全库）。"""

    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _rows_about(conn: sqlite3.Connection, user_id: str, limit: int, now: float):
    """按人查：归属人是他，或者这条挂在 `user_global` / `user_group` 的域里。"""

    where = "(subject_user_id = ? OR scope_key = ? OR scope_key LIKE ?)"
    params = (user_id, user_id, f"%:{user_id}")
    return conn.execute(_select(where),
                        (MIN_CONFIDENCE, now, *params, MIN_CONFIDENCE, now, *params, limit))


def _rows_matching(conn: sqlite3.Connection, text: str, limit: int, now: float):
    """按话查：正文里出现这个词。"""

    where = "content LIKE ? ESCAPE '\\'"
    params = (_like(text),)
    return conn.execute(_select(where),
                        (MIN_CONFIDENCE, now, *params, MIN_CONFIDENCE, now, *params, limit))


def _clean(row: sqlite3.Row) -> dict:
    """只留白名单字段；正文按 `MAX_CONTENT_CHARS` 截断。"""

    cleaned: dict[str, object] = {}
    for field in READONLY_FIELDS:
        value = row[field] if field in row.keys() else None
        if field == "content":
            value = sanitize_chat_text(str(value or ""), max_length=MAX_CONTENT_CHARS)
        elif field == "confidence":
            value = float(value or 0.0)
        elif field == "updated_at":
            value = float(value or 0.0)
        cleaned[field] = value
    return cleaned


def lookup(query: str, *, db_path: str | Path | None = None, limit: int = 5,
           now: float | None = None) -> tuple[dict, ...]:
    """她问一句，回几条旧事。**任何失败都是空元组 + 一条日志**，不抛异常。

    两种问法（由这一侧判断，写信那边只管说人话）：

    - **一串数字**（群里那个号）：这个人的事——他说过什么、她记下了什么、什么关系；
    - **别的话**：按正文找，例如"考试""那个接口""喜欢什么"。

    查不到就是空元组：模型那边会看到一句"没有找到"，接着照现在知道的写。
    """

    text = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    if not text:
        return ()
    path = Path(db_path) if db_path else default_path()
    stamp = time.time() if now is None else float(now)
    capped = max(1, min(MAX_ROWS, int(limit)))
    try:
        conn = _connect(path)
    except Exception as exc:  # noqa: BLE001 - 库不在/没权限：她说"想不起来"就行
        logger.warning("memory_readonly_unavailable category=%s path=%s",
                       type(exc).__name__, path)
        return ()
    try:
        rows = (_rows_about(conn, text, capped, stamp) if _USER_ID.match(text)
                else _rows_matching(conn, text, capped, stamp))
        return tuple(_clean(row) for row in rows)
    except Exception as exc:  # noqa: BLE001 - 表结构变了也不该让写信炸掉
        logger.warning("memory_readonly_failed category=%s query_len=%s",
                       type(exc).__name__, len(text))
        return ()
    finally:
        conn.close()


#: 关系表里允许出给写信插件的那几列。
RELATIONSHIP_FIELDS = ("user_id", "closeness", "guardedness", "last_seen_at")
#: 关系变动流水里允许出的列——**没有 `source`**：那是审查看的，信里用不着。
CHANGE_FIELDS = ("user_id", "axis", "before_value", "after_value", "reason", "occurred_at")


def _query(path: Path, sql: str, params: tuple, fields: tuple[str, ...], *, what: str):
    """一条只读查询：连不上、查不动都只出声、返回空。"""

    try:
        conn = _connect(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_readonly_unavailable category=%s path=%s",
                       type(exc).__name__, path)
        return ()
    try:
        return tuple({name: row[name] for name in fields}
                     for row in conn.execute(sql, params).fetchall())
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_readonly_failed category=%s what=%s",
                       type(exc).__name__, what)
        return ()
    finally:
        conn.close()


def people(*, db_path: str | Path | None = None, since: float = 0.0,
           limit: int = 20) -> tuple[dict, ...]:
    """最近打过交道的人（`last_seen_at >= since`，按最近说话时间倒序）。

    与 `MemoryStore.top_relationships` 同一份数据，但**不从 `connection()` 走**
    （那条路每个 SELECT 都 `BEGIN IMMEDIATE`）。
    """

    path = Path(db_path) if db_path else default_path()
    capped = max(1, min(100, int(limit)))
    return _query(
        path,
        "SELECT user_id, closeness, guardedness, last_seen_at FROM relationships "
        "WHERE last_seen_at >= ? ORDER BY last_seen_at DESC LIMIT ?",
        (float(since or 0.0), capped), RELATIONSHIP_FIELDS, what="people",
    )


def changes(since: float, *, db_path: str | Path | None = None,
            limit: int = 20) -> tuple[dict, ...]:
    """某段时间里的关系变动（档位 + 那句依据，没有聊天正文）。"""

    path = Path(db_path) if db_path else default_path()
    capped = max(1, min(100, int(limit)))
    return _query(
        path,
        "SELECT user_id, axis, before_value, after_value, reason, occurred_at "
        "FROM relationship_log WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT ?",
        (float(since or 0.0), capped), CHANGE_FIELDS, what="changes",
    )


@dataclass(frozen=True, slots=True)
class MemoryRead:
    """递给写信插件的那一份：**只有查询**——没有 store、没有 service、没有写。

    三个方法都是"读一条、回一段"，失败回空。写信那条路拿到的就是这一个对象
    （`ReportSeams.memory_read`），以前的 `memory_service` 字段已经撤掉：
    那时候插件顺着 `service.store` 能拿到整个可写的 `MemoryStore`，
    "插件不准碰记忆"只靠它恰巧只调了两个只读方法。

    `db_path` 由装配点填成 `MemoryStore.path`（同一个文件）；没填就用默认目录。
    """

    db_path: Path | str | None = None
    clock: Callable[[], float] = time.time

    def search(self, query: str, *, limit: int = 5) -> tuple[dict, ...]:
        """她问一句，回几条记录（字段白名单，见 `READONLY_FIELDS`）。"""

        return lookup(query, db_path=self.db_path, limit=limit, now=self.clock())

    def recall(self, query: str, *, limit: int = 5) -> str:
        """同上，但直接给**渲染好的文本**：插件把这段原样放进对话即可。

        为什么要渲染好再给：转义与截断都在核心这一侧（`render()`），
        插件手上永远没有原始记录，也就没有"忘了转义"这种事。
        """

        rows = self.search(query, limit=limit)
        text = render(rows)
        if not text:
            return "（翻了一下，没找到跟这个有关的旧事。）"
        return text

    def people(self, since: float = 0.0, *, limit: int = 20) -> tuple[dict, ...]:
        """最近打过交道的人（信里"跟谁说过话"那句的素材）。"""

        return people(db_path=self.db_path, since=since, limit=limit)

    def changes(self, since: float, *, limit: int = 20) -> tuple[dict, ...]:
        """这段时间里她改过看法的人（"原因是…"那句的素材）。"""

        return changes(since, db_path=self.db_path, limit=limit)


def render(rows) -> str:
    """渲染成 DATA 段里的 `<memory>` 行（口径照 `MemoryMaterial.as_data`）。

    正文与属性都转义：这些是**模型写的**文本，进 prompt 之前一律当数据。
    """

    lines: list[str] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        content = escape(str(row.get("content") or ""), quote=False)
        if not content:
            continue
        scope = escape(str(row.get("scope_type") or "group"), quote=True)
        owner = escape(str(row.get("subject_user_id") or row.get("scope_key") or "group"),
                       quote=True)
        kind = escape(str(row.get("kind") or ""), quote=True)
        lines.append(f'<memory scope="{scope}" subject="{owner}" kind="{kind}">{content}</memory>')
    return "\n".join(lines)
