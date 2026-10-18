"""记忆的**出处**（读侧）：每条 `<memory>` 带 `source="quote|inferred"` 与 `age="N天前"`。

由来（用户 2026-10-18 交底）：之前她手里的记忆长得一模一样——"别人当时说的原话"和
"她自己攒下的印象"没有分别，于是印象被说成"你上次说过"。实测最近 2 天"断言自身过往"
的 23 条里 **16 条不可核实（70%）**。

这一份钉住四件事：

1. `source="quote"` 只给**有原话顶着**的（`source_event_ids` 非空）；
2. `source="inferred"` 给其余全部（**默认就是它**：拿不到出处不许冒充原话）；
3. `age="N天前"` 从 `created_at` 算，**不是** `updated_at`（后者每次合并都会被推到当下）；
4. 这一节开头那句规矩在，而且**没有机制词**、两个取值 `quote` / `inferred` 原样出现。

渲染样例（脱敏，见 `test_a_quote_and_an_impression_are_rendered_differently`）：

    每条开头都标着出处：source="quote" 的是当时那句原话，可以照"我记得""你说过"来讲；
    source="inferred" 的只是你自己攒下的印象，只能当印象，别说成谁说过。
    <memory scope="user_group" subject="100" about="100" source="quote" age="3天前">他提过这件事。</memory>
    <memory scope="user_group" subject="100" about="100" source="inferred" age="12天前">不太爱说话。</memory>
"""
import time

from qq_roleplay_bot.memory_model import (
    MEMORY_SOURCE_RULE,
    MemoryMaterial,
    MemoryRecord,
)
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.prompt_guard import scan_persona_text

GROUP = "717151356"


def _record(**overrides) -> MemoryRecord:
    data = dict(
        id="r1", scope_type="user_group", scope_key=f"{GROUP}:100", subject_user_id="100",
        kind="status", normalized_key="处境", content="他提过这件事。", confidence=0.9,
        created_at=time.time() - 3 * 86400 - 3600, updated_at=time.time(),
    )
    data.update(overrides)
    return MemoryRecord(**data)


def test_a_quote_and_an_impression_are_rendered_differently() -> None:
    """`quote` / `inferred` 两种渲染各一条（这就是脱敏后的样例）。"""

    quoted = _record(id="q1", quoted=True)
    impression = _record(
        id="i1", content="不太爱说话。",
        created_at=time.time() - 12 * 86400 - 3600, quoted=False,
    )
    rendered = MemoryMaterial((quoted, impression)).as_data(GROUP, "100")
    lines = rendered.split("\n")
    assert lines[0] == MEMORY_SOURCE_RULE, lines[0]
    assert lines[1] == (
        '<memory scope="user_group" subject="100" about="100" '
        'source="quote" age="3天前">他提过这件事。</memory>'
    ), lines[1]
    assert lines[2] == (
        '<memory scope="user_group" subject="100" about="100" '
        'source="inferred" age="12天前">不太爱说话。</memory>'
    ), lines[2]


def test_the_rule_sentence_is_in_the_material_and_has_no_mechanism_words() -> None:
    """规矩那句话在材料里；机制词 0 命中；两个取值原样出现（她要能对上属性）。"""

    assert scan_persona_text(MEMORY_SOURCE_RULE) == [], scan_persona_text(MEMORY_SOURCE_RULE)
    for value in ('source="quote"', 'source="inferred"'):
        assert value in MEMORY_SOURCE_RULE, value
    # 一句就够：这一节每轮都跟着回复走（不是靠行数，是别把规矩展开成一段）。
    assert MEMORY_SOURCE_RULE.count("。") <= 2
    rendered = MemoryMaterial((_record(quoted=True),)).as_data(GROUP, "100")
    assert rendered.startswith(MEMORY_SOURCE_RULE + "\n")


def test_no_memory_keeps_the_old_sentence_verbatim() -> None:
    """没有可用记忆时**与改动前逐字相同**：那句规矩不单独出现（没有指称对象）。"""

    assert MemoryMaterial().as_data(GROUP, "100") == "（暂无可用长期记忆）"
    assert MemoryMaterial().as_data(None, "100") == "（暂无可用长期记忆）"


def test_age_comes_from_created_at_not_updated_at() -> None:
    """`age` 用 `created_at`：三年前学到的、昨天刚被合并过的一条，仍然是三年前那条。"""

    old = _record(
        created_at=time.time() - 3 * 365 * 86400,
        updated_at=time.time() - 3600,
    )
    rendered = MemoryMaterial((old,)).as_data(GROUP, "100")
    assert 'age="1095天前"' in rendered, rendered


def test_age_never_renders_a_negative_day_count() -> None:
    """时钟跳变 / 未来时间戳按"今天"算，不许出现 `-1天前` 那种话。"""

    future = _record(created_at=time.time() + 86400, updated_at=time.time())
    rendered = MemoryMaterial((future,)).as_data(GROUP, "100")
    assert 'age="0天前"' in rendered, rendered
    assert "-" not in rendered.split("<memory ")[1].split(">")[0]


def _row(**overrides) -> dict:
    """一行库记录（`MemoryStore._record` 要的列都齐）。"""

    now = time.time()
    row = {
        "id": "r1", "scope_type": "user_group", "scope_key": f"{GROUP}:100",
        "subject_user_id": "100", "kind": "status", "normalized_key": "处境",
        "content": "他提过这件事。", "confidence": 0.9,
        "created_at": now, "updated_at": now, "expires_at": None, "revision": 1,
        "visibility": "group_safe", "status": "active", "source_event_ids": '["e1"]',
    }
    row.update(overrides)
    return row


def test_the_store_folds_source_event_ids_into_the_flag() -> None:
    """`source_event_ids` 是 JSON 文本列，由 `MemoryStore._record` 折算成 `quoted`。"""

    assert MemoryStore._record(_row()).quoted is True
    assert MemoryStore._record(_row(source_event_ids="[]")).quoted is False
    assert MemoryStore._record(_row(source_event_ids="")).quoted is False
    # 坏行不许让整次检索炸：按"没有出处"处理（宁可让她说得谨慎）
    assert MemoryStore._record(_row(source_event_ids="{不是 JSON")).quoted is False
    # 有些查询只挑几列、根本没有这一列 → 默认 False
    row = _row()
    row.pop("source_event_ids")
    assert MemoryStore._record(row).quoted is False


def test_an_impression_is_not_allowed_to_look_like_a_quote() -> None:
    """**默认那一侧是安全的**：只给出正文的构造方式（测试/别的路径）一律算印象。"""

    bare = MemoryRecord(
        id="r2", scope_type="user_group", scope_key=f"{GROUP}:100", subject_user_id="100",
        kind="preference", normalized_key="plans", content="和乐乐约好一起买耳机",
        confidence=0.9, created_at=time.time(), updated_at=time.time(),
    )
    rendered = MemoryMaterial((bare,)).as_data(GROUP, "100")
    # 只看 `<memory>` 那一行：规矩那句话本身就带着两种取值（那是给它讲道理用的）。
    tagged = [line for line in rendered.split("\n") if line.startswith("<memory ")]
    assert len(tagged) == 1, tagged
    assert 'source="inferred"' in tagged[0], tagged[0]
    assert 'source="quote"' not in tagged[0], tagged[0]
