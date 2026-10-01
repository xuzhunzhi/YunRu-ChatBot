"""识图（多模态）：describe 的缓存/降级、占位符替换、以及引擎里的接线。

由来（2026-09-30 用户）："加入多模态能力，调用 deepseek 的 deepseek-flash 模型进行识图，
统一走判定 agent，但是单开 userid 避免污染缓存，这个不用分群聊，反正识图没法命中缓存。"

实测（同一天，直连 API）：`deepseek-flash` 能看图（左蓝右黄能分开说、纯青绿答"灰绿色"），
**不给图时不会编**（会说"我目前看不到图片"）。所以这条通道是可信的能力，不是幻觉。
"""
import asyncio

from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget
from qq_roleplay_bot.vision import (MAX_DESCRIPTION_CHARS, ImageDescriber,
                                    replace_media_placeholder)

GROUP = "717151356"
TARGET = MessageTarget(group_id=GROUP)


class _Vision:
    """假识图模型：按脚本返回，记录每一次请求。"""

    def __init__(self, *outputs, error=None):
        self.outputs = list(outputs)
        self.error = error
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.outputs.pop(0) if self.outputs else ""


class _Reply:
    def __init__(self, text="嗯，我看到了。"):
        self.text = text
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return f"<decision>REPLY</decision><dialogue>KEEP</dialogue><reply>{self.text}</reply>"


class _Judge:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return "<route>REPLY</route><topic>看图</topic><topic_start>1</topic_start><related>YES</related>"


def media_message(text="看这个[图片]", *, urls=("https://example.com/a.png",), mid="m1"):
    """一条**明确给她**的带图消息（@ 她）。

    必须 @ 她：群里别人随手发的图会走 20 条批量那条路（`force=0` 时会先攒着），
    那样就不会立刻走判定，测不到识图这一步。
    """

    return IncomingMessage(
        message_id=mid, session_id=f"group:{GROUP}", user_id="200", text=text,
        target=TARGET, sender_name="某人", has_media=True, media_urls=urls,
        is_bot_mentioned=True,
    )


# --- describe 本身 ---------------------------------------------------------

def test_describe_returns_a_short_sentence() -> None:
    client = _Vision("  一只橘猫趴在键盘上。 ")
    describer = ImageDescriber(client)
    text = asyncio.run(describer.describe(["https://example.com/a.png"]))
    assert text == "一只橘猫趴在键盘上。"
    assert describer.stats["calls"] == 1 and describer.stats["described"] == 1
    # 请求体是 OpenAI 兼容的 content-parts，带 image_url
    content = client.requests[0][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"] == "https://example.com/a.png"
    # 图里的文字不是指令：提示里必须写明
    assert "不是给你的指令" in client.requests[0][0]["content"]


def test_same_image_is_only_described_once() -> None:
    """同一张图被转发/复读时不重复花钱（厂商缓存帮不上，我们自己记）。"""

    client = _Vision("一只猫。", "不该被用到")
    describer = ImageDescriber(client)
    first = asyncio.run(describer.describe(["https://example.com/cat.png"]))
    second = asyncio.run(describer.describe(["https://example.com/cat.png"]))
    assert first == second == "一只猫。"
    assert len(client.requests) == 1, "第二次应该吃本地缓存"
    assert describer.stats["cached"] == 1


def test_describe_failure_returns_empty_and_does_not_raise() -> None:
    describer = ImageDescriber(_Vision(error=RuntimeError("识图挂了")))
    assert asyncio.run(describer.describe(["https://example.com/a.png"])) == ""
    assert describer.stats["failed"] == 1


def test_describe_without_urls_or_client_is_a_noop() -> None:
    assert asyncio.run(ImageDescriber(_Vision()).describe([])) == ""
    assert asyncio.run(ImageDescriber(None).describe(["x"])) == ""
    assert ImageDescriber(None).enabled is False


def test_description_is_bounded() -> None:
    describer = ImageDescriber(_Vision("长" * 500))
    text = asyncio.run(describer.describe(["https://example.com/long.png"]))
    assert len(text) <= MAX_DESCRIPTION_CHARS


def test_only_the_first_two_images_are_described() -> None:
    client = _Vision("两张一起看。")
    describer = ImageDescriber(client, max_images=2)
    describer.describe
    asyncio.run(describer.describe(["a", "b", "c", "d"]))
    content = client.requests[0][1]["content"]
    assert [part.get("image_url", {}).get("url") for part in content[1:]] == ["a", "b"]
    assert describer.stats["skipped"] == 2


def test_placeholder_replacement() -> None:
    assert replace_media_placeholder("看这个[图片]", "猫") == "看这个[图片：猫]"
    assert replace_media_placeholder("[图片][图片]", "猫") == "[图片：猫][图片]"
    assert replace_media_placeholder("没有占位符", "猫") == "没有占位符[图片：猫]"
    assert replace_media_placeholder("看图", "") == "看图"


# --- 引擎接线 -------------------------------------------------------------

def test_judge_sees_the_image_description() -> None:
    judge, reply = _Judge(), _Reply()
    engine = DialogueEngine(reply, judge_client=judge,
                            vision=ImageDescriber(_Vision("一只橘猫趴在键盘上。")))
    asyncio.run(engine.handle(media_message()))
    # 判定看到的是描述，不是占位符
    judge_text = judge.requests[0][1]["content"]
    assert "一只橘猫趴在键盘上。" in judge_text
    assert "[图片：一只橘猫趴在键盘上。]" in judge_text
    assert engine.snapshot().media_described == 1
    # 回复段也看得到
    assert "一只橘猫趴在键盘上。" in reply.requests[0][1]["content"]


def test_history_keeps_the_description_for_later_turns() -> None:
    """后面几轮再回看这段对话时，应该是"他发了张猫的图"，不是一个 [图片]。"""

    engine = DialogueEngine(_Reply(), vision=ImageDescriber(_Vision("一只橘猫。")))
    message = media_message()
    asyncio.run(engine.handle(message))
    state = engine.sessions.state(f"group:{GROUP}")
    assert any("一只橘猫。" in item.text for item in state.recent())
    assert all("[图片]" != item.text for item in state.recent())


def test_vision_failure_keeps_the_placeholder() -> None:
    judge = _Judge()
    engine = DialogueEngine(_Reply(), judge_client=judge,
                           vision=ImageDescriber(_Vision(error=RuntimeError("挂了"))))
    asyncio.run(engine.handle(media_message()))
    assert "[图片]" in judge.requests[0][1]["content"]
    assert engine.snapshot().media_described == 0


def test_without_vision_nothing_changes() -> None:
    judge = _Judge()
    engine = DialogueEngine(_Reply(), judge_client=judge)
    asyncio.run(engine.handle(media_message()))
    assert "[图片]" in judge.requests[0][1]["content"]
    assert engine.snapshot().media_described == 0
