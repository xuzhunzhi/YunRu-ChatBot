"""控制面板插件（**后台插件**）。

按配置装配本地/远程两套接入，然后交给和其它后台通道同一条节拍看护。装配细节在
`wire.py`，HTTP 与前端在 `webui_panel.py` / `webui_access.py` / `webui_data.py`。

**它跑在 bot 进程里**（用户 2026-10-01："面板属于 stage4 内容，本质插件"），
但**拿不到 `transport`、拿不到 `engine`**：只有装配点注入的一串闭包。
"""
from __future__ import annotations

from .wire import build_web_panel


def register(registry) -> None:
    plugin = build_web_panel(registry)
    registry.background(plugin)
