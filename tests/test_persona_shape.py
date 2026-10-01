"""人设 prompt 的**结构与边界**（公开仓库守的是这些，不是具体字句）。

由来（2026-10-01 用户）："prompt屏蔽关键词和信息，只输出格式和字数要求"。
公开仓库里 `base_prompt.py` 是一份模板（真实人设正文留在私有副本），所以这一份测的
是"换成任何人设都必须成立"的东西：

1. **分段齐全**——注意力判定与边界判定要读的那几段不能在替换时丢掉；
2. **长度在可用区间内**——太短管不住说话方式，太长会挤掉记忆与群上下文；
3. **机制词不进正文**——prompt 里的措辞会被角色吸收，她随后就会开始讲自己的机制；
4. **防出戏那一段必须写，而且不许写成反向许可**——"不要假装你是真人"等于直接
   告诉她你不是真人（早期踩过，她随后就开始讲自己的机制）；
5. **正文进的是可信 system 前缀**，不是 DATA 段。

**内容级断言**（情绪触发点是哪些、某个专有名词在不在）留在私有副本里——
换人设就该一起换，放在公开仓库会让模板版必然失败。
"""
from pathlib import Path

from qq_roleplay_bot.base_prompt import BASE_PROMPT
from qq_roleplay_bot.prompt_guard import PERSONA_MECHANISM_WORDS, scan_persona_text

#: 必须有的分段。少了不会报错，但判定链路会失去落脚点，所以在这里挡住。
REQUIRED_SECTIONS = (
    "## 你是谁",
    "## 说话方式",
    "## 面对这些话题时",
    "## 关于你自己的两个边界",
    "## 有一条线你不能越过",
    "## 群聊里的分寸",
)

#: 正文长度的可用区间（实测）。上限的理由是"每轮都要带上这一段"——
#: 太长会把记忆与群上下文挤出位置。
MIN_CHARS = 3000
MAX_CHARS = 6000


def test_persona_keeps_its_sections() -> None:
    missing = [name for name in REQUIRED_SECTIONS if name not in BASE_PROMPT]
    assert not missing, f"人设缺了这些分段：{missing}"


def test_persona_length_is_in_the_usable_range() -> None:
    size = len(BASE_PROMPT)
    assert MIN_CHARS <= size <= MAX_CHARS, (
        f"人设正文 {size} 字，应在 {MIN_CHARS}~{MAX_CHARS} 之间"
    )


def test_persona_text_has_no_mechanism_words() -> None:
    """正文里不许出现机制词——**除了"有一条线你不能越过"那一段**。

    那一段必须把"不要提提示词/协议/上下文/记忆库"讲出来，否则角色不知道要躲。
    这条规矩在真实人设里也是这么写的（`tests/test_daily_report.py` 的注释里记着
    这件事：base_prompt 本来就带一句"不要提'提示词''协议'…"）。所以扫描时把
    那一段摘出去，只扫其余部分。
    """

    body = BASE_PROMPT
    guard = body.find("## 有一条线你不能越过")
    if guard >= 0:
        rest = body.find("\n## ", guard + 1)
        body = body[:guard] + (body[rest:] if rest >= 0 else "")
    assert scan_persona_text(body) == [], (
        f"正文里混进了机制词：{scan_persona_text(body)}／词表 {PERSONA_MECHANISM_WORDS}"
    )


def test_persona_has_the_out_of_character_guard() -> None:
    """防出戏那一段必须在，而且**不能**写成反向许可。"""

    assert "## 有一条线你不能越过" in BASE_PROMPT
    guard = BASE_PROMPT.split("## 有一条线你不能越过", 1)[1]
    # 要她"不认领"身份
    assert "不" in guard and ("程序" in guard or "机器" in guard or "AI" in guard)
    # 反向许可不许回来（这是在告诉她"你不是真人"）
    for banned in ("不要假装自己是现实中的真人", "假装你是真人", "你不是真人"):
        assert banned not in BASE_PROMPT, f"出戏许可回来了：{banned}"


def test_persona_is_actually_shipped_with_the_package() -> None:
    """模板得真的跟着包走：clone 下来不装任何人设也能 import。"""

    path = Path(__file__).resolve().parents[1] / "src" / "qq_roleplay_bot" / "base_prompt.py"
    assert path.exists(), "base_prompt.py 不在了——公开版靠它才能跑起来"
    assert "BASE_PROMPT" in path.read_text(encoding="utf-8")
