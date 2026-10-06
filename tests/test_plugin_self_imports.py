r"""插件的**绝对自引用**与**跨插件 import**：两条静态断言 + 一条改名的实测。

## 为什么要有这个文件（2026-10-06 外部审查实测）

`vision` / `mail` 两个插件把**自己的文件夹名**刻进了源码：

    <包前缀>.plugins.vision.vision 里取 `ImageDescriber`
    <包前缀>.plugins.mail.mail_client 里取 `MailClient`

（原文是两句 `from … import …`，这里写成散文：`check_module_removal.py` 用正则
`^\s*(from|import)\s+\S*\b<模块名>\b` 扫"哪些测试文件在顶层 import 了它"，
文档里摆一句真 import 会被它当成"这个测试依赖那个模块"。）

代价不是"难看"，是**改名即隐身**：把 `plugins/vision/` 改名成 `zz_vision/` 之后，
那一行 `ModuleNotFoundError`，而 `discover()` 对这条错误的处理是记一行
`plugin_import_missing_module` 然后**静默不装**——插件从 `loaded` 里消失、从面板清单
里消失，部署方看不出是"改名改坏了"。对照组（`group_admin` / `join_approval` / `webui`）
用相对 import，改名照装。

所以这里钉两条：

1. `test_no_plugin_has_an_absolute_self_import`——插件源码里不许出现
   `plugins.<自己>` 的 **import**（字符串里提名字不算，见 `self_imports`）；
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

**靶子插件这次部署没装就跳过**（拔 `vision/` 的核对路径，见那个测试的 docstring）；
靶子还在却装不上，照样红。

**为什么在子进程里**（不是图省事）：本进程里 `discover()` 早就把真插件 import 过了，
`sys.modules` 里已经有 `qq_roleplay_bot.plugins.vision.vision`——改名前那个绝对
import 于是照样找得到，测出来是**假绿**。新进程里只有副本树，那条路才真的是断的
（实测对照：真树 `loaded=('mail','vision')`、0 条坏日志；副本里改回绝对写法
`loaded=()`、`plugin_import_missing_module name=zz_vision missing=…` +
`plugin_register_failed name=zz_mail`）。
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))

from qq_roleplay_bot import plugins as plugins_module  # noqa: E402

#: 这些包名属于"核心"，不是"另一个插件"。插件 import 核心是**允许**的方向。
PLUGIN_PACKAGE = "qq_roleplay_bot.plugins"

#: **已知的、故意留着的**跨插件 import（"插件/文件:行 目标" → 为什么）。只允许**这一条**：
#: 它是本次改动**之前就在**的，审查要求"有就报，别自己修"（见汇报），所以这里把它写死，
#: 一条不多、一条不少——这一条将来被撤掉，`assertEqual` 也会红（提醒把这里删掉）。
ALLOWED_CROSS_PLUGIN_IMPORTS: dict[str, str] = {
    "webui/webui_panel.py:561 from ..join_approval.join_approval import JOIN_SUB_TYPE, parse_pending": (
        "面板的「入群请求」那一页要用 `join_approval` 的解析函数（`JOIN_SUB_TYPE` / "
        "`parse_pending`）。**这是报告项**（2026-10-06 外部审查：'插件 import 别的插件'"
        "那条禁令照旧）——要不要改成走接缝由人定，测试不替它决定，但也不许多出第二条。"
    ),
}


def plugin_dirs() -> list[pathlib.Path]:
    """`plugins/` 下每一个插件目录（有 `plugin.py` 的，且不是包根自己）。"""

    found: list[pathlib.Path] = []
    for root in plugins_module.__path__:
        base = pathlib.Path(root)
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "plugin.py").is_file():
                found.append(child)
    return found


def plugin_dir(name: str) -> pathlib.Path | None:
    """按名字找一个插件目录；**这次部署没装它**就返回 `None`（调用方自己跳过）。"""

    for root in plugins_module.__path__:
        candidate = pathlib.Path(root) / name
        if (candidate / "plugin.py").is_file():
            return candidate
    return None


def cross_plugin_imports(directory: pathlib.Path) -> list[tuple[str, int, str, str]]:
    """这个插件里**碰了别的插件**的 import。

    判据同样是点号路径：含 `qq_roleplay_bot.plugins.`，但后面那一段**不是自己**
    ——`from ..join_approval.join_approval import …` 走的就是这个形状（前导点会被
    剥掉，剩下的 `.join_approval.join_approval` 是**别的插件**的名字）。
    只认**静态** import 语句：`importlib.import_module("…")` 与字符串里提模块名不算
    （`plugins/__init__.py` 与 `prompt_library` 用 `find_spec("…plugins.vision.vision")`
    按名字问"在不在"，那不是 import，也不该被判红）。
    """

    prefix = f"{PLUGIN_PACKAGE}."
    own_root = f"{PLUGIN_PACKAGE}.{directory.name}"
    #: 兄弟插件的名字（`..join_approval.join_approval` 里 `join_approval` 那一段就是它）——
    #: 相对 import 里**不会**出现 `qq_roleplay_bot.plugins.` 这段字面，所以光按前缀判会漏。
    siblings = {other.name for other in plugin_dirs()} - {directory.name}
    hits: list[tuple[str, int, str, str]] = []
    for path in python_sources(directory):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        relative = path.relative_to(directory).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(prefix) and not alias.name.startswith(own_root):
                        hits.append((relative, node.lineno, alias.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                walked = "." * int(node.level or 0) + (node.module or "")
                named = [alias.name for alias in node.names]
                written = "from " + walked + " import " + ", ".join(named)
                plain = walked.lstrip(".")
                if plain.startswith(prefix) and not plain.startswith(own_root):
                    hits.append((relative, node.lineno, written, plain))
                elif plain.split(".")[0] in siblings:
                    hits.append((relative, node.lineno, written, plain))
                elif any(name.startswith(prefix) and not name.startswith(own_root)
                         for name in named):
                    # `from qq_roleplay_bot.plugins import join_approval` 这种也认。
                    hits.append((relative, node.lineno, written, ",".join(named)))
    return hits


def python_sources(directory: pathlib.Path) -> list[pathlib.Path]:
    """目录下所有 `.py`（跳过 `__pycache__`）。**只扫这个插件的目录**，不跟进符号链接。"""

    return sorted(path for path in directory.rglob("*.py")
                  if "__pycache__" not in path.parts)


def self_imports(directory: pathlib.Path) -> list[tuple[str, int, str, str]]:
    """这个插件里"把自己文件夹名写进 import 路径"的地方。

    判据是 import 的**模块路径里出现 `plugins.<自己这个目录名>`**（按点号分段认，
    不是子串——`plugins` 后面必须正好是自己那个名字，接着是 `.` 或结束）。
    两种写法都要抓：

    | 写法 | 为什么算 |
    | --- | --- |
    | `from qq_roleplay_bot.plugins.vision.vision import …` | 绝对路子，写死了 `vision` |
    | `from ...plugins.mail.mail_client import …` | `...` 从 `…plugins.mail` 升三级正好落回 `…plugins`，后面的 `plugins.mail` 一样写死了 `mail`（**混在相对 import 里**） |

    **为什么不按"解析后的绝对模块名"判**（试过，会误伤）：`from .webui_access import …`
    在 `plugins/webui/wire.py` 里解析后是 `…plugins.webui.webui_access`，也"以自己为前缀"——
    可那是同一目录里的兄弟模块，是好写法；`plugins/webui/webui/` 这个**同名嵌套包**
    更会把这一类全变成假红。所以认的是"源码里那条模块路径"，与审查的 grep 同口径。
    """

    pattern = re.compile(rf"(?:^|\.)plugins\.{re.escape(directory.name)}(?:\.|$)")
    hits: list[tuple[str, int, str, str]] = []
    for path in python_sources(directory):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:                   # 坏文件先让 pyflakes / 导入去报
            continue
        relative = path.relative_to(directory).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if pattern.search(alias.name):
                        hits.append((relative, node.lineno, alias.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                # `module` 是点号路径，`level` 是前导点个数。**只看模块路径，不看
                # `import` 后面的那些名字**：`from . import wire`（`mail/plugin.py:18`）
                # 的 `module` 是 `None`，名字 `wire` 里也当然不含自己的包名——
                # 把那些名字也拿来判就会误伤（`webui_panel.py:37` 的
                # `from . import webui_data` 差点被算进去）。
                walked = "." * int(node.level or 0) + (node.module or "")
                if pattern.search(walked):
                    named = [alias.name for alias in node.names]
                    written = "from " + walked + " import " + ", ".join(named)
                    hits.append((relative, node.lineno, written, walked))
    return hits


def find_hits(directory: pathlib.Path) -> list[tuple[str, str, int, str]]:
    """这个插件目录里的问题：`(种类, 这个插件里的相对路径, 行号, 写的是什么)`。

    种类两样：`"self"`（绝对自引用）与 `"cross"`（跨插件 import）。
    """

    plugin_name = directory.name
    hits: list[tuple[str, str, int, str]] = []
    for relative, line, written, _ in self_imports(directory):
        hits.append(("self", relative, line, written))
    for relative, line, written, _ in cross_plugin_imports(directory):
        hits.append(("cross", relative, line, written))
    _ = plugin_name
    return hits


def _describe(directory: pathlib.Path, hit: tuple[str, str, int, str]) -> str:
    _, relative, line, target = hit
    return f"{directory.name}/{relative}:{line} {target}"


def test_no_plugin_has_an_absolute_self_import() -> None:
    """插件不许把自己文件夹的名字刻进源码——改名之后它就静默不装了。"""

    directories = plugin_dirs()
    assert directories, "一个插件目录都没扫到，断言等于没跑"
    bad = [_describe(directory, hit)
           for directory in directories
           for hit in find_hits(directory) if hit[0] == "self"]
    assert not bad, (
        "插件里出现绝对自引用（把自己的文件夹名刻进源码）。改成相对 import"
        "（`from .vision import …`，照 `group_admin/plugin.py` 的写法）：\n"
        + "\n".join(bad))


def test_no_plugin_imports_another_plugin() -> None:
    """插件之间不许互相 import：依赖用 `REQUIRES` 表达（`AGENTS.md` §2.3）。

    **这条是"报告项"**：审查要求"有就报，别自己修"。所以它红的含义是
    "多出了一条（或者少了一条已知的）"，先看事实再决定，别顺手改插件——
    顺序问题由 `discover()` 的 `REQUIRES` 负责，直接 import 对方的模块绕开了那套。
    """

    directories = plugin_dirs()
    assert directories, "一个插件目录都没扫到，断言等于没跑"
    cross = [_describe(directory, hit)
             for directory in directories
             for hit in find_hits(directory) if hit[0] == "cross"]
    assert sorted(cross) == sorted(ALLOWED_CROSS_PLUGIN_IMPORTS), (
        "跨插件 import 的清单变了（插件之间要用 `REQUIRES` 表达依赖）。\n"
        f"实测：{sorted(cross)}\n已知：{sorted(ALLOWED_CROSS_PLUGIN_IMPORTS)}")


def test_a_renamed_plugin_still_loads_and_registers() -> None:
    """**改名的实测**：复制一份改名（`zz_vision` / `zz_mail`）之后照样装上。

    子进程 + `plugins.__path__` 只指副本树（理由见模块 docstring：本进程的
    `sys.modules` 会让改名前那种写法**假绿**）。副本树用 `finally` 删干净。

    **两个靶子插件必须都在**（`vision` / `mail`）：这条本来就只测"这两个曾经写死
    文件夹名的插件改名后照样装"。所以先查目录，缺了才跳过——那说明**这次部署真没装它**
    （例如 `tests/check_module_removal.py` 正在验"把 `vision/` 拔掉核心照跑"），
    不是测试被放水。**靶子还在却装不上，照样红。**
    """

    targets = []
    for name in ("vision", "mail"):
        directory = plugin_dir(name)
        if directory is None:
            raise unittest.SkipTest(f"这次部署没有 {name}/（拔插件的核对路径），跳过改名实测")
        targets.append((directory, f"zz_{name}"))
    assert len(targets) == 2
    # `outage_notice` **不参与改名**（虽然它也在这棵树里）：它声明的是
    # `REQUIRES = ("mail",)`，副本里前置被改名成 `zz_mail`，于是它必然被
    # `plugin_skipped_missing_dependency` 跳过——那是"前置不在就跳过自己"，**是对的**，
    # 与"绝对自引用"无关。要把它测成绿的，就得在副本里连 `REQUIRES` 一起改写，
    # 那测的就不是同一件事了（而它本来就没有自引用，见 `test_no_plugin_has_an_...`）。

    #: 子进程里跑的那段（`run_single.py` 用 unittest 收集，所以写成 `def test_`）。
    #: **参数全部走环境变量**，不给 `run_single.py` 传额外的位置参数：它把 `argv` 里
    #: 第一个之后的东西也当成"要加载的测试文件"（实测：多给一个参数就报
    #: `'NoneType' object has no attribute 'loader'`）。
    #: `src` 的路径直接写进生成的文件里——worker 住在临时目录，自己插一下最稳。
    worker = textwrap.dedent(
        f'''
        import logging, os, sys

        sys.path.insert(0, {str(ROOT / "src")!r})
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


        def test_renamed_plugins_load() -> None:
            roots = os.environ["ZZ_RENAME_ROOTS"].split(os.pathsep)
            names = os.environ["ZZ_RENAME_NAMES"].split(",")
            handler = Grab()
            logging.getLogger("qq_roleplay_bot.plugins").addHandler(handler)
            P.__path__ = list(roots)        # **只留副本树那几个根**
            loaded = P.discover(PluginRegistry(), only=tuple(names))
            bad = [line for line in handler.lines if any(b in line for b in BAD)]
            print("LOADED=" + ",".join(loaded))
            print("BAD=" + "|".join(bad))
            for name in names:
                assert name in loaded, f"{{name}} 改名后没装上：{{loaded}}"
            assert not bad, "\\n".join(bad)
        '''
    )

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="zz_rename_by_test_"))
    try:
        copy = tmp / "zz_plugins"
        shutil.copytree(ROOT / "src" / "qq_roleplay_bot" / "plugins", copy,
                        ignore=shutil.ignore_patterns("__pycache__"))
        new_names = []
        for directory, new_name in targets:
            (copy / directory.name).rename(copy / new_name)
            new_names.append(new_name)
        script = tmp / "worker.py"
        script.write_text(worker, encoding="utf-8", newline="\n")

        # 子进程跑的是 `tests/run_single.py`（它会 `record_run(label="unit")`）。
        # **别让它往真 `data/logs` 里写一份"某次全绿"的凭证**：那个凭证说的是一次
        # 子进程内的单文件跑，混进留痕会让"某个提交全绿过"这句话对不上
        # （`QQBOT_VERIFY_LOG_DIR` 是留痕自己就认的环境变量，见 `verify_log._log_dir`）。
        env = dict(os.environ, QQBOT_VERIFY_LOG_DIR=str(tmp / "verify_logs"),
                   ZZ_RENAME_ROOTS=str(copy), ZZ_RENAME_NAMES=",".join(new_names))
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "run_single.py"), str(script)],
            cwd=str(tmp), capture_output=True, text=True, encoding="utf-8", env=env)
        assert result.returncode == 0, (
            "子进程（改名后的副本树）没跑过：\n" + (result.stdout or "")[-2000:]
            + (result.stderr or "")[-2000:])
        fields = dict(
            line.split("=", 1) for line in result.stdout.splitlines()
            if "=" in line and line.split("=", 1)[0] in {"LOADED", "BAD"})
        loaded = tuple(name for name in fields.get("LOADED", "").split(",") if name)
        bad = [line for line in fields.get("BAD", "").split("|") if line]
        assert fields.get("LOADED") is not None, (
            "子进程没报 `LOADED=`（worker 没跑起来？）：\n" + (result.stdout or "")[-2000:])

        for _, new_name in targets:
            assert new_name in loaded, f"改名后的插件没装上：{new_name} loaded={loaded}"
        assert not bad, "改名后插件又静默不装了：\n" + "\n".join(bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        assert not tmp.exists(), f"副本没删干净：{tmp}"
