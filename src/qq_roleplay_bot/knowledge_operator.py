"""面板能改的知识库：**面板块**叠在只读语料之上。

由来（2026-10-01 用户）："要求知识库可以手动更改"，紧接着一句更要紧的话——
"stage3 可以改，要改说明你当时就没做好未来插件插入的接口"。

他说得对：`runtime.build_knowledge_base()` 返回的对象只有 `search` 一个能力，
**没有任何编辑入口**，所以过去"改知识库"只能靠重建整份索引。这里补的就是那个接口，
而且刻意**不改 `knowledge_base.py` 的检索与切块逻辑**——只包一层：

    AsyncKnowledgeBase → OperatorKnowledge(KnowledgeIndex, OperatorChunksStore)

三层各管一件事，互不越界：

| 层 | 位置 | 谁能改 |
| --- | --- | --- |
| 基准语料 | `docs/yunru-source/`（AGENTS §五：原文一字不动） | 谁也改不了 |
| 检索索引 | `data/knowledge/knowledge.sqlite3` 的 `chunks`（FTS5 trigram + vectors） | 只由重建流程整份重建 |
| **面板块** | `data/knowledge/operator_chunks.json` | 面板增 / 改 / 删（有版本） |

几条硬约束：

1. **重建不丢面板内容**：`chunks` 表会被 `KnowledgeIndex.build` 整份 DROP 重建，
   而面板块活在**另一个文件**里，所以只读语料与面板内容天然互不覆盖。
2. **写时不放过边界**：面板块照样进 prompt（走 `PromptSources` 的 user 段 DATA），
   所以保存前必过 `knowledge_base.is_meta_note`（构建笔记/机制自述的块不许进）
   与 `prompt_guard` 的机制词扫描。面板不能成为绕过 DATA 边界与 AGENTS 2.2 的口子。
3. **失败一律降级**：面板块文件坏了 → 当没有面板块；索引缺失 → 只用面板块。
   任何一侧出问题都不该让"这次没资料"变成"这次报错"。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .extensions import KnowledgeItem
from .prompt_guard import scan_persona_text

logger = logging.getLogger(__name__)

STORE_VERSION = 1
#: 单块正文上限（与索引侧的 `MAX_CHUNK_CHARS` 同量级，别让一块顶掉整段预算）。
MAX_CONTENT = 2000
MAX_TITLE = 120
#: 面板块的权威度：**排在官方口径之上**——它是操作者当场的纠正/补充，
#: 不该被 885 KB 同人叙事挤下去。`AUTHORITY` 里官方最高是 3。
OPERATOR_AUTHORITY = 4
#: 面板块数量上限（防"把语料全文贴进来"这种用法把内存吃满）。
MAX_CHUNKS = 2000
#: 每块留几个历史版本。
MAX_VERSIONS = 20


class KnowledgeRejected(ValueError):
    """面板提交的知识块不合法。消息是给人看的固定说法。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail or message


def default_dir() -> Path:
    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    root = Path(base).resolve() if base else Path(__file__).resolve().parents[2] / "data"
    return root / "knowledge"


@dataclass(frozen=True, slots=True)
class OperatorChunk:
    """一个面板块。字段刻意与索引里的块对齐（source/title/content/lines/authority）。"""

    id: str
    title: str
    content: str
    note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def source(self) -> str:
        return f"operator/{self.id}"

    @property
    def lines(self) -> str:
        return ""

    @property
    def authority(self) -> int:
        return OPERATOR_AUTHORITY

    def as_row(self) -> dict[str, object]:
        return {"id": self.id, "title": self.title, "content": self.content, "note": self.note,
                "created_at": self.created_at, "updated_at": self.updated_at}


def _validate(title: str, content: str) -> tuple[str, str]:
    head = " ".join(str(title or "").split())[:MAX_TITLE]
    body = str(content or "").strip()
    if not head:
        raise KnowledgeRejected("标题不能为空")
    if not body:
        raise KnowledgeRejected("正文不能为空")
    if len(body) > MAX_CONTENT:
        raise KnowledgeRejected(f"正文太长了（上限 {MAX_CONTENT} 字）")
    # 与索引侧同一份判据：构建笔记（"云茹 bot 应体现…"）读进 prompt 等于在 DATA 里下指令。
    from .knowledge_base import is_meta_note

    if is_meta_note(head, body):
        raise KnowledgeRejected(
            "这块读起来像“写给 bot 的说明”，不是设定资料；会被模型当成指令，保存被拒绝",
            detail="判据与索引侧一致（is_meta_note）",
        )
    # 知识块读进的是她自己的 prompt 段，所以按**人格口径**扫（与人设一样严）：
    # "检查/触发/调用/协议/提示词/上下文/记忆库/Stage" 一个都不许从资料里混进去。
    hits = scan_persona_text(body)
    if hits:
        raise KnowledgeRejected(
            "这块里有机制性措辞，会被角色吸收，保存被拒绝",
            detail="命中：" + "、".join(hits),
        )
    return head, body


