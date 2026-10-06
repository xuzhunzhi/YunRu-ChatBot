"""门槛：**把某个可插能力拿掉，核心还起不起得来**（判据的机械检查）。

判据（用户）："删掉它，Stage 3 会不会出问题。"

## 为什么它在 `tests/` 里，而不是 `data/`

2026-10-02 一位外部审查者指出：`AGENTS.md` §三 把某个脚本列为"改可插能力后必须跑的
门槛"，而那个脚本在 `/data/`（忽略范围）里、**未跟踪**——于是**干净 clone 拿不到它**，
门槛只在那台机器上成立。写在文档里的门槛如果不在仓库里，它就不是门槛。

所以它搬到这里，并改成一个**不需要写权限**的实现（见下）。

## 为什么不改文件名（原来那版的做法）

原来那版把 `src/qq_roleplay_bot/<模块>.py` 改名成 `.bak`、跑一次、再改回来。缺点：

- **要写权限**：只读沙箱里根本跑不了（那位审查者就是因此没法验证这道门槛）；
- **会短暂破坏工作区**：跑挂了就留下 `.bak`；
- 跑完还得复探一次确认"还原成功了"。

现在换成 **meta-path 拦截器**：在一个全新解释器里插一个 finder，让目标模块
`import` 时抛 `ModuleNotFoundError`。**不碰磁盘、不改任何文件、只读也能跑**，
而且更接近"这个模块真的不在"这件事。

拦哪个模块名由 `_block_target()` 现算：包根那个文件在就拦 `qq_roleplay_bot.<名字>`，
`plugins/<名字>/plugin.py` 在就拦 `qq_roleplay_bot.plugins.<名字>` **整棵子树**
（2026-10-05 加的后一种：识图那时从包根搬成了插件，而判据问的正是它——
**不能因为"文件不在包根了"就把它从名单里去**，那等于把这道门槛悄悄关掉）。

## 两列都要看（这是它存在的第二个理由）

**判据问的是"能不能跑起来"，不是"能不能 import"。**

`import qq_roleplay_bot.runtime` 能过，是因为那些可选能力的 import 在**函数体里**；
真正的入口是 `build_engine()` → `serve`。2026-10-02 实测过：修之前
`vision` / `qq_roles` / `control_audit` 三个模块的 `import` 全绿、`build_engine` 全炸。
（`qq_roles` 已于 2026-10-06 归核心并改名 `group_roles`，见 `DEFAULT_MODULES` 上面
那段说明——**它现在是核心依赖，不再是本脚本探的对象**。）
我原来那版**只报了 import 那一列**，于是给出误导性的"已解耦"。

## 名单里的一项查不了时**不许静默跳过**（2026-10-05 修）

原来那版遇到"包根与 `plugins/` 下都没有它"就打印一行"跳过 X"，然后**照样**
打印"全部通过：7 个可插能力都能拔掉而核心照跑"。实测（把 6 个插件文件夹移走、
只留 `plugins/__init__.py`）：`vision` 那一项**静默从 7 项变 6 项**，结论却长得像 7 项全过。
（那两个"7"是**当时**的名单长度：2026-10-06 `qq_roles` 进核心后 `DEFAULT_MODULES`
是**六项**，且其中 `vision` 在**本体侧**不参与检查——见 `AGENTS.md` §三那段说明。）

现在分三种情况，各有各的说法：

| 情况 | 处理 |
| --- | --- |
| 包根 `<名字>.py` 在，或 `plugins/<名字>/plugin.py` 在 | 正常拦掉它、探一次 |
| 由插件提供（`PLUGIN_PROVIDED`），**而这条线不带插件** | 显式说明"本树没有插件，故不参与检查"，**不计入通过数** |
| 哪儿都没有，而**这条线带插件** | 报错、退出码 1（名字写错 / 能力被删了） |

"这条线带不带插件"由 `_has_plugin_folders()` 判：它看 **git 跟踪的**文件
（`plugins/` 下除 `__init__.py` 之外还有没有别的），因为光看目录会出错——
`plugins/__pycache__/` 会让"有插件"成真（2026-10-05 实测踩过这个坑，
`vision` 因此被报成"哪都找不到"）。

用法（在仓库根目录）：

    .\\.venv\\Scripts\\python.exe tests\\check_module_removal.py
    .\\.venv\\Scripts\\python.exe tests\\check_module_removal.py vision control_audit

退出码：全部 `build_engine` 都起得来是 0，否则 1（这样它能进脚本化的门槛）。
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "qq_roleplay_bot"
TESTS = ROOT / "tests"
PY = ROOT / ".venv" / "Scripts" / "python.exe"

#: 默认检查哪些"可插能力"。判据上它们都不该是底层必需品。
#:
#: 2026-10-05：识图从包根 `vision.py` 搬成了插件 `plugins/vision/`——这条判据
#: 问的正是它，所以**不能**因为"文件不在包根了"就把它从名单里去（那等于把这道
#: 门槛悄悄关掉）。名字保持 `vision`，拦截的模块名由 `probe()` 现算
#: （见 `_block_target`）。
DEFAULT_MODULES = ("vision", "control_audit", "typing_sim", "help_card",
                   "provider_registry", "runtime_diagnostics")

#: 名单里**由插件提供**的那几个（包根已经没有对应的 `.py` 了）。
#:
#: 2026-10-05 加：`vision` 从包根搬进了 `plugins/vision/`。于是"本树里找不到它"
#: 有三种完全不同的含义，必须分开说（这是这一版修的东西）：
#:
#: - 它在 `plugins/<名字>/plugin.py` 里 → **正常检查**（判断核心拔掉它还能不能跑）；
#: - 它哪儿都没有，**而这条线本来就不带插件**（本体侧的正常形态）→ 这一项
#:   **没法检查**，输出要显式说明、并且**不计入通过项**；
#: - 它哪儿都没有，而这条线**带插件** → 名单里的名字真找不到了（名字写错、
#:   或能力被删），这是**错误**，退出码 1。
#:
#: 原来那版是"第一种之外一律打印一行『跳过 X』"，然后**照样打印
#: 『全部通过：7 个可插能力都能拔掉而核心照跑』**——实测（把 6 个插件文件夹移走、
#: 只留 `plugins/__init__.py`）`vision` **静默**从 7 项变 6 项，结论却长得像 7 项
#: 全过。**不许为了让输出好看而删名字。**（那个"7"是当时的名单长度：2026-10-06
#: `qq_roles` 进核心之后 `DEFAULT_MODULES` 是六项，见下面那段。）
#:
#: ## 2026-10-06：`qq_roles` 从这个名单里**撤掉**了（这里如实交代）
#:
#: 用户当天拍板 **"行，进核心"**：群成员角色事实（`group_roles.py`）从"可插能力"
#: 变成**核心能力**——判据是"删掉它，Stage 3 会不会出问题？"答案是**会**
#: （她认不出谁是管理员/群主，自己的身份也不知道）。
#:
#: 所以它的名字从 `DEFAULT_MODULES` 里撤掉了，并**没有**改成"算一项通过"：
#: 它的缺席是"搭不起来"，不是"已解耦"。为什么不放进下面这一组：
#: `PLUGIN_PROVIDED` 的含义是"**名字由插件提供**、这条线不带插件所以查不了"，
#: 而 `group_roles` 是核心自己的模块、插件侧没有它——塞进去等于换一种方式撒谎。
#:
#: 同一轮里 `src/qq_roleplay_bot/qq_roles.py`（单源角色之后的死代码）已删除。
PLUGIN_PROVIDED = ("vision",)

#: 在一个**全新解释器**里拦掉指定的模块，然后看核心能不能起来。
#: `{blocked!r}` 是完整模块名集合；`{src!r}` 是 `src` 的绝对路径。
PROBE = r"""
import sys

