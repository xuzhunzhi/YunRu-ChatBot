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

    ## 判据必须**就是运行时那个判据**（2026-10-06 又改了一次，如实交代）

    这里的判据现在直接调 `runtime._memory_endpoint()` —— 那是记忆服务装配时
    **同一个函数**。以前这里读 `dev_config.MEMORY_API_KEY` 这个常量，于是
    "常量有值、而运行时按别的东西判断"这种漂移只能靠人去对齐
    （`test_config_support.py` 里有一条测试专门盯这件事，它原来断言的是
    "`_start_memory` 的源码里出现 `MEMORY_API_KEY` 这个字符串"）。

    2026-10-06 三层配置落地后，记忆那把 key 由 `model_config.resolve("memory")`
    解析（自己的 key → 同家回落 → 兼容档），所以**守卫也改成问同一件事**：
    谁都不再依赖"某个常量是不是非空"这种间接信号。

    ## ⚠️ 2026-10-02 那次修正（这段历史仍然有效）

    原来这里写的是 `if not getattr(dev_config, "MEMORY_ENABLED", False)` ——
    **`dev_config` 里从来没有这个属性**，于是 `getattr(..., False)` 恒为 `False`
    → 这条守卫**恒跳过**。

    后果不是"保守"，是**真回归被静默跳过**：`test_memory` 里
    `test_startup_connects_service_and_successful_test_ack` 是**唯一**验
    "记忆服务有没有被接上组装路径"的测试，而它从来没在这台机器上跑过。
    外部审查第五轮实测：把 `_start_memory` 改成永远 `return None`，
    这组测试**仍然 SKIP**；把守卫摘掉、同一个突变 → **RED**（IndexError）。
    *（我本机留痕里那个 `skipped=1` 就是它——"跳数"本身就是这台机器上
    配好了却没跑的凭证。）*

    这就是我反复栽的那个模式：**属性名是推断的，不是查过的**。
    """

    from qq_roleplay_bot import runtime

    if runtime._memory_endpoint() is None:
        raise unittest.SkipTest(
            "本机没有可用的记忆 key（干净 clone 只有 .env.example）："
            "这条走真实入口验'记忆服务接上了组装路径'")


def skip_unless_review_enabled() -> None:
    """风格审核没开就跳过（它需要 QQBOT_STYLE_REVIEW=1 且配了 key）。"""

    from qq_roleplay_bot import dev_config

    if not getattr(dev_config, "REVIEW_ENABLED", False):
        raise unittest.SkipTest("风格审核没开（本机没有 QQBOT_STYLE_REVIEW：干净 clone 只有 .env.example）")
