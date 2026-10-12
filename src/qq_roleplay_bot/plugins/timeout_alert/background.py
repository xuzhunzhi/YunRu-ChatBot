"""后台节拍：每隔一会儿看一次快照，把"要不要写一封信"交给 `alert.py`。

这个类只做三件小事：**看**（拿只读快照）、**数**（数超时）、**判**（问 `TimeoutWatch`）。
措辞与发送在 `alert.py`、装配在 `plugin.py`——所以它既不知道 mail 插件长什么样，
也不知道 http 与 CLI 长什么样。
"""

from __future__ import annotations

import logging
import time

from . import alert as alert_module

logger = logging.getLogger(__name__)


class TimeoutAlertPlugin:
    """核心后台节拍认的形状：`name` / `interval_seconds` / `enabled` / `poll_once()`。

    照 `plugins/mail/background.py` 那套写：构造函数收**窄接缝**（`send` 是发信口、
    `snap` 是拿只读快照的口），不收引擎——插件拿不到引擎，也不该想着去拿。
    `clock` 可注入，只为让"什么时候会响"这件事在测试里能确定地走一遍。
    """

    name = "timeout-alert"

    def __init__(self, watch: alert_module.TimeoutWatch, *, send, snap,
                 interval_seconds: float, clock=time.time) -> None:
        self.watch = watch
        # 三个都是**公开**的：装配时由 `plugin.py` 填，测试里也直接换掉它们
        # （换假发信口、拨一下钟、换一份快照），比造一整套引擎便宜得多。
        self.sender = send
        self.snap = snap
        self.clock = clock
        self.interval_seconds = float(interval_seconds)
        # 两个口只要有一个不可用，这一路就不该跑（`plugin.py` 那边也拦了一道）。
        self.enabled = callable(send) and callable(snap)

    async def poll_once(self) -> object:
        """看一次、数一次、判一次。**不抛异常**（后台插件的形状约定）。"""

        if not self.enabled:
            return None
        try:
            snapshot = self.snap()
        except Exception as exc:  # noqa: BLE001 - 快照读不出来只记一行，不算插件坏
            logger.warning("timeout_alert_snapshot_failed error=%s",
                           type(exc).__name__, exc_info=True)
            return None
        total = alert_module.timeout_count(snapshot)
        if total is None:
            # 看不见就别猜（快照形状不对、核心没带指标，都会走到这里）。
            return None
        found = self.watch.observe(self.clock(), total)
        if found is None:
            return None
        if await alert_module.send_alert(self.sender, found):
            return found
        return None
