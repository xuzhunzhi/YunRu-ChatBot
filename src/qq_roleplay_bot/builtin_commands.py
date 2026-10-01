"""内置的普通用户命令插件。

`/help` 是第一个插件，也是命令插件接口的验证样本。它本身没有真实功能依赖，
但覆盖了接口的全部要点：多别名匹配、会话范围、自述帮助、动态汇总。
"""
from __future__ import annotations

import re

from .transport import IncomingMessage, MessageTarget

# 帮助的写法：**斜杠和 yunru 都可以有、也可以没有**。
# 用户 2026-09-28：「我总记不得到底要不要加 yunru」——所以两种都收：
#   /help、#help、/ yunru、/yunru
#   yunru help、/yunru help、#yunru 帮助、yunru 帮助
# 只保留一条底线：**裸 `help` 不算命令**（"help me"、"请帮助我" 这类是聊天，
# 认了等于把日常对话吞掉）。
HELP_COMMAND_PATTERN = re.compile(
    r"^[/#]\s*(?:help|yunru)$"
    r"|^[/#]?\s*yunru\s+(?:help|帮助)$",
    re.IGNORECASE,
)

# 帮助正文的头尾固定部分。插件自述的行会插在尾段之前，
# 顺序与注册顺序一致——加了新命令不需要改这里。
HELP_HEADER = (
    "云茹 · 使用说明\n"
    "· 群里 @我、直接叫我名字、或回复我说过的话，我都会认真看。\n"
    "· 群里的其他对话我也在听，但那不代表每句都要我接——插不上的时候我就不出声。\n"
    "· 私聊我，我会当成一对一说话。\n"
    "· 只发一张图或表情给我看也行。\n"
    "· 我说话简短，不刷屏；不想聊了直接说。\n"
)

HELP_FOOTER = (
    "\n"
    "管理与超管命令\n"
    "· 管理员命令菜单：/admin help（谁都能看，菜单里的命令要管理权限才能执行）。\n"
    "· 想执行菜单里的某一条：先把它发出来，再请超管引用你那条消息发 /super permit，"
    "他会替你执行这一条（只执行一次）。\n"
    "· 超管命令用 /super help 查看，只有超级管理员能用。\n"
)

# 公开帮助画成卡片时的标题（见 `help_card.py`）。
HELP_CARD_TITLE = "云茹 · 使用说明"


def build_help_text(plugin_help_lines: tuple[str, ...] = ()) -> str:
    """把各插件自述的帮助汇总成一份公开帮助。"""

    middle = "".join(f"{line.rstrip()}\n" for line in plugin_help_lines if line.strip())
    if middle:
        middle = "\n" + middle
    return HELP_HEADER + middle + HELP_FOOTER


class HelpCommand:
    """列出可用命令。

    公开命令：**任何会话都能用**，包括未启用群和私聊，也不受 enabled 开关影响
    （旧实现就是这个语义，测试里有断言）。

    `help_provider` 由注册表注入：帮助正文要汇总**所有**插件的自述，包括自己，
    所以它不能由 HelpCommand 自己在构造时固化下来。
    """

    name = "help"

    def __init__(self, help_provider=None) -> None:
        self._help_provider = help_provider

    def match(self, message) -> bool:
        return isinstance(message.text, str) and bool(
            HELP_COMMAND_PATTERN.fullmatch(message.text.strip())
        )

    def session_allowed(self, target: MessageTarget) -> bool:
        # 公开命令不挑会话。
        return True

    async def handle(self, message: IncomingMessage):
        """帮忙正文汇总成一份；**交给核心决定用图片还是文字发**。

        用户 2026-09-30："后面命令的 help 就用图片展示了"。这里返回 `ImageReply`
        不等于插件在发图——它只是声明"这份内容适合画成卡片"，画与发都在核心，
        渲染失败时核心会退回 `text`。
        """

        lines: tuple[str, ...] = ()
        if callable(self._help_provider):
            try:
                lines = tuple(self._help_provider())
            except Exception:  # noqa: BLE001 - 帮助生成失败不能反过来打断命令
                lines = ()
        text = build_help_text(lines)
        from .command_plugins import ImageReply

        return ImageReply(title=HELP_CARD_TITLE, text=text)

    def help_text(self) -> str:
        # 帮助本身的用法由头段承担，不在命令清单里重复列出自己。
        return ""