src = {src!r}
blocked = set({blocked!r})
sys.path.insert(0, src)

# 先把已经被别人 import 过的目标从缓存里踢掉，再插拦截器——
# 否则 `sys.modules` 会短路，拦截器根本没机会说话。
for name in blocked:
    sys.modules.pop(name, None)

import importlib.abc


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked:
            raise ModuleNotFoundError("blocked for the probe: %s" % fullname, name=fullname)
        return None


sys.meta_path.insert(0, _Blocker())

try:
    from qq_roleplay_bot import runtime
except Exception as exc:
    print("core=no %s: %s" % (type(exc).__name__, str(exc)[:80]))
    raise SystemExit(0)

print("core=yes")


class FakeTransport:
    async def call_api(self, action, params=None):
        return {{"status": "ok", "retcode": 0,
                 "data": {{"role": "owner", "user_id": "9", "nickname": "n"}}}}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


try:
    runtime.build_engine(FakeTransport())
except Exception as exc:
    print("engine=no %s: %s" % (type(exc).__name__, str(exc)[:90]))
else:
    print("engine=yes")
"""

#: **按目录拦**的探法（2026-10-06 加，补上原来那两列的盲区）。
#:
#: 由来：外部审查实测出 `stage3_main.execute_action` 里有两句
#: `from .plugins.group_admin.group_admin import execute`——
#: **核心在"核心 → 插件"方向上直接 import 了具体插件的模块**。
#: 而 `DEFAULT_MODULES` 是六项短名单（`vision` / `control_audit` / …），
#: `group_admin` **不在里面**，所以这条拴缚**一道检查都没有**；
#: `_block_target()` 的注释又写着"核心就算绕过插件去 import 里面的模块，也必须能降级"——
#: 那句话在这条路径上从来没被证实过。
#:
#: 这个探法与上面那两列的问法不同：它不问"删掉 X 之后核心能不能起来"，
#: 它问**"核心还会不会自己去 import 那个插件的模块"**——把那棵子树整段拦掉，
#: 再往注册表里塞一个假执行函数，看核心走的是不是注册表那条路。
#: 走对了 → 打印 `IFACE_OK`；绕过注册表去 import → 撞上拦截器 → 走不到那一句。
#:
#: 验收（2026-10-06 实测，两个方向都跑了）：在 `bd13bd4` **之前**的提交上它是
#: `group=no`（正确指认那处直连），改完是 `group=ok`。
GROUP_ACTION_PROBE = r"""
import asyncio
import sys

