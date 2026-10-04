"""媒体段的身份与占位标记：**表情包和图片是两个东西**。

由来（2026-09-30 用户）："stage4出现问题，表情包和图片分不清"。

SnowLuma/NapCat 把**表情包也发成 `image` 段**，所以原先"按段类型分"
（`image` → `[图片]`、`mface` → `[表情]`）必然把表情包当成图片。
群里的原话是"我发出的 90% 表情包都是 jpg"——她看后缀，当然分不出来。

**实测**（`data/emoji_probe.py` 拉三个群各 200 条历史，取每种形状各一张图
下载下来看过；`data/emoji_kind_check.py` 再按 sub_type 分层抽样交给识图模型分类）：

| 段形状 | 样例 | 判断 |
| --- | --- | --- |
| `image` `sub_type=0` `summary=[图片]` | JPEG 照片 / 截图 | 图片 |
| `image` `sub_type=1` `summary=[动画表情]` | 恐龙玩偶 GIF 贴图 | **表情包** |
| `image` `sub_type=11` `summary=[图片]` | 初音贴图 GIF（summary 写的是"图片"，**只看 summary 会漏**） | **表情包** |
| `image` `sub_type=7` `summary=[可怜]` | 商城表情 | **表情包** |
| `image` `sub_type=0` `summary=[嘻嘻]` + `emoji_id`/`emoji_package_id`/`key` | 商城表情（QQ 把 mface 也以 image 形态发过来） | **表情包** |
| `face` `{id}` | 自带小黄脸，没有地址 | 表情 |

结论：判据是 **`sub_type ∈ {1, 7, 11}`** 或 **带 emoji 元数据**；`summary` 只在
等于 `[动画表情]` 时可用（它也可能是表情自己的名字，比如"可怜""嘻嘻"）。
`mface` 类型在实测里**一次都没出现**，但按语义它同样是表情包，一并归过去。

这个模块是**唯一**的判据来源：`onebot_ws`（实时事件）与 `conversation_context`
（补历史）都从这里取，别在各自文件里再抄一份标签表。
"""
from __future__ import annotations

IMAGE_LABEL = "[图片]"
STICKER_LABEL = "[表情包]"

# 只进入上下文、不参与指令解析的媒体段；值为展示给模型的占位描述。
# 注意 `image` 不在这里：它要先看段里的字段才知道是图片还是表情包。
MEDIA_LABELS = {
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "forward": "[转发消息]",
    "mface": STICKER_LABEL,
    "poke": "[戳一戳]",
}

# 实测：这三个 sub_type 都是表情包（1 与 11 各下载看过，7 为商城表情）。
_STICKER_SUB_TYPES = frozenset({1, 7, 11})
# 实测：summary 可能是表情自己的名字（"可怜""嘻嘻"），只有这两个是类型标记。
_STICKER_SUMMARIES = frozenset({"[动画表情]", "[表情]"})
# 商城表情会额外带这几个字段（QQ 用它标识表情包里的某一张）。
_EMOJI_KEYS = ("emoji_id", "emoji_package_id", "key")
# 能交给识图去取的地址前缀。NapCat 的 `file` 常常只是 `a.jpg` 这种文件名。
_FETCHABLE_PREFIXES = ("http://", "https://", "data:image/")

STICKER = "sticker"
IMAGE = "image"


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def image_is_sticker(data: object) -> bool:
    """这个 `image` 段是表情包还是照片。判据见模块头部那张表。"""

    if not isinstance(data, dict):
        return False
    if any(str(data.get(key) or "").strip() for key in _EMOJI_KEYS):
        return True
    if str(data.get("summary") or "").strip() in _STICKER_SUMMARIES:
        return True
    return _as_int(data.get("sub_type")) in _STICKER_SUB_TYPES


def media_label(segment: object) -> str:
    """一个消息段的占位描述；不是媒体段返回空串。"""

    if not isinstance(segment, dict):
        return ""
    kind = str(segment.get("type") or "")
    if kind == "image":
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        return STICKER_LABEL if image_is_sticker(data) else IMAGE_LABEL
    return MEDIA_LABELS.get(kind, "")


def media_markers(message: object, *, limit: int = 8) -> tuple[str, ...]:
    """把非文本媒体段转成有限数量的占位描述（不下载、不请求任何资源）。"""

    if not isinstance(message, list):
        return ()
    markers: list[str] = []
    for segment in message:
        label = media_label(segment)
        if not label:
            continue
        markers.append(label)
        if len(markers) >= limit:
            break
    return tuple(markers)


def _first_url(data: dict) -> str:
    for key in ("url", "file"):
        url = str(data.get(key) or "").strip()
        if url.startswith(_FETCHABLE_PREFIXES):
            return url
    return ""


def image_refs(message: object, *, limit: int = 8) -> tuple[tuple[str, str], ...]:
    """取出图片段：`(kind, url)`，kind 是 `STICKER`/`IMAGE`，url 可能是空串。

    只取地址，**不下载、不上传**；取不到地址的段也会返回（url 为空），
    这样调用方按顺序看 kind 时不会错位。
    """

    if not isinstance(message, list):
        return ()
    refs: list[tuple[str, str]] = []
    for segment in message:
        if not isinstance(segment, dict) or str(segment.get("type") or "") != "image":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        kind = STICKER if image_is_sticker(data) else IMAGE
        refs.append((kind, _first_url(data)))
        if len(refs) >= limit:
            break
    return tuple(refs)


def replace_media_placeholder(text: str, description: str) -> str:
    """把正文里的媒体占位符换成描述（只换**最先出现的那个**，其余原样留着）。

    图片与表情包各换各的标记：`[图片：…]` / `[表情包：…]`——她得看得出这是个贴图。

    **为什么这个函数在核心（2026-10-05 从 `vision.py` 搬来）**：它一行插件都不需要
    ——只用这个模块自己的两个标签。原来放在识图插件里，于是 `stage3_main` 那条
    "把描述塞回正文"的路径得**从插件 import**；而 `stage3_main` 与这个模块本来就
    在顶层互相认识（同一层的派生 key：媒体标记 ↔ 媒体渲染），放这里"谁都不用动"。
    插件那个文件夹（`plugins/vision/`）里不再有它，用它的两处测试直接 import 这里，
    **对外接口一个字没变**（只是换了个家）。
    """

    if not description:
        return text
    for label in (IMAGE_LABEL, STICKER_LABEL):
        if label in text:
            return text.replace(label, f"{label[:-1]}：{description}]", 1)
    return f"{text}[{IMAGE_LABEL[1:-1]}：{description}]" if text else f"{IMAGE_LABEL[:-1]}：{description}]"
