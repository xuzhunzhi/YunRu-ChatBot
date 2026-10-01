"""Stage 4 的后台插件：不是命令，是"隔一会儿干一件事"（用户 2026-09-30 要求）。

由来：用户先要求"stage4 的内容都用插件实现"，把群管理与群主命令搬进插件之后，
又指出"**入群审批也是插件，属于 stage4 内容**"。它确实不是命令——没人发一句什么，
它是自己按节拍去查有没有待处理的入群申请。所以插件接口要分两种：

| 种类 | 触发 | 长什么样 |
| --- | --- | --- |
| **命令插件**（`command_plugins.py`） | 一条消息进来 | `match()` / `handle()` → 文本 / `ImageReply` / `ActionRequest` |
| **后台插件**（这里） | 时间到了 | `name` / `interval_seconds` / `poll_once()`，可选 `close()` |

共同约束不变：**插件不直接持有 `transport`**。后台插件要动外部世界时，走核心注入的
两个窄接缝：

- `call_action(action, params)`：核心先过 `capabilities` 闸门（按用途），再调对面，
  并把回执拆成 `data` 返回；失败抛异常。插件因此不需要知道协议长什么样；
- `notify(target, text)`：核心统一发送（走补发队列）。

**`runtime.serve` 不再认识任何具体通道**：它只问 `build_background_plugins()` 拿到一串
插件，给每个插件跑同一个节拍循环。加一个新的后台能力 = 加一个插件，不改主循环。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

# 启动后第一轮之前等一会儿：对面（NapCat）常常比 bot 晚几秒连上来。
FIRST_TICK_DELAY_SECONDS = 20.0
# 节拍下限：防配置写个 1 秒把对面打爆。
MIN_INTERVAL_SECONDS = 15.0


class BackgroundPlugin(Protocol):
    """一个后台插件。"""

    name: str
    #: 每隔多少秒跑一次 `poll_once`。
    interval_seconds: float
    #: 关掉时核心不会为它起任务（装配阶段就能判断，不用等第一轮）。
    enabled: bool

    async def poll_once(self) -> object:
        """跑一轮。**不许抛出**（核心会兜一层，但自己处理掉更好记日志）。"""

    async def close(self) -> None:
        """可选：收尾。节拍任务被取消时由 `run_background_plugin` 调一次。

        2026-10-01 补：这个钩子原来只写在文档里、**没有任何地方调它**（模块头那句
        "可选 `close()`"）。面板插件要持有 HTTP 线程，收尾必须有个确定的落点，
        所以把它真正接上了：`run_background_plugin` 的 `finally` 里调。
        没有 `close()` 的插件照旧（其它几个不需要）。
        """


def plugin_enabled(plugin: object) -> bool:
    """插件是否启用。没声明 `enabled` 的按启用处理（它自己会在 poll_once 里判断）。"""

    return bool(getattr(plugin, "enabled", True))


async def run_background_plugin(plugin: BackgroundPlugin, *, first_delay: float | None = None,
                                min_interval: float = MIN_INTERVAL_SECONDS) -> None:
    """**唯一的后台节拍**：等一会儿 → 反复 `poll_once`，出错只记日志、不停。

    `runtime.serve` 里每个插件一个任务，都跑这个函数——所以"加一条后台通道"
    不需要再写一个 `while True` 循环（以前邮箱、入群审批、每日汇报各写了一份）。

    `min_interval` 是节拍下限（默认 15 秒，防配置写个 1 秒把对面打爆）；
    测试会把它调小，不然一轮要等 15 秒。

    退出时（任务被取消）调插件自己的 `close()`——这是插件收尾的**唯一确定时机**。
    """

    delay = FIRST_TICK_DELAY_SECONDS if first_delay is None else float(first_delay)
    interval = max(float(min_interval), float(getattr(plugin, "interval_seconds", 60.0) or 60.0))
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        while True:
            try:
                await plugin.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 一轮失败不能停掉这条通道
                logger.exception("后台插件这一轮出错 name=%s，继续等下一次",
                                 getattr(plugin, "name", "?"))
            await asyncio.sleep(interval)
    finally:
        closer = getattr(plugin, "close", None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 收尾失败也不能带走别的任务
                logger.exception("后台插件收尾出错 name=%s", getattr(plugin, "name", "?"))




# --- 具体插件 ---------------------------------------------------------------
#
# 这里原来是"把已有的 Stage 4 通道包成同一形状"的那几个类（邮件通道、每日汇报、
# 入群审批、WebUI 面板）。**它们不在这条分支上**：本仓库的 `main` 只放 Stage 3 的
# 对话/记忆/防护与底层，Stage 4 的功能扩展整体在 `stage4-plugins` 分支
# （见 README 的「分支」一节）。
#
# 机制留在 main 的原因：加后台能力的**接口**属于底层，谁都能照它加自己的插件——
# 这正是 `docs/STAGE_BOUNDARY.md` 里"不许为 Stage 4 预先建接口"的例外：接口不是
# 预先设计的，是从这三个真实通道里长出来的，现在已经稳定，所以留在这里当契约。


def build_background_plugins(engine, *, call_action=None, notify=None, roles=None,
                             loop=None) -> tuple[BackgroundPlugin, ...]:
    """装配后台插件。**唯一的入口**。

    `runtime.serve` 不认识任何具体通道：它只问这里拿一串插件，给每个跑同一个节拍。
    加一个后台能力 = 加一个插件 + 在这里登记，**不要往主循环里再加一个循环**。

    这条分支上没有任何后台插件，所以返回空元组——bot 照常跑，只是没有那些
    "隔一会儿干一件事"的能力。要看完整的四个（邮件通道、每日汇报、入群审批、
    WebUI 面板）怎么写的，切到 `stage4-plugins` 分支。

    各参数是核心注入的窄接缝，插件**拿不到 `transport`**：
      engine        只读引擎视图（它自己也是窄的）
      call_action   走 capabilities 闸门后调对面，回执拆成 data
      notify        核心统一发送（走补发队列）
      roles         "她在这个群里是不是群主"的只读查询
      loop          后台插件要往 asyncio 主循环上投任务时用（面板的 HTTP 线程要用）
    """

    return ()
