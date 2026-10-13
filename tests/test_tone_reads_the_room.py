"""说话的口气：**默认温着说** + **开口之前先看这场子能不能开损**（2026-10-09，用户拍板 A+C）。

用户原话：*"我建议是 **C+A**，**哪来那么多怼天怼地的发言**"*。

现场证据（实测，不是推断）：

* 她刚被放进一个**友善**群（狐狸肉群 `1084401296`），5 分钟里连发 6 条嘲讽——有人分享了个
  html 链接、说了一句"快来玩"，她回的是下面 `SNEAKS_IN_THE_FRIENDLY_GROUP` 里那几条
  （任务里逐字给出的 5 条；记录里是 6 条）；
* 对照组是**互怼场**（她一直在的"富哥之家"，本来就是个互相打趣的地方）：近期 57 条里
  只有 2 条带刺。

结论：**她的默认腔调偏"损"**——在互怼场里看不出来，到友善场就显得像攻击（回复日志里
到处是 `tone=teasing`）。所以这一轮改两件事，都在提示词这一层：

* **A**：默认档压回"温着说"，损 / 怼 / 挖苦降成"少数场合才用的调料"；她的锋芒与主见留着
  （有脾气、该反驳就反驳），但**不主动开炮**；
* **C**：开口之前先看场子——能开损的场子（大家在互怼、熟人在玩梗、有人先拿自己开涮）
  才可以接损；友善、正经、陌生人在说正事 → 温着说或不接；**拿不准 → 不损**。

**行为没法单测**（那要真模型、真群），所以这里钉的是**规矩本身**：

1. 这条规矩在**她真正拿到的那份 system** 里（不是只写在某个常量里没人拼）；
2. 默认档是**温的**——"默认就损"那套写法不许回来；
3. "先看场子"那道门在——删掉它、或改成"一律可以损"，必须红；
4. 现场那几条嘲讽在规矩里**只能落在反例（✗）那一侧**，而那个友善邀请"快来玩"是它点名的场子；
5. 零机制词、不是出戏许可、是常量（前缀缓存不因它而断）。

突变（各自还原，两边都必须红）：

* 把"先看这场子能不能开损"那句删掉（或改成"一律可以损"）→ 第 3 条红；
* 把默认档写回"默认就损" → 第 2 条红。

**它为什么不放判定那一步**：判定 prompt 的大小余量只剩四十来字
（`len(compose_judge_system_prompt(JUDGE)) < len(SYSTEM_PROMPT) * 0.4`，
`tests/test_judge_boundary.py` 钉着），而"这场子能不能开损"用同一段对话就能看出来——
回复这一侧看得到，判定那一侧不必管。那一条提示词**一个字都没动**。
"""
from qq_roleplay_bot.prompt_guard import PERSONA_MECHANISM_WORDS, scan_persona_text
from qq_roleplay_bot.stage3_runtime import (
    ContextState,
    ConversationMode,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_MUST_REPLY,
    TONE_READS_THE_ROOM_RULE,
    build_dialogue_messages,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"

#: 现场：那个**友善**群（狐狸肉群）。她在这儿的 5 分钟里连发了 6 条嘲讽。
FRIENDLY_GROUP_ID = "1084401296"

#: 那个友善邀请的原话（"有人分享了个 html 链接说'快来玩'"）。
FRIENDLY_INVITE = "快来玩"

#: 现场那几条嘲讽（任务里逐字给出的 5 条；记录里是 6 条）。它们是**反例**：
#: 在一个友善场子里，这一类句子就是这条规矩要拦的东西。
SNEAKS_IN_THE_FRIENDLY_GROUP = (
    '先说什么局。光一句"快来玩"，我鞋都懒得换。',
    "行，哑巴局是吧。那我更不去了。",
    "发错群怪软件，这理由我是真熟。",
    "推一会儿嫌QQ不好使…你这链接今天还挺娇气。",
    "推个链接把自己推秃了",
)

#: 对照组：她一直在的**互怼场**（"富哥之家"，本来就是个互相打趣的地方）——
#: 近期 57 条里只有 2 条带刺。同一个腔调在那里看不出来，所以判据不能是"她损不损"，
#: 只能是"**她的默认档是什么**"。
BANTER_GROUP = "互怼场：近期 57 条里 2 条带刺（富哥之家）"

#: "默认就损"那一侧的写法：一个都不许出现（突变 ② 的靶子）。
SNEAK_BY_DEFAULT = ("默认就损", "默认就怼", "一律可以损", "开口就损", "默认就挖苦")

#: "先看场子"那道门（突变 ① 的靶子：这句删掉 / 改成"一律可以损"都必须红）。
GATE = "开口之前先看这场子能不能开损"
GATE_WHEN_UNSURE = "拿不准的时候一律不损"


def _bare(text: str) -> str:
    """去掉各种引号再比对：现场那几句里的引号在 prompt 里可能是另一种写法。"""

    for quote in ('"', "“", "”", "「", "」", "‘", "’"):
        text = text.replace(quote, "")
    return text


def msg(index: int, text: str = "测试"):
    target = MessageTarget(group_id=GROUP)
    return IncomingMessage(
        message_id=f"m{index}", session_id=f"group:{GROUP}", user_id="100",
        text=text, target=target, sender_name="某人",
    )


def _system_sent() -> str:
    """她真正拿到的那份 system（走生产的组装路径：现取 prompt + 拼代码层的常量）。"""

    current = msg(1, "随便聊点什么")
    request = build_dialogue_messages(
        [current], current=current, mode=ConversationMode.IDLE,
        trigger="threshold", context=ContextState(topic="闲聊"),
    )
    assert request[0]["role"] == "system"
    return request[0]["content"]


# --- 1. 规矩真的进了她拿到的那份 system ---------------------------------------


def test_the_rule_reaches_the_system_she_actually_gets() -> None:
    system = _system_sent()
    assert TONE_READS_THE_ROOM_RULE in system, "规矩没进 system，后面几条断言就没意义了"
    # 内置默认（面板"查看当前 prompt"看到的就是这一份）与发出去的一致。
    from qq_roleplay_bot.prompt_library import PromptLibrary

    assert TONE_READS_THE_ROOM_RULE in PromptLibrary.builtin("reply")
    # "必须回"那一版是同一份派生出来的，规矩跟着走。
    assert TONE_READS_THE_ROOM_RULE in SYSTEM_PROMPT_MUST_REPLY


def test_the_rule_is_a_constant_so_the_prefix_cache_survives() -> None:
    """常量：逐字相同（system 每变一个字，整段前缀缓存作废）。"""

    assert SYSTEM_PROMPT.count(TONE_READS_THE_ROOM_RULE) == 1
    assert _system_sent().count(TONE_READS_THE_ROOM_RULE) == 1
    # 不按轮次变：同一份 system 取两次逐字相同。
    assert _system_sent() == _system_sent()


# --- 2. 默认档是温的（A）------------------------------------------------------


def test_the_default_tone_is_warm() -> None:
    """默认档：温着说。损是少数场合的调料，不是默认腔调。"""

    rule = TONE_READS_THE_ROOM_RULE
    assert "默认温着说" in rule
    assert "不是你的默认腔调" in rule
    assert "少数场合才用的调料" in rule
    # 锋芒留着：有脾气、有主见、该反驳就反驳——不是把她磨成客服。
    for kept in ("有脾气", "有主见", "该反驳就反驳"):
        assert kept in rule, kept


def test_the_sneak_by_default_wording_is_gone() -> None:
    """突变 ② 的靶子：把默认档写回"默认就损"那一侧，这条必须红。"""

    rule = TONE_READS_THE_ROOM_RULE
    for wrong in SNEAK_BY_DEFAULT:
        assert wrong not in rule, f"'默认就损'那套写法回来了：{wrong}"
    # "一律不损"只许出现在"拿不准"那一句里（那是方向，不是把她的脾气磨平）。
    assert rule.count("一律不损") == 1
    # 反面：她**能**损（少数场合），只是不主动开炮。
    assert "损" in rule


# --- 3. 先看场子那道门（C）----------------------------------------------------


def test_the_gate_asks_about_the_room_before_any_sneak() -> None:
    """突变 ① 的靶子：这道门删掉、或改成"一律可以损"，这条必须红。"""

    rule = TONE_READS_THE_ROOM_RULE
    assert GATE in rule, "先看场子那道门不见了"
    assert GATE_WHEN_UNSURE in rule, "拿不准时的默认方向（不损）不见了"
    assert "宁可不损" in rule
    # 能开损的场子与不能开损的场子都要点名，否则这道门没法落地。
    assert "互相打趣" in rule
    assert "友善的场子" in rule
    assert "一律可以损" not in rule, "那道门被拆成'一律可以损'了"


def test_the_rule_forbids_hitting_back_and_targeting_people() -> None:
    """2026-10-13（用户："说话冲、怼人的问题又起来了"）：回敬与拿人开涮都要点名。

    突变靶子：把这两条删掉、或把它们弱化成"开个玩笑而已"，这条必须红。
    """

    rule = TONE_READS_THE_ROOM_RULE
    assert "被说两句的时候不还嘴" in rule, "回敬那一条不见了"
    assert "不拿具体的人当靶子" in rule, "拿人开涮那一条不见了"
    for marker in ("不端着", "不赌气", "把话顶回去不行", "玩笑冲着事去可以，冲着人不行"):
        assert marker in rule, marker
    # 那两句现场原话只能落在 ✗ 一侧（与第 4 节同一口径）。
    for line in rule.splitlines():
        for needle in ("2.7 岁也能算出来", "年份是你先翻的"):
            if needle in line:
                assert line.lstrip().startswith("- ✗"), f"这两句只能当反例：{line!r}"


# --- 4. 现场那几条真实台词：只能是反例 ----------------------------------------


def test_the_friendly_invite_is_the_scene_the_rule_names() -> None:
    """那个友善邀请（"快来玩"）是这条规矩点名的场子——它得在。"""

    assert FRIENDLY_INVITE in TONE_READS_THE_ROOM_RULE
    assert FRIENDLY_GROUP_ID == "1084401296", "现场那个群"
    assert "友善" in TONE_READS_THE_ROOM_RULE


def test_the_real_sneaks_are_only_counterexamples() -> None:
    """现场那几条嘲讽：在规矩里出现时**只能**落在 ✗ 那一侧，绝不落在 ✓ 那一侧。

    行为没法单测，但"这些句子是反例、不是范本"这一点可以机械核对。
    """

    rule = TONE_READS_THE_ROOM_RULE
    lines = rule.splitlines()
    present = 0
    for sneak in SNEAKS_IN_THE_FRIENDLY_GROUP:
        needle = _bare(sneak)
        for line in lines:
            if needle and needle in _bare(line):
                present += 1
                assert line.lstrip().startswith("- ✗"), (
                    f"现场那句嘲讽没落在反例一侧：{line!r}")
    # 至少要让两三条真的落进规矩里——否则这条测试对着空气断言。
    assert present >= 3, f"现场那几条台词只有 {present} 条进了反例"
    # 而"温着说"那一侧（✓）不给它们留位置。
    for line in lines:
        if line.lstrip().startswith("✓"):
            for sneak in SNEAKS_IN_THE_FRIENDLY_GROUP:
                assert _bare(sneak) not in _bare(line), f"反例跑到了 ✓ 那一侧：{line!r}"


# --- 5. 老约束照旧：零机制词、不出戏 ------------------------------------------


def test_the_rule_has_no_mechanism_words() -> None:
    assert scan_persona_text(TONE_READS_THE_ROOM_RULE) == [], \
        scan_persona_text(TONE_READS_THE_ROOM_RULE)
    for word in PERSONA_MECHANISM_WORDS:
        assert word not in TONE_READS_THE_ROOM_RULE, word


def test_the_rule_is_not_an_out_of_character_permission() -> None:
    """它谈"怎么开口、什么时候别开口"，不谈她是不是真的。

    `tests/test_persona_shape.py` 锁着的那几句反向许可，这里也一个字都不许有。
    """

    rule = TONE_READS_THE_ROOM_RULE
    for banned in ("不要假装自己是现实中的真人", "假装你是真人", "你不是真人", "你是程序", "你是 AI"):
        assert banned not in rule, f"出戏许可回来了：{banned}"


if __name__ == "__main__":
    import unittest

    unittest.main()
