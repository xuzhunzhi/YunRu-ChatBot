"""**第二个加载根**（`data/plugins/`）与清单字段（`VERSION` / `HOST_API` / `SOURCE`）。

## 为什么有这道判据

装进来的插件住在 `data/plugins/<名字>/plugin.py`——那是一个**普通文件夹**（没有
`__init__.py`）。2026-10-08 的探针实测（`docs/research/plugin-ecosystem-2026-10-08.md`）：

```
pkgutil.iter_modules(第二个根)   → 0 个（插件全看不见）
_plugin_file(<名字>)             → 6/6 命中
importlib.import_module(...)     → 成立（命名空间包），__file__ 确实在第二个根
```

也就是说发现器原来用的 `pkgutil.iter_modules` **会静默漏掉整个第二个根**
（它对没有 `__init__.py` 的目录 `continue`）。所以扫目录改成 `plugin_dirs()`
（自己列目录、只看 `<根>/<名字>/plugin.py`），两个根同一个口径。

## 每条测什么

| 用例 | 钉住的事 |
| --- | --- |
| `test_second_root_..._found_and_registered` | 没有 `__init__.py` 的插件被发现、`register()` 真的被调、`source="installed"` |
| `test_old_plugin_without_manifest_fields_still_loads` | **老插件一个字不改照样装**（缺 `VERSION`/`HOST_API` = 通过） |
| `test_host_api_mismatch_is_rejected_with_a_clear_log` | `HOST_API` 不等于宿主那份 → 拒绝加载 + 一条明确日志（fail-closed） |
| `test_non_numeric_or_boolean_host_api_is_rejected` | 写坏的 `HOST_API`（`"x"`、`True`）也拒，不许"恰好等于 1"蒙过去 |
| `test_broken_plugins_are_skipped_and_the_core_still_builds` | 坏插件（缺 `register` / 语法错 / 缺模块 / 纯资源目录）只跳过自己，`build_engine()` 照常起来 |
| `test_absent_data_root_changes_nothing` | 没有 `data/plugins/` = 没有第二个根，不是错误，`__path__` 一个字节不变 |
| `test_present_data_root_is_mounted_once_and_marked_removable` | 有那个目录 → 接上、**幂等**、算 `installed`、`removable=True` |
| `test_webui_from_data_root_is_still_not_removable` | `webui` 从哪个根来都不给卸（卸了等于把面板自己删了） |

## 突变验过（两个方向都跑了，2026-10-08）

* 把 `discover()` 里的 `HOST_API` 那一段删掉 → `test_host_api_mismatch_...` 红。
* 把 `plugin_dirs()` 换回 `pkgutil.iter_modules` → 前三条"第二个根"的用例红
  （这正是那个探针的结论：漏的不是一行逻辑，是**整个根**）。
恢复后全绿。所以这些断言不是摆设：每一条都对应一处能单独改坏的代码。
"""
from __future__ import annotations

import importlib
import os
import pathlib
import pkgutil
import tempfile
import unittest
from unittest import mock

from qq_roleplay_bot import plugins as plugins_pkg

GOOD = '''TITLE = "判据用插件"
VERSION = "2.0.1"
HOST_API = 1


def register(registry):
    registry.provide_owner_channel("install-root-probe")
'''

OLD = '''TITLE = "老插件"


def register(registry):
    registry.provide_owner_channel("install-root-probe-old")
'''


class FakeTransport:
    """`check_module_removal.py` 里那个假传输层的最小版（只为让 `build_engine` 起来）。"""

    async def call_api(self, action, params=None):
        return {"status": "ok", "retcode": 0,
                "data": {"role": "owner", "user_id": "9", "nickname": "n"}}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


class SecondRootCase(unittest.TestCase):
    """基类：把 `data/plugins` 那个形状的临时根接到 `__path__` 上，事后原样还原。"""

    def setUp(self) -> None:
        self._path = list(plugins_pkg.__path__)
        self._last_loaded = plugins_pkg._LAST_LOADED
        self._registry = plugins_pkg._REGISTRY
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name) / "plugins"
        self.root.mkdir(parents=True)
        plugins_pkg.__path__.append(str(self.root))

    def tearDown(self) -> None:
        plugins_pkg.__path__[:] = self._path
        plugins_pkg._LAST_LOADED = self._last_loaded
        plugins_pkg._REGISTRY = self._registry
        self._tmp.cleanup()

    def write(self, name: str, body: str) -> pathlib.Path:
        """写一个**没有 `__init__.py`** 的插件目录（`data/plugins` 下的真实形状）。"""

        directory = self.root / name
        directory.mkdir()
        (directory / "plugin.py").write_text(body, encoding="utf-8")
        return directory

    def discover(self) -> tuple[str, ...]:
        return plugins_pkg.discover(plugins_pkg.PluginRegistry())

    def rows(self) -> dict:
        return {row["name"]: row for row in plugins_pkg.inventory()}


