"""帮助改图片：渲染卡片、走核心发图、失败退回文字。

由来（2026-09-30 用户）："后面命令的 help 就用图片展示了"，随后明确"改图片是肯定要的"。

三件事在这里钉住：

1. **渲染本身**：Pillow 画得出来、画的是 PNG、按内容缓存（同一份帮助不重复绘图）；
   Pillow 缺失或字体坏掉时返回空字节，绝不抛出去。
2. **插件边界没破**：`HelpCommand.handle()` 返回的是 `ImageReply`（标题 + 正文），
   它**没有**发送能力；画与发都在核心，`/help` 仍然由核心统一送出。
3. **降级路径**：没接图片通道 / 渲染失败 / 发送抛异常 → 帮助变成文字照常发出。
   图片是锦上添花，不是单点故障。
"""
import asyncio

from qq_roleplay_bot.builtin_commands import HELP_CARD_TITLE, HelpCommand, build_help_text
from qq_roleplay_bot.command_plugins import CommandRegistry, ImageReply
from qq_roleplay_bot.memory_view import ADMIN_HELP, SUPER_HELP
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ME = "900000001"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class NeverCalled:
    async def complete(self, request):  # pragma: no cover - 命令不该走到模型
        raise AssertionError("命令不该调用模型")


def msg(text: str) -> IncomingMessage:
    return IncomingMessage(message_id=f"m:{text}", session_id=f"group:{GROUP}", user_id=ME,
                           text=text, target=MessageTarget(group_id=GROUP))


# --- 渲染 -------------------------------------------------------------------

def test_render_produces_a_png() -> None:
    from qq_roleplay_bot import help_card

    if not help_card.available():
        return  # 没装 Pillow 的环境：跳过（降级路径另有测试）
    png = help_card.render("云茹 · 使用说明", build_help_text(()))
    assert png.startswith(PNG_MAGIC), "应当是 PNG"
    assert len(png) > 5000, "一张卡片不该只有几百字节"


def test_render_is_cached_by_content() -> None:
    from qq_roleplay_bot import help_card

    if not help_card.available():
        return
    help_card.clear_cache()
    first = help_card.render("标题", "正文一行")
    second = help_card.render("标题", "正文一行")
    assert first is second, "同内容应当直接命中缓存（返回同一个对象）"
    assert help_card.render("标题", "换了正文") is not first
    help_card.clear_cache()


def test_render_failure_returns_empty_bytes() -> None:
    """画不出来时返回空字节，**不抛异常**——调用方据此退回文字。"""

    from qq_roleplay_bot import help_card

    def boom(*args, **kwargs):
        raise RuntimeError("字体炸了")

    original = help_card._render
    help_card.clear_cache()
    help_card._render = boom  # type: ignore[assignment]
    try:
        assert help_card.render("标题", "正文") == b""
    finally:
        help_card._render = original  # type: ignore[assignment]
        help_card.clear_cache()


def test_long_lines_are_wrapped_without_splitting_latin_words() -> None:
    """按像素折行，且不把 `GPU` 拆成 `GP`+`U`（画原型时真出现过）。"""

    from qq_roleplay_bot.help_card import _load_font, _wrap

    font = _load_font(23)
    lines = _wrap("· /super processes memory|cpu|gpu|gpu-memory 内存 / 5 秒 CPU / GPU 3D / 显存 前 8",
                  font, 700)
    assert len(lines) > 1
    joined = "".join(lines)
    assert "GPU" in joined
    assert "GP" not in joined.replace("GPU", ""), "不该把 GPU 拆开"


# --- 插件仍然不发消息 --------------------------------------------------------

def test_help_plugin_returns_content_not_a_send() -> None:
    registry = CommandRegistry((HelpCommand(help_provider=lambda: ()),))
    result = asyncio.run(registry.dispatch(msg("/help")))
    assert result is not None
    name, reply = result
    assert name == "help"
    assert isinstance(reply, ImageReply), "插件只交内容，发不发图由核心定"
    assert reply.title == HELP_CARD_TITLE
    assert "使用说明" in reply.text


# --- 核心：发图 / 降级 --------------------------------------------------------

