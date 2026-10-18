"""知识块的**模型判档**那一层（2026-10-18 用户："这块不行也加个agent，用dsflash，effort调none"）。

它接在本地三档之后：本地先算 0/1/2（便宜、能兜底），留下来的候选**一起**塞进**一次**调用，
按"能不能用上"重判；模型判出的档就是最终档，最终分仍然 = **档 × 权威度**。

这个文件钉住：
1. 输出只认 `编号:档位`（容错），读不出来 → fail-open 用本地档；
2. **一轮一次**调用（不是每块一次），本地判 0 的不进模型；
3. 模型判 0 的条目**一条都不取**；
4. `QQBOT_KNOWLEDGE_TIER_LLM=0` 关掉时与"只有本地那套"完全一致；
5. 判据 prompt 里没有机制词（它会被扫）。
"""
import asyncio

from qq_roleplay_bot.extensions import KnowledgeItem
from qq_roleplay_bot.knowledge_tier import (
    TIER_SYSTEM_PROMPT,
    apply_tiers,
    build_tier_messages,
    parse_tiers,
    tier_items,
)


class _Client:
    """假模型：记下调用次数与请求，按给定文本作答。"""

    def __init__(self, answer: str = "", *, boom: bool = False) -> None:
        self.answer = answer
        self.boom = boom
        self.calls = 0
        self.requests: list[list[dict[str, str]]] = []

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        if self.boom:
            raise RuntimeError("模型炸了")
        return self.answer


def _items(*specs):
    return [KnowledgeItem(source=source, title=title, content=content, authority=authority,
                          tier=tier)
            for source, title, content, authority, tier in specs]


OFFICIAL = ("02-核心设定/a.md", "官方", "巴米扬那次她被关在地下设施里。", 5, 2)
PANEL = ("operator/x", "面板", "她不喜欢别人提巴米扬那几年。", 4, 1)
WEAK = ("05-同人系列原文/b.md", "同人", "那年之后她就没再提。", 2, 1)


def test_parse_tiers_is_lenient() -> None:
    """只认行首的 `编号:档位`；夹了别的话也不崩（这套格式在写信那条验过）。"""

    raw = "1:2\n2: 0\n3：1\n4:0 这一条看不出来\n\n说明：第 4 条看起来无关\n5:x\n"
    assert parse_tiers(raw) == {1: 2, 2: 0, 3: 1, 4: 0}
    assert parse_tiers("完全不按格式说了一堆") == {}


def test_one_call_for_all_candidates() -> None:
    """**一轮一次**：三条候选一起进同一次请求；本地判 0 的不会进（这一层只收留下来的）。"""

    items = _items(OFFICIAL, PANEL, WEAK)
    client = _Client("1:2\n2:1\n3:0")
    tiers = asyncio.run(tier_items(client, "巴米扬那次她被关在哪", items,
                                   local_tiers=[2, 1, 1]))
    assert client.calls == 1
    prompt = client.requests[0][0]["content"]
    assert prompt.count("）") >= 0  # 只是读一下，别崩
    for index in (1, 2, 3):
        assert f"\n{index}) " in prompt, prompt[:200]
    assert tiers == [2, 1, 0]


def test_unparseable_output_falls_back_to_local_tiers() -> None:
    """读不出来 → `None`（调用方原样用本地档，这一轮不能因此没料）。"""

    items = _items(OFFICIAL)
    client = _Client("我觉得第一条还行吧")
    assert asyncio.run(tier_items(client, "问一句", items, local_tiers=[2])) is None
    assert client.calls == 1


def test_a_failed_call_falls_back_to_local_tiers() -> None:
    client = _Client(boom=True)
    assert asyncio.run(tier_items(client, "问一句", _items(OFFICIAL),
                                  local_tiers=[2])) is None


def test_model_zero_drops_the_item_and_keeps_the_rest() -> None:
    """模型判 0 → 一条都不取；最终分 = 档 × 权威度。"""

    items = _items(PANEL, OFFICIAL)          # 面板强（1 档本地）、官方强
    kept = apply_tiers(items, [2, 0], limit=5)
    assert [item.title for item in kept] == ["面板"]
    # 模型把面板判成 2、官方判成 1：2×4=8 > 1×5=5，面板上位（"真对题的补充能上位"）
    kept = apply_tiers(items, [2, 1], limit=5)
    assert [item.title for item in kept] == ["面板", "官方"]


