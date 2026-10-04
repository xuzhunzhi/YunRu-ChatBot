"""人设 prompt 的**结构与边界**（公开仓库守的是这些，不是具体字句）。

由来（2026-10-01 用户）："prompt屏蔽关键词和信息，只输出格式和字数要求"。
公开仓库里 `base_prompt.py` 是一份模板（真实人设正文留在私有副本），所以这一份测的
是"换成任何人设都必须成立"的东西：

1. **分段齐全**——注意力判定与边界判定要读的那几段不能在替换时丢掉；
2. **长度在可用区间内**——太短管不住说话方式，太长会挤掉记忆与群上下文；
3. **机制词不进正文**——prompt 里的措辞会被角色吸收，她随后就会开始讲自己的机制；
4. **防出戏那一段必须写，而且不许写成反向许可**——"不要假装你是真人"等于直接
   告诉她你不是真人（早期踩过，她随后就开始讲自己的机制）；
5. **正文进的是可信 system 前缀**，不是 DATA 段；
6. **情绪许可与"不怼人"那条边界必须在**（2026-10-04），而且**风格审核不许被授权
   去磨她的语气**——详见下面 `EMOTION_PERMISSION_ANCHORS` 那段注释。

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

#: **情绪许可**的两句锚点（2026-10-04 用户原话："你先看看 prompt 里有没有约束，
#: 有的话改掉，**只要不是怼人就行，可以表达情绪**"）。
#:
#: 为什么要在这里钉住：这一轮的审计发现，她之所以"三无"，一部分是 prompt 在压
#: （"这些不是要压住的缺陷"只说了情绪可以存在，**没给她表达它的许可**），
#: 而更大一口在风格审核那边（它每条回复都跑一次，专删"冲"）。约束删掉之后必须留下守卫，
#: 否则下一轮"她太吵了"一句反馈，就会有人把同一条约束悄悄加回来——**没有任何测试会红**。
#:
#: 锚点取的是"许可"与"边界"两句里最不可能被顺手改写的说法；改这两句时断言会红，
#: 那是**故意的**：先回来读这段注释，再决定是改断言还是改回 prompt。
EMOTION_PERMISSION_ANCHORS = (
    "情绪出来的时候不要收着",
    "情绪该出来就出来，不必收着",
)
NO_ATTACK_ANCHORS = (
    "别拿它去砸人",
    "不是砸向面前这个人",
)
#: `base_prompt.py:68` 那条（"不当裁判、不纠正别人"）压的是**纠正别人**，不是表达情绪。
#: 用户点名要求保留它，只补一句限定说明它管不到情绪。
NO_CORRECTING_ANCHOR = "不当裁判、不纠正别人"
NO_CORRECTING_CLARIFIER = "你自己有什么反应、什么情绪，不在这一条里"


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


# --- 情绪许可（2026-10-04）--------------------------------------------------


def test_persona_lets_her_show_emotion() -> None:
    """她**可以表达情绪**——这句许可必须在，而且分两处（说话方式 + 群聊里的分寸）。

    缺了它，前面那句"这些不是要压住的缺陷"只是一句关于人设写法的说明，
    并没有允许她真的把情绪放出来。
    """

    missing = [anchor for anchor in EMOTION_PERMISSION_ANCHORS if anchor not in BASE_PROMPT]
    assert not missing, f"情绪许可被拿掉了（或改写了）：{missing}"


def test_persona_keeps_the_only_boundary_about_people() -> None:
    """许可的边界只有一条：**不对人发难**（用户："只要不是怼人就行"）。

    这是"允许表达情绪"与"允许怼人"之间唯一的区别，所以两边都要钉住：
    边界句子在，而且它说的是"攻击/羞辱/教训/阴阳怪气"，不是"不许有反应"。
    """

    missing = [anchor for anchor in NO_ATTACK_ANCHORS if anchor not in BASE_PROMPT]
    assert not missing, f"不怼人那条边界不见了：{missing}"
    assert "烦了、嫌弃、来劲、懒得理、被戳到了、突然没兴趣" in BASE_PROMPT, (
        "情绪许可没有具体说清哪些情绪是被允许的"
    )


def test_persona_does_not_silence_emotion_while_forbidding_correction() -> None:
    """`不当裁判、不纠正别人` 要留着（它压的是纠正别人，不是情绪），并带上那句限定。"""

    assert NO_CORRECTING_ANCHOR in BASE_PROMPT, "这条是用户点名保留的，不许删"
    assert NO_CORRECTING_CLARIFIER in BASE_PROMPT, (
        "它读起来像'不许有反应'——要补一句限定，说明它管不到她自己的情绪"
    )


def test_review_prompt_keeps_emotion_and_only_blocks_four_kinds() -> None:
    """风格审核的**授权范围**：只挡事实错误 / 越权 / 出戏 / 伤人。

    它是每条回复发送前的最后一道，写得保守就会把情绪磨平（2026-10-04 审计：
    上一版判据 1 删反问、判据 3 删吐槽、判据 5 按字数压）。这里钉三件事：
    四类在、**"情绪与语气不归它管"**在、**上一版那套"去冲"的说法不许回来**。
    """

    from qq_roleplay_bot.prompt_guard import scan_agent_text
    from qq_roleplay_bot.style_reviewer import REVIEW_SYSTEM_PROMPT as review

    for scope in ("事实错了", "越权", "出戏", "伤人"):
        assert scope in review, scope
    assert "只改这四类" in review
    assert "不负责把话改短、改客气、改平稳" in review, "审核又被授权去磨她的语气了"
    assert "写得冲也不改" in review
    assert "一字不差" in review
    assert "超过 40 字" not in review, "按字数压话的那条判据回来了"
    assert "改完不许比原稿更冷、更淡、更客气" in review
    assert scan_agent_text(review) == [], "审核 prompt 里混进了机制词"
