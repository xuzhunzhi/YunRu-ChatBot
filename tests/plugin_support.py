"""测试用的插件装配辅助。

**为什么需要它**：命令与后台现在都从 `plugins/` 里发现（`build_engine` 只跑一次
`discover()`），所以 `builtin_commands.build_command_registry()` 拿到的是
**只有核心命令**的注册表——群管理那几条不在上面。测试想验"插件认不认这条命令"，
就必须走真实的装配路径，而不是自己拼一份。

这里的 `wired_command_registry()` 就干这件事：造一个带窄接缝的 `PluginRegistry`，
调生产用的 `attach_plugins()`，把命令链原样递给被测代码。它不复制任何生产逻辑
（不自己拼命令列表、不自己造角色缓存），所以装配线改了这里会跟着改。
"""
from __future__ import annotations

from qq_roleplay_bot.onebot_client import unwrap_result
from qq_roleplay_bot.plugins import PluginRegistry, attach_plugins


async def no_action(action, params=None):
    """什么都不做的 `call_action`：只给"装配成功"用，真调用了会返回 None。

    角色查询经它拿到 None → 角色判成 `unknown`（fail-closed），
    想验"她是不是群主"的测试自己注入 `roles`。
    """

    return None


def plugin_registry(*, call_action=no_action, notify=None, roles=None):
    """造一个带窄接缝的插件注册表（还没发现插件）。"""

    return PluginRegistry(call_action=call_action, notify=notify, roles=roles)


def wired_command_registry(registry=None, **kwargs):
    """接上插件之后的命令注册表——和 `build_engine` 里给引擎的那份同一条路径。"""

    return attach_plugins(registry if registry is not None else plugin_registry(**kwargs))


def transport_seam(transport):
    """模仿核心注入的 `call_action`（`runtime._plugin_action_seams` 的读那半边）。

    过闸门那部分由 `CapabilityRegistry` 自己在生产路径里做；测试里要的是
    "回执被拆成 data"这一条——插件不懂协议形状，就靠它。
    """

    async def call_action(action, params=None):
        return unwrap_result(await transport.call_api(action, params or {}))

    return call_action
