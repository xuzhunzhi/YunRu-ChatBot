"""**`discover()` 到底装上了什么**——这条以前没有任何检查。

由来（2026-10-06 外部审查实测）：把 6 个插件文件夹全改个名字，`discover()` 的返回值
从 6 掉到 3，而**仓库里一条测试都不会红**。也就是说"插件是不是真的装上了"这件事
在判据上是空的：`discover()` 里任何一条静默的 `continue`（纯资源目录、缺依赖、
`ENABLED = False`、`register` 不存在、`register` 抛异常）都会让它**悄悄少装**，
而套件照样全绿。

## 这条断言怎么判（**每个 `plugin.py` 要么装上、要么明说"别启用"**）

遍历 `plugins/` 下**每一个带 `plugin.py` 的目录**（名字不以 `_` 开头），逐个要求：

| 情形 | 判 |
| --- | --- |
| 名字在 `discover()` 的返回值里 | 通过 |
| 没在，但模块显式导出 `ENABLED = False` | 通过（"装在这里但先别启用"是**声明**，不是漏装） |
| 没在、又没声明 `ENABLED = False` | **红**（缺依赖 / 坏了 / 被静默跳过） |

所以这条断言的粒度是"**不许有插件无声无息地消失**"，不是"每个目录都必须在
`loaded` 里"——后者会把 `REQUIRES` 缺依赖而被有意跳过、以及 `ENABLED = False`
这两种**合法**情形算成失败。

## 本体侧（这棵树没有插件文件夹）为什么不会假红

`plugins/` 下只有 `__init__.py`（发现器）与 `__pycache__/`，`_plugin_dirs()` 遍历到的
是**空集**，于是"每个插件"这个全称命题**空真**、逐个点名那一圈一次都不进，
"两者集合相等"那一句也是 `set() == set()`——不会因为"本树没有插件"报错。
这不是把断言写松了：判据本来就是"目录里有几个 `plugin.py`，就该有几个是装上的或
明说别启用的"，没有插件时那个集合本来就是空的。
（插件侧那条分支上同一个文件会真的遍历到那几个插件。）

## 与 `check_module_removal.py` 的分工

那个脚本问"**核心**能不能拔掉某项能力照跑"；这条问"**装配**有没有把该装的装上"。
两句都得问：前者管"不依赖"，后者管"真的接上了"。

## 为什么断言用的是"新造一个注册表、自己 `discover()` 一次"

不从进程里那个 `plugins.registry()` 读：套件里很多用例会 `build_engine()`
（它们会**换成最新那一份**注册表），拿别人的状态来判会得出"这台机器上一个插件都没有"
这类假红。这里自己发现一次、拿返回值当事实——`discover()` 的返回值就是"装上了哪些"。
"""
from __future__ import annotations

import importlib
import pkgutil

from qq_roleplay_bot import plugins as plugins_pkg


def _discovered() -> tuple[str, ...]:
    """自己发现一次，返回"装上了哪些"（`discover()` 的返回值）。"""

    return plugins_pkg.discover(plugins_pkg.PluginRegistry())


def _plugin_dirs() -> tuple[str, ...]:
    """`plugins/` 下**带 `plugin.py`** 的目录名（名字以下划线开头的排除）。

    判据用 `plugins/__init__.py` 自己在用的那个扫描形状（`pkgutil.iter_modules`），
    所以这里数出来的东西就是 `discover()` 会去装的东西——不从目录结构另立一套口径。
    """

    names = []
    for info in pkgutil.iter_modules(plugins_pkg.__path__):
        if not info.ispkg or info.name.startswith("_"):
            continue
        if plugins_pkg._plugin_file(info.name).is_file():
            names.append(info.name)
    return tuple(sorted(names))


def _declares_disabled(name: str) -> bool:
    """这个插件的 `plugin.py` 是不是明确声明了 `ENABLED = False`。

    导入失败（缺依赖、坏了）**不算**"声明别启用"——那是要报出来的情况，
    不是合法状态。所以这里只认"导得进来、且 `ENABLED` 是假值"。
    """

    try:
        module = importlib.import_module(f"{plugins_pkg.__name__}.{name}.plugin")
    except Exception:  # noqa: BLE001 - 导不进来就不是"声明别启用"
        return False
    return not bool(getattr(module, "ENABLED", True))


def test_every_plugin_directory_is_either_loaded_or_declares_itself_disabled() -> None:
    """**一个插件都不许无声无息地消失**（审查实测：6 个改名 → 掉到 3，没有一条测试红）。"""

    loaded = _discovered()
    missing = [name for name in _plugin_dirs()
               if name not in loaded and not _declares_disabled(name)]

    assert not missing, (
        "这些插件目录有 plugin.py，既没被 discover() 装上、也没有声明 ENABLED = False："
        f"{missing}。装上了的：{sorted(loaded)}。"
        "少了它们说明装配把某个插件静默跳过了（缺依赖 / register 抛异常 / 发现器漏扫）。"
    )


def test_the_loaded_count_matches_what_the_scan_sees() -> None:
    """**数一数**：遍历到的插件目录必须与"装上的 + 声明别启用的"对得上。

    与上一条的区别：那条逐个点名，这条防止"遍历本身悄悄漏了东西"
    （例如 `_plugin_dirs()` 的扫描口径与 `discover()` 的口径漂移）。
    两边的集合必须一样大——本体侧两边都是空集（空真，不是假绿）。
    """

    dirs = set(_plugin_dirs())
    loaded = set(_discovered())
    disabled = {name for name in dirs if _declares_disabled(name)}

    assert dirs == (loaded & dirs) | disabled, (
        f"扫描看到 {sorted(dirs)}，装上的 {sorted(loaded & dirs)}，"
        f"声明别启用的 {sorted(disabled)}——三者对不上。"
    )
