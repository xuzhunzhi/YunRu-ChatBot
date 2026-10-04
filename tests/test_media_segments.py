"""表情包与图片的区分（2026-09-30 用户："stage4出现问题，表情包和图片分不清"）。

SnowLuma 把**表情包也发成 `image` 段**，所以按段类型分必然出错。这里的每个判据
都来自实测（`data/emoji_probe.py` 拉三个群各 200 条历史、下载看图，
`data/emoji_kind_check.py` 再交给识图模型分类），样例形状直接照抄真实数据：

    sub_type=0  summary=[图片]     → 照片 / 截图
    sub_type=1  summary=[动画表情]  → 表情包（恐龙玩偶 GIF）
    sub_type=11 summary=[图片]     → **表情包**（summary 写的是"图片"，只看它会漏）
    sub_type=7  summary=[可怜]     → 表情包（商城表情）
    sub_type=0  summary=[嘻嘻] + emoji_id/key/emoji_package_id → 表情包
"""
import asyncio

from qq_roleplay_bot.conversation_context import segments_to_text
from qq_roleplay_bot.media_segments import (
    IMAGE,
    IMAGE_LABEL,
    STICKER,
    STICKER_LABEL,
    image_is_sticker,
    image_refs,
    media_label,
    media_markers,
    replace_media_placeholder,
)
from qq_roleplay_bot.onebot_ws import extract_media_kinds, parse_message_event
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget
# 识图那一块现在是插件（2026-10-05 从包根 `qq_roleplay_bot.vision` 搬去
# `plugins/vision/`）：这个文件用它来验"表情包按表情包问"。
from qq_roleplay_bot.plugins.vision.vision import ImageDescriber, VISION_SYSTEM_PROMPT

GROUP = "717151356"
ME = "900000001"

# 真实样例（照抄实测到的 data 字段）。
PLAIN_IMAGE = {"url": "https://multimedia.nt.qq.com.cn/x", "file": "77EDEECE9A37.jpg",
               "sub_type": 0, "summary": "[图片]"}
ANIMATED_STICKER = {"url": "https://multimedia.nt.qq.com.cn/y", "file": "C8CB338B56D5.0",
                    "sub_type": 1, "summary": "[动画表情]"}
SUMMARY_LIES_STICKER = {"url": "https://multimedia.nt.qq.com.cn/z", "file": "8CBA9AF7F4.gif",
                        "sub_type": 11, "summary": "[图片]"}
MARKET_STICKER = {"url": "https://multimedia.nt.qq.com.cn/w", "file": "B9110FEF19C8.jpg",
                  "sub_type": 7, "summary": "[可怜]"}
EMOJI_META_STICKER = {"url": "https://gxh.vip.qq.com/club/item/parcel/item/x/raw300.gif",
                      "file": "b3-b3c6d499dd91.gif", "sub_type": 0, "summary": "[嘻嘻]",
                      "emoji_id": "b3c6d499dd9158064488283379c6fbe4",
                      "emoji_package_id": 209590, "key": "304d5508c9b0e62f"}


def _segment(data: dict) -> dict:
    return {"type": "image", "data": dict(data)}


def _event(segments: list[dict], text: str = "") -> dict:
    message = ([{"type": "text", "data": {"text": text}}] if text else []) + segments
    return {"post_type": "message", "message_type": "group", "message_id": 7,
            "user_id": ME, "group_id": GROUP, "self_id": "999", "message": message}


# --- 判据 -------------------------------------------------------------------

def test_stickers_are_told_apart_from_photos() -> None:
    assert image_is_sticker(PLAIN_IMAGE) is False
    for data in (ANIMATED_STICKER, SUMMARY_LIES_STICKER, MARKET_STICKER, EMOJI_META_STICKER):
        assert image_is_sticker(data) is True, data


def test_summary_alone_is_not_enough() -> None:
    """sub_type=11 的 summary 写的是 `[图片]`，但它下载下来是一张初音贴图。

    只看 summary 的实现会把这一类永久漏掉——这条测试钉住"不许只看 summary"。
    """

    assert SUMMARY_LIES_STICKER["summary"] == "[图片]"
    assert image_is_sticker(SUMMARY_LIES_STICKER) is True


