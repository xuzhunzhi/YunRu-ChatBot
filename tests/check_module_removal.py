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

## 两列都要看（这是它存在的第二个理由）

**判据问的是"能不能跑起来"，不是"能不能 import"。**

`import qq_roleplay_bot.runtime` 能过，是因为那些可选能力的 import 在**函数体里**；
真正的入口是 `build_engine()` → `serve`。2026-10-02 实测过：修之前
`vision` / `qq_roles` / `control_audit` 三个模块的 `import` 全绿、`build_engine` 全炸。
我原来那版**只报了 import 那一列**，于是给出误导性的"已解耦"。

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
DEFAULT_MODULES = ("vision", "qq_roles", "control_audit", "typing_sim", "help_card",
                   "provider_registry", "runtime_diagnostics")

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


def probe(module: str) -> tuple[bool, bool, str]:
    """拦掉 `module` 之后探一次：`(能不能 import 核心, build_engine 能不能起, 原因)`。"""

    full = f"qq_roleplay_bot.{module}"
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


def main(argv: list[str]) -> int:
    if not PY.exists():
        print(f"找不到解释器 {PY}（这个脚本要在仓库根目录、用仓库自带的 .venv 跑）")
        return 2
    names = argv or list(DEFAULT_MODULES)
    verdicts = []
    for name in names:
        if not (PKG / f"{name}.py").exists():
            print(f"跳过 {name}：{PKG / f'{name}.py'} 不存在")
            continue
        core_ok, engine_ok, reason = probe(name)
        verdicts.append((name, core_ok, engine_ok, reason, test_files_importing(name)))

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
    if failed:
        print(f"不通过：{', '.join(failed)}——删掉之后 `build_engine()` 起不来。")
        return 1
    print(f"全部通过：{len(verdicts)} 个可插能力都能拔掉而核心照跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
