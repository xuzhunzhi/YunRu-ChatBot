"""把帮助文本渲染成一张图（用户 2026-09-30："后面命令的 help 就用图片展示了"）。

为什么不直接把文字发出去：帮助是一屏长得多的清单，群里刷一屏文字很吵；图片是**一条**
消息，翻上去也好看。

设计取舍：

1. **Pillow 是可选依赖**：`import PIL` 失败、字体找不到、渲染抛异常 —— 一律返回空字节，
   调用方（引擎）原样退回文字。**功能缺失绝不能变成"help 用不了了"**。
   项目其余部分不依赖 Pillow（`pyproject.toml` 没加），装不装都能跑。
2. **字体**：中文要 `msyh.ttc`（微软雅黑），Windows 自带的等宽字体（Consolas）
   **没有中文字形**，会渲染成豆腐块——这是画原型时就踩到的。
3. **按像素宽度折行**，不是按字符数：中英混排按字符数折会一边空一边挤。
4. **渲染结果按内容缓存**：同一份帮助反复请求（群里刷 `/help`）不该反复绘图。

布局刻意做得很素：深色标题条 + 浅底正文，小节标题加粗，条目用小圆点。
不做花哨装饰——这是一张说明卡，不是海报。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 卡片宽度：手机上看得清，PC 上也不至于占满屏。
DEFAULT_WIDTH = 880
# 内边距与行距（都是像素）。
PADDING = 40
TITLE_SIZE = 36
HEADING_SIZE = 25
BODY_SIZE = 23
LINE_GAP = 14

# 配色：浅底 + 深灰字 + 一个不抢眼的标题条。
BG = (247, 248, 250)
HEADER_BG = (47, 59, 82)
TITLE_FG = (255, 255, 255)
HEADING_FG = (47, 59, 82)
BODY_FG = (35, 39, 47)
BULLET_FG = (120, 132, 150)
RULE = (219, 223, 230)
FOOTER_FG = (150, 158, 170)

# 字体候选：按优先级找第一个存在的。`msyhbd` 是雅黑粗体，用来画标题与小节名。
_FONT_FILES = {
    "body": ("msyh.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc"),
    "bold": ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "Deng.ttf"),
}
_FONT_DIRS = (Path(r"C:\Windows\Fonts"), Path("/usr/share/fonts"), Path("/System/Library/Fonts"))

_CACHE: dict[tuple, bytes] = {}
_CACHE_LIMIT = 8


def available() -> bool:
    """Pillow 装没装。**只用来提前判断**，真渲染失败照样走兜底。"""

    try:
        import importlib.util

        return importlib.util.find_spec("PIL") is not None
    except Exception:  # noqa: BLE001 - 任何导入期异常都算"没有"
        return False


def _font_path(kind: str) -> str | None:
    for directory in _FONT_DIRS:
        for name in _FONT_FILES[kind]:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _load_font(size: int, kind: str = "body"):
    from PIL import ImageFont

    path = _font_path(kind)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 - 字体坏了就退回默认
            logger.warning("help_card_font_failed path=%s", Path(path).name)
    return ImageFont.load_default(size)


def _width(font, text: str) -> float:
    try:
        return font.getlength(text)
    except Exception:  # noqa: BLE001 - 老版本 Pillow 没有 getlength
        return len(text) * (font.size if hasattr(font, "size") else 12)


# 这些符号不该被甩到下一行行首（收尾标点独占一行很难看）。
_CLOSERS = "）】」》”’、。，；：！？) ] } > %"
# 往回找断点时的最大回退字符数：退太远会让上一行短得离谱。
_BREAK_LOOKBACK = 14


def _break_index(current: str) -> int:
    """从末尾往前找一个"自然的"断点：空格优先，其次是 ASCII 词的边界。

    为什么要这个：按字符硬折会把 `GPU` 折成 `GP` + `U`，上一版实测就是这样。
    找不到（整行都是中文）就返回 0，交给调用方按字符断开——中文本来哪都能断。
    """

    lowest = max(0, len(current) - _BREAK_LOOKBACK)
    for index in range(len(current) - 1, lowest - 1, -1):
        if current[index] == " ":
            return index + 1
    for index in range(len(current) - 1, lowest, -1):
        if current[index].isascii() and current[index].isalnum() and not (
                current[index - 1].isascii() and current[index - 1].isalnum()):
            return index
    return 0


def _wrap(text: str, font, limit: int) -> list[str]:
    """按**像素**折行，并尽量不把英文单词与收尾标点拆开。"""

    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        if not current or _width(font, current + char) <= limit:
            current += char
            continue
        # 收尾标点：宁可略微超宽，也不让它独占下一行。
        if char in _CLOSERS and _width(font, current + char) <= limit * 1.1:
            current += char
            continue
        cut = _break_index(current)
        if cut:
            lines.append(current[:cut].rstrip())
            current = current[cut:].lstrip() + char
        else:
            lines.append(current.rstrip())
            current = char
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _classify(line: str) -> str:
    """一行是小节标题、条目，还是普通正文。

    判据很朴素：以 `·` 或 `-` 开头的是条目；没有标点且不超过 14 个字的是小节标题。
    帮助文本本来就长这样（见 `memory_view.SUPER_HELP` / `builtin_commands.HELP_HEADER`）。
    """

    stripped = line.strip()
    if not stripped:
        return "blank"
    if stripped[0] in "·-•*":
        return "bullet"
    if len(stripped) <= 14 and not any(mark in stripped for mark in "。；，,.;"):
        return "heading"
    return "body"


def render(title: str, text: str, *, width: int = DEFAULT_WIDTH) -> bytes:
    """把 `text` 画成一张 PNG，返回字节；**失败返回空字节**（调用方退回文字）。"""

    key = (title, text, int(width))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        png = _render(title, text, int(width))
    except Exception as exc:  # noqa: BLE001 - 画不出来绝不能影响 help 本身
        logger.warning("help_card_render_failed category=%s", type(exc).__name__)
        return b""
    if png:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = png
    return png


def _render(title: str, text: str, width: int) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    width = max(420, width)
    body_font = _load_font(BODY_SIZE, "body")
    heading_font = _load_font(HEADING_SIZE, "bold")
    title_font = _load_font(TITLE_SIZE, "bold")

    # --- 先排版、算出总高度（Pillow 不能动态扩画布）---
    blocks: list[tuple[str, list[str]]] = []
    text_width = width - PADDING * 2
    for raw in (text or "").splitlines():
        kind = _classify(raw)
        if kind == "blank":
            blocks.append((kind, [""]))
            continue
        content = raw.strip()
        if kind == "bullet":
            content = content[1:].strip()
            lines = _wrap(content, body_font, text_width - 26)
        elif kind == "heading":
            lines = _wrap(content, heading_font, text_width)
        else:
            lines = _wrap(content, body_font, text_width)
        blocks.append((kind, lines))

    header_height = TITLE_SIZE + PADDING + 18
    height = header_height + PADDING
    for kind, lines in blocks:
        if kind == "blank":
            height += LINE_GAP
        elif kind == "heading":
            height += LINE_GAP + len(lines) * (HEADING_SIZE + LINE_GAP)
        else:
            height += len(lines) * (BODY_SIZE + LINE_GAP)
    height += PADDING + 30  # 页脚

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    # --- 标题条 ---
    draw.rectangle([0, 0, width, header_height], fill=HEADER_BG)
    draw.text((PADDING, 20), title, font=title_font, fill=TITLE_FG)

    # --- 正文 ---
    y = header_height + PADDING // 2
    for kind, lines in blocks:
        if kind == "blank":
            y += LINE_GAP
            continue
        font = heading_font if kind == "heading" else body_font
        color = HEADING_FG if kind == "heading" else BODY_FG
        if kind == "heading":
            y += LINE_GAP
            draw.line([PADDING, y - 10, width - PADDING, y - 10], fill=RULE, width=1)
            y += 6
        for index, line in enumerate(lines):
            if kind == "bullet" and index == 0:
                draw.ellipse([PADDING + 4, y + BODY_SIZE // 2 - 3,
                              PADDING + 10, y + BODY_SIZE // 2 + 3], fill=BULLET_FG)
            x = PADDING + (26 if kind == "bullet" else 0)
            draw.text((x, y), line, font=font, fill=color)
            y += font.size + LINE_GAP
    draw.text((PADDING, height - PADDING + 4), "云茹 · Yunru", font=_load_font(18), fill=FOOTER_FG)

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def clear_cache() -> None:
    """测试用：缓存是按内容存字节的，跑测试时清一下免得互相影响。"""

    _CACHE.clear()
