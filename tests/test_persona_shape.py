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
6. **风格审核不许被授权去磨她的语气**（2026-10-04）——它是每条回复发送前的最后一道，
   详见下面 `test_review_prompt_keeps_emotion_and_only_blocks_four_kinds`。

2026-10-04 第二次改动（**撤掉的一半**）：`45f3535` 那条人格审计往模板里加的"情绪许可 /
不怼人边界 / `:68` 那条的限定"三处**已经撤回**（`git checkout 45f3535^ --
src/qq_roleplay_bot/base_prompt.py`），所以守它们的三条断言也一起删了。理由不是"让它变绿"，
而是**它们守的前提已经不存在**：生产上 `BASE_PROMPT` 整段来自
`data/private_docs/base_prompt.REAL.py`（`base_prompt.py:249-251` 找到就整段返回），
仓库模板里那两句在生产上**一个字都不生效**；留着断言只会让 `run/` 的守卫对着一份
没人读的模板红。人格那两句等真人格的事定了、连同真人格一起做。
**风格审核那一半保留**（它是每轮都真的跑的那一道），守卫也保留。

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


# --- 风格审核的授权范围（2026-10-04）----------------------------------------


def test_review_prompt_keeps_emotion_and_only_blocks_four_kinds() -> None:
    """风格审核的**授权范围**：只挡事实错误 / 越权 / 出戏 / 伤人。

    它是每条回复发送前的最后一道，写得保守就会把情绪磨平（2026-10-04 审计：
    上一版判据 1 删反问、判据 3 删吐槽、判据 5 按字数压）。

    2026-10-05 它从"只改这四类"改成"**只判这四类**"（用户："审核只负责打回，
    不负责修改"），所以这里的锚点跟着改口径，但守的东西一个字没变：
    四类在、**"情绪与语气不归它管"**在、**上一版那套"去冲"的说法不许回来**。
    "只改"那几句现在都换成"只看/不算越界/判过"——它已经没有任何改写权限了。
    """

    from qq_roleplay_bot.prompt_guard import scan_agent_text
    from qq_roleplay_bot.style_reviewer import REVIEW_SYSTEM_PROMPT as review

    for scope in ("事实错了", "越权", "出戏", "伤人"):
        assert scope in review, scope
    assert "只看这四类" in review
    assert "不负责把话改短、改客气、改平稳" in review, "审核又被授权去磨她的语气了"
    assert "写得冲也不算越界" in review
    assert "只判断，不改写" in review, "审核又开始自己动手改台词了"
    assert "超过 40 字" not in review, "按字数压话的那条判据回来了"
    assert "一律判过" in review
    assert scan_agent_text(review) == [], "审核 prompt 里混进了机制词"
