from qq_roleplay_bot.stage3_runtime import ConversationMode, ContextState, build_dialogue_messages
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget


def message(text: str, user_id: str = "debug-user") -> IncomingMessage:
    return IncomingMessage(
        message_id=text,
        session_id="private:debug",
        user_id=user_id,
        text=text,
        target=MessageTarget(user_id=user_id),
    )


def test_prompt_requires_empathy_without_overdiagnosis() -> None:
    current = message("我今天有点累，但不想把事情说得太严重。")
    request = build_dialogue_messages(
        [current],
        current=current,
        mode=ConversationMode.ACTIVE,
        trigger="private_debug",
        context=ContextState(topic="日常状态"),
    )
    system = request[0]["content"]
    assert "不擅自诊断" in system
    assert "先具体回应当下感受" in system
    assert "不要连续追问" in system


def test_prompt_distinguishes_joke_from_attack() -> None:
    current = message("你可真聪明啊，哈哈我开玩笑的。")
    request = build_dialogue_messages(
        [current],
        current=current,
        mode=ConversationMode.ACTIVE,
        trigger="private_debug",
        context=ContextState(tone="uncertain"),
    )
    system = request[0]["content"]
    assert "玩笑" in system
    assert "不能只因为出现" in system
    assert "不确定" in system


def test_prompt_treats_topic_change_as_a_real_transition() -> None:
    history = [message("我们刚刚在聊睡眠。"), message("我最近总是睡不好。")]
    current = message("算了，换个话题，最近有什么好玩的游戏？")
    request = build_dialogue_messages(
        history,
        current=current,
        mode=ConversationMode.ACTIVE,
        trigger="active_message",
        context=ContextState(topic="睡眠", pending_question="如何改善睡眠"),
    )
    stable, volatile = request[1]["content"], request[2]["content"]
    assert "topic=睡眠" in volatile
    assert current.text in volatile
    assert "CURRENT EVENT" not in volatile
    assert "topic=睡眠" not in stable
