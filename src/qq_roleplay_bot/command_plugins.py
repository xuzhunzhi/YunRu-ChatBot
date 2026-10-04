"""普通用户命令的插件接口与注册表。

背景：命令曾经是 `stage3_main.py` 里一排硬编码的 `if is_xxx_command(...)`。
每加一个功能就要动核心分发文件，而且帮助文字、权限判断、去重都散在同一个
函数里。这里把"一个命令"抽成可注册的插件。

设计边界（有意留窄）：

1. **插件不发送消息。** 它只返回文本，由核心统一走既有的发送路径。
   这样速率限制、引用校验、分段节奏、脱敏都绕不过去；插件拿不到 transport，
   也就不能把消息发到别的会话去。
2. **插件不判断身份。** 它只声明"需要什么级别的会话"，由核心按其权限规则判定。
   插件拿不到 admin_user_ids，也无法自行放行。
3. **命令回复不参与拟人化节奏。** 工具性响应要立刻送出，等一秒反而显得迟钝。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .transport import IncomingMessage, MessageTarget

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImageReply:
    """插件希望这条回复**以图片形式发出**（2026-09-30 用户："help 就用图片展示"）。

    边界没变：插件仍然**不发送任何东西、也拿不到 transport**——它只说明"这条回复适合
    画成卡片"，附上标题与正文。要不要画、画多大、发不出去怎么办，全在核心
    （`help_card.render` + 引擎的 `image_sender` 接缝）。渲染失败或没装 Pillow 时，
    核心直接发 `text`，所以插件的调用方永远拿得到一份可读的帮助。
    """

    title: str
    text: str


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """插件声明"想做一个动作"，**执行在核心**（2026-09-30 用户要求 Stage 4 走插件）。

    由来：用户指出"我之前说过 stage4 的内容都用插件实现，你好像直接加到 super 的底层里去了"。
    群管理与群主动作要真去调 SnowLuma action（踢人、禁言、设管理员……），而插件
    **拿不到 transport**；把 transport 交给插件就毁掉了"插件不发送消息"这条边界。
    所以插件只负责**把命令文字解析成意图**（`group`/`kind`/目标/参数），
    剩下全在核心：权限判定、她是不是群主、动作白名单、护栏、审计——`group_admin.py`
    与 `group_owner.py` 一行都不用搬。

    `group` 目前有两条：`"group_admin"`（禁言/踢/撤回/全员禁言）与
    `"group_owner"`（设管理员/群名片/群名/头衔/群公告）。
    """

    group: str
    kind: str
    target_id: str = ""
    text: str = ""
    mentioned: bool = False
    message_id: str = ""


# 插件可以返回的四种东西：正文、图片请求、动作意图，或者 None（不处理）。
PluginReply = str | ImageReply | ActionRequest

# 插件档位：public（谁都能用）/ admin（管理员）/ super（超管）。
# **判定不在插件里**：注册表只把档位交出去，由核心按 `_is_admin_control()` /
# `_is_super_admin_control()` 判——把身份判定下放给插件会破坏 fail-closed。
LEVELS = ("public", "admin", "super")


class CommandPlugin(Protocol):
    """一个普通用户命令。"""

    name: str

    # 需要的档位；不声明按 "public"。`stage4` 的群管理/群主命令声明 "super"。
    min_level: str

    def match(self, message: IncomingMessage) -> bool:
        """这条消息是不是本命令。只做匹配，不做鉴权。

        拿整条消息而不是只拿正文：命令可能要看 @ 了谁（`/super addadmin @某人`）、
        或者"@ 我 + 某几个字"这种形态（`@云茹 ping`）。正文仍然在 `message.text` 里。
        """

    def session_allowed(self, target: MessageTarget) -> bool:
        """本命令是否允许在该会话生效。

        默认实现应当对所有会话返回 True：公开命令（如帮助、ping）**不受群白名单
        限制**——这个语义从旧实现继承而来，未启用群里也应该能查帮助。
        需要限制范围的命令自行收窄即可。
        """

    async def handle(self, message: IncomingMessage) -> PluginReply | None:
        """产生回复；返回 None 表示不处理（交回普通对话流程）。

        - 返回 `ImageReply`：声明"这份内容适合画成卡片"，由核心决定发图还是发字；
        - 返回 `ActionRequest`：声明"想做一个动作"，由核心判权限、查角色、走闸门、记审计、
          真去调 action——插件**不碰 transport**。
        """

    def help_text(self) -> str:
        """本命令在帮助里的说明，一行或几行。返回空串表示不列出。"""


def level_of(plugin: object) -> str:
    """插件的档位。没声明或不认识的值一律按 `"public"`——**不认识的档位不放宽**，
    因为放不放行由核心按档位判，而 "public" 只是"不需要特权"，真正的会话范围仍由
    `session_allowed` 决定。
    """

    value = str(getattr(plugin, "min_level", "") or "").strip().casefold()
    return value if value in LEVELS else "public"


class CommandRegistry:
    """按注册顺序匹配命令；第一个命中的插件负责这条消息。"""

    def __init__(self, plugins: tuple[CommandPlugin, ...] = ()) -> None:
        self.plugins: tuple[CommandPlugin, ...] = tuple(plugins)

    def add(self, plugin: object) -> None:
        """把一条插件挂到链尾。

        命令链**只能有一份**（`resolve()` 按注册顺序取第一个命中的），
        所以插件是往这里加，而不是各攒一份列表再拼——拼的时候漏一处就是
        "命令装上了、链上却没有"。挂到链尾 = 优先级低于核心自己的命令
        （`ping` / `help` 先匹配），这正是我们要的：特权命令不与它们抢。

        **它是装载点的落点**：`plugins.PluginRegistry.command()` / `install()` 都调
        这个方法（装配见 `runtime.build_engine`）。2026-10-04 之前本体没有它，
        于是插件的命令无处可挂；这一条是从插件线 `stage4-plugins` 原样搬来的。
        """

        if plugin is not None:
            self.plugins = self.plugins + (plugin,)

    def resolve(self, message: IncomingMessage) -> CommandPlugin | None:
        """**只匹配、不执行**：返回认领这条消息的插件（或其档位不允许而回落时 None）。

        为什么要拆成两步（2026-09-30 用户要求 Stage 4 走插件）：权限判定必须留在核心。
        核心先 `resolve()` 拿到插件与 `level_of(plugin)`，自己判身份，再 `run()` 执行——
        插件从头到尾拿不到 `admin_user_ids`，也没法自行放行（fail-closed 的语义不变）。
        """

        for plugin in self.plugins:
            if not _matches(plugin, message):
                continue
            if not _session_allowed(plugin, message.target):
                return None
            return plugin
        return None

    async def run(self, plugin: CommandPlugin, message: IncomingMessage) -> PluginReply | None:
        """执行一个已经解析过的插件（含异常兜底）。"""

        try:
            return await plugin.handle(message)
        except Exception:  # noqa: BLE001
            logger.exception("command plugin failed: name=%s", _name_of(plugin))
            return None

    async def dispatch(self, message: IncomingMessage) -> tuple[str, PluginReply] | None:
        """尝试用插件处理这条消息（**不判档位**，给不需要权限的调用方与测试用）。

        核心的 `handle()` **不走这里**：它用 `resolve()` + 自己的档位判定 + `run()`，
        否则 `min_level` 就形同虚设。返回 `(插件名, 回复)`；返回 None 有三种情况，
        且对调用方无差别：

        - 没有插件认领这条消息（继续走普通对话）；
        - 插件认领了，但会话不被允许——**静默回落**，不回复也不报错，
          避免通过"有没有反应"探测出命令的存在与权限范围；
        - 插件处理时抛异常（记日志后回落，不能让坏插件吃掉消息）。
        """

        plugin = self.resolve(message)
        if plugin is None:
            return None
        reply = await self.run(plugin, message)
        if reply is None:
            return None
        return _name_of(plugin), reply

    def help_lines(self) -> tuple[str, ...]:
        """汇总所有插件自述的帮助，用于动态生成公开帮助。"""

        lines: list[str] = []
        for plugin in self.plugins:
            text = (plugin.help_text() or "").strip()
            if text:
                lines.append(text)
        return tuple(lines)


def _name_of(plugin: CommandPlugin) -> str:
    return str(getattr(plugin, "name", "") or type(plugin).__name__)


def _matches(plugin: CommandPlugin, message: IncomingMessage) -> bool:
    """插件的 match 抛异常时视为不匹配，不能让一个坏插件吃掉所有消息。"""

    try:
        return bool(plugin.match(message))
    except Exception:  # noqa: BLE001
        return False


def _session_allowed(plugin: CommandPlugin, target: MessageTarget) -> bool:
    """未实现 session_allowed 的插件按"任何会话都允许"处理（公开命令语义）。"""

    checker = getattr(plugin, "session_allowed", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(target))
    except Exception:  # noqa: BLE001
        return False
