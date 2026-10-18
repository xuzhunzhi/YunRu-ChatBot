"""知识库的**相关程度 / 权威度乘法 / 门已废**（2026-10-18 那三条用户裁定）。

三条原话（照抄）：

- *"我不介意查库，消息积压了就多条一判，ds速度是够的"* → 每一轮 REPLY 都查（门废了）；
- *"相关程度3挡，012，0永远不调起，优先程度用乘法"* → 档 0/1/2 × 权威度，降序取前 N；
- *"官方权威程度5，面板4"* / *"同人2，世界知识1"* → 五档刻度，官方最高、面板次之。

这个文件钉住：
1. 三档判据（0 档：只有虚词/纯数字；1 档：只有一个内容词；2 档：≥3 字连续命中或 ≥2 个内容词）；
2. **0 档一条都不取**（权威度多高都不取）；
3. 乘法得到的四组关系（用户逐条点名的那四组）；
4. 面板块**同一把尺子**（不因为 authority=4 就免筛）；
5. 面板块的档位字段（缺省 4、只收 1/4、旧文件读成 4）；
6. `QQBOT_KNOWLEDGE_ITEMS` 可配（默认 5）。
"""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

from qq_roleplay_bot.extensions import KnowledgeItem, PromptSources, knowledge_item_limit
from qq_roleplay_bot.knowledge_base import (
    AUTHORITY,
    KnowledgeIndex,
    _authority_scale,
    _query_tokens,
    _scaled_authority,
    relevance_tier,
)
from qq_roleplay_bot.knowledge_operator import (
    OPERATOR_AUTHORITY,
    WORLD_KNOWLEDGE_AUTHORITY,
    KnowledgeRejected,
    OperatorChunksStore,
    OperatorKnowledge,
)


def _tier(query: str, title: str, content: str) -> int:
    """按真实口径算一档（`literal` 就是"查询里真的写着的词"）。"""

    tokens = _query_tokens(query)
    literal = {token for token in tokens if token in query}
    return relevance_tier(tokens, literal, title, content)


class _Index:
    """最小索引替身：回**固定的几条**基准块；`limit` 记下来给断言用。"""

    path = Path(tempfile.gettempdir()) / "absent-knowledge.sqlite3"

    def __init__(self, items=()) -> None:
        self.items = list(items)
        self.calls = 0
        self.limits: list[int] = []

    def search(self, query, *, limit, prefer_canon=True):
        self.calls += 1
        self.limits.append(limit)
        return list(self.items)


def _knowledge(tmp: str, items=()) -> OperatorKnowledge:
    return OperatorKnowledge(_Index(items), OperatorChunksStore(Path(tmp)))


def _titles(items) -> list[str]:
    return [item.title for item in items]


# --- 1. 三档判据 ------------------------------------------------------------


def test_a_single_two_char_hit_is_weak_but_kept() -> None:
    """只撞上一个 2 字词 → **1 档（弱）**，不是 0 档。

    用户的三档里"1 = 沾得上、可能用得上"，所以它还占名额——只是分数低（1×权威度）。
    `家人` 这种两字内容词问句（"你有家人吗"）就靠这一档活下来。
    """

    assert _tier("你有家人吗", "六、家人", "云茹的家人是她最不愿意提的一段。") == 1


def test_a_three_char_window_is_strong() -> None:
    """查询里有 ≥3 个字的连续片段真的对上 → **2 档**。"""

    assert _tier("百夫长攻城机甲是什么", "能力", "百夫长攻城机甲是她亲手造的保护壳。") == 2


def test_two_content_words_without_a_window_are_strong() -> None:
    """没有 3 字连续命中，但撞上 **2 个不同的内容词** → 也算 2 档。"""

    assert _tier("家用净水器滤芯", "杂谈", "净水机换下来的滤芯，价格不便宜。") == 2


def test_only_function_words_are_zero() -> None:
    """只撞上虚词 → **0 档**（"了一"/"一个"/"不是"/"这么"撞上任何长句就是这类）。"""

    tokens = _query_tokens("这么惨真不是人受的")
    literal = {token for token in tokens if token in "这么惨真不是人受的"}
    # 正文里只出现"这么"（虚词）这种两字组合，没有任何 ≥3 字的连续命中
    assert relevance_tier(tokens, literal, "随笔", "他笑了，这么一说，倒也。") == 0


def test_only_short_numbers_are_zero() -> None:
    """只撞上纯数字 → **0 档**：`19` 撞正文里的 `1983/1984/1985` 就是这么来的。"""

    tokens = _query_tokens("19号那天")
    literal = {token for token in tokens if token in "19号那天"}
    assert relevance_tier(tokens, literal, "年表", "1983 年的事，1984 年又提了一次。") == 0
    # 四位数字对上才算真实命中
    long_tokens = _query_tokens("1983年的事")
    long_literal = {token for token in long_tokens if token in "1983年的事"}
    assert relevance_tier(long_tokens, long_literal, "年表", "1983 年的事有记录。") == 2


# --- 2. 权威度乘法：用户点名的那四组 ----------------------------------------