class OperatorChunksStore:
    """面板块的读写（`data/knowledge/operator_chunks.json` + `versions/`）。"""

    def __init__(self, directory: Path | str | None = None, *, enabled: bool = True,
                 clock=time.time) -> None:
        self.directory = Path(directory) if directory is not None else default_dir()
        self.enabled = bool(enabled)
        self.clock = clock
        self.chunks: list[OperatorChunk] = []
        self.last_error = ""
        self._loaded = False

    # --- 位置 -------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.directory / "operator_chunks.json"

    def _versions_dir(self) -> Path:
        return self.directory / "operator_versions"

    # --- 读 ---------------------------------------------------------------

    def load(self) -> list[OperatorChunk]:
        self.chunks = []
        self.last_error = ""
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            self.last_error = type(exc).__name__
            logger.warning("operator_chunks_read_failed category=%s", type(exc).__name__)
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "invalid_json"
            logger.warning("operator_chunks_invalid_json；当没有面板块处理")
            return []
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            self.last_error = "unsupported_version"
            return []
        items = data.get("chunks")
        if not isinstance(items, list):
            return []
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "").strip()
            content = str(item.get("content") or "")
            if not identifier or not content:
                continue
            self.chunks.append(OperatorChunk(
                id=identifier,
                title=str(item.get("title") or ""),
                content=content,
                note=str(item.get("note") or ""),
                created_at=float(item.get("created_at") or 0.0),
                updated_at=float(item.get("updated_at") or 0.0),
            ))
        return list(self.chunks)

    def all(self) -> list[OperatorChunk]:
        if not self._loaded:
            self.load()
        return list(self.chunks)

    def get(self, chunk_id: str) -> OperatorChunk | None:
        target = str(chunk_id or "")
        for chunk in self.all():
            if chunk.id == target:
                return chunk
        return None

    # --- 写 ---------------------------------------------------------------

    def add(self, *, title: str, content: str, note: str = "") -> OperatorChunk:
        head, body = _validate(title, content)
        if len(self.all()) >= MAX_CHUNKS:
            raise KnowledgeRejected(f"面板块太多了（上限 {MAX_CHUNKS} 条）")
        now = self.clock()
        chunk = OperatorChunk(id=uuid.uuid4().hex[:12], title=head, content=body,
                              note=" ".join(str(note or "").split())[:200],
                              created_at=now, updated_at=now)
        self._snapshot()
        self.chunks = self.all() + [chunk]
        self._write()
        logger.warning("operator_chunk_added id=%s chars=%s", chunk.id, len(body))
        return chunk

    def update(self, chunk_id: str, *, title: str | None = None,
               content: str | None = None, note: str | None = None) -> OperatorChunk:
        existing = self.get(chunk_id)
        if existing is None:
            raise KnowledgeRejected("没有这个块")
        head, body = _validate(
            existing.title if title is None else title,
            existing.content if content is None else content,
        )
        updated = OperatorChunk(
            id=existing.id, title=head, content=body,
            note=existing.note if note is None else " ".join(str(note).split())[:200],
            created_at=existing.created_at, updated_at=self.clock(),
        )
        self._snapshot()
        self.chunks = [updated if item.id == existing.id else item for item in self.all()]
        self._write()
        logger.warning("operator_chunk_updated id=%s chars=%s", updated.id, len(body))
        return updated

    def delete(self, chunk_id: str) -> bool:
        existing = self.get(chunk_id)
        if existing is None:
            return False
        self._snapshot()
        self.chunks = [item for item in self.all() if item.id != existing.id]
        self._write()
        logger.warning("operator_chunk_deleted id=%s", existing.id)
        return True

    # --- 落盘 -------------------------------------------------------------

    def _snapshot(self) -> None:
        """把**当前**整份留一版（回滚与"刚才删错了"的依据）。"""

        if not self.enabled or not self.chunks:
            return
        directory = self._versions_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            stamp = f"{int(self.clock() * 1000):013d}"
            target = directory / f"chunks.{stamp}.json"
            target.write_text(json.dumps(
                {"version": STORE_VERSION, "chunks": [c.as_row() for c in self.chunks]},
                ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.warning("operator_chunk_version_failed category=%s", type(exc).__name__)
            return
        try:
            files = sorted(directory.glob("chunks.*.json"))
            for stale in files[:-MAX_VERSIONS]:
                stale.unlink()
        except OSError:  # pragma: no cover
            pass

    def versions(self) -> list[dict[str, object]]:
        try:
            files = sorted(self._versions_dir().glob("chunks.*.json"), reverse=True)
        except OSError:
            return []
        rows: list[dict[str, object]] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            chunks = data.get("chunks") if isinstance(data, dict) else None
            rows.append({"id": path.stem, "count": len(chunks or [])})
        return rows

    def restore(self, version_id: str) -> int:
        """回滚到某一版（整份）。返回恢复后的块数。"""

        safe = str(version_id)
        digits = safe[len("chunks."):] if safe.startswith("chunks.") else ""
        if not digits.isdigit():
            raise KnowledgeRejected("版本号不合法")
        path = self._versions_dir() / f"chunks.{digits}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KnowledgeRejected("没有这个版本") from None
        except (OSError, ValueError):
            raise KnowledgeRejected("这个版本读不出来") from None
        items = data.get("chunks") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise KnowledgeRejected("这个版本读不出来")
        self._snapshot()
        restored: list[OperatorChunk] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id") or "")
            content = str(item.get("content") or "")
            if not identifier or not content:
                continue
            restored.append(OperatorChunk(
                id=identifier, title=str(item.get("title") or ""), content=content,
                note=str(item.get("note") or ""),
                created_at=float(item.get("created_at") or 0.0),
                updated_at=float(item.get("updated_at") or 0.0),
            ))
        self.chunks = restored
        self._write()
        logger.warning("operator_chunks_restored version=%s count=%s", safe, len(restored))
        return len(restored)

    def _write(self) -> None:
        if not self.enabled:
            return
        body = {"version": STORE_VERSION, "chunks": [c.as_row() for c in self.chunks]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                with handle:
                    json.dump(body, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                Path(handle.name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("operator_chunks_write_failed category=%s", type(exc).__name__)
            raise KnowledgeRejected("写不进去（磁盘或权限问题）") from None

    def stats(self) -> dict[str, object]:
        return {"path": str(self.path), "chunks": len(self.all()),
                "last_error": self.last_error}


@dataclass(frozen=True, slots=True)
class _OperatorRow:
    """给 `knowledge_base` 的排序函数用的行。

    它同时按**属性**（`_mentions_her` 用 `row["title"]`）与**下标**
    （`_rank_key` 用 `row["authority"]`）取字段——`sqlite3.Row` 就是这两种都支持，
    所以这里也得两种都给，否则"复用同一份排序"这件事做不到。
    """

    source: str
    title: str
    content: str
    authority: int
    semantic: float = 0.0

    def __getitem__(self, key: str):
        if key == "authority":
            return self.authority
        if key == "title":
            return self.title
        if key == "content":
            return self.content
        if key == "source":
            return self.source
        raise KeyError(key)

    def keys(self) -> tuple[str, ...]:
        return ("source", "title", "content", "authority", "semantic")


def _overlap(row: _OperatorRow, tokens: list[str]) -> int:
    haystack = f"{row.title}\n{row.content}".casefold()
    return sum(1 for token in tokens if token.casefold() in haystack)


def _merge_ranked(rows: list[tuple[_OperatorRow, int]], *, limit: int,
                  prefer_canon: bool) -> list[_OperatorRow]:
    """把"基准块 + 面板块"放一起按**既有排序键**重排。

    名次（`lexical`）由调用方给：基准块用它自己的名次，面板块按词面重合度折算，
    这样"同时命中时面板块排前面"是靠 `authority=4` 实现的，而不是靠偷偷插队。
    """

    from .knowledge_base import _rank_key  # noqa: PLC0415 - 与检索侧共用同一份排序

    ranked = sorted(
        rows,
        key=lambda pair: _rank_key(pair[0], lexical=pair[1], semantic_rank=0,
                                   has_semantic=False, expansion=0,
                                   prefer_canon=prefer_canon),
    )
    return [row for row, _lexical in ranked[:limit]]


class OperatorKnowledge:
    """`AsyncKnowledgeBase` 的同形状包装：检索时叠加面板块，并给出编辑接口。

    引擎只依赖 `async def search(query, *, limit, prefer_canon)`；多出来的方法
    （`chunks` 系列 / `rebuild`）只给面板用，所以不需要抽象的查找方法名。
    """

    def __init__(self, index, store: OperatorChunksStore | None = None) -> None:
        self.index = index
        self.store = store if store is not None else OperatorChunksStore()
        self.last_operator_hits = 0

    # --- 检索 -------------------------------------------------------------

    async def search(self, query: str, *, limit: int,
                     prefer_canon: bool = True) -> list[KnowledgeItem]:
        base: list[KnowledgeItem] = []
        try:
            base = self.index.search(query, limit=limit, prefer_canon=prefer_canon)
        except Exception:  # noqa: BLE001 - 索引坏了只当没有基准资料（沿用原降级）
            logger.warning("knowledge_lookup_failed", exc_info=True)
        chunks = self.store.all()
        if not chunks:
            self.last_operator_hits = 0
            return base
        try:
            from .knowledge_base import _query_tokens, sanitize_chat_text  # noqa: PLC0415

            tokens = _query_tokens(sanitize_chat_text(query or "", max_length=1000))
        except Exception:  # noqa: BLE001 - 分词失败就当没有命中，仍返回基准结果
            tokens = []
        if not tokens:
            self.last_operator_hits = 0
            return base
        scored: list[tuple[_OperatorRow, int]] = []
        for chunk in chunks:
            row = _OperatorRow(source=chunk.source, title=chunk.title,
                               content=chunk.content, authority=OPERATOR_AUTHORITY)
            overlap = _overlap(row, tokens)
            if overlap:
                # 名次折算：命中越多越靠前（-1 让"至少命中一个词"的块排在第 0 位之前）。
                scored.append((row, max(0, len(tokens) - overlap - 1)))
        if not scored:
            self.last_operator_hits = 0
            return base
        self.last_operator_hits = len(scored)
        merged: list[tuple[_OperatorRow, int]] = []
        for position, item in enumerate(base):
            merged.append((_OperatorRow(source=item.source, title=item.title,
                                        content=item.content, authority=_authority_of(item)),
                           position))
        merged.extend(scored)
        ranked = _merge_ranked(merged, limit=limit, prefer_canon=prefer_canon)
        return [KnowledgeItem(source=row.source, title=row.title, content=row.content)
                for row in ranked]

    # --- 编辑（只给面板） -------------------------------------------------

    def chunks(self) -> list[OperatorChunk]:
        return self.store.all()

    def add_chunk(self, *, title: str, content: str, note: str = "") -> OperatorChunk:
        return self.store.add(title=title, content=content, note=note)

    def update_chunk(self, chunk_id: str, *, title: str | None = None,
                     content: str | None = None, note: str | None = None) -> OperatorChunk:
        return self.store.update(chunk_id, title=title, content=content, note=note)

    def delete_chunk(self, chunk_id: str) -> bool:
        return self.store.delete(chunk_id)

    def chunk_versions(self) -> list[dict[str, object]]:
        return self.store.versions()

    def restore_chunks(self, version_id: str) -> int:
        return self.store.restore(version_id)

    def overview(self) -> dict[str, object]:
        """给面板的只读概览：基准索引的规模 + 面板块的数量。"""

        base: dict[str, object] = {}
        try:
            base = index_overview(self.index)
        except Exception:  # noqa: BLE001 - 索引坏了也要能看到面板块
            logger.warning("knowledge_overview_failed", exc_info=True)
        return {"base": base, "operator": self.store.stats(),
                "operator_chunks": len(self.chunks())}


def index_overview(index) -> dict[str, object]:
    """只读地数一下基准索引：块数、文件数、向量数、构建信息。

    **`mode=ro`**：面板只是看看，绝不能因为这个动作给索引库加写锁、更不该建表。
    """

    connection = open_index_readonly(index.path)
    if connection is None:
        return {}
    try:
        connection.row_factory = sqlite3.Row
        chunks = _count(connection, "chunks")
        files = 0
        try:
            files = int(connection.execute(
                "SELECT COUNT(DISTINCT source) FROM chunks").fetchone()[0])
        except sqlite3.Error:
            files = 0
        meta: dict[str, str] = {}
        try:
            for row in connection.execute("SELECT key, value FROM meta"):
                meta[str(row["key"])] = str(row["value"])
        except sqlite3.Error:
            meta = {}
        return {"chunks": chunks, "files": files,
                "vectors": _count(connection, "vectors"), "meta": meta,
                "size_bytes": _size_of(index.path)}
    except sqlite3.Error as exc:
        logger.warning("knowledge_overview_failed category=%s", type(exc).__name__)
        return {}
    finally:
        connection.close()


def _count(connection, table: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0


def _size_of(path) -> int:
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


def open_index_readonly(path):
    """以 `mode=ro` 打开索引；打不开返回 None（调用方据此降级）。"""

    import sqlite3 as _sqlite3

    try:
        if not Path(path).exists():
            return None
        return _sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=0.5)
    except _sqlite3.Error:
        return None


def _authority_of(item: KnowledgeItem) -> int:
    """基准块的权威度：按它自己的来源目录算（与索引侧同一函数）。"""

    from .knowledge_base import _authority

    return _authority(str(getattr(item, "source", "") or ""))


#: 面板块的公开方法白名单（测试钉住：这里没有"改基准语料"的入口）。
PUBLIC_API = ("search", "chunks", "add_chunk", "update_chunk", "delete_chunk",
              "chunk_versions", "restore_chunks", "overview")
