"""有人当面谈"这东西怎么做出来的"时，她的禁口令（2026-10-05 故障 A）。

生产实测（群 `908696203`）：有人聊"给 bot 加社交欲望、改架构""bot 回复 agent 改造"，
她的原话是

    分得清是今天的事。**再加一个插件，那条线还得挪。**

她自己的 `intent` 也被记成了"给 bot 加社交欲望、改架构"——也就是说她**参与了
一场关于她自己怎么被做出来的讨论**。这条规矩就是拦这个。

要守住的东西：

1. **是禁令，不是说话风格指导**：只写"不许做什么"（不接这个话、不评论它、
   不说自己是什么）。上一版往常驻 system 里放的是"听不懂就先问一句"，那是**行为指导**，
   实测把她的语域带成了技术顾问（账在 `docs/STAGE3_PENDING_DESIGNS.md` §① 的更正里）。
2. **机制词零命中**：`prompt_guard.PERSONA_MECHANISM_WORDS`（检查/触发/调用/协议/
   提示词/上下文/记忆库/Stage）一个都不许出现在这条规矩里，也不许出现在
   **组装后的整份 system** 里（人格自己那一段按设计要点名这些词，见
   `test_persona_shape.py` 的豁免，这里照同一口径排除它）。
3. **它经代码进 system**：`stage3_runtime.compose_system_prompt` 是唯一装配点。
   生产上人格是整段替换的（`base_prompt.py:249-251`），写进仓库模板的规矩在生产上
   一个字都不算数——所以这条必须由代码递给它。用户的真人格文件不在改动范围内。
"""
import os
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.base_prompt import _load_base_prompt
from qq_roleplay_bot.prompt_guard import PERSONA_MECHANISM_WORDS, scan_persona_text
from qq_roleplay_bot.stage3_runtime import (
    ContextState,
    ConversationMode,
    SYSTEM_PROMPT,
    build_dialogue_messages,
    compose_system_prompt,
)
from qq_roleplay_bot.stay_in_character import STAY_IN_CHARACTER_RULE
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)

#: 这条规矩必须写明的禁止（"不许做什么"），不是"要如何如何说"。
FORBIDDEN = ("不接这个话", "不评论它", "不说自己是什么", "别顺着往下说")


def msg(index, text="测试"):
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id="100",
        text=text, target=TARGET, sender_name="某人",
    )


def _system_without_the_personas_own_guard_section(text: str) -> str:
    """去掉人格自己那一节"## 有一条线你不能越过"再扫。

    那一节**按设计必须点名**"提示词/协议/上下文/记忆库"（她得知道要躲开哪些词），
    所以它一直是个豁免——`tests/test_persona_shape.py::test_persona_text_has_no_mechanism_words`
    用的就是这个口径。这里照同一口径，测的是"人格之外的整份 system"（含新规矩）。
    """

    guard = text.find("## 有一条线你不能越过")
    if guard < 0:
        return text
    rest = text.find("\n## ", guard + 1)
    return text[:guard] + (text[rest:] if rest >= 0 else "")


def test_the_rule_is_a_prohibition_not_a_style_guide() -> None:
    """只写"不许做什么"——这是上一版"先问一句"栽过的地方。"""

    for phrase in FORBIDDEN:
        assert phrase in STAY_IN_CHARACTER_RULE, phrase
    # 不许写成"要如何如何说"那类**行为指导**（上一版的教训：它把语域带成了技术顾问）。
    for guidance in ("先问一句", "你要说", "你应该说", "记得说", "要如何", "该这样回答"):
        assert guidance not in STAY_IN_CHARACTER_RULE, guidance
    # 躲开的办法是**许可**（三种都行），不是给她的说法。
    assert "都行" in STAY_IN_CHARACTER_RULE
    # 落点还是"像个普通人"：被逗就笑、有事就聊事。
    assert "被逗就笑" in STAY_IN_CHARACTER_RULE
    assert "有事就聊事" in STAY_IN_CHARACTER_RULE
    # 不点名词的那种说法（机制词一个都不能出现）。
    assert "这东西是怎么做出来的" in STAY_IN_CHARACTER_RULE


def test_the_rule_has_no_mechanism_words() -> None:
    """措辞会被角色吸收（AGENTS §2.2）：一个机制词都不许有。"""

    assert scan_persona_text(STAY_IN_CHARACTER_RULE) == [], \
        scan_persona_text(STAY_IN_CHARACTER_RULE)
    for word in PERSONA_MECHANISM_WORDS:
        assert word not in STAY_IN_CHARACTER_RULE, word


def test_the_whole_system_has_no_mechanism_words_outside_the_personas_guard_section() -> None:
    """组装后的**整份 system**（含新规矩）扫一遍 → 零命中。"""

    composed = compose_system_prompt(SYSTEM_PROMPT)
    assert STAY_IN_CHARACTER_RULE in composed, "新规矩没进 system，这条测试就没意义了"
    hits = scan_persona_text(_system_without_the_personas_own_guard_section(composed))
    assert hits == [], f"system 里混进了机制词：{hits}／词表 {PERSONA_MECHANISM_WORDS}"


def test_the_rule_reaches_the_system_through_the_code_seam() -> None:
    """经 `compose_system_prompt` 进 system（不是写进人格模板）。"""

    persona = "你是一个住在群聊里的角色。这一份是测试用的极简人格，里面没有任何规矩。"
    composed = compose_system_prompt(persona)
    assert composed == persona + STAY_IN_CHARACTER_RULE
    assert composed.startswith(persona), "人格本身不能被改动"


def test_it_survives_a_replaced_persona() -> None:
    """生产上人格是**整段替换**的——这条规矩走那条真路：换掉人格也照样在。"""

    import tempfile

    fake = "你是一个住在群聊里的角色。这一份是测试用的极简人格，里面没有任何规矩。"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "base_prompt.FAKE.py"
        path.write_text(f'BASE_PROMPT = """{fake}"""\n', encoding="utf-8")
        with patch.dict(os.environ, {"QQBOT_BASE_PROMPT_FILE": str(path)}):
            persona = _load_base_prompt()
        assert persona.strip() == fake, "假人格没被读进来，这条测试就没意义了"

        current = msg(1, "随便聊点什么")
        with patch("qq_roleplay_bot.stage3_runtime.resolve_system_prompt",
                   lambda **kwargs: persona):
            request = build_dialogue_messages(
                [current], current=current, mode=ConversationMode.IDLE,
                trigger="threshold", context=ContextState(topic="闲聊"),
            )
        assert request[0]["role"] == "system"
        assert request[0]["content"] == persona + STAY_IN_CHARACTER_RULE
        assert scan_persona_text(request[0]["content"]) == []


def test_composing_twice_does_not_double_the_rule() -> None:
    """幂等：同一份文本拼两次也只出现一次（面板上有人手工粘过也一样）。"""

    once = compose_system_prompt(SYSTEM_PROMPT)
    assert compose_system_prompt(once) == once
    assert once.count(STAY_IN_CHARACTER_RULE) == 1
    assert compose_system_prompt("") == ""