class SecondRootTests(SecondRootCase):
    def test_second_root_plugin_without_init_py_is_found_and_registered(self) -> None:
        self.write("probe_good", GOOD)

        assert "probe_good" in plugins_pkg.plugin_dirs()
        assert str(plugins_pkg._plugin_file("probe_good").parent.parent) == str(self.root)

        registry = plugins_pkg.PluginRegistry()
        loaded = plugins_pkg.discover(registry)

        assert "probe_good" in loaded, f"第二个根上的插件没被装上：{loaded}"
        assert registry.knows_owner_channel("install-root-probe"), "register() 里的登记没生效"
        row = self.rows()["probe_good"]
        assert row["source"] == "installed" and row["removable"] is True
        assert row["version"] == "2.0.1"

    def test_the_scan_does_not_rely_on_the_stdlib_package_listing(self) -> None:
        """记下"为什么不用 `pkgutil.iter_modules`"：它看不见没有 `__init__.py` 的目录。

        这条断言若哪天变红，说明标准库改了行为——那时 `plugin_dirs()` 仍然是对的，
        重新读一遍它上面那段注释即可，不要反过来把 `plugin_dirs()` 改回去。
        """

        self.write("probe_good", GOOD)

        stdlib_sees = sorted(m.name for m in pkgutil.iter_modules([str(self.root)]) if m.ispkg)
        assert stdlib_sees == [], f"预期 stdlib 看不见它，实际看见了：{stdlib_sees}"
        # 只断言"**我写进临时根的那个在结果里**"。**不比整个集合**：这棵树上本来有几个
        # 插件不是这条用例的事（本体树 0 个、插件树 6 个，两边都要成立）。
        assert "probe_good" in plugins_pkg.plugin_dirs(), plugins_pkg.plugin_dirs()

    def test_old_plugin_without_manifest_fields_still_loads(self) -> None:
        self.write("probe_old", OLD)

        loaded = self.discover()
        row = self.rows()["probe_old"]

        assert "probe_old" in loaded, f"缺 VERSION/HOST_API 的老插件被卡住了：{loaded}"
        assert row["version"] == ""
        assert row["builtin"] is False and row["source"] == "installed"

    def test_host_api_mismatch_is_rejected_with_a_clear_log(self) -> None:
        self.write("probe_old", OLD)
        self.write("probe_new_api", GOOD.replace("HOST_API = 1", "HOST_API = 999"))

        with self.assertLogs("qq_roleplay_bot.plugins", level="ERROR") as captured:
            loaded = self.discover()

        assert "probe_new_api" not in loaded, "HOST_API 不匹配的插件被放行了（应该 fail-closed）"
        assert "probe_old" in loaded, "同目录里正常的插件被牵连了"
        assert any("plugin_host_api_mismatch" in line and "probe_new_api" in line
                   for line in captured.output), captured.output
        # 被拒的插件在清单上也要看得见（面板能解释"它为什么没跑"）
        assert "probe_new_api" in self.rows()

    def test_non_numeric_or_boolean_host_api_is_rejected(self) -> None:
        self.write("probe_str", GOOD.replace("HOST_API = 1", 'HOST_API = "1"'))
        self.write("probe_bool", GOOD.replace("HOST_API = 1", "HOST_API = True"))

        loaded = self.discover()

        assert "probe_str" not in loaded, "非数字的 HOST_API 被当成了 1"
        assert "probe_bool" not in loaded, "HOST_API = True 被 `True == 1` 蒙过去了"

    def test_broken_plugins_are_skipped_and_the_core_still_builds(self) -> None:
        self.write("probe_good", GOOD)
        self.write("probe_noreg", "X = 1\n")
        self.write("probe_syntaxerr", "def register(registry)\n    pass\n")
        self.write("probe_missingmod", "import definitely_not_a_module_xyz\n\n\ndef register(registry):\n    pass\n")
        (self.root / "just_resources").mkdir()          # 纯资源目录：没有 plugin.py
        (self.root / "_private").mkdir()                # 下划线开头：不是插件
        (self.root / "_private" / "plugin.py").write_text(OLD, encoding="utf-8")

        loaded = self.discover()

        # 断言**我写进临时根的**这几个：好的装上、坏的只跳过自己。**不比整个集合**——
        # 这棵树上本来有几个插件（本体树 0 个、插件树 6 个）不是这条用例的事。
        assert "probe_good" in loaded, f"正常插件没被装上：{loaded}"
        for name in ("probe_noreg", "probe_syntaxerr", "probe_missingmod"):
            assert name not in loaded, f"坏插件 {name} 没被单独跳过：{loaded}"

        rows = self.rows()
        probes = ("probe_good", "probe_noreg", "probe_syntaxerr", "probe_missingmod")
        for name in probes:
            assert name in rows, f"{name} 没出现在清单上：{sorted(rows)}"
        assert rows["probe_good"]["loaded"] is True
        for name in probes[1:]:
            assert rows[name]["loaded"] is False, f"{name} 不该算装上"
        assert "just_resources" not in rows, "没有 plugin.py 的纯资源目录不该算插件"
        assert "_private" not in rows, "下划线开头的目录不该算插件"

        from qq_roleplay_bot import runtime

        runtime.build_engine(FakeTransport())           # 起不来会直接抛，用例就红


