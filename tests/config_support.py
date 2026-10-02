"""测试用的"本机配置在不在"判定（2026-10-01 加）。

公开仓库只发布 `.env.example`，**真实 `.env` 永不提交**（含凭据，见 `.gitignore`）。
于是有一类测试在干净 clone 上必然失败：它们验的是"**配置好之后**行为对不对"。

    - `test_clearing_the_main_key_falls_back_or_is_refused`：前提是 `.env` 里有一把主 key；
    - `test_engine_sends_the_reviewed_line`：前提是风格审核开着并且配了 key；
    - `test_memory...test_startup_connects_...`：前提是记忆服务能起来。

这些断言本身有价值（部署机上要跑），所以在**没有那份配置时跳过**而不是删掉。
`tests/run_offline.py` 用 unittest，`SkipTest` 记成 skip、不算失败。
"""
from __future__ import annotations

import os
import unittest


def skip_without_env(name: str, why: str) -> None:
    """环境变量 / `.env` 填充项不在就跳过。"""

    from qq_roleplay_bot import dev_config

    value = os.environ.get(name, "").strip() or str(getattr(dev_config, name, "") or "").strip()
    if not value:
        raise unittest.SkipTest(f"{why}（本机没有 {name}：干净 clone 只有 .env.example）")


def skip_unless_memory_enabled() -> None:
    """记忆服务没开就跳过（干净 clone 上 `QQBOT_MEMORY_*` 都没有）。

    ## ⚠️ 2026-10-02 修正：这里原来是一道**死守卫**，把测试永久关掉了

    原文是 `if not getattr(dev_config, "MEMORY_ENABLED", False)` ——
    **`dev_config` 里从来没有这个属性**（全树 grep：`MEMORY_ENABLED` 只出现在
    `memory_config.py:39` 的 `os.environ.get("QQBOT_MEMORY_ENABLED", "1")`、
    `operator_config.py:45` 与 `test_env_config.py` 里）。于是
    `getattr(..., False)` **恒为 `False`** → 这条守卫**恒跳过**。

    后果不是"保守"，是**真回归被静默跳过**：`test_memory` 里
    `test_startup_connects_service_and_successful_test_ack` 是**唯一**验
    "记忆服务有没有被接上组装路径"的测试，而它从来没在这台机器上跑过。
    外部审查第五轮实测：把 `_start_memory` 改成永远 `return None`，
    这组测试**仍然 SKIP**；把守卫摘掉、同一个突变 → **RED**（IndexError）。
    *（我本机留痕里那个 `skipped=1` 就是它——"跳数"本身就是这台机器上
    配好了却没跑的凭证。）*

    这就是我反复栽的那个模式：**属性名是推断的，不是查过的**。

    ## 判据必须是"真实前提"，不能是"看着像"（我第一版修法也是错的）

    那位审查者给的两个修法（`MemorySettings().enabled` / 读 `QQBOT_MEMORY_ENABLED`
    环境变量）**都不对**——我照第一个改了一版，被 `tests/test_config_support.py` 里
    "开关关掉时必须真的跳过"那条测试当场抓住。去读 `runtime._start_memory` 才看清：

    * `MemorySettings()` 的 `enabled` 是 **dataclass 默认值 `True`**，与 env 无关
      （生产 `MemoryService` 用的就是 `MemorySettings()`）；
    * 读 env 的是 `MemorySettings.from_environment()`，而 `_start_memory` 用的正是
      它 —— 但它**并不检查 `enabled`**；
    * `_start_memory` 唯一的前提是 **`dev_config.MEMORY_API_KEY` 非空**，
      否则直接 `return None`（原注释："未配置 key 时返回 None，对话照常"）。

    所以判据是**那把 key**。用错判据的后果是**反方向**的：守卫会永不跳过，
    那条测试在干净 clone 上直接红（`services` 空 → `services[0]` → IndexError）。
    """

    skip_without_env("MEMORY_API_KEY", "这条走真实入口验'记忆服务接上了组装路径'，需要记忆 key")


def skip_unless_review_enabled() -> None:
    """风格审核没开就跳过（它需要 QQBOT_STYLE_REVIEW=1 且配了 key）。"""

    from qq_roleplay_bot import dev_config

    if not getattr(dev_config, "REVIEW_ENABLED", False):
        raise unittest.SkipTest("风格审核没开（本机没有 QQBOT_STYLE_REVIEW：干净 clone 只有 .env.example）")
