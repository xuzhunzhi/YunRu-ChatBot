"""在**独立的测试副本**里跑离线测试——开发环境与测试环境分开。

## 为什么要有这个（2026-10-02，一次真实的损失）

在那之前，"跑测试"就是直接在开发树里跑 `tests/run_offline.py`。后果有两类，都真实发生过：

1. **测试会读这台机器的 `.env`**（真实凭据、真实超管号）。于是"测试结果"取决于
   本机配置，而同一个套件在干净 clone 上又是另一套结果——我们为此加了三处
   "没配就跳过"，但那只是遮住了症状。
2. **测试与探针会写开发树里的 `data/`**（`api_usage.json`、`control_audit.jsonl`、
   prompt 库、知识库…）。更糟的是我为了做验证，在开发树上反复 `git checkout` /
   `git stash` / `git archive`——**其中一次 `checkout` 把 `dev_config.py` 上
   一处未提交的本地修改（用户的真实超管号）还原成了仓库里的占位号**，
   用户当场失去全部 `/super` 权限，且没有任何告警。

根因不是"某条命令手滑"，而是**在同一个目录里既开发又验证**。
所以现在分开：

    <repo>/                开发环境（真实 .env、真实 data/、真实运行）
    <repo>/testenv/        测试副本（合成 .env、自己的 data/、随便折腾）

`testenv/` 在 `.gitignore` 里，永远不会被提交。

## 它做什么

1. 把 `src/`、`tests/`、`archive/` **复制**进 `testenv/`（跳过 `__pycache__`）；
2. 写一份**合成**的 `testenv/.env`：
   - key 一律是 `sk-offline-placeholder`（不碰真凭据）；
   - 超管/管理员钉成 `900000001`（与测试里用的 owner 一致 → 测试**与这台机器的
     真实配置无关**）；
   - **`QQBOT_DATA_DIR` 指向 `testenv/data/`** ← 这一条是"开发树绝对不被写"的关键；
   - 显式打开风格审核与记忆（让那三条"没配就跳过"的测试**真的跑起来**）；
3. 若本机有真人格（`data/private_docs/base_prompt.REAL.py`），**拷进副本**
   （否则人格内容级测试会跳过；用 `--no-persona` 可以故意试"干净 clone"那条路）；
4. `testenv/.venv` 做成本仓库 `.venv` 的**目录联接**（不复制几百 MB；失败也不算错）；
5. 在 `testenv/` 里跑 `tests/run_offline.py`，透传退出码。

## 用法

    .\\.venv\\Scripts\\python.exe run_tests.py                 # 建/刷新副本并跑套件
    .\\.venv\\Scripts\\python.exe run_tests.py --no-persona    # 走"干净 clone"那条路
    .\\.venv\\Scripts\\python.exe run_tests.py --no-refresh    # 不重建，直接跑（快）
    .\\.venv\\Scripts\\python.exe run_tests.py -- tests/run_offline.py --help

`--` 之后的参数原样交给被跑的那个脚本。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 这个脚本自己会打印中文路径与说明；控制台代码页不是 UTF-8 时会花掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - 老解释器/被重定向
        pass

REPO = Path(__file__).resolve().parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
DEFAULT_TARGET = REPO / "testenv"
COPY_DIRS = ("src", "tests", "archive")
#: 根目录的元数据文件也要拷——**测试会读它们**，缺了会有假失败：
#:   `.gitignore`             → `test_env_bot_is_gitignored` 等
#:   `.env.example`/`.env.bot.example` → `test_bot_example_template_is_complete_and_secret_free`
#:   `.gitattributes`         → 换行/编码约定
#:   `pyproject.toml`         → 有些测试读它拿元信息
COPY_FILES = (".gitignore", ".gitattributes", ".env.example", ".env.bot.example",
              "pyproject.toml", "requirements.txt")

#: 合成 `.env`——**故意不读开发树里那份**。
#:
#: 每一条都写清为什么：
SYNTHETIC_ENV = """\
# 由 run_tests.py 生成——**测试副本专用**，与开发树的 .env 无关。
# 不要在这里放真实凭据：它就在仓库里（虽然被 .gitignore 忽略）。