class DataRootTests(SecondRootCase):
    def test_absent_data_root_changes_nothing(self) -> None:
        empty = pathlib.Path(self._tmp.name) / "empty-data"
        empty.mkdir()
        before = [str(p) for p in plugins_pkg.__path__]

        with mock.patch.dict(os.environ, {"QQBOT_DATA_DIR": str(empty)}):
            assert plugins_pkg.data_plugins_root() is None
            assert plugins_pkg.mount_data_plugins() is None

        assert [str(p) for p in plugins_pkg.__path__] == before, "没有 data/plugins 却被加了根"

    def test_present_data_root_is_mounted_once_and_marked_removable(self) -> None:
        data = pathlib.Path(self._tmp.name) / "real-data"
        installed = data / "plugins" / "probe_installed"
        installed.mkdir(parents=True)
        (installed / "plugin.py").write_text(GOOD, encoding="utf-8")

        with mock.patch.dict(os.environ, {"QQBOT_DATA_DIR": str(data)}):
            mounted = plugins_pkg.mount_data_plugins()
            assert mounted == data / "plugins"
            assert plugins_pkg.mount_data_plugins() == mounted, "mount 不幂等"
            assert [str(p) for p in plugins_pkg.__path__].count(str(mounted)) == 1
            loaded = self.discover()

        assert "probe_installed" in loaded
        row = self.rows()["probe_installed"]
        assert row["source"] == "installed" and row["removable"] is True
        assert row["builtin"] is False

    def test_webui_from_data_root_is_still_not_removable(self) -> None:
        data = pathlib.Path(self._tmp.name) / "real-data"
        panel = data / "plugins" / "webui"
        panel.mkdir(parents=True)
        (panel / "plugin.py").write_text(OLD, encoding="utf-8")

        with mock.patch.dict(os.environ, {"QQBOT_DATA_DIR": str(data)}):
            plugins_pkg.mount_data_plugins()
            row = self.rows()["webui"]

        assert row["panel"] is True
        assert row["removable"] is False, "webui 从 data/plugins 来也不给卸"


class InventoryShapeTests(SecondRootCase):
    def test_every_row_has_the_same_keys(self) -> None:
        """清单行**成功与失败同一组键**：面板可以无脑读（`inventory()` 的契约）。"""

        broken = self.root / "broken"
        broken.mkdir()
        (broken / "plugin.py").write_text("import definitely_not_a_module_xyz\n", encoding="utf-8")
        self.write("probe_good", GOOD)

        rows = self.rows()

        # 不比整个集合（这棵树上还有别的插件）；钉的是"这两行都在、且同一组键"。
        assert {"probe_good", "broken"} <= set(rows), sorted(rows)
        expected = {"name", "title", "note", "version", "builtin", "source",
                    "enabled", "loaded", "requires", "removable", "panel"}
        assert set(rows["broken"]) == expected, sorted(rows["broken"])
        assert set(rows["probe_good"]) == expected, sorted(rows["probe_good"])

    def test_importlib_sees_a_namespace_plugin_module(self) -> None:
        """`importlib.import_module` 对没有 `__init__.py` 的插件目录**成立**（探针第 3 条）。"""

        self.write("probe_ns", GOOD)

        module = importlib.import_module(f"{plugins_pkg.__name__}.probe_ns.plugin")

        assert callable(module.register)
        assert str(self.root) in str(module.__file__)
