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
    """记忆服务没开就跳过（干净 clone 上 `QQBOT_MEMORY_*` 都没有）。"""

    from qq_roleplay_bot import dev_config

    if not getattr(dev_config, "MEMORY_ENABLED", False):
        raise unittest.SkipTest("记忆服务没开（本机没有 QQBOT_MEMORY_ENABLED：干净 clone 只有 .env.example）")


def skip_unless_review_enabled() -> None:
    """风格审核没开就跳过（它需要 QQBOT_STYLE_REVIEW=1 且配了 key）。"""

    from qq_roleplay_bot import dev_config

    if not getattr(dev_config, "REVIEW_ENABLED", False):
        raise unittest.SkipTest("风格审核没开（本机没有 QQBOT_STYLE_REVIEW：干净 clone 只有 .env.example）")
