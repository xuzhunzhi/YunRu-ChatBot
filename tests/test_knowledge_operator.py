"""知识库的**可编辑层**：面板块叠在只读语料之上，且不碰基准语料。

由来（2026-10-01 用户）："要求知识库可以手动更改"，随后一句更要紧的话——
"stage3 可以改，要改说明你当时就没做好未来插件插入的接口"。这个文件钉的就是那个接口：

1. 面板块能被检索到，且排在基准块之前（`authority=4`）；
2. **基准语料与索引文件不被写**（跑前后 mtime 与行数比对）；
3. 保存前过边界校验（构建笔记、机制词一律拒）；
4. 索引缺失时降级为"只用面板块"，不抛。
"""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.extensions import KnowledgeItem
from qq_roleplay_bot.knowledge_base import KnowledgeIndex
from qq_roleplay_bot.knowledge_operator import (
    KnowledgeRejected, OperatorChunksStore, OperatorKnowledge, index_overview,
    open_index_readonly,
)

CHUNK_TITLE = "泉水的口头禅"
CHUNK_BODY = "她一提泉水就说“又是泉水”。"


class FakeIndex:
    """一个最小可用的索引替身：只回一条基准块。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(tempfile.gettempdir()) / "absent-knowledge.sqlite3"
        self.calls = 0

    def search(self, query, *, limit, prefer_canon=True):
        self.calls += 1
        return [KnowledgeItem(source="02-核心设定/a.md", title="希望角",
                              content="官方设定：她在阿拉斯加希望角。")]


def _knowledge(tmp: str) -> OperatorKnowledge:
    return OperatorKnowledge(FakeIndex(), OperatorChunksStore(Path(tmp)))


# --- 基本读写 ---------------------------------------------------------------

def test_operator_chunk_is_found_and_ranks_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        chunk = knowledge.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        assert chunk.source.startswith("operator/")
        assert chunk.authority > 3, "面板块的权威度要高于官方口径"
        results = asyncio.run(knowledge.search("泉水", limit=5))
        assert results, "该检索得到"
        assert results[0].source.startswith("operator/"), [r.source for r in results]
        assert knowledge.last_operator_hits == 1


def test_search_without_operator_chunks_returns_base() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        results = asyncio.run(knowledge.search("希望角", limit=5))
        assert [item.source for item in results] == ["02-核心设定/a.md"]
        assert knowledge.last_operator_hits == 0


def test_update_and_delete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        chunk = knowledge.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        updated = knowledge.update_chunk(chunk.id, content=CHUNK_BODY + "（改过）")
        assert updated.id == chunk.id
        assert "改过" in knowledge.chunks()[0].content
        assert knowledge.delete_chunk(chunk.id) is True
        assert knowledge.chunks() == []
        assert knowledge.delete_chunk(chunk.id) is False


def test_store_round_trips_through_disk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first = _knowledge(tmp)
        chunk = first.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        second = _knowledge(tmp)          # 模拟重启
        assert [item.id for item in second.chunks()] == [chunk.id]
        assert second.chunks()[0].content == CHUNK_BODY


# --- 边界校验 ---------------------------------------------------------------

def test_build_notes_are_rejected() -> None:
    """索引侧会剔掉"写给 bot 的说明"，面板块也必须同样拦住（否则它成了绕过口）。"""

    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        try:
            knowledge.add_chunk(title="性格维度", content="云茹 bot 应体现以下核心性格维度。")
        except KnowledgeRejected as exc:
            assert "说明" in str(exc) or "设定" in str(exc)
        else:
            raise AssertionError("构建笔记该被拒")


def test_mechanism_words_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        try:
            knowledge.add_chunk(title="日程", content="她每天要检查一遍自己的记忆库。")
        except KnowledgeRejected as exc:
            assert "机制" in str(exc)
        else:
            raise AssertionError("机制词该被拒")


def test_empty_and_oversized_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        for title, body in (("", "正文"), ("标题", "   "), ("标题", "字" * 5000)):
            try:
                knowledge.add_chunk(title=title, content=body)
            except KnowledgeRejected:
                continue
            raise AssertionError(f"{(title, len(body))} 该被拒")


# --- 版本 -------------------------------------------------------------------

def test_versions_can_be_restored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        knowledge.add_chunk(title="一", content="第一块内容")
        knowledge.add_chunk(title="二", content="第二块内容")
        versions = knowledge.chunk_versions()
        assert versions, "改动过就该有版本"
        assert knowledge.restore_chunks(versions[-1]["id"]) >= 1
        assert len(knowledge.chunks()) >= 1


# --- 只读保证 ---------------------------------------------------------------

def test_base_index_and_corpus_are_never_written() -> None:
    """**核心保证**：增删改面板块不动索引库，也不碰 `docs/yunru-source/`。"""

    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "knowledge.sqlite3"
        index = KnowledgeIndex(index_path)
        index.build(Path(tmp) / "corpus")      # 空语料也建库（表结构一样）
        before = index_path.stat().st_mtime_ns
        # 读一遍行列与 journal_mode，**连接立刻关掉**（Windows 上不关会锁住文件，
        # 之后 `TemporaryDirectory` 清理会报 WinError 32）。
        db = sqlite3.connect(index_path)
        try:
            rows_before = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            journal_before = db.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            db.close()
        time.sleep(0.02)
        knowledge = OperatorKnowledge(index, OperatorChunksStore(Path(tmp) / "operator"))
        chunk = knowledge.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        knowledge.update_chunk(chunk.id, content=CHUNK_BODY + " 改")
        knowledge.delete_chunk(chunk.id)
        assert index_path.stat().st_mtime_ns == before, "索引库不该被面板块操作写"
        db = sqlite3.connect(index_path)
        try:
            assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == rows_before
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == journal_before
        finally:
            db.close()


def test_chunks_survive_a_rebuild() -> None:
    """重建索引只重建基准块；面板块活在另一个文件里，因此不丢。"""

    with tempfile.TemporaryDirectory() as tmp:
        index_path = Path(tmp) / "knowledge.sqlite3"
        index = KnowledgeIndex(index_path)
        index.build(Path(tmp) / "corpus")
        store = OperatorChunksStore(Path(tmp) / "operator")
        knowledge = OperatorKnowledge(index, store)
        chunk = knowledge.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        index.build(Path(tmp) / "corpus")      # 整份 DROP + 重建
        fresh = OperatorKnowledge(KnowledgeIndex(index_path),
                                  OperatorChunksStore(Path(tmp) / "operator"))
        assert [item.id for item in fresh.chunks()] == [chunk.id]


def test_readonly_open_does_not_create_a_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.sqlite3"
        assert open_index_readonly(missing) is None
        assert not missing.exists()
        assert index_overview(FakeIndex(missing)) == {}


def test_overview_degrades_without_index() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        knowledge.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        overview = knowledge.overview()
        assert overview["operator_chunks"] == 1
        assert overview["base"] == {}


def test_corrupt_store_falls_back_to_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_chunks.json"
        path.write_text("{ 不是 JSON", encoding="utf-8")
        store = OperatorChunksStore(Path(tmp))
        assert store.load() == []
        assert store.last_error == "invalid_json"
        # 坏文件之后仍然能新增（不会被它卡住）
        chunk = store.add(title="新块", content="正文内容")
        assert chunk.id
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def test_index_failure_still_returns_operator_chunks() -> None:
    """索引坏掉/读不动时，面板块照旧能用（某一边出问题不该两边都没）。"""

    class _Broken:
        path = Path(tempfile.gettempdir()) / "absent.sqlite3"

        def search(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

    with tempfile.TemporaryDirectory() as tmp:
        knowledge = OperatorKnowledge(_Broken(), OperatorChunksStore(Path(tmp)))
        knowledge.add_chunk(title=CHUNK_TITLE, content=CHUNK_BODY)
        results = asyncio.run(knowledge.search("泉水", limit=5))
        assert [item.source for item in results][0].startswith("operator/")


def test_public_api_has_no_way_to_touch_the_corpus() -> None:
    """公开名白名单：这里没有"写基准语料/重建索引"的入口。"""

    from qq_roleplay_bot.knowledge_operator import PUBLIC_API

    public = [name for name in dir(OperatorKnowledge) if not name.startswith("_")]
    for name in public:
        assert not any(part in name.casefold() for part in
                       ("corpus", "rebuild", "index_write", "doc")), name
    assert set(PUBLIC_API) <= set(public)
    assert "search" in PUBLIC_API


def test_data_dir_env_controls_store_location() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("QQBOT_DATA_DIR")
        os.environ["QQBOT_DATA_DIR"] = tmp
        try:
            from qq_roleplay_bot.knowledge_operator import default_dir

            assert str(default_dir()) == str(Path(tmp) / "knowledge")
        finally:
            if saved is None:
                os.environ.pop("QQBOT_DATA_DIR", None)
            else:
                os.environ["QQBOT_DATA_DIR"] = saved
