"""把 dev 的代码部署到 run/（实测运行）。

目录拆分（2026-10-02）之后的部署方式：**从 dev 复制**，而不是让两边共享一个检出。

    dev/   你写代码、跑测试的地方（git 仓库在这里）
    run/   bot 实际运行的那份（真实 .env、真实 data/、backups/）

这个脚本只碰**代码**：`src/` 与几个启动脚本。**绝不碰** `run/.env`、
`run/data/`、`run/backups/`——那是生产数据（记忆库在里面）。

用法：

    .\\.venv\\Scripts\\python.exe deploy_to_run.py            # 复制并报告差异
    .\\.venv\\Scripts\\python.exe deploy_to_run.py --dry-run   # 只看会改什么
    .\\.venv\\Scripts\\python.exe deploy_to_run.py --force     # 跳过"工作区脏"的提醒

它会在复制前**记下 `run/` 那份的指纹**，复制后核对 `run/.env` 与其
`data/` 一个字节没动——"部署一下把记忆库搞没了"是这里唯一不可接受的失败。
"""
from __future__ import annotations

import argparse
import filecmp
import hashlib
import shutil
import subprocess
from pathlib import Path

DEV = Path(__file__).resolve().parent
RUN = DEV.parent / "run"

#: 部署过去的**代码**。刻意是白名单而不是"整个 dev/"——
#: 黑名单式的同步早晚会把 tests/、docs/ 甚至 .env 一起搬过去。
DEPLOY_DIRS = ("src",)
DEPLOY_FILES = ("run_stage3.bat", "run_yunru_hidden.vbs", "watch_memory.py", "watch_round2.py")

#: **绝不碰**（生产数据）。列出来是为了让检查显式、可读。
PROTECTED = ("data", "backups", ".env", ".tmp_test_run")


def fingerprint(path: Path) -> tuple[int, str]:
    """`(文件数, 内容哈希)`。

    ⚠️ **只对"部署不该碰且不会被 bot 写"的东西用哈希**。对 `data/` 用它是错的：
    2026-10-03 实测踩到——bot 正在跑、正在追加 `data/logs/*.jsonl`，于是部署期间
    哈希必变，脚本报"生产数据被动过"并退出码 1，**而其实一个字节都不是部署改的**
    （复制只发生在 `src/` 与那几个启动脚本上）。那次误报差点让人以为部署把数据搞坏了。
    """

    if path.is_file():
        return 1, hashlib.sha256(path.read_bytes()).hexdigest()
    files = sorted(p for p in path.rglob("*") if p.is_file())
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(path).as_posix().encode())
        digest.update(p.read_bytes())
    return len(files), digest.hexdigest()


def count_files(path: Path) -> int:
    """只数文件数——**部署能保证的东西**，且不受"bot 正在追加日志"干扰。

    部署的白名单只有 `src/` 与四个启动脚本，所以它能破坏的东西只有一种：
    **删掉** `data/`、`backups/`、`.env` 里的东西。文件数只增不减就是这条保证的证据。
    """

    if not path.exists():
        return -1
    if path.is_file():
        return 1
    return sum(1 for p in path.rglob("*") if p.is_file())


def changed_files(src: Path, dst: Path) -> list[str]:
    """列出会变化的文件（相对路径）。"""

    out: list[str] = []
    for path in sorted(src.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            continue
        if not target.is_file() or not filecmp.cmp(path, target, shallow=False):
            out.append(str(rel))
    return out


def git_state() -> str:
    done = subprocess.run(["git", "status", "--short"], cwd=DEV,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (done.stdout or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 dev 的代码部署到 run/")
    parser.add_argument("--dry-run", action="store_true", help="只报告会改什么")
    parser.add_argument("--force", action="store_true", help="工作区脏也照部署")
    args = parser.parse_args(argv)

    if not RUN.is_dir():
        print(f"找不到 {RUN}——run/ 还没建好？")
        return 2

    state = git_state()
    if state and not args.force and not args.dry_run:
        print("dev 的工作区是脏的（下面这些改动也会一起部署过去）：")
        for line in state.splitlines():
            print("   ", line)
        print("  确认没问题就加 --force；或者先提交。")

    # 部署前的生产数据**文件数**（不哈希：bot 正在跑时 data/ 一直在被追加，
    # 哈希必变、必然误报——原因见 `fingerprint` 的说明）
    before = {name: count_files(RUN / name) for name in PROTECTED if (RUN / name).exists()}

    print(f"\n=== 将要部署（{'dry-run' if args.dry_run else '复制'}）")
    total = 0
    for name in DEPLOY_DIRS:
        diff = changed_files(DEV / name, RUN / name)
        total += len(diff)
        print(f"  {name}/: {len(diff)} 个文件会变")
        for rel in diff[:12]:
            print(f"      {rel}")
        if len(diff) > 12:
            print(f"      …… 还有 {len(diff) - 12} 个")
        if not args.dry_run and diff:
            # 只覆盖变化的文件（保留 run/ 里别的东西）
            for rel in diff:
                src, dst = DEV / name / rel, RUN / name / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    for name in DEPLOY_FILES:
        src = DEV / name
        if not src.is_file():
            continue
        dst = RUN / name
        same = dst.is_file() and filecmp.cmp(src, dst, shallow=False)
        if not same:
            total += 1
            print(f"  {name}: 会更新")
            if not args.dry_run:
                shutil.copy2(src, dst)
        else:
            print(f"  {name}: 一致")

    if args.dry_run:
        print(f"\n（dry-run：没有写任何东西；共 {total} 个文件会变）")
        return 0

    print("\n=== 核对：部署没删掉生产数据里的任何东西")
    ok = True
    for name, was in before.items():
        now = count_files(RUN / name)
        # 只增不减才算安全：bot 在跑时这些目录本来就会长（日志、记忆库）。
        same = now >= was
        ok = ok and same
        delta = now - was
        note = f"（+{delta}，bot 运行中的正常写入）" if delta > 0 else ""
        print(f"  {'✓' if same else '✗'} {name}: {now} 个文件"
              f"{note}{'' if same else '  ← 少了文件！'}")
    if not ok:
        print("\n!! 有文件消失——立刻停下查原因")
        return 3
    print(f"\n部署完成；共 {total} 个文件变化。"
          f"复制只发生在 src/ 与启动脚本上，run/ 的 data/、backups/、.env 没被写。")
    print("提示：bot 要**重启**才会用上新代码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
