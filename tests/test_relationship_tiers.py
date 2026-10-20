"""档位表（22 + 7）与三处消费的口径。

用户 2026-10-20 逐行看过 `data/relationship-tiers.md`，并要求：

1. `relationship_tiers.py` 里的档名与那句话**逐字**照表，一个字都不许改；
2. 判定 agent 拿**数值 + 该档文字**，**不给任何接话阈值**；
3. 回复 agent **只看档位文字、看不到数字**；
4. 人物画像维护拿**文字档**。

这份测试的四件事正好对着这四条。逐字对照在 `data/relationship-tiers.md`
不存在时会 Skip（干净 clone 没有 `data/`）；其余结构断言不依赖那个文件。
"""
import re
import unittest
from pathlib import Path

from qq_roleplay_bot.dialogue_judge import JUDGE_SYSTEM_PROMPT
from qq_roleplay_bot.memory_maintenance_agent import MEMORY_SYSTEM_PROMPT
from qq_roleplay_bot.relationship_tiers import (
    GUARD_DEFAULT,
    GUARD_MAX,
    GUARD_MIN,
    GUARD_TIERS,
    LIKES_DEFAULT,
    LIKES_MAX,
    LIKES_MIN,
    LIKES_TIERS,
    Tier,
    clamp_guard,
    clamp_likes,
    guard_index,
    guard_text,
    guard_tier,
    likes_index,
    likes_text,
    likes_tier,
)
from qq_roleplay_bot.stage3_runtime import Stance

ROOT = Path(__file__).resolve().parents[1]
TIERS_MD = ROOT / "data" / "relationship-tiers.md"

#: 结构（档名 + 区间）的独立转写：不依赖 `data/`，也不依赖实现里的顺序。
EXPECTED_LIKES = (
    ("爱慕", (100, 100)), ("喜欢", (95, 99)), ("密友", (90, 94)),
    ("亲近", (80, 89)), ("熟络", (70, 79)), ("聊得来", (60, 69)),
    ("还算投缘", (50, 59)), ("有点熟", (40, 49)), ("客气但生分", (30, 39)),
    ("淡淡的", (20, 29)), ("陌生人", (10, 19)), ("刚打过照面", (0, 9)),
    ("不太想搭理", (-10, -1)), ("明显躲着", (-20, -11)), ("有点厌烦", (-30, -21)),
    ("不耐烦", (-40, -31)), ("反感", (-50, -41)), ("很反感", (-60, -51)),
    ("厌恶", (-70, -61)), ("很厌恶", (-80, -71)), ("敌意", (-90, -81)),
    ("仇视", (-100, -91)),
)
EXPECTED_GUARD = (
    ("绷得最紧", (30, 30)), ("很克制", (20, 29)), ("有点端着", (10, 19)),
    ("正常分寸", (0, 9)), ("松下来了", (-10, -1)), ("很放得开", (-20, -11)),
    ("完全不设防", (-30, -21)),
)
MECHANISM_WORDS = ("检查", "触发", "调用", "协议", "提示词", "上下文", "记忆库", "Stage")


def _parse_markdown_table(start: str, end: str) -> list[tuple[str, str, str]]:
    body = TIERS_MD.read_text(encoding="utf-8").split(start, 1)[1].split(end, 1)[0]
    cells_rows = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3 or cells[0] == "区间" or set(cells[0]) <= set("-: "):
            continue
        cells_rows.append((cells[0], cells[1], cells[2]))
    return cells_rows


def _parse_span(raw: str) -> tuple[int, int]:
    normalized = raw.replace("−", "-").replace("–", "…")
    numbers = [int(part.strip()) for part in normalized.split("…") if part.strip()]
    return (numbers[0], numbers[-1])


def _clean_name(raw: str) -> str:
    return re.sub(r"（初始值在这里）", "", raw.replace("**", ""))


# --- 1. 22 + 7 条，一条不少、区间盖满 --------------------------------------


def test_there_are_exactly_twenty_two_and_seven_tiers() -> None:
    assert len(LIKES_TIERS) == 22
    assert len(GUARD_TIERS) == 7
    assert isinstance(LIKES_TIERS[0], Tier)


def test_the_tier_structure_is_pinned() -> None:
    """档名与区间是结构，不是文案；独立转写一遍，防止"少一条也测不出来"。"""

    assert tuple((tier.name, tier.span) for tier in LIKES_TIERS) == EXPECTED_LIKES
    assert tuple((tier.name, tier.span) for tier in GUARD_TIERS) == EXPECTED_GUARD


def test_every_value_in_the_scale_lands_in_exactly_one_tier() -> None:
    for value in range(LIKES_MIN, LIKES_MAX + 1):
        tier = likes_tier(value)
        assert tier.span[0] <= value <= tier.span[1], (value, tier)
    for value in range(GUARD_MIN, GUARD_MAX + 1):
        tier = guard_tier(value)
        assert tier.span[0] <= value <= tier.span[1], (value, tier)


def test_out_of_range_values_are_clamped_not_crashing() -> None:
    assert likes_tier(999).name == "爱慕"
    assert likes_tier(-999).name == "仇视"
    assert guard_tier(999).name == "绷得最紧"
    assert guard_tier(-999).name == "完全不设防"
    assert clamp_likes(999) == LIKES_MAX == 100
    assert clamp_guard(-999) == GUARD_MIN == -30
    assert (LIKES_DEFAULT, GUARD_DEFAULT) == (10, 0), "用户 2026-10-20 定的初始值"


