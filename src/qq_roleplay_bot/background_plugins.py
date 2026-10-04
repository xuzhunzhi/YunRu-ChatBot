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


# --- 具体插件：把已有的通道包成同一形状 -------------------------------------
#
# 这些类**只做装配与节拍**，业务逻辑还在各自的模块里（`mail_channel.py`、
# `daily_report.py`、`join_approval.py`）。搬进来的原因是"Stage 4 的东西怎么接上"
# 应当集中在一处，而不是散在 `runtime.serve` 里三个 `create_task`。


# --- 装配：只做"把插件装起来"，不认识任何具体插件 ---------------------------
#
# 这一段是**薄壳**：它不认识"邮件""审批""面板"这些名字，只问 `plugins` 包
# 要一串插件，然后把它们交给 `runtime.serve` 的同一条节拍循环。
# 加一个新的后台能力 = 在 `plugins/` 下加一个目录，**不改这里**。
#
# 2026-10-01 修：装配入口收成一个。以前这里是 `build_background_plugins()`
# ——它自己造一个 `PluginRegistry` 跑 `discover()`，于是**命令插件登记的命令
# 没人收**（群管理命令"装上了但认不出"）。现在发现只在 `build_engine` 里跑一次，
# 命令与后台各归各位；这里只把已经装好的后台插件递出来。


def build_background_plugins(engine):
    """返回**已经装配好**的后台插件（`build_engine` 接插件时登记的）。

    `engine.plugin_registry` 是 `build_engine` 留下的那一份——命令与后台同源，
    所以这里不再自己 `discover()`（跑第二次会把同一条命令认两遍）。
    没有注册表时（测试直接造引擎、不走 `build_engine`）返回空：宁可不跑后台，
    也不要在这里偷偷装一份不一样的。

    ## 签名里**故意没有**那五个参数（2026-10-01 删）

    它原来是 `(engine, *, call_action=None, notify=None, roles=None, loop=None,
    transport=None)`，而**函数体一个都没读**。那种签名会让人以为"传了就会接上"——
    独立审查两次都点了这条（第一次我说了要删、只改了别处）。接缝现在只有
    `build_engine` 一个注入点；要传参就从那里传，别在这里留一排假开关。
    """

    registry = getattr(engine, "plugin_registry", None)
    if registry is None:
        logger.warning("后台插件未装配：引擎上没有 plugin_registry（没有走 build_engine？）")
        return ()
    loaded = getattr(registry, "loaded", ())
    if loaded:
        logger.info("后台插件已装配：%s", "、".join(loaded))
    return tuple(registry.backgrounds)