src = {src!r}
prefix = {prefix!r}
sys.path.insert(0, src)

for name in [key for key in sys.modules if key == prefix or key.startswith(prefix + ".")]:
    sys.modules.pop(name, None)

import importlib.abc


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == prefix or fullname.startswith(prefix + "."):
            raise ModuleNotFoundError("blocked for the probe: %s" % fullname, name=fullname)
        return None


sys.meta_path.insert(0, _Blocker())

try:
    from qq_roleplay_bot import runtime
    from qq_roleplay_bot.command_plugins import ActionRequest
except Exception as exc:
    print("core=no %s: %s" % (type(exc).__name__, str(exc)[:80]))
    raise SystemExit(0)

print("core=yes")


class FakeTransport:
    async def call_api(self, action, params=None):
        return {{"status": "ok", "retcode": 0,
                 "data": {{"role": "owner", "user_id": "9", "nickname": "n"}}}}

    async def send(self, target, text, **kwargs):
        return None

    async def start(self):
        return None


try:
    engine = runtime.build_engine(FakeTransport())
except Exception as exc:
    print("engine=no %s: %s" % (type(exc).__name__, str(exc)[:90]))
    raise SystemExit(0)

print("engine=yes")

# 拦掉 {plugin!r} 这棵子树之后，核心**不该**再自己去 import 它：
# 往注册表里塞一个假执行函数，看核心调的是不是它。
#
# 塞的方式优先用注册表那一侧的正式接口（`provide_group_action`，登记一个工厂），
# 再用**核心自己的装配函数**把它变成 `engine.group_actions`——
# 于是探法走的是生产那条路（与 `build_engine` 里同一行代码）。
# 老注册表没有那个接口时退回私有表，好让同一个探法在"改之前"的树上跑得起来、
# 给出干净的判词（而不是撞在 AttributeError 上）——对照实验的价值全在这里。
seen = []


async def _fake_execute(kind, **kwargs):
    seen.append(kind)
    return "IFACE_OK"


def _fake_factory(usage_store):
    return _fake_execute


_registry = engine.plugin_registry
if hasattr(_registry, "provide_group_action"):
    _registry.provide_group_action({group!r}, _fake_factory)
    runtime._build_group_actions(engine, _registry, getattr(engine, "usage_store", None))
elif hasattr(_registry, "_group_actions"):
    _registry._group_actions[{group!r}] = _fake_execute
else:
    print("group=no 注册表上既没有 group_action 接缝、也没有 _group_actions")
    raise SystemExit(0)
try:
    text = asyncio.run(engine.execute_action(
        ActionRequest(group={group!r}, kind="kick", target_id="123"),
        group_id="717151356", actor_id="900000001"))