# 主 key：所有 agent 的兜底。占位符，任何真实调用都会失败——测试不该真的联网。
QQBOT_API_KEY=sk-offline-placeholder
# 判定 / 记忆 / 审核各自回落主 key（留空 = 回落）。
QQBOT_JUDGE_API_KEY=
QQBOT_MEMORY_API_KEY=sk-offline-placeholder
# 风格审核显式打开：否则 test_engine_sends_the_reviewed_line 会跳过。
QQBOT_STYLE_REVIEW=1
# 记忆维护显式打开：否则 test_startup_connects_service_and_successful_send_ack 会跳过。
QQBOT_MEMORY_ENABLED=1
# 识图打开（离线，不会真调）。
QQBOT_VISION=1

# **超管/管理员钉死**：测试里到处用 `900000001` 当 owner。
# 不钉的话，套件会跟着这台机器的真实超管号跑——那正是"测试结果取决于本机配置"。
QQBOT_SUPER_ADMIN_USER_IDS=900000001
QQBOT_ADMIN_USER_IDS=900000001

# ★ 最关键的一条：所有运行数据落在**副本自己的** data/ 下。
#   没有它，测试与探针会写开发树的 data/（用量账本、控制审计、prompt 库…）。
QQBOT_DATA_DIR={data_dir}

