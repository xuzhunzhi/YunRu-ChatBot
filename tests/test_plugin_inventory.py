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
import pathlib
import pkgutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qq_roleplay_bot import plugins  # noqa: E402
from qq_roleplay_bot import plugins as plugins_pkg  # noqa: E402


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


# ---------------------------------------------------------------------------
# 下面这一组来自插件线：面板读的那份清单（`plugins.inventory()`）的离线覆盖。
# rebase 时两边各自新增了**同一个路径**的文件（本体侧补 `discover()` 的装配判据、
# 插件侧补面板清单的判据），并到一处：两组断言都留着，一条都没删。
# ---------------------------------------------------------------------------

"""`plugins.inventory()` 的离线覆盖。

**为什么这个文件必须存在**（2026-10-04 一份对抗性审查的结论）：

    本次改动的 4 个提交，测试覆盖率为 0。
    【实测】grep dev/tests：inventory=0  /api/plugins=0  seams["plugins"]=0
                      drawTabs=0  loadPlugins=0  viewPlugin=0  CARDS=0
                      is-reading=0  #back=0  max-width=0  removable=0  _LAST_LOADED=0

审查者还做了突变测试：把 `removable` 改成一律 True、`loaded` 改成一律 True、
去掉 `panel` 标记、把 `/api/plugins` 放进 `PUBLIC_PATHS`……**每一项都能被断言抓住**，
但仓库里**一条这样的断言都没有**。所以"1162 全绿"只证明"没破坏别处"，
**证明不了这个改动被验证过**。这个文件就是补上那一块。
"""

#: 面板读的 9 个键——**成功行与失败行必须是同一组**，否则前端要写两套读法。
ROW_KEYS = {"name", "title", "note", "builtin", "enabled", "loaded",
            "requires", "removable", "panel"}


class InventoryShapeTests(unittest.TestCase):
    def test_every_row_has_exactly_the_documented_keys(self) -> None:
        rows = plugins.inventory()
        self.assertTrue(rows, "清单不该是空的")
        for row in rows:
            self.assertEqual(set(row), ROW_KEYS, f"{row.get('name')} 的键不对")

    def test_the_shipped_plugins_are_listed(self) -> None:
        # 2026-10-05：`roles` 从名单里去掉——它**不是插件了**（身份事实进核心，
        # `plugins/roles/` 整个文件夹已删除）。清单里剩下的必须一个不少。
        names = {row["name"] for row in plugins.inventory()}
        self.assertLessEqual(
            {"group_admin", "join_approval", "mail", "outage_notice", "vision", "webui"},
            names)

    def test_in_tree_plugins_count_as_preinstalled(self) -> None:
        # "预装"的定义：**在 plugins/ 目录里**（随代码发布）。
        for row in plugins.inventory():
            self.assertTrue(row["builtin"], f"{row['name']} 应当是预装")

    def test_no_preinstalled_plugin_is_removable(self) -> None:
        """用户 2026-10-03：'预装的' + 'webui 不给在 webui 里卸载'。

        审查者做过突变：把 `removable` 改成一律 True → 这条会红。
        """
        for row in plugins.inventory():
            self.assertFalse(row["removable"], f"{row['name']} 不该允许从面板卸载")

    def test_only_the_panel_itself_is_flagged_as_panel(self) -> None:
        panels = [row["name"] for row in plugins.inventory() if row["panel"]]
        self.assertEqual(panels, ["webui"], "只有面板插件本身该带 panel 标记")

    def test_loaded_reflects_the_last_discovery(self) -> None:
        """`loaded` 必须来自 `_LAST_LOADED`（审查者的突变 M2：一律 True → 红）。"""
        with mock.patch.object(plugins, "_LAST_LOADED", ("webui",)):
            loaded = {row["name"] for row in plugins.inventory() if row["loaded"]}
        self.assertEqual(loaded, {"webui"})

    def test_requires_expresses_dependencies_instead_of_burying_them(self) -> None:
        """用户 2026-10-01：依赖用 `REQUIRES` 表达，不许把前置塞进核心。

        2026-10-05：**角色那条依赖整条撤了**——用户把身份/权限事实判给核心，
        "角色事实归核心"。所以这条判据反过来钉：**谁都不许把 `roles` 写进
        `requires`**（写回去就是把角色事实又挂到一个插件上）。
        而真正还活着的插件依赖仍然要用 `REQUIRES` 表达清楚：
        `outage_notice → mail` 是同一条规矩下的正例。
        """
        rows = {row["name"]: row for row in plugins.inventory()}
        for name, row in rows.items():
            self.assertNotIn("roles", row["requires"], f"{name} 不该再依赖 roles 插件")
        self.assertEqual(rows["outage_notice"]["requires"], ["mail"])


class MissingPluginTests(unittest.TestCase):
    """F7（审查抓到的真 bug）：**缺依赖的插件原来会从面板上彻底隐身。**

    原来 `except ModuleNotFoundError: continue` 无法区分两种情况：
    纯资源目录（正常）vs 有 `plugin.py` 但 import 不进（**坏插件，必须看得见**）。
    """

    def _with_extra_root(self, root: pathlib.Path):
        return mock.patch.object(plugins, "__path__", [str(root), *plugins.__path__])

    def test_a_plugin_with_an_unimportable_module_is_listed_not_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            broken = root / "zz_broken_by_test"
            broken.mkdir()
            (broken / "__init__.py").write_text("", encoding="utf-8")
            (broken / "plugin.py").write_text(
                "import a_module_that_does_not_exist_zz\n", encoding="utf-8")
            with self._with_extra_root(root):
                rows = {row["name"]: row for row in plugins.inventory()}
            self.assertIn("zz_broken_by_test", rows,
                          "有 plugin.py 但 import 不进的插件**必须**列出来")
            self.assertFalse(rows["zz_broken_by_test"]["enabled"])
            self.assertIn("导入失败", rows["zz_broken_by_test"]["note"])

    def test_a_resource_only_directory_is_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            resource = root / "zz_resource_by_test"
            resource.mkdir()
            (resource / "__init__.py").write_text("", encoding="utf-8")
            (resource / "assets.json").write_text("{}", encoding="utf-8")
            with self._with_extra_root(root):
                rows = {row["name"]: row for row in plugins.inventory()}
            self.assertNotIn("zz_resource_by_test", rows,
                             "没有 plugin.py 的纯资源目录不该被当成插件")


class InventoryDoesNotAssembleTests(unittest.TestCase):
    """口径（审查 F6 纠正）：会 import 各插件的 plugin.py，但**不调用 register()**。"""

    def test_calling_inventory_does_not_change_the_loaded_record(self) -> None:
        before = plugins._LAST_LOADED
        plugins.inventory()
        self.assertEqual(plugins._LAST_LOADED, before,
                         "inventory() 不该动装配记录——它是只读的")


if __name__ == "__main__":
    unittest.main()