except Exception as exc:
    print("group=no %s: %s" % (type(exc).__name__, str(exc)[:160]))
    raise SystemExit(0)

if text == "IFACE_OK":
    print("group=ok")
else:
    print("group=no 回的是 %r（核心没走注册表那条路：它要么去 import 那个插件、"
          "要么根本没接上注册表）" % (text,))
"""

#: 核心**直连插件模块**的已知拴缚：`{插件目录名: (分组名, 人话说明)}`。
#:
#: 为什么写死在名单里、而不是遍历 `plugins/*/`：**遍历拦不出这一类耦合**——
#: `stage3_main` 那两句 import 只在真的收到那种 `ActionRequest` 时才执行，
#: 所以"把目录拦掉再 build_engine"永远是绿的（2026-10-06 实测确认）。
#: 名单写死之后，下面那个探法是**真的去调一次那个动作**，于是它判得出来。
#: 代价是新增一处直连时要来这里补一行；这正是 `main()` 里那条规矩：
#: 名单里有的插件目录**在树上却探不出东西**时报错，而不是静默算过。
CORE_TO_PLUGIN_COUPLINGS = {
    "group_admin": ("group_admin", "群管理动作的执行函数（原来按模块名 import）"),
}


def test_files_importing(name: str) -> list[str]:
    """哪些测试文件在**顶层** import 了这个模块（收集阶段就会崩）。

    那些文件是"这个模块自己的测试"，不是"核心依赖它"的证据。
    """

    pattern = re.compile(rf"^\s*(from|import)\s+(\S*\b{re.escape(name)}\b)", re.M)
    hits = []
    for path in sorted(TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            if match.start() == line_start:   # 只看顶层（行首无缩进）的 import
                hits.append(path.name)
                break
    return hits


def _block_target(name: str) -> str | None:
    """`name` 这个可插能力**在哪**、拦哪个模块名；两处都没有就返回 `None`。

    两种情况（2026-10-05 加第二种，因为识图搬进了插件）：

    - 包根模块：`src/qq_roleplay_bot/<name>.py` → 拦 `qq_roleplay_bot.<name>`；
    - 插件：`src/qq_roleplay_bot/plugins/<name>/plugin.py` → 拦
      `qq_roleplay_bot.plugins.<name>`（**整棵子树**：连它自己的 `vision.py` 一起拦掉，
      等价于"这个文件夹整个不在"）。拦整棵子树而不是只拦 `plugin.py`：核心就算
      绕过插件去 import 里面的模块，也必须能降级——那正是判据要问的。
    """

    if (PKG / f"{name}.py").exists():
        return f"qq_roleplay_bot.{name}"
    if (PKG / "plugins" / name / "plugin.py").is_file():
        return f"qq_roleplay_bot.plugins.{name}"
    return None


def probe(module: str) -> tuple[bool, bool, str]:
    """拦掉 `module` 之后探一次：`(能不能 import 核心, build_engine 能不能起, 原因)`。"""

    full = _block_target(module) or f"qq_roleplay_bot.{module}"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
           # 干跑：别碰真实的运行状态文件
           "QQBOT_STATE_PERSIST": "0"}
    code = PROBE.format(src=str(ROOT / "src"), blocked=[full])
    proc = subprocess.run([str(PY), "-c", code], cwd=ROOT, capture_output=True,
                          text=True, errors="replace", env=env)
    out = proc.stdout + proc.stderr
    core_ok = "core=yes" in out
    engine_ok = "engine=yes" in out
    reason = ""
    for line in out.splitlines():
        if line.startswith("engine=no"):
            reason = line[len("engine=no"):].strip()
            break
        if line.startswith("core=no"):
            reason = reason or line[len("core=no"):].strip()
    if not reason:
        tail = [line for line in out.strip().splitlines() if line.strip()][-1:]
        reason = tail[0][:90] if tail else "?"
    return core_ok, engine_ok, reason


def probe_group_action(plugin: str, group: str) -> tuple[bool, bool, bool, str]:
    """**按目录拦**探一次：`(插件目录在不在, 核心与装配能不能起, 有没有走注册表, 原因)`。

    与 `probe()` 的区别：那个问"核心还能不能起来"，这个问**"核心还会不会自己去
    import 那个插件"**。做法是把 `qq_roleplay_bot.plugins.<plugin>` 整棵子树拦掉，
    再往注册表里塞一个假执行函数，然后**真的调一次那个动作**——核心走注册表那条路
    才拿得到 `IFACE_OK`；它要是绕过注册表去 `import`，就会撞上拦截器、
    落到那句 fail-closed 的文案上。
    """

    present = (PKG / "plugins" / plugin / "plugin.py").is_file()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8",
           # 干跑：别碰真实的运行状态文件
           "QQBOT_STATE_PERSIST": "0"}
    code = GROUP_ACTION_PROBE.format(src=str(ROOT / "src"),
                                     prefix=f"qq_roleplay_bot.plugins.{plugin}",
                                     plugin=plugin, group=group)
    proc = subprocess.run([str(PY), "-c", code], cwd=ROOT, capture_output=True,
                          text=True, errors="replace", env=env)
    out = proc.stdout + proc.stderr
    core_ok = "core=yes" in out
    engine_ok = "engine=yes" in out
    iface_ok = "group=ok" in out
    reason = ""
    for line in out.splitlines():
        if line.startswith("group=no"):
            reason = line[len("group=no"):].strip()
            break
        if line.startswith("engine=no"):
            reason = reason or line[len("engine=no"):].strip()
        if line.startswith("core=no"):
            reason = reason or line[len("core=no"):].strip()
    if not reason:
        tail = [line for line in out.strip().splitlines() if line.strip()][-1:]
        reason = tail[0][:90] if tail else "?"
    return present, core_ok or engine_ok, iface_ok, reason


def _has_plugin_folders() -> bool:
    """这条线**带不带插件**（即"是不是本体侧那棵树"），不是"目录在不在"。

    判据用 **git 跟踪的文件**，因为 `plugins/` 里总有 `__init__.py`（发现器）与
    `__pycache__/`，光看目录会得出相反的答案——2026-10-05 实测踩过：把 6 个插件
    文件夹移走之后 `__pycache__/` 让"有插件"成了真，于是 `vision` 被报成
    "哪都找不到"（错的那一类）。

    - 跟踪的文件里除 `plugins/__init__.py` 之外还有别的 → **这条线带插件**；
    - 只有 `__init__.py`（本体侧的正常形态）→ 不带；
    - git 用不了（受限沙箱）：退到"有没有哪个子目录里躺着 `plugin.py`"。

    这是**只读**查询，不改仓库；拿不准时它宁可说"带插件"（那会把一项报成错误，
    而不是把一项静默算成通过）。
    """

    plugins = PKG / "plugins"
    if not plugins.is_dir():
        return False
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", "src/qq_roleplay_bot/plugins"],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            tracked = [line.strip() for line in out.stdout.splitlines() if line.strip()]
            return any(not line.endswith("/plugins/__init__.py") for line in tracked)
    except Exception:  # noqa: BLE001 - 拿不到就退到磁盘形状
        pass
    return any(child.is_dir() and (child / "plugin.py").is_file()
               for child in plugins.iterdir())


def main(argv: list[str]) -> int:
    if not PY.exists():
        print(f"找不到解释器 {PY}（这个脚本要在仓库根目录、用仓库自带的 .venv 跑）")
        return 2
    names = argv or list(DEFAULT_MODULES)
    verdicts = []
    plugin_provided = []      # 名字由插件提供，而**这条线不带插件** → 无从检查
    nowhere = []              # 包根与 plugins/ 下都没有，而这条线并不缺插件 → 名单出错了
    tree_has_plugins = _has_plugin_folders()
    for name in names:
        if _block_target(name) is not None:
            core_ok, engine_ok, reason = probe(name)
            verdicts.append((name, core_ok, engine_ok, reason, test_files_importing(name)))
        elif name in PLUGIN_PROVIDED and not tree_has_plugins:
            plugin_provided.append(name)
        else:
            nowhere.append(name)

    print("=" * 96)
    print("模块                 能 import   build_engine 能起     它自己的测试（收集阶段就会崩）")
    print("=" * 96)
    failed = []
    for name, core_ok, engine_ok, reason, own_tests in verdicts:
        core = "能" if core_ok else "不能"
        # **判据是这一列**：能不能真跑起来。`import` 那列只作对照。
        engine = "能（已解耦）" if engine_ok else "**不能（还被拴着）**"
        if not engine_ok:
            failed.append(name)
        print(f"{name:20} {core:9} {engine:22} {', '.join(own_tests) or '—'}")
        if not engine_ok:
            print(f"{'':20} 原因: {reason}")
    print("=" * 96)

    # --- 第二列检查：**核心有没有直连插件目录**（2026-10-06 加） -----------------
    #
    # 上面那张表问"把某项拿掉，核心还起不起得来"；它**看不到**"核心绕过注册表去
    # import 插件模块"这类拴缚（`group_admin` 就是这么藏了很久的）。这一段专门问它。
    couplings = []
    missing_couplings = []
    for plugin, (group, why) in sorted(CORE_TO_PLUGIN_COUPLINGS.items()):
        present, core_ok, iface_ok, reason = probe_group_action(plugin, group)
        if not present and not tree_has_plugins:
            continue          # 本体侧的正常形态：本来就没有插件，这一项无从检查
        if not present:
            missing_couplings.append((plugin, why))
            continue
        couplings.append((plugin, group, core_ok, iface_ok, why, reason))
    if couplings or missing_couplings:
        print("核心与插件之间的拴缚（拦掉整棵插件目录，再走一次那个动作）")
        print("-" * 96)
        for plugin, group, core_ok, iface_ok, why, reason in couplings:
            verdict = "已解耦（走注册表）" if iface_ok else "**还拴着（核心自己 import 插件）**"
            print(f"{plugin:20} {verdict:32} {why}")
            if not iface_ok:
                print(f"{'':20} 原因: {reason}")
                failed.append(f"{plugin}:{group}")
        print("-" * 96)

    if missing_couplings:
        for plugin, why in missing_couplings:
            print(f"**名单里记着 {plugin}（{why}），但这条线上找不到 "
                  f"plugins/{plugin}/plugin.py**：这一项探不了。")
        print("  要么把它从 CORE_TO_PLUGIN_COUPLINGS 里如实交代掉，要么改回名字——"
              "**不许让一项静默消失**。")

    # 没参与检查的那几项**必须显式列出来**：静默少查一项，结论就不该长得像"全过"。
    if plugin_provided:
        print(f"由插件提供、这条线不带插件，故**不参与检查**"
              f"（{len(plugin_provided)} 项）：{', '.join(plugin_provided)}")
        print(f"  说明：这些能力在 plugins/<名字>/plugin.py 里，而本树的 "
              f"{PKG / 'plugins'} 下没有插件文件夹。")
        print(f"  本体侧的正常形态就是这样：{', '.join(plugin_provided)} 由插件提供，"
              f"核心不依赖它才是对的。")
        print("  要真正验它们得在**带插件的树**上跑（插件侧那条分支）——"
              "所以这项**没有**算进下面的通过数。")
    if nowhere:
        print(f"**名单里有名字，但包根与 plugins/ 下都找不到（{len(nowhere)} 项）："
              f"{', '.join(nowhere)}**")
        print(f"  找过：{PKG / '<名字>.py'} 与 {PKG / 'plugins' / '<名字>' / 'plugin.py'}"
              f"（这条线{'带' if tree_has_plugins else '不带'}插件）。")
        print("  这不是通过，是这一项已经不在本仓库里了——名字写错了，"
              "或者能力真的被删了。要么改回名字，要么在 PLUGIN_PROVIDED / "
              "DEFAULT_MODULES 里如实交代。")

    if failed:
        print(f"不通过：{', '.join(failed)}——"
              f"要么删掉之后 `build_engine()` 起不来，要么核心还在按插件模块名直连。")
        return 1
    # 报数如实：查了几项、几项没查。**不要**把没查的算进"全过"里。
    tail = f"检查了 {len(verdicts)} 项，全部能拔掉而核心照跑。"
    if plugin_provided:
        tail += f"另有 {len(plugin_provided)} 项由插件提供、这条线不带插件，未参与检查。"
    if nowhere:
        tail += f"另有 {len(nowhere)} 项在包根与 plugins/ 下都找不到。"
    if couplings:
        tail += f"另有 {len(couplings)} 项核心↔插件拴缚检查全部走注册表。"
    if missing_couplings:
        tail += (f"另有 {len(missing_couplings)} 项拴缚名单里的插件在这条线上不存在，"
                 f"探不了。")
    print(f"通过：{tail}")
    return 1 if (nowhere or missing_couplings) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