def test_weak_official_beats_strong_fanfic() -> None:
    """弱（1×5=5）**压过**强同人（2×2=4）——官方弱命中仍然优先。"""

    with tempfile.TemporaryDirectory() as tmp:
        official = KnowledgeItem(source="02-核心设定/年表.md", title="官方",
                                 content="她多大年纪，档案里没写。")  # 只撞上"多大"=1 档
        fanfic = KnowledgeItem(source="05-同人系列原文/其一.md", title="同人",
                               content="巴米扬那年她二十四岁，把数据带回去的时候手是抖的。")
        knowledge = _knowledge(tmp, [official, fanfic])
        knowledge.add_chunk(title="无关", content="今天天气不错，适合出门走走。")
        results = asyncio.run(knowledge.search("巴米扬她多大", limit=5))
        assert _titles(results)[0] == "官方", _titles(results)
        assert "同人" in _titles(results)


def test_strong_panel_beats_weak_official() -> None:
    """强面板（2×4=8）**压过**弱官方（1×5=5）——真对题的补充能上位。"""

    with tempfile.TemporaryDirectory() as tmp:
        official = KnowledgeItem(source="02-核心设定/年表.md", title="官方",
                                 content="她用这个的时候很小心。")  # 只撞上"她用"=1 档
        knowledge = _knowledge(tmp, [official])
        knowledge.add_chunk(title="面板", content="她用的是那台裂地者，后来才换。")
        results = asyncio.run(knowledge.search("她用的裂地者是什么", limit=5))
        assert _titles(results)[0] == "面板", _titles(results)
        assert "官方" in _titles(results)


def test_world_knowledge_never_grabs_the_top() -> None:
    """强世界知识（2×1=2）**不高于**弱同人（1×2=2）——世界知识永远不抢占。

    并列时按稳定次序（这里：来源字符串），同人（`05-…`）排在 `operator/…` 之前。
    """

    with tempfile.TemporaryDirectory() as tmp:
        fanfic = KnowledgeItem(source="05-同人系列原文/其一.md", title="同人",
                               content="那年之后她就没再提。")  # 只撞上"那年"=1 档
        knowledge = _knowledge(tmp, [fanfic])
        knowledge.add_chunk(title="世界知识",
                            content="巴米扬是阿富汗的地下设施，被关的人很多。",
                            authority=WORLD_KNOWLEDGE_AUTHORITY)
        results = asyncio.run(knowledge.search("巴米扬那年她被关在哪", limit=5))
        assert _titles(results)[0] == "同人", _titles(results)
        assert "世界知识" in _titles(results)


def test_zero_tier_is_never_taken_even_for_official() -> None:
    """0 档**一条都不取**——哪怕它是官方档（5）。"""

    with tempfile.TemporaryDirectory() as tmp:
        official = KnowledgeItem(source="02-核心设定/年表.md", title="官方",
                                 content="1983 年的事，1984 年又提了一次。")
        knowledge = _knowledge(tmp, [official])
        knowledge.add_chunk(title="别的东西", content="净水机的滤芯该换了。")
        results = asyncio.run(knowledge.search("19号那天", limit=5))
        assert results == [], _titles(results)


# --- 3. 面板块同一把尺子 ----------------------------------------------------


def test_panel_chunk_is_not_exempt_from_the_same_ruler() -> None:
    """authority=4 的面板块只有 1 个内容词命中时是 **1 档**，压不过强对题的官方块。

    这是用户"面板块与官方语料同一把尺子（不许因为 4 就免筛）"的直接断言：
    面板块照样算档，档低就是低分（1×4=4 < 2×5=10）。
    """

    official = KnowledgeItem(source="02-核心设定/年表.md", title="官方",
                             content="巴米扬那次她被关在地下设施里。")
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp, [official])
        knowledge.add_chunk(title="面板", content="她不喜欢别人提巴米扬那几年。")
        results = asyncio.run(knowledge.search("巴米扬那次", limit=5))
        assert _titles(results)[0] == "官方", _titles(results)
        assert "面板" in _titles(results)
        assert knowledge.last_operator_hits == 1, "面板块仍参与候选（只是档低）"


def test_panel_chunk_with_only_digits_is_dropped() -> None:
    """面板块只撞上纯数字 → 0 档 → 不占名额（同一把尺子）。"""

    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(tmp)
        knowledge.add_chunk(title="年表", content="她 1983 年第一次出场。")
        assert asyncio.run(knowledge.search("19号", limit=5)) == []
        assert knowledge.last_operator_hits == 1, "词面重合是真的，只是不算相关"


# --- 4. 面板块的档位字段 ----------------------------------------------------