# ping 的匹配规则：**只保留两种写法**。
# 1. `/yunru ping`
# 2. @ 她 + `ping`（即 `@YunRu ping`；NapCat 会把 at 段摘掉，所以看 is_bot_mentioned）
# 从前那套"裸 ping、/ping、!ping、ping?、yunru 在吗、云茹 ping"全都去掉：
# 它们既容易误吞聊天里的 ping（例如"ping 服务器"），也让命令面看起来比实际复杂。
PING_TEXT_PATTERN = re.compile(r"^/yunru\s+ping\s*$", re.IGNORECASE)
MENTION_PING_PATTERN = re.compile(r"^ping\s*$", re.IGNORECASE)


def is_ping_command(message) -> bool:
    """这条消息是不是一次连通性自检。命令插件与核心共用这一个判断。"""

    text = getattr(message, "text", "") or ""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if PING_TEXT_PATTERN.fullmatch(stripped):
        return True
    return bool(getattr(message, "is_bot_mentioned", False)) and bool(
        MENTION_PING_PATTERN.fullmatch(stripped)
    )


class PingCommand:
    """连通性自检。

    用户已确认这算**基础功能**，所以它是唯一一个不打算移除的核心命令；
    但仍然以插件形式接入，这样才能验证接口对"有真实行为"的命令也够用
    （ping 的特点：不调模型、不读写记忆、任何会话都可用、无视 enabled 开关）。
    """

    name = "ping"

    def __init__(self, reply: str = "pong") -> None:
        self.reply = reply

    def match(self, message) -> bool:
        return is_ping_command(message)

    def session_allowed(self, target: MessageTarget) -> bool:
        # 健康检查要在任何会话、任何开关状态下都能用。
        return True

    async def handle(self, message: IncomingMessage) -> str | None:
        return self.reply

    def help_text(self) -> str:
        return "连通测试\n· /yunru ping（也可以直接 @我 发一句 ping）→ 我回 pong。"


def default_command_plugins() -> tuple[object, ...]:
    """当前内置的普通用户命令。新增功能应当以插件形式加到这里。"""

    return (HelpCommand(), PingCommand())


def build_command_registry(extra: tuple[object, ...] = ()):
    """组装命令注册表，并把"汇总帮助"的能力回注给 HelpCommand。

    ping 排在 help 之前：它更具体，且行为最简单，先匹配掉可以少走一层。
    余额命令放在最后，且默认只在超管私聊可用——它读的是账号资金信息。
    最后是 Stage 4 的两条：群管理与群主命令（`min_level = "super"`，
    见 `builtin_group_commands.py`——它们只解析与声明意图，执行在核心）。
    """

    from . import dev_config
    from .builtin_balance_command import BalanceCommand, build_balance_client
    from .builtin_group_commands import GroupManageCommand, GroupOwnerCommand, TitleCommand
    from .command_plugins import CommandRegistry

    help_plugin = HelpCommand()
    ping_plugin = PingCommand()
    balance_plugin = BalanceCommand(
        build_balance_client(
            dev_config.API_BASE_URL,
            dev_config.API_KEY,
            cache_seconds=dev_config.BALANCE_CACHE_SECONDS,
        ),
        allowed_user_ids=dev_config.BALANCE_PRIVATE_USER_IDS,
        allowed_group_ids=dev_config.BALANCE_GROUP_IDS,
    )
    plugins = (ping_plugin, help_plugin, balance_plugin, TitleCommand(),
               GroupManageCommand(), GroupOwnerCommand()) + tuple(extra)
    registry = CommandRegistry(plugins)
    help_plugin._help_provider = registry.help_lines
    return registry