def test_index_and_text_helpers_agree_with_the_table() -> None:
    assert likes_index(100) == 0 and likes_index(-100) == 21
    assert guard_index(30) == 0 and guard_index(-30) == 6
    assert likes_text(10) == "陌生人——按规矩说，不带温度。"
    assert guard_text(0) == "正常分寸——该说说，该停停。"


# --- 2. 逐字照表 ------------------------------------------------------------


def test_tiers_match_the_approved_markdown_verbatim() -> None:
    """用户逐行看过的表就是口径；差一个字都算改口径。

    表在 `data/`（不进仓库），干净 clone 上没有它 —— 那时只跑结构断言。
    """

    if not TIERS_MD.exists():
        raise unittest.SkipTest("没有 data/relationship-tiers.md（干净 clone）")
    for start, end, tiers in (
        ("## 一、好感 22 档", "## 二、", LIKES_TIERS),
        ("## 二、防备 7 档（理性程度）", "## 三、", GUARD_TIERS),
    ):
        parsed = _parse_markdown_table(start, end)
        assert len(parsed) == len(tiers), (len(parsed), len(tiers))
        for (span, name, text), tier in zip(parsed, tiers):
            assert _parse_span(span) == tier.span, (span, tier)
            assert _clean_name(name) == tier.name, (name, tier)
            assert text == tier.text, (text, tier)


def test_tier_text_has_no_digits_or_mechanism_words() -> None:
    for tier in (*LIKES_TIERS, *GUARD_TIERS):
        assert not any(ch.isdigit() for ch in tier.name + tier.text), tier
        for word in MECHANISM_WORDS:
            assert word not in tier.name + tier.text, (word, tier)


# --- 3. 回复侧：只有文字，没有数字 ------------------------------------------


def test_reply_material_never_shows_a_number_at_any_tier() -> None:
    """遍历 29 档各取一个代表值：回复材料里**一个数字都没有**。

    包含区间边界值（-100/95/…）：`as_data()` 里连三位数都不许出现。
    """

    representatives = [tier.span[1] for tier in LIKES_TIERS]
    representatives += [tier.span[1] for tier in GUARD_TIERS]
    for value in representatives:
        for stance in (Stance(value, 0), Stance(0, value),
                       Stance(LIKES_DEFAULT, GUARD_DEFAULT)):
            rendered = stance.as_data()
            assert not any(ch.isdigit() for ch in rendered), rendered
            assert "-100" not in rendered and "100" not in rendered


def test_reply_material_uses_the_tier_name_and_text() -> None:
    rendered = Stance(90, 30).as_data()
    assert "密友——什么都能说。" in rendered
    assert "绷得最紧——一句多余的话没有。" in rendered


# --- 4. 判定侧：数值 + 文字，不给阈值 ----------------------------------------


def test_judge_material_has_the_value_and_the_tier_text() -> None:
    hint = Stance(90, 20).as_judge_hint()
    assert "好感 90（" in hint and "密友——" in hint
    assert "防备 20（" in hint and "很克制——" in hint


def test_judge_material_has_no_answering_threshold() -> None:
    """用户 2026-10-20：**不给任何接话阈值**，由判定自己看这一轮。"""

    for value in (-100, -30, 0, 10, 50, 95, 100):
        hint = Stance(value, 0).as_judge_hint()
        for banned in ("以上才", "以上就", "低于", "高于", "阈值", ">=", "≤", "≥", "才接"):
            assert banned not in hint, (banned, hint)
    for banned in ("以上才", "阈值", ">= 50", "≥50"):
        assert banned not in JUDGE_SYSTEM_PROMPT, banned
    # 那条口径必须真的写进判定 prompt，而不是只写在注释里。
    assert "防备只作语气参考，不参与接不接" in JUDGE_SYSTEM_PROMPT


# --- 5. 人物画像维护：给它文字档 --------------------------------------------


def test_maintenance_prompt_gets_the_tier_text_and_demands_original_words() -> None:
    assert "`relationship`" in MEMORY_SYSTEM_PROMPT
    assert "好感" in MEMORY_SYSTEM_PROMPT and "防备" in MEMORY_SYSTEM_PROMPT
    assert "各四档" not in MEMORY_SYSTEM_PROMPT, "旧的四档口径不许留"
    assert "0..3" not in MEMORY_SYSTEM_PROMPT
    assert "原话" in MEMORY_SYSTEM_PROMPT
    assert "severe" in MEMORY_SYSTEM_PROMPT


def test_maintenance_request_carries_tier_text_without_numbers() -> None:
    """第三处消费：给它的是**档位文字**，不是数值。"""

    import json

    from qq_roleplay_bot.memory_maintenance_agent import build_maintenance_messages
    from qq_roleplay_bot.memory_model import MemoryBatch

    batch = MemoryBatch(id="b1", group_id="717151356", user_id="900000001",
                        events=(), records=(), lease_until=0.0)
    request = build_maintenance_messages(
        batch, 1.0, (), relationship={"好感": likes_text(90), "防备": guard_text(20)})
    user_content = request[1]["content"]
    payload = json.loads(user_content.split("\n", 1)[1])
    assert payload["relationship"] == {"好感": likes_text(90), "防备": guard_text(20)}
    for value in payload["relationship"].values():
        assert "密友——" in value or "很克制——" in value
        assert not any(ch.isdigit() for ch in value), value
