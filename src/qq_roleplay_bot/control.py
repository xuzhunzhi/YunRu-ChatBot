"""控制端口：未来 WebUI/API 适配引擎时用的最小契约。

## 这里只有类型，没有实现

- `EngineSnapshot` / `SessionSnapshot` 是**引擎自己的形状**，定义在 `snapshots.py`；
  这里 re-export 一份，方便控制面按同一份形状读（单一源头仍在 `snapshots.py`）。
- `EngineControl` 是那个最小控制端口的 Protocol。

**实现在引擎那边**（`stage3_main.DialogueEngine`）。这个文件零依赖、不 import 引擎，
所以 `stage3_main` 引用它不会成环，反过来也不会。

> 注意：WebUI 面板是 **stage4 插件**，它拿不到引擎，只拿得到装配点注入的**闭包**。
> 这个 Protocol 描述的是"引擎对外能做什么"，面板用到的那几项由装配点逐个包成函数。
"""
from __future__ import annotations

from typing import Protocol

from .snapshots import EngineSnapshot, SessionSnapshot

__all__ = ["EngineSnapshot", "SessionSnapshot", "EngineControl"]


class EngineControl(Protocol):
    """未来 WebUI/API 可适配的最小控制端口。

    实际 WebUI 必须另行实现认证、CSRF 防护和本机/内网访问策略；这个端口本身不是认证层。
    """

    def snapshot(self) -> EngineSnapshot:
        """读取不含密钥和完整聊天历史的运行状态。"""

    def set_enabled(self, enabled: bool) -> None:
        """启停消息处理；不改变白名单或安全策略。"""

    def leave_session(self, session_id: str) -> bool:
        """清除指定短期会话并返回它是否存在。"""

    def enable_group(self, group_id: str) -> bool:
        """启用指定群聊；返回它是否由未启用变为启用。"""

    def disable_group(self, group_id: str) -> bool:
        """停用指定群聊；返回它是否由已启用变为停用。"""