# 状态与日志也留在副本里（run_offline.py 还会各自再钉一次）。
QQBOT_STATE_PERSIST=0
QQBOT_DEBUG_MODEL_IO=0
"""


def _force_rmtree(path: Path) -> None:
    """删掉一棵树，**顺手清掉只读属性**。

    Windows 上 `shutil.rmtree` 撞到只读文件会 `PermissionError: [WinError 5] 拒绝访问`
    ——而副本里有 `.git/objects/pack/*.idx|*.pack`，那些**天然是只读的**
    （git 故意这么设）。第一次跑没问题（副本里那时还没有仓库），
    **第二次重建就撞上了**（2026-10-02 实测）。
    """

    import stat

    def onerror(func, target, _exc):          # noqa: ANN001 - 回调签名由标准库定
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            raise

    try:
        shutil.rmtree(path, onexc=onerror)     # 3.12+
    except TypeError:                          # pragma: no cover - 很老的解释器
        shutil.rmtree(path, onerror=onerror)   # type: ignore[call-arg]


def _copy_tree(src: Path, dst: Path) -> int:
    """复制一棵树，跳过 `__pycache__`。返回复制的文件数。"""

    count = 0
    for path in sorted(src.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def _junction(link: Path, target: Path) -> bool:
    """给副本做一个指向真实 `.venv` 的目录联接（不复制几百 MB）。

    用 `mklink /J`：它**不需要管理员权限**（符号链接才需要）。
    失败只当"副本里没有 .venv"——套件用真实的解释器跑，不受影响。
    """

    if link.exists() or link.is_symlink():
        return True
    if os.name != "nt":
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            return False
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    return done.returncode == 0


def _init_repo(target: Path) -> str:
    """在副本里 `git init` 并提交一次——**让它像一次干净 clone**。

    有几条测试验的是"这个文件被 git 跟踪吗 / 它被忽略了吗"（例如能力白名单必须进
    版本控制那条）。没有仓库它们会假红或不成立。副本自己有仓库之后：
    `git ls-files` / `git rev-parse` / `git status` 都是针对**副本**的，
    与开发树的仓库互不影响（`/testenv/` 在开发树的 `.gitignore` 里）。

    用 `-c user.*` 单次传入身份，**不碰这台机器的 git 全局配置**。
    失败不算错（那几条测试会如实红，而不是假装绿）。
    """

    if shutil.which("git") is None:
        return "（这台机器没有 git，仓库相关断言会红）"
    common = ["-c", "user.email=testenv@local", "-c", "user.name=testenv",
              "-c", "commit.gpgsign=false"]
    steps = (
        ["git", "init", "-q"],
        ["git", *common, "add", "-A"],
        ["git", *common, "commit", "-q", "-m", "testenv snapshot"],
    )
    for step in steps:
        done = subprocess.run(step, cwd=target, capture_output=True, text=True)
        if done.returncode != 0:
            return f"（`{' '.join(step[:2])}` 失败：{done.stderr.strip()[:80]}）"
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=target,
                          capture_output=True, text=True)
    return f"已建（HEAD={head.stdout.strip()}，工作区干净）"


def build(target: Path, *, persona: bool) -> None:
    """建/刷新测试副本。"""

    if target.resolve() == REPO:
        raise SystemExit("拒绝把仓库根当测试副本（那正是我们要避免的）")

    if target.exists():
        _force_rmtree(target)
    target.mkdir(parents=True)

    copied = 0
    for name in COPY_DIRS:
        source = REPO / name
        if source.is_dir():
            copied += _copy_tree(source, target / name)
        else:
            print(f"  ⚠️ 跳过 {name}/：不存在")
    for name in COPY_FILES:
        source = REPO / name
        if source.is_file():
            shutil.copy2(source, target / name)
            copied += 1

    (target / "data").mkdir(exist_ok=True)
    (target / ".env").write_text(
        SYNTHETIC_ENV.format(data_dir=target / "data"), encoding="utf-8", newline="\n")

    # 真人格：有就拷（内容级断言才有东西可断），用 --no-persona 排除。
    #
    # **两处都找**（2026-10-02 目录拆成 dev/run 之后）：
    #   1. `dev/data/private_docs/`  —— 如果在 dev 里
    #   2. `../run/data/private_docs/` —— 拆开之后真人格住在**部署树**那边
    #      （它是生产数据，跟着 run/ 走）。不看这里的话，
    #      每次跑测试都会退回模板人格，内容级断言等于没跑。
    persona_candidates = (
        REPO / "data" / "private_docs" / "base_prompt.REAL.py",
        REPO.parent / "run" / "data" / "private_docs" / "base_prompt.REAL.py",
    )
    persona_src = next((p for p in persona_candidates if p.is_file()), None)
    persona_note = f"（两处都没有，退回模板人格；试过 {len(persona_candidates)} 个路径）"
    if persona and persona_src is not None:
        dst = target / "data" / "private_docs"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(persona_src, dst / persona_src.name)
        persona_note = f"已拷入副本：{persona_src.parent.parent.parent.name}/ 那份（不打印内容）"
    elif not persona:
        persona_note = "按 --no-persona 排除（试『干净 clone』那条路）"

    venv_note = "已联接" if _junction(target / ".venv", REPO / ".venv") else "未联接（用真实解释器跑）"
    repo_note = _init_repo(target)
    print(f"  副本：{target}")
    print(f"  复制了 {copied} 个文件；真人格：{persona_note}；.venv：{venv_note}")
    print(f"  副本内的 git 仓库：{repo_note}")
    print(f"  合成 .env 写好了（QQBOT_DATA_DIR={target / 'data'}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在独立副本里跑离线测试（开发树只读）")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="副本目录（默认 testenv/）")
    parser.add_argument("--no-refresh", action="store_true", help="不重建副本，直接跑")
    parser.add_argument("--no-persona", action="store_true", help="不把这台机器的真人格拷进副本")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="`--` 之后要跑的脚本（默认 tests/run_offline.py）")
    args = parser.parse_args(argv)

    target = Path(args.target)
    if not target.is_absolute():
        target = REPO / target

    if not VENV_PY.is_file():
        print(f"找不到解释器 {VENV_PY}")
        return 2

    if args.no_refresh:
        if not (target / "tests" / "run_offline.py").is_file():
            print(f"{target} 里没有套件，去掉 --no-refresh 先建一次")
            return 2
        print(f"  复用副本：{target}")
    else:
        build(target, persona=not args.no_persona)

    command = [a for a in args.command if a != "--"] or ["tests/run_offline.py"]
    script = target / command[0]
    if not script.is_file():
        print(f"副本里没有 {script}")
        return 2

    print(f"  运行：{command[0]} {' '.join(command[1:])}".rstrip())
    print("  " + "-" * 68)
    env = {**os.environ, "PYTHONPATH": str(target / "src"), "PYTHONIOENCODING": "utf-8",
           # 只读开发树：让任何"写回仓库"的路径都指不到仓库
           "QQBOT_DATA_DIR": str(target / "data")}
    done = subprocess.run([str(VENV_PY), str(script), *command[1:]],
                          cwd=target, env=env)
    print("  " + "-" * 68)
    print(f"  退出码 {done.returncode}；开发树没有被这次运行写过（数据全在 {target.name}/）")
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
