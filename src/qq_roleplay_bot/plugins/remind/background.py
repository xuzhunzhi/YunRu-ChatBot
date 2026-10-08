"""到点那条**后台节拍**：核心每隔一会儿叫一次 `poll_once()`。

形状是 `background_plugins.BackgroundPlugin`（`name` / `interval_seconds` / `poll_once()`）：
它接着的是**唯一那一份节拍循环**（`run_background_plugin`），本插件不新开循环、
也不碰 `runtime.serve`。

`poll_once()` **不许抛出**（那是那套节拍的约定）；这里自己兜一层，
一轮出错只记一行，下一次照跑——一条提醒发不出去不该让整条通道停下来。
"""
from __future__ import annotations

import logging

from .remind import POLL_SECONDS

logger = logging.getLogger(__name__)


class RemindPoller:
    """到点了就把它挑出来交出去。**发不由这里做**：交给核心的发送口。"""

    #: 节拍循环按这个名字记日志（`background_plugins.run_background_plugin`）。
    name = "remind"
    enabled = True

    def __init__(self, capability, *, interval_seconds: float = POLL_SECONDS) -> None:
        self._capability = capability
        self.interval_seconds = float(interval_seconds)

    async def poll_once(self) -> object:
        """跑一轮：`flush()` 自己会判"该问的问了没有 / 到点了没有 / 试够次数没有"。

        返回值是这一轮真交出去的内容/意图（核心那份节拍现在不读它，
        所以**不能**靠返回值投递——投递走的是接缝里那条发送口）。
        """

        try:
            return await self._capability.flush()
        except Exception:  # noqa: BLE001 - 一轮失败不停下这条通道
            logger.exception("remind_poll_failed（这一轮跳过，下一次照跑）")
            return ()

    async def close(self) -> None:
        """收尾：没有要收的东西（数据是追加写的，每一条写完就落盘了）。"""

        return None
