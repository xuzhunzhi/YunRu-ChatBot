"""`test_optional_capabilities.py` 里**要造插件**的那两条（插件侧）。

## 为什么单独一个文件（2026-10-05 分支拆分）

本体侧那份钉的是"**没有**可插能力时核心照跑"——那是核心的降级契约，
`test_build_engine_survives_a_missing_audit_module`（没有 `control_audit`）、
`test_every_available_prompt_is_readable`、
`test_backfill_all_skips_a_missing_capability_without_faking_a_prompt`
（这两条把识图那套 prompt 藏起来，验的是"少了它照样起"）都留在那里。

但这两条要**真的 import 插件的 `plugin.py`**（拿 `roles_plugin` / `vision_plugin`
去验"插件自己拒绝装配"与"能力经由注册表注入"），本体侧的正常形态是插件不在，
那两条在那种树上**根本不该存在**（不是"存在但跳过"），所以**整条搬到这里**。

## 为什么要 `skipUnless`

这个文件住在**插件侧**，所以正常跑得到；但它用的是"藏模块"这种手段，只有在
**插件真的在磁盘上**时才有意义。留一道守卫是为了让它在"插件被移走"的树上
**诚实跳过、报出原因**，而不是假装绿——skip 不是本体的用例被跳过，
这两条本来就只在带插件的树上有意义。

断言**一字未改**。`_FakeTransport` / `_Hidden` 从本体侧那份 import 过来
（借而不是抄，`_Hidden` 那套踩过的坑只该有一份维护）。
"""
import unittest

from qq_roleplay_bot import runtime
from qq_roleplay_bot.prompt_library import PromptLibrary, PromptRejected
from test_optional_capabilities import _FakeTransport, _Hidden


def _plugins_present() -> bool:
    """两个插件文件夹在不在（`roles` / `vision` 是这两条用到的）。"""

    from pathlib import Path

    pkg = Path(runtime.__file__).resolve().parent
    return all((pkg / "plugins" / name / "plugin.py").is_file()
               for name in ("roles", "vision"))


_NEEDS_PLUGINS = unittest.skipUnless(
    _plugins_present(), "插件不在（本体侧的形态）：这两条只在带插件的树上有意义")


@_NEEDS_PLUGINS
def test_build_engine_survives_a_missing_role_module() -> None:
    """**角色查询缺席**时是降级、不是崩；而且它的唯一来源是插件。

    ## 这条在 2026-10-04 改了测的东西（改测的东西，不是因为红了才改）

    原来它 `_Hidden("qq_roleplay_bot.qq_roles")` 再断言 `engine.self_roles is None`。
    `qq_roles.SelfRoleCache` 是**核心自己那第二份**角色缓存——"单源"那一改之后，
    核心不再 import 它（`runtime.build_engine` 里那个创建已删），于是藏起它
    什么也证明不了（能力还在，是插件给的）。**这属于测试的前提变了，不是放宽断言。**

    （函数名留着不改了：它是 `qq_roles` 那个模块第一次被抓到的现场，改名字会让
    "这条守的是什么"的历史断掉。测的东西以本 docstring 为准。）

    新的前提：角色查询归 `plugins/roles/`，由核心注入的 `call_action` 现造。
    所以"这项能力不在"= **`call_action` 不在**。这条负向路径分两半验：

    1. `roles.plugin.register` 在没有 `call_action` 时**明确抛错**，
       不许"装上一个查不了角色的缓存"（那正是 2026-09-30 那次静默回归的形状）；
    2. 核心这一侧：`build_engine` 装出来的机器**不再自己造**那份缓存——
       `self_roles` 就是插件放进注册表的那一个对象（单源）。
    """

    from qq_roleplay_bot.plugins import PluginRegistry
    from qq_roleplay_bot.plugins.roles import plugin as roles_plugin

    registry = PluginRegistry()  # `call_action` 缺省就是 None
    try:
        roles_plugin.register(registry)
    except RuntimeError:
        # 明确拒绝是对的——**不许静默装一个查不了角色的缓存**。
        pass
    else:  # pragma: no cover - 不该走到
        raise AssertionError("没有 call_action 时 roles 插件不该装上去")
    assert registry.shared_roles() is None, "装失败了就不该留下半个共享角色"

    # 核心照旧起得来（`build_engine` 跑通本身就是这条判据），
    # 而且 `self_roles` 与注册表上那份**是同一个对象**：单源。
    engine = runtime.build_engine(_FakeTransport())
    assert engine.plugin_registry.shared_roles() is engine.self_roles, (
        "角色的唯一来源必须是 roles 插件放进注册表的那一份（单源）")


@_NEEDS_PLUGINS
def test_build_engine_survives_a_missing_vision_module() -> None:
    """识图那套 prompt 缺席时**受控降级**，而且"插件不给识图"时引擎照起。

    ## 2026-10-05 改了它测的东西（前提变了，不是放宽断言）

    识图从包根 `qq_roleplay_bot/vision.py` 搬进了插件 `plugins/vision/`。原来
    `_Hidden("qq_roleplay_bot.vision")` 打的是 `prompt_library.builtin("vision")` 那句
    `from .vision import VISION_SYSTEM_PROMPT`；搬完**核心不再 import 插件**，那一套
    prompt 改由插件在 `register()` 里 `registry.provide_prompt("vision", …)` 登记。
    所以分两半验，各用**可靠**的那个手段：

    1. **prompt 那一侧**（藏模块）：`builtin("vision")` 必须给 `PromptRejected`、
       `available()` 必须把识图滤掉。这条只依赖插件模块本身，`_Hidden` 挡得住。
    2. **核心那一侧**（把插件的登记撤掉）：`engine.vision` 的唯一来源是插件经
       `registry.vision` 放上来的工厂——工厂不在就该是 `None`。**不藏模块**是因为
       "藏模块"在进程内不可靠：`plugin.py` 一旦被执行过就把 `vision.vision` 绑进了自己
       的命名空间，而且另一个测试文件（`test_group_action_execution.py`，2026-10-05
       与这条同时出现）会重新绑父包属性，实测能把 `_Hidden` 整套绕过去。
       撤掉登记直接验的是**核心的契约**：没有那个工厂 = 没有识图。
    """

    with _Hidden("qq_roleplay_bot.plugins.vision.vision"):
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