def test_operator_chunk_tier_defaults_to_panel_and_only_accepts_1_or_4() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = OperatorChunksStore(Path(tmp))
        default = store.add(title="默认", content="这是面板档的内容。")
        assert default.authority == OPERATOR_AUTHORITY == 4
        world = store.add(title="世界知识", content="钢板焊接前要先打磨。", authority=1)
        assert world.authority == WORLD_KNOWLEDGE_AUTHORITY == 1
        for bad in (0, 2, 3, 5, "abc"):
            try:
                store.add(title="越界", content="不该写进去。", authority=bad)
            except KnowledgeRejected as exc:
                assert "档位" in str(exc)
            else:
                raise AssertionError(f"{bad!r} 该被拒")
        # 读回来还在（档位随文件走，不是内存里的临时值）
        assert [c.authority for c in OperatorChunksStore(Path(tmp)).all()] == [4, 1]
        assert "authority" in default.as_row(), "面板视图要能看到档位"


def test_old_operator_file_without_tier_reads_as_panel() -> None:
    """**现有那 18 条没有这个字段 → 天然按 4，不许为它重写文件。**"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_chunks.json"
        path.write_text(json.dumps({"version": 1, "chunks": [
            {"id": "a1", "title": "旧块", "content": "她曾在阿拉斯加。", "note": ""},
        ]}, ensure_ascii=False), encoding="utf-8")
        chunks = OperatorChunksStore(Path(tmp)).all()
        assert [chunk.authority for chunk in chunks] == [OPERATOR_AUTHORITY]


def test_update_changes_tier_and_keeps_it_when_omitted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = OperatorChunksStore(Path(tmp))
        chunk = store.add(title="一", content="净水机的滤芯该换了。")
        store.update(chunk.id, content="净水机的滤芯该换了，贵。")
        assert store.get(chunk.id).authority == 4
        updated = store.update(chunk.id, authority=1)
        assert updated.authority == 1


# --- 5. 权威度刻度与读时映射 ------------------------------------------------


def test_authority_scale_is_five_three_two_and_mapped_on_read() -> None:
    """五档刻度：官方 5 / 面板 4 / 官方补充 3 / 同人 2 / 世界知识 1；库里旧值读时映射。"""

    assert AUTHORITY["02-核心设定"] == 5
    assert AUTHORITY["03-官方设定补充"] == 3
    assert AUTHORITY["05-同人系列原文"] == 2
    assert OPERATOR_AUTHORITY == 4
    assert WORLD_KNOWLEDGE_AUTHORITY == 1
    # 旧刻度 3/2/1 → 新刻度 5/3/2（`_scaled_authority` 是读侧唯一入口）
    assert [_scaled_authority(value, 1) for value in (3, 2, 1)] == [5, 3, 2]
    # 新刻度建的库原样返回（不会把"官方补充 3"又抬成 5）
    assert [_scaled_authority(value, 2) for value in (5, 3, 2, 1)] == [5, 3, 2, 1]


def test_a_rebuilt_index_records_the_scale_and_stops_mapping() -> None:
    """重建过的库带 `meta.authority_scale=2`，读侧就不再换算旧值。"""

    with tempfile.TemporaryDirectory() as tmp:
        index = KnowledgeIndex(Path(tmp) / "knowledge.sqlite3")
        index.build(Path(tmp) / "corpus")
        connection = sqlite3.connect(index.path)
        try:
            assert _authority_scale(connection) == 2
        finally:
            connection.close()


# --- 6. 条数可配 ------------------------------------------------------------


def test_knowledge_item_limit_is_configurable(monkeypatch=None) -> None:
    """`QQBOT_KNOWLEDGE_ITEMS` 现读现用，默认 5，夹在 1..10。"""

    import os

    from qq_roleplay_bot.extensions import PromptContext, MAX_KNOWLEDGE_ITEMS
    from qq_roleplay_bot.stage3_runtime import ContextState
    from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

    saved = os.environ.get("QQBOT_KNOWLEDGE_ITEMS")
    message = IncomingMessage(message_id="m1", session_id="group:1", user_id="100",
                              text="百夫长", target=MessageTarget(group_id="1"),
                              sender_name="某人")
    context = PromptContext(session_id="group:1", message=message, recent_messages=(),
                            mode="idle", trigger="threshold", context=ContextState())
    index = _Index([KnowledgeItem(source="02-核心设定/a.md", title="t", content="正文")])
    sources = PromptSources(knowledge_base=index)
    try:
        os.environ.pop("QQBOT_KNOWLEDGE_ITEMS", None)
        asyncio.run(sources.collect(context))
        assert index.limits[-1] == MAX_KNOWLEDGE_ITEMS == 5
        os.environ["QQBOT_KNOWLEDGE_ITEMS"] = "3"
        assert knowledge_item_limit() == 3
        asyncio.run(sources.collect(context))
        assert index.limits[-1] == 3
        os.environ["QQBOT_KNOWLEDGE_ITEMS"] = "0"
        assert knowledge_item_limit() == 1, "下限 1（0 条不是用这个数表达的）"
        os.environ["QQBOT_KNOWLEDGE_ITEMS"] = "99"
        assert knowledge_item_limit() == 10
    finally:
        if saved is None:
            os.environ.pop("QQBOT_KNOWLEDGE_ITEMS", None)
        else:
            os.environ["QQBOT_KNOWLEDGE_ITEMS"] = saved