def test_ties_keep_the_incoming_order() -> None:
    """并列（同档不同权威度仍可能同分）时保持进来时的次序——那一份已经是本地排好的。"""

    items = _items(WEAK, PANEL)              # 2×2=4 vs 1×4=4
    assert [item.title for item in apply_tiers(items, [2, 1], limit=5)] == ["同人", "面板"]


def test_prompt_has_no_persona_mechanism_words() -> None:
    """判据 prompt 里不许有机制词（AGENTS §2.2：措辞会被角色吸收；禁词测试会扫）。"""

    from qq_roleplay_bot.prompt_guard import scan_persona_text

    rendered = TIER_SYSTEM_PROMPT.format(query="你在哪呢", items="1) 标题：正文")
    assert scan_persona_text(rendered) == [], scan_persona_text(rendered)
    messages = build_tier_messages("你在哪呢", _items(OFFICIAL))
    assert messages and messages[0]["role"] == "system"


# --- 引擎那一层（`DialogueEngine._apply_knowledge_tiers`） -------------------


class _FakeEngine:
    """只借 `_apply_knowledge_tiers` 需要的那两样：client 与时钟。"""

    def __init__(self, client) -> None:
        self.client = client

    def _relevance_client(self):
        return self.client

    def clock(self) -> float:
        return 0.0


def _apply(engine, items, query: str = "巴米扬那次她被关在哪"):
    from qq_roleplay_bot.extensions import PromptMaterial
    from qq_roleplay_bot.stage3_main import DialogueEngine

    material = PromptMaterial(knowledge_items=tuple(items))
    return asyncio.run(DialogueEngine._apply_knowledge_tiers(engine, material, query))


class _Env:
    """临时改 `QQBOT_KNOWLEDGE_TIER_LLM`（`None` = 不设，走默认开）。"""

    def __init__(self, value: str | None) -> None:
        self.value = value

    def __enter__(self):
        import os

        self.saved = os.environ.get("QQBOT_KNOWLEDGE_TIER_LLM")
        if self.value is None:
            os.environ.pop("QQBOT_KNOWLEDGE_TIER_LLM", None)
        else:
            os.environ["QQBOT_KNOWLEDGE_TIER_LLM"] = self.value

    def __exit__(self, *exc):
        import os

        if self.saved is None:
            os.environ.pop("QQBOT_KNOWLEDGE_TIER_LLM", None)
        else:
            os.environ["QQBOT_KNOWLEDGE_TIER_LLM"] = self.saved


def test_switched_off_equals_local_only() -> None:
    """`QQBOT_KNOWLEDGE_TIER_LLM=0` → **一次调用都不发**，材料逐字不变（与只有本地那套等价）。"""

    items = _items(OFFICIAL, PANEL)
    client = _Client("1:0\n2:0")  # 如果真调了，会把两条都判掉
    engine = _FakeEngine(client)
    with _Env("0"):
        result = _apply(engine, items)
    assert client.calls == 0, "关掉开关就不许再调模型"
    assert result.knowledge_items == tuple(items), "关掉时材料必须逐字不变"


def test_model_zero_removes_the_item() -> None:
    items = _items(OFFICIAL, PANEL)
    engine = _FakeEngine(_Client("1:2\n2:0"))
    with _Env(None):
        result = _apply(engine, items)
    assert [item.title for item in result.knowledge_items] == ["官方"]


def test_model_all_zero_falls_back_to_the_profile() -> None:
    """模型把全部判 0 → 与"本地全 0 档"同一个出口：追一条 ≤200 字的档案兜底。"""

    from qq_roleplay_bot.knowledge_base import PROFILE_FALLBACK_TITLE

    engine = _FakeEngine(_Client("1:0\n2:0"))
    with _Env(None):
        result = _apply(engine, _items(OFFICIAL, PANEL))
    assert [item.title for item in result.knowledge_items] == [PROFILE_FALLBACK_TITLE]
    assert len(result.knowledge_items[0].content) <= 200


def test_model_output_garbage_falls_back_to_local_order() -> None:
    """读不出来 → 材料**原样**返回（fail-open），不许因为格式抖动少料。"""

    items = _items(OFFICIAL, PANEL)
    engine = _FakeEngine(_Client("这两条我都说不准"))
    with _Env(None):
        result = _apply(engine, items)
    assert result.knowledge_items == tuple(items)
