"""`test_optional_capabilities.py` 里**要造插件**的那一条（插件侧）。

## 为什么单独一个文件（2026-10-05 分支拆分）

本体侧那份钉的是"**没有**可插能力时核心照跑"——那是核心的降级契约，
`test_build_engine_survives_a_missing_audit_module`（没有 `control_audit`）、
`test_every_available_prompt_is_readable`、
`test_backfill_all_skips_a_missing_capability_without_faking_a_prompt`
（这两条把识图那套 prompt 藏起来，验的是"少了它照样起"）都留在那里。

但这一条要**真的 import 插件的 `plugin.py`**（拿 `vision_plugin` 去验"能力经由
注册表注入"），本体侧的正常形态是插件不在，那条在那种树上**根本不该存在**
（不是"存在但跳过"），所以整条搬到这里。

## 2026-10-05：删掉了原来那一半（`roles` 那条）

原来这里还有 `test_build_engine_survives_a_missing_role_module`，钉的是
"角色查询归 `plugins/roles/`，核心不自己造那份缓存"。**前提已经不存在**：
用户拍板"身份/权限事实进核心"（本体 `1c5fcf3`），角色由核心的 `group_roles.py`
提供，`plugins/roles/` 整个文件夹删掉了。那条测试要验的东西由本体侧接过去了——
`tests/test_group_roles.py`（核心怎么降级、怎么 fail-closed）与
`tests/test_runtime_assembly.py`（真装配出来是**同一个**来源）。
留在这里只会是一条"假装还测着什么"的空壳。

## 为什么要 `skipUnless`

这个文件住在**插件侧**，所以正常跑得到；但它用的是"藏模块"这种手段，只有在
**插件真的在磁盘上**时才有意义。留一道守卫是为了让它在"插件被移走"的树上
**诚实跳过、报出原因**，而不是假装绿——skip 不是本体的用例被跳过，
这一条本来就只在带插件的树上有意义。

断言**一字未改**。`_FakeTransport` / `_Hidden` 从本体侧那份 import 过来
（借而不是抄，`_Hidden` 那套踩过的坑只该有一份维护）。
"""
import unittest

from qq_roleplay_bot import runtime
from qq_roleplay_bot.prompt_library import PromptLibrary, PromptRejected
from test_optional_capabilities import _FakeTransport, _Hidden


def _plugins_present() -> bool:
    """识图插件文件夹在不在（这一条用到的就是它）。"""

    from pathlib import Path

    pkg = Path(runtime.__file__).resolve().parent
    return (pkg / "plugins" / "vision" / "plugin.py").is_file()


_NEEDS_PLUGINS = unittest.skipUnless(
    _plugins_present(), "插件不在（本体侧的形态）：这一条只在带插件的树上有意义")


@_NEEDS_PLUGINS
def test_build_engine_survives_a_missing_vision_module() -> None:
    """识图那套 prompt 缺席时**受控降级**，而且"插件不给识图"时引擎照起。

    ## 2026-10-05 改了它测的东西（前提变了，不是放宽断言）

    识图从包根 `qq_roleplay_bot/vision.py` 搬进了插件 `plugins/vision/`。原来
    `_Hidden("qq_roleplay_bot.vision")` 打的是 `prompt_library.builtin("vision")` 那句
    `from .vision import VISION_SYSTEM_PROMPT`；搬完**核心不再 import 插件**，那一套
    prompt 改由插件在 `register()` 里 `registry.provide_prompt("vision", …)` 登记。
    所以分两半验，各用**可靠**的那个手段：

    1. **prompt 那一侧**（藏插件包 + 重新装配）：`builtin("vision")` 必须给
       `PromptRejected`、`available()` 必须把识图滤掉。2026-10-06（rebase 到本体
       `a23f748` 之后）：判据从"那个模块名在不在"改成"**插件登记过这一套没有**"，
       所以光藏子模块不够——要藏**整个插件包**再装配一次，见 `_Hidden` 那段说明。
    2. **核心那一侧**（把插件的登记撤掉）：`engine.vision` 的唯一来源是插件经
       `registry.vision` 放上来的工厂——工厂不在就该是 `None`。**不藏模块**是因为
       "藏模块"在进程内不可靠：`plugin.py` 一旦被执行过就把 `vision.vision` 绑进了自己
       的命名空间，而且另一个测试文件（`test_group_action_execution.py`，2026-10-05
       与这条同时出现）会重新绑父包属性，实测能把 `_Hidden` 整套绕过去。
       撤掉登记直接验的是**核心的契约**：没有那个工厂 = 没有识图。
    """

    with _Hidden("qq_roleplay_bot.plugins.vision"):
        # 按新判据造缺席：插件包藏掉之后装配一次，"最新那一份注册表"上就没有识图
        runtime.build_engine(_FakeTransport())
        # builtin 必须给一个**受控**的拒绝，而不是放 ModuleNotFoundError 出去
        try:
            PromptLibrary.builtin("vision")
        except PromptRejected:
            pass
        else:
            raise AssertionError("识图插件装不上时 builtin('vision') 应该抛 PromptRejected")

        assert "vision" not in PromptLibrary.available(), PromptLibrary.available()

    # 真正的判据：**没有人提供识图**时整台机器装得起来，而且能力真的没了。
    from qq_roleplay_bot.plugins.vision import plugin as vision_plugin

    original = vision_plugin.build
    vision_plugin.build = lambda usage_store=None: None
    try:
        engine = runtime.build_engine(_FakeTransport())
    finally:
        vision_plugin.build = original
    assert engine is not None
    # 能力真的没了：引擎上没有识图器 → 有图的消息只留 `[图片]` 占位符。
    # （插件文件夹被整个删掉时走的也是这条路，由 `tests/check_module_removal.py`
    #   在**全新解释器**里验，那里没有"模块已经被 import 过"这层干扰。）
    assert engine.vision is None
