"""`test_media_segments.py` 里**服务识图插件**的那几条（插件侧）。

## 为什么单独一个文件（2026-10-05 分支拆分）

"表情包与图片分不清"那条判据**大部分是本体**的：`media_segments.py` 自己判
`sub_type` / `summary` / `emoji_id`，那部分留在 `tests/test_media_segments.py`，
不带插件也跑得动。

但这个文件里的三条要**真的造一个识图器**：

- `test_vision_asks_differently_for_a_sticker`——表情包要按表情包问；
- `test_sticker_goes_through_vision_as_a_sticker`——最终正文是 `[表情包：…]`；
- `test_mixed_media_is_asked_as_a_plain_picture`——贴图+照片混着时不按贴图问。

`ImageDescriber` 住在 `plugins/vision/`，而本体侧的正常形态是**插件不在**
（`plugins/` 下只有 `__init__.py`）。所以这三条**搬到这里**，本体侧那份不带它们，
也不留 `skipUnless`——它们在上面那条线上**根本不该存在**，而不是"存在但跳过"。

断言一个字没改，只是搬了家（判据归插件侧，因为它要靠插件提供的能力才成立）。

共享的样例数据与 `Recorder` 从本体侧那份借——它已经 `sys.path` 在
`tests/` 上（`tests/run_offline.py` 会加），所以直接 import 普通模块名即可。
**借**而不是**复制**：样例形状只该有一份（照抄自 `data/emoji_probe.py` 的实测）。
"""
import asyncio

from qq_roleplay_bot.plugins.vision.vision import ImageDescriber, VISION_SYSTEM_PROMPT
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget
from test_media_segments import (
    ANIMATED_STICKER,
    GROUP,
    IMAGE,
    ME,
    PLAIN_IMAGE,
    STICKER,
    Recorder,
)


def test_vision_asks_differently_for_a_sticker() -> None:
    client = Recorder()
    describer = ImageDescriber(client)
    asyncio.run(describer.describe(["https://x/1.gif"], sticker=True))
    content = client.requests[0][-1]["content"]
    assert "表情包" in content[0]["text"]
    assert "表情包" in VISION_SYSTEM_PROMPT and "不是别人拍的照片" in VISION_SYSTEM_PROMPT

    asyncio.run(describer.describe(["https://x/2.jpg"]))
    content = client.requests[1][-1]["content"]
    assert "表情包" not in content[0]["text"]


def test_sticker_goes_through_vision_as_a_sticker() -> None:
    """一条只有表情包的消息：正文里最终应当是 `[表情包：…]`，不是 `[图片：…]`。"""

    client = Recorder("一只黄色的恐龙玩偶")
    engine = DialogueEngine(client, super_admin_user_ids=frozenset({ME}))
    engine.vision = ImageDescriber(client)
    message = IncomingMessage(
        message_id="m1", session_id=f"group:{GROUP}", user_id=ME,
        text="（发送了一条媒体消息）[表情包]", target=MessageTarget(group_id=GROUP),
        has_media=True, media_urls=(ANIMATED_STICKER["url"],), media_kinds=(STICKER,),
    )
    state = engine.sessions.state(message.session_id)
    updated = asyncio.run(engine._apply_vision(message, state))
    assert "[表情包：一只黄色的恐龙玩偶]" in updated.text
    assert "[图片" not in updated.text
    assert updated.media_kinds == ()


def test_mixed_media_is_asked_as_a_plain_picture() -> None:
    """一条消息里既有贴图又有照片时，不按贴图问——免得把照片讲成梗图。"""

    client = Recorder()
    engine = DialogueEngine(client, super_admin_user_ids=frozenset({ME}))
    engine.vision = ImageDescriber(client)
    message = IncomingMessage(
        message_id="m2", session_id=f"group:{GROUP}", user_id=ME,
        text="[表情包][图片]", target=MessageTarget(group_id=GROUP),
        has_media=True, media_urls=(ANIMATED_STICKER["url"], PLAIN_IMAGE["url"]),
        media_kinds=(STICKER, IMAGE),
    )
    state = engine.sessions.state(message.session_id)
    asyncio.run(engine._apply_vision(message, state))
    question = client.requests[0][-1]["content"][0]["text"]
    assert "表情包" not in question
