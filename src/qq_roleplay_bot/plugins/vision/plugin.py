"""识图插件的装配点：把**两样东西**交给核心，自己不认识核心。

## 由来（2026-10-05）

`AGENTS.md` §2.3 点名这条一直没做：识图是包根的 `vision.py`，不是插件；
项目规矩是"Stage 4 的内容一律做成插件"，判据是"**删掉它，Stage 3 照样跑得起来**"。
现在它在这里，判据由 `tests/check_module_removal.py` 与
`tests/test_optional_capabilities.py` 守。

## 交出去的两样（`PluginRegistry` 上那两个窄口）

| 交给谁 | 交的是什么 | 核心拿它做什么 |
| --- | --- | --- |
| `registry.provide_prompt("vision", …)` | 这一套 system prompt 的内置原稿 | `prompt_library.builtin("vision")` 读它；面板照旧能改能回滚 |
| `registry.vision = build` | 一个**工厂**：收一个用量账本，回一个识图器（没开就回 `None`） | `build_engine` 在 `attach_plugins()` **之后**调它，把结果放到 `engine.vision` |

**为什么给工厂、不给造好的对象**：识图的用量要记进**核心的**用量账本
（`/super status` 那套按功能分类的统计）。账本是核心的，构造点又在插件里，
所以核心把账本递给工厂——与 `_build_judge_client(usage_store)` 同一形状。

**为什么核心不再 import 这个包**：`plugins/__init__.py` 写着"核心**不 import 具体插件**，
只调 `discover()`"。搬过来之后运行时那两处（`runtime._build_vision`、
`stage3_main._apply_vision` 里的 `replace_media_placeholder`）都不再 import 插件，
走的都是 `registry.vision` 与插件登记的 prompt——于是"删掉 `plugins/vision/`"这条
判据**结构上就成立**，不再依赖某处的 `except ImportError` 兜底。

`dev_config` 的四个开关（`QQBOT_VISION` / `QQBOT_VISION_MODEL` /
`QQBOT_VISION_USER_ID` / `QQBOT_VISION_API_KEY`）留在核心里：它们是**配置面**
（面板的"开关"页与 `runtime_flags` 都按名字读它），插件只是读它。

## 一件没有跟着搬的东西：`replace_media_placeholder`

"把 `[图片]` 换成描述"那个纯文本函数搬回了**核心**（`media_segments.py`）——
它只拼两个媒体标签，一行插件都不需要，而用它的那条路径在 `stage3_main`（核心）。
放这里等于让核心为了一句字符串替换 import 插件，与上面那条"核心不 import 插件"直接冲突。
接口没变，只是换了个家。
"""
from __future__ import annotations

import logging

# **绝对 import 是刻意的**：测试用 `spec_from_file_location(名字, 路径)` 加载测试文件
# （`tests/run_offline.py` 就是这么干的），于是测试 import 本模块时 `__package__` 是
# **测试文件的名字**、不是一个包——相对 import 会抛 `attempted relative import with
# no known parent package`，而 `test_vision.py` / `test_media_segments.py` 是在**顶层**
# 直接 import 本模块的。插件 import 核心本来就是允许的方向，写成绝对路径没有代价。
from qq_roleplay_bot import dev_config
from qq_roleplay_bot.plugins.vision.vision import ImageDescriber, VISION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def build(usage_store=None):
    """造一个识图器；关掉开关或没有任何可用 key 时返回 `None`（有图的消息只留占位符）。

    独立 client 与 user_id（默认 `qqbot-vision`），而且**不分群**：识图每次带的图都不同，
    前缀注定命中不了缓存，按群拆只会把一份缓存拆散（用户 2026-09-30 的要求）。
    模型默认 `deepseek-flash`——实测它能看图，不给图时也不会编。
    """

    if not dev_config.VISION_ENABLED:
        logger.info("识图未启用（QQBOT_VISION=0）")
        return None
    key = dev_config.VISION_API_KEY or dev_config.API_KEY
    if not key:
        logger.warning("识图已开启但没有任何可用 key，本次不启用")
        return None
    logger.info("识图已启用：model=%s user_id=%s（有图的消息在判定前先看一眼）",
                dev_config.VISION_MODEL, dev_config.VISION_USER_ID)
    # 本地 import：`llm_client` 是核心的**底层**，插件 import 核心是允许的方向。
    from qq_roleplay_bot.llm_client import OpenAICompatibleClient

    return ImageDescriber(
        OpenAICompatibleClient(
            dev_config.API_BASE_URL,
            key,
            dev_config.VISION_MODEL,
            user_id=dev_config.VISION_USER_ID,
            usage_store=usage_store,
            usage_role="vision",
        )
    )


def register(registry) -> None:
    """把识图的 prompt 原稿与识图器工厂交给核心（两处都按"没有就降级"设计）。"""

    # 与 `VISION_SYSTEM_PROMPT` 同源（就用它）：这一份就是面板里"识图"那一套的内置原稿。
    registry.provide_prompt("vision", VISION_SYSTEM_PROMPT)
    registry.vision = build
