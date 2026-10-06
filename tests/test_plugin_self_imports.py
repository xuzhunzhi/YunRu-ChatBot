"""插件的**绝对自引用**与**跨插件 import**：两条静态断言 + 一条改名的实测。

## 为什么要有这个文件（2026-10-06 外部审查实测）

`vision` / `mail` 两个插件把**自己的文件夹名**刻进了源码：

    from qq_roleplay_bot.plugins.vision.vision import ImageDescriber, …
    from ...plugins.mail.mail_client import MailClient

代价不是"难看"，是**改名即隐身**：把 `plugins/vision/` 改名成 `zz_vision/` 之后，
那一行 `ModuleNotFoundError`，而 `discover()` 对这条错误的处理是记一行
`plugin_import_missing_module` 然后**静默不装**——插件从 `loaded` 里消失、从面板清单
里消失，部署方看不出是"改名改坏了"。对照组（`group_admin` / `join_approval` / `webui`）
用相对 import，改名照装。

所以这里钉两条：

1. `test_no_plugin_has_an_absolute_self_import`——插件源码里不许出现
   `qq_roleplay_bot.plugins.<自己>` 的 **import**（字符串里提名字不算，见 `find_issues`）；
2. `test_no_plugin_imports_another_plugin`——插件之间不许互相 import
   （`AGENTS.md` §2.3：插件之间用 `REQUIRES` 表达依赖，不许直接 import 对方的模块）。
   这是**报告项**：审查要求"有就报，别自己修"。实际是**已经有一条**：
   `plugins/webui/webui_panel.py` 里 `from ..join_approval.join_approval import
   JOIN_SUB_TYPE, parse_pending`（见那个测试的 docstring，它把这一条钉成红色之前
   先把事实写清楚）。

## 还有一条"改名的实测"（同一个文件，端到端的），它在子进程里跑

`test_a_renamed_plugin_still_loads_and_registers`：把 `plugins/` 整棵复制到一个
**临时目录**、把 `vision/` `mail/` 改名成 `zz_vision/` `zz_mail/`，在**新进程**里
只让 `plugins.__path__` 指向那棵副本树，跑真实的 `discover()`，要求：

* `zz_vision` / `zz_mail` 都在 `loaded` 里；
* 日志里**没有** `plugin_import_missing_module` / `plugin_register_failed`。

**为什么在子进程里**（不是图省事）：本进程里 `discover()` 早就把真插件 import 过了，
`sys.modules` 里已经有 `qq_roleplay_bot.plugins.vision.vision`——改名前那个绝对
import 于是照样找得到，测出来是**假绿**。新进程里只有副本树，那条路才真的是断的
（实测对照：真树 `loaded=('mail','vision')`、0 条坏日志；副本里改回绝对写法
`loaded=()`、`plugin_import_missing_module name=zz_vision missing=…` +
`plugin_register_failed name=zz_mail`）。
"""
from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from qq_roleplay_bot import plugins as plugins_module  # noqa: E402

#: 这些包名属于"核心"，不是"另一个插件"。插件 import 核心是**允许**的方向。
PLUGIN_PACKAGE = "qq_roleplay_bot.plugins"

#: 已知的、**故意的**例外（名字 → 为什么）——目前为空：审查要求"有就报，不要自己修"，
#: 所以真有的话由测试红出来，再由人决定，不在这里悄悄放行。
KNOWN_CROSS_PLUGIN_IMPORTS: dict[str, str] = {}


def plugin_dirs() -> list[pathlib.Path]:
    """`plugins/` 下每一个插件目录（有 `plugin.py` 的，且不是包根自己）。"""

    found: list[pathlib.Path] = []
    for root in plugins_module.__path__:
        base = pathlib.Path(root)
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "plugin.py").is_file():
                found.append(child)
    return found


def dotted_name(node: ast.AST | None) -> str:
    """把 `ast.Attribute` / `ast.Name` 拼回点号名字；拼不出来就回空串。"""

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def import_targets(tree: ast.AST) -> list[tuple[str, int]]:
    """文件里每一条 import 的**目标模块名** + 行号（相对 import 的前导点原样保留）。

    只认**静态**写法（`import X` / `from X import Y`）。`importlib.import_module("…")`
    与字符串里提模块名不算命中——`plugins/__init__.py` 与 `prompt_library` 就靠
    `find_spec("qq_roleplay_bot.plugins.vision.vision")` **按名字问"在不在"**，
    那不是 import，也不该被判红。
    """

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * int(node.level or 0)
            found.append((prefix + (node.module or ""), node.lineno))
    return found


def python_sources(directory: pathlib.Path) -> list[pathlib.Path]:
    """目录下所有 `.py`（跳过 `__pycache__`）。**只扫这个插件的目录**，不跟进符号链接。"""

    return sorted(path for path in directory.rglob("*.py")
                  if "__pycache__" not in path.parts)