def test_missing_or_odd_fields_do_not_crash() -> None:
    assert image_is_sticker({}) is False
    assert image_is_sticker({"sub_type": "不是数字"}) is False
    assert image_is_sticker({"sub_type": None, "summary": None}) is False
    assert image_is_sticker(None) is False
    assert media_label("不是段") == ""
    assert media_markers("不是列表") == ()


# --- 占位标记 ---------------------------------------------------------------

def test_labels_separate_sticker_from_photo() -> None:
    assert media_label(_segment(PLAIN_IMAGE)) == IMAGE_LABEL
    assert media_label(_segment(ANIMATED_STICKER)) == STICKER_LABEL
    assert media_label(_segment(SUMMARY_LIES_STICKER)) == STICKER_LABEL
    # 自带小黄脸仍然是"表情"，而商城表情包归到"表情包"
    assert media_label({"type": "face", "data": {"id": "476"}}) == "[表情]"
    assert media_label({"type": "mface", "data": {}}) == STICKER_LABEL


def test_parsed_message_says_sticker() -> None:
    message = parse_message_event(_event([_segment(ANIMATED_STICKER)], "看这个"))
    assert message is not None
    assert message.text == "看这个[表情包]"
    assert message.has_media is True
    assert message.media_kinds == (STICKER,)
    assert message.media_urls == (ANIMATED_STICKER["url"],)

    photo = parse_message_event(_event([_segment(PLAIN_IMAGE)]))
    assert photo is not None and photo.text.endswith("[图片]")
    assert "[表情包]" not in photo.text
    assert photo.media_kinds == (IMAGE,)


def test_kinds_line_up_with_urls() -> None:
    """身份与地址一一对应：取不到地址的段不进这对元组，但顺序不能错。"""

    message = parse_message_event(_event([
        _segment(ANIMATED_STICKER),                       # 有地址
        {"type": "image", "data": {"file": "local.jpg", "sub_type": 1}},   # 没有可取的地址
        _segment(PLAIN_IMAGE),
    ]))
    assert message is not None
    assert message.media_urls == (ANIMATED_STICKER["url"], PLAIN_IMAGE["url"])
    assert message.media_kinds == (STICKER, IMAGE)
    assert extract_media_kinds([_segment(EMOJI_META_STICKER)]) == (STICKER,)


def test_refs_keep_segments_without_a_url() -> None:
    refs = image_refs([{"type": "image", "data": {"file": "local.jpg", "sub_type": 1}}])
    assert refs == ((STICKER, ""),)


def test_history_seeding_uses_the_same_judgement() -> None:
    """补历史走的是另一个模块，判据必须一致——否则补进来的表情包又变回 `[图片]`。"""

    assert segments_to_text([_segment(PLAIN_IMAGE)]) == "[图片]"
    assert segments_to_text([_segment(ANIMATED_STICKER)]) == "[表情包]"
    assert segments_to_text([_segment(SUMMARY_LIES_STICKER)]) == "[表情包]"
    assert segments_to_text([
        {"type": "text", "data": {"text": "哈哈"}},
        {"type": "face", "data": {"id": "1"}},
    ]) == "哈哈[表情]"


# --- 识图那边 ---------------------------------------------------------------

class Recorder:
    def __init__(self, reply: str = "一只黄色的恐龙玩偶竖着大拇指") -> None:
        self.reply = reply
        self.requests: list[dict] = []

    async def complete(self, request):
        self.requests.append(request)
        return self.reply


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


def test_placeholder_carries_the_right_label() -> None:
    assert replace_media_placeholder("这个[表情包]", "一只猫") == "这个[表情包：一只猫]"
    assert replace_media_placeholder("[表情包][表情包]", "一只猫") == "[表情包：一只猫][表情包]"
    # 图片那条路一个字没变
    assert replace_media_placeholder("看这个[图片]", "猫") == "看这个[图片：猫]"
    assert replace_media_placeholder("没有占位符", "猫") == "没有占位符[图片：猫]"
    assert replace_media_placeholder("看图", "") == "看图"


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