class Sender:
    def __init__(self, ok: bool = True, explode: bool = False):
        self.ok = ok
        self.explode = explode
        self.sent: list[bytes] = []

    async def __call__(self, target, png: bytes) -> bool:
        if self.explode:
            raise RuntimeError("通道炸了")
        self.sent.append(png)
        return self.ok


def _engine(**kwargs) -> DialogueEngine:
    return DialogueEngine(NeverCalled(), **kwargs)


def test_help_is_sent_as_a_card_when_the_channel_exists() -> None:
    from qq_roleplay_bot import help_card

    if not help_card.available():
        return
    engine = _engine()
    sender = Sender()
    engine.image_sender = sender
    result = asyncio.run(engine.handle(msg("/help")))
    assert result is None, "图片已经发出去，不该再补一条文字"
    assert len(sender.sent) == 1 and sender.sent[0].startswith(PNG_MAGIC)


def test_admin_and_super_help_are_cards_too() -> None:
    from qq_roleplay_bot import help_card

    if not help_card.available():
        return
    engine = _engine(super_admin_user_ids=frozenset({ME}))
    sender = Sender()
    engine.image_sender = sender
    assert asyncio.run(engine.handle(msg("/admin help"))) is None
    assert asyncio.run(engine.handle(msg("/super help"))) is None
    assert len(sender.sent) == 2


def test_help_falls_back_to_text_without_a_channel() -> None:
    """没接图片通道（测试、别的传输层）→ 照旧发文字。"""

    engine = _engine(super_admin_user_ids=frozenset({ME}))
    result = asyncio.run(engine.handle(msg("/help")))
    assert result is not None and "使用说明" in result.text
    result = asyncio.run(engine.handle(msg("/admin help")))
    assert result is not None and "管理员命令菜单" in result.text
    result = asyncio.run(engine.handle(msg("/super help")))
    assert result is not None and "超管命令" in result.text


def test_help_falls_back_when_sending_fails() -> None:
    from qq_roleplay_bot import help_card

    if not help_card.available():
        return
    for sender in (Sender(ok=False), Sender(explode=True)):
        engine = _engine(super_admin_user_ids=frozenset({ME}))
        engine.image_sender = sender
        result = asyncio.run(engine.handle(msg("/help")))
        assert result is not None, "发图失败必须退回文字"
        assert "使用说明" in result.text


def test_help_falls_back_when_rendering_is_broken() -> None:
    from qq_roleplay_bot import help_card

    original = help_card.render
    help_card.render = lambda *a, **k: b""  # type: ignore[assignment]
    try:
        engine = _engine()
        sender = Sender()
        engine.image_sender = sender
        result = asyncio.run(engine.handle(msg("/help")))
        assert result is not None and "使用说明" in result.text
        assert sender.sent == []
    finally:
        help_card.render = original  # type: ignore[assignment]


def test_help_image_can_be_turned_off() -> None:
    """`QQBOT_HELP_IMAGE=0`：一个字都不画，直接发文字。"""

    from qq_roleplay_bot import dev_config, help_card

    if not help_card.available():
        return
    engine = _engine()
    sender = Sender()
    engine.image_sender = sender
    original = dev_config.HELP_IMAGE
    dev_config.HELP_IMAGE = False
    try:
        result = asyncio.run(engine.handle(msg("/help")))
        assert result is not None and "使用说明" in result.text
        assert sender.sent == []
    finally:
        dev_config.HELP_IMAGE = original


def test_cards_carry_the_real_text() -> None:
    """卡片里的内容就是原来那份文字——换展示形式不该丢内容。"""

    from qq_roleplay_bot import help_card
    from qq_roleplay_bot.help_card import _classify

    assert _classify("· /super kick @某人 移出群聊") == "bullet"
    assert _classify("记忆查看") == "heading"
    assert _classify("超管命令是全局的：在哪个群、私聊都能用。") == "body"
    assert _classify("") == "blank"
    if not help_card.available():
        return
    png = help_card.render("云茹 · 超管命令", SUPER_HELP)
    assert png and len(SUPER_HELP) > 200
    assert len(help_card.render("云茹 · 管理员命令", ADMIN_HELP)) > 1000
