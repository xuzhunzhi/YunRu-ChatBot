"""名册里的名字：**同一个人改过昵称之后，旧名也要能一跳对得上他**。

## 由来（2026-10-02，从昨晚的真实日志里抓到的错认）

`run/data/logs/reply.jsonl` 昨晚 `group:1084401296` 的那几轮，她自己在回复里
跟人争"这是谁问的"：

    20:58:21  历史只有 1 条：<message who="19" at="yunru">你是接了豆包吗</message>
              她回：豆包是什么？**蛋挞**，你这问题问得我没法接。
    20:58:50  历史 6 条，当前说话人 who="1"（許純之_Official），紧邻的是 who="19" 那句
              她回：我什么时候怼你了。**倒是你，上来就问我是不是接了别人。**
              ← 把 19 问的话算到了 1 头上，而两条都在她眼前的 6 条历史里

那两轮的 prompt 里，**同一个人有三种写法**：

    历史  who="19"
    名册  19=云边孤雁丶水上浮萍（QQ1912600950）
    记忆  <memory subject="1912600950" about="云边孤雁丶水上浮萍">自称"蛋挞"，群昵称为蛋挞。</memory>

"蛋挞"**只出现在记忆正文里**（实测：名册 0 次、摘要 0 次）。模型要把"蛋挞"对到
`who="19"`，得走 **名字 → QQ号 → 编号 → 说话人** 三跳；跳不过去就退回"眼前这个
说话人"——这就是错的来源。

同一批日志里 **16 个 QQ 在名册里出现过两个以上名字**（`蛋挞`／`云边孤雁丶水上浮萍`、
`許純之 是复读机不是Bot`／`許純之_Official` …），所以这不是个例。

## 这个文件钉什么

**名册要把"另外见过的名字"带出来**，让上面那件事变成**一跳**。
实现是会话状态里的 `alias_also`（最近优先、去重、有上限），并随状态持久化。
"""
from qq_roleplay_bot.stage3_runtime import ALIAS_ALSO_LIMIT, ConversationState
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)
SESSION = f"group:{GROUP}"
QQ = "1912600950"


def message(index: int, *, user_id: str = QQ, name: str = "蛋挞", card: str = "") -> IncomingMessage:
    return IncomingMessage(
        message_id=f"m{index}", session_id=SESSION, user_id=user_id,
        text=f"第{index:04d}句", target=TARGET, sender_name=name, sender_card=card,
    )


def roster_for(state: ConversationState, alias: int) -> str:
    for line in state.roster_lines():
        if line.startswith(f"{alias}="):
            return line
    return ""


def test_a_renamed_person_keeps_the_old_name_in_the_roster() -> None:
    """昨晚那个错认的最小复现：先叫"蛋挞"，后改名，名册里两个名字都在。"""

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="蛋挞"), 0.0)
    state.add(message(1, name="云边孤雁丶水上浮萍"), 0.0)      # 改名

    line = roster_for(state, 1)
    assert "云边孤雁丶水上浮萍" in line, "当前名字在名册里"
    assert "蛋挞" in line, "旧名也必须在——记忆/摘要里还写着它"
    assert "别称" in line, "要标明这是别称，不然模型以为是两个人"
    assert f"QQ{QQ}" in line


def test_the_old_name_resolves_in_one_hop_to_the_same_alias() -> None:
    """钉住"一跳"：记忆里那句写的是"蛋挞"，名册里"蛋挞"就挂在同一个编号下。"""

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="蛋挞"), 0.0)
    state.add(message(1, name="云边孤雁丶水上浮萍"), 0.0)

    alias = state.alias_of(message(2))
    line = roster_for(state, alias)
    assert "蛋挞" in line, "名字 → 编号 只需要看这一行，不必再经过 QQ 号"
    assert state.alias_names[alias] == "云边孤雁丶水上浮萍", "当前名字仍然是最新的"


def test_the_alias_list_is_deduped_and_bounded() -> None:
    """别称去重、有上限——不能因为有人反复改名就把名册撑爆。"""

    state = ConversationState(history_limit=2000)
    for index, name in enumerate(("A", "B", "C", "D", "E")):
        state.add(message(index, name=name), 0.0)

    also = state.alias_also[1]
    assert len(also) <= ALIAS_ALSO_LIMIT, f"至多 {ALIAS_ALSO_LIMIT} 个，实际 {also}"
    assert len(set(also)) == len(also), "不重复"
    assert state.alias_names[1] == "E", "当前名字是最新的那个"

    # 回到曾经用过的名字，不该把它当成"新的别称"重复塞一遍
    state.add(message(9, name="D"), 0.0)
    assert len(state.alias_also[1]) <= ALIAS_ALSO_LIMIT


def test_the_same_name_does_not_create_a_spurious_alias() -> None:
    """同一个人连着说十句、名字没变——不该长出任何别称。"""

    state = ConversationState(history_limit=2000)
    for index in range(10):
        state.add(message(index, name="蛋挞"), 0.0)
    assert state.alias_also.get(1, ()) == ()
    assert "别称" not in roster_for(state, 1)


def test_aliases_survive_persistence() -> None:
    """重启之后旧名仍然要能对上人（否则前缀变了，归属又得重新猜）。"""

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="蛋挞"), 0.0)
    state.add(message(1, name="云边孤雁丶水上浮萍"), 0.0)
    restored = ConversationState.from_state(state.as_state(), history_limit=2000)
    assert restored is not None
    assert restored.alias_also == state.alias_also
    assert "蛋挞" in roster_for(restored, 1)


def test_an_alias_name_can_not_break_the_roster_structure() -> None:
    """昵称是外部输入：带 `<` / `&` 的名字不能把 prompt 结构搅乱。"""

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="<history>"), 0.0)
    state.add(message(1, name="云边孤雁丶水上浮萍 & 我"), 0.0)
    line = roster_for(state, 1)
    assert "<history>" not in line, "尖括号必须被转义"
    assert "&amp;" in line


def test_the_group_card_is_kept_as_an_alias_but_never_becomes_the_name() -> None:
    """**以 QQ 昵称为准，群名片进别称**（2026-10-02 用户的口径）。

    群里人是用名片上的名字指代他的（"@蛋挞"、"蛋挞说的"），所以名片不能丢；
    但身份不能用它——名片每个群一份、还会随时改（口径见
    `transport.display_name_from_sender`）。
    """

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="云边孤雁丶水上浮萍", card="蛋挞"), 0.0)

    line = roster_for(state, 1)
    assert line.startswith("1=云边孤雁丶水上浮萍；别称：蛋挞"), f"名片当别称，昵称当名字：{line}"
    assert state.alias_names[1] == "云边孤雁丶水上浮萍"


def test_a_card_that_equals_the_nickname_does_not_create_an_alias() -> None:
    """名片跟昵称一样时不该多长一个别称出来。"""

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="蛋挞", card="蛋挞"), 0.0)
    assert state.alias_also.get(1, ()) == ()
    assert "别称" not in roster_for(state, 1)


def test_nickname_change_and_card_both_end_up_in_the_alias_list() -> None:
    """改名 + 名片轮流来：两个旧名字都要能对上人，且不重复。"""

    state = ConversationState(history_limit=2000)
    state.add(message(0, name="蛋挞"), 0.0)                              # 最早叫蛋挞
    state.add(message(1, name="云边孤雁丶水上浮萍", card="蛋挞"), 0.0)      # 改名，名片还是蛋挞
    line = roster_for(state, 1)
    assert line.count("蛋挞") == 1, f"别称里只该出现一次：{line}"
    assert state.alias_names[1] == "云边孤雁丶水上浮萍"