def find_issues(directory: pathlib.Path) -> list[str]:
    """这个插件目录里的两类问题（回可读的行，空 = 干净）。"""

    plugin_name = directory.name
    self_prefix = f"{PLUGIN_PACKAGE}.{plugin_name}"
    issues: list[str] = []
    for path in python_sources(directory):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:            # 坏文件先让 pyflakes / 导入去报
            issues.append(f"{path}:{exc.lineno} 语法错误（{exc.msg}）")
            continue
        relative = path.relative_to(directory).as_posix()
        for target, line in import_targets(tree):
            if target == self_prefix or target.startswith(self_prefix + "."):
                issues.append(f"{plugin_name}/{relative}:{line} 绝对自引用 {target}")
            elif target.startswith(PLUGIN_PACKAGE + "."):
                issues.append(f"{plugin_name}/{relative}:{line} 跨插件 import {target}")
    return issues


def test_no_plugin_has_an_absolute_self_import() -> None:
    """插件不许把自己文件夹的名字刻进源码——改名之后它就静默不装了。"""

    directories = plugin_dirs()
    assert directories, "一个插件目录都没扫到，断言等于没跑"
    import_issues = [issue for directory in directories
                     for issue in find_issues(directory) if "绝对自引用" in issue]
    assert not import_issues, (
        "插件里出现绝对自引用（把自己的文件夹名刻进源码）。改成相对 import"
        "（`from .vision import …`，照 `group_admin/plugin.py` 的写法）：\n"
        + "\n".join(import_issues))


def test_no_plugin_imports_another_plugin() -> None:
    """插件之间不许互相 import：依赖用 `REQUIRES` 表达（`AGENTS.md` §2.3）。

    **这条是"报告项"**：审查要求"有就报，别自己修"。所以它红了要先看事实——
    顺序问题由 `discover()` 的 `REQUIRES` 负责，直接 import 对方的模块绕开了那套。
    """

    cross = [issue for directory in plugin_dirs()
             for issue in find_issues(directory) if "跨插件 import" in issue]
    for issue in cross:
        plugin = issue.split("/", 1)[0]
        assert plugin in KNOWN_CROSS_PLUGIN_IMPORTS, (
            f"插件 import 了别的插件的模块：\n{issue}\n"
            "（要留的话把它写进 KNOWN_CROSS_PLUGIN_IMPORTS，并写清为什么）")


def test_a_renamed_plugin_still_loads_and_registers() -> None:
    """**改名的实测**：复制一份改名（`zz_vision` / `zz_mail`）之后照样装上。

    子进程 + `plugins.__path__` 只指副本树（理由见模块 docstring：本进程的
    `sys.modules` 会让改名前那种写法**假绿**）。副本树用 `finally` 删干净。
    """

    worker = textwrap.dedent(
        '''
        import logging, pathlib, sys
        sys.path.insert(0, sys.argv[3])
        from qq_roleplay_bot import plugins as P
        from qq_roleplay_bot.plugins import PluginRegistry

        BAD = ("plugin_import_missing_module", "plugin_register_failed",
               "plugin_import_failed", "plugin_skipped_missing_dependency")

        class Grab(logging.Handler):
            def __init__(self):
                super().__init__(level=logging.DEBUG)
                self.lines = []
            def emit(self, record):
                self.lines.append(record.getMessage() % () if record.args
                                  else record.getMessage())

        handler = Grab()
        logging.getLogger("qq_roleplay_bot.plugins").addHandler(handler)
        P.__path__ = [sys.argv[1]]          # **只有副本树这一个根**
        loaded = P.discover(PluginRegistry(), only=("zz_vision", "zz_mail"))
        print("LOADED=" + ",".join(loaded))
        print("BAD=" + "|".join(l for l in handler.lines if any(b in l for b in BAD)))
        '''
    )

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="zz_rename_by_test_"))
    try:
        copy = tmp / "zz_plugins"
        shutil.copytree(ROOT / "src" / "qq_roleplay_bot" / "plugins", copy,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "vision").rename(copy / "zz_vision")
        (copy / "mail").rename(copy / "zz_mail")
        script = tmp / "worker.py"
        script.write_text(worker, encoding="utf-8", newline="\n")

        result = subprocess.run(
            [sys.executable, str(script), str(copy), "",
             str(ROOT / "src")],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, f"子进程失败：{result.stderr[-2000:]}"
        fields = dict(
            line.split("=", 1) for line in result.stdout.splitlines()
            if "=" in line and line.split("=", 1)[0] in {"LOADED", "BAD"})
        loaded = tuple(name for name in fields.get("LOADED", "").split(",") if name)
        bad = [line for line in fields.get("BAD", "").split("|") if line]

        assert "zz_vision" in loaded, f"改名后的识图插件没装上：loaded={loaded}"
        assert "zz_mail" in loaded, f"改名后的邮件插件没装上：loaded={loaded}"
        assert not bad, "改名后插件又静默不装了：\n" + "\n".join(bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        assert not tmp.exists(), f"副本没删干净：{tmp}"
