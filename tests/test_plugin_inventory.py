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

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qq_roleplay_bot import plugins  # noqa: E402

#: 面板读的 9 个键——**成功行与失败行必须是同一组**，否则前端要写两套读法。
ROW_KEYS = {"name", "title", "note", "builtin", "enabled", "loaded",
            "requires", "removable", "panel"}


class InventoryShapeTests(unittest.TestCase):
    def test_every_row_has_exactly_the_documented_keys(self) -> None:
        rows = plugins.inventory()
        self.assertTrue(rows, "清单不该是空的")
        for row in rows:
            self.assertEqual(set(row), ROW_KEYS, f"{row.get('name')} 的键不对")

    def test_the_five_shipped_plugins_are_listed(self) -> None:
        names = {row["name"] for row in plugins.inventory()}
        self.assertLessEqual(
            {"group_admin", "join_approval", "mail", "roles", "webui"}, names)

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
        """用户 2026-10-01：依赖用 `REQUIRES` 表达，不许把前置塞进核心。"""
        rows = {row["name"]: row for row in plugins.inventory()}
        self.assertEqual(rows["group_admin"]["requires"], ["roles"])
        self.assertEqual(rows["join_approval"]["requires"], ["roles"])


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
