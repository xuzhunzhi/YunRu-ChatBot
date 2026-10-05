"""验证运行的留痕：**"全绿"必须留下可核对的凭证**。

## 为什么它是底层能力，而不是测试脚本里的几行

2026-10-02 的一次外部观察指出：`AGENTS.md` 写着"1139 个全绿、干净 clone 上也可复现"，
但**磁盘上找不到对应那次运行的记录**——`data/` 里最新的一份日志是更早提交上的 1115。
也就是说那句声明**无法被独立核对**：读者只能选择相信我，或者自己重跑一遍。

这类"声明无凭证"的问题不该靠"下次记得存一下"来解决——那就是没做配套。
所以它归**底层**：与 `feature_log.py`（按功能分的模型 I/O 日志）同一层，
由**唯一的验证入口**（`tests/run_offline.py`）无条件调用。

## 它记什么、记在哪

- **一行摘要**追加到 `<data>/logs/verify_runs.log`：
  `时间 sha=… branch=… dirty=… ran=… failed=… errors=… skipped=… result=PASS`
  带 `sha` 是为了让"某次全绿"能**绑定到某个提交**；带 `dirty` 是因为
  工作区脏的时候那次运行的**内容不等于任何提交**，凭证效力不同（必须如实标出来）。
- **整份输出**覆盖写到 `<data>/logs/verify_last.txt`：失败时要能直接看 traceback，
  不必让人重跑一遍（重跑会碰真实 `data/`，前几任审查者都因此不敢跑）。

写在 `data/` 下是**故意的**：那是 `.gitignore` 覆盖的目录，
所以这些日志（可能含本机路径、测试用的合成 prompt）**不会进公开仓库**。

## 失败不能拖垮验证

留痕本身出错时**只打一行 warning**，绝不改变退出码——否则"日志写不进去"
会伪装成"测试失败"。反过来也一样：`record_run()` 成功不代表测试通过，
`result` 字段才是结论。

## 在 worktree 里也要读得对（2026-10-05 修）

`git_head()` 原来只跟 `.git` 文件里的 `gitdir:`，**不跟 `commondir`**——于是
在 `git worktree add` 出来的树里，`HEAD` 读得到、分支 ref 读不到，返回
`('unknown', 'stage4-plugins')`，`tests/test_verify_log.py` 里那条
"不起子进程也能拿到 sha"的用例因此**假红**。现在 `_read_ref()` 会按
`commondir` 去共享目录里找 ref 与 `packed-refs`（见 `_common_dir()`）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: 摘要日志（一行一次运行，只追加）
SUMMARY_NAME = "verify_runs.log"
#: 最近一次运行的完整输出（每次覆盖）
LAST_OUTPUT_NAME = "verify_last.txt"


def _git_dir(root: Path) -> Path | None:
    """仓库的 git 目录。worktree 里的 `.git` 是一个指向真身的**文件**。"""

    marker = root / ".git"
    try:
        if marker.is_dir():
            return marker
        if marker.is_file():
            first = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if first.startswith("gitdir:"):
                # `gitdir:` 允许是**相对路径**（`git worktree add --relative-paths`），
                # 相对于那个 `.git` 文件所在的目录（gitrepository-layout 的规矩）。
                target = Path(first.split(":", 1)[1].strip())
                return target if target.is_absolute() else (marker.parent / target).resolve()
    except (OSError, IndexError):
        pass
    return None


def _common_dir(git: Path) -> Path:
    """`git` 目录对应的**共享** git 目录（跟 `commondir`，worktree 修的那一步）。

    ## 为什么必须有它（2026-10-05 实测到的假红）

    `git worktree add` 造出来的树里，`.git` 指向
    `<主库>/.git/worktrees/<名字>`——那是**这个 worktree 自己的** git 目录，里面只有
    `HEAD` / `index` / `ORIG_HEAD` 这类**每个 worktree 各一份**的东西。分支 ref
    （`refs/heads/<名字>`）与 `packed-refs` 在**共享**目录里，由那个目录下的
    `commondir` 文件指出来（内容通常是 `../..`）。

    不跟这个指针的后果：`git_head()` 读得到 HEAD、读不到那个 ref，于是**在一个完全
    干净、提交号明明就在磁盘上的 worktree 里**返回 `('unknown', <分支名>)`——
    `tests/test_verify_log.py::test_git_head_reads_without_spawning_git` 会因此**假红**
    （不是留痕坏了，是这段解析少走了一步）。

    没有 `commondir`（普通仓库、以及 `git worktree` 的主工作树）时就是 `git` 自己。
    """

    try:
        text = (git / "commondir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return git
    if not text:
        return git
    target = Path(text)
    # 同样允许相对路径；它相对于 `git` 目录本身。
    return target if target.is_absolute() else (git / target).resolve()


def _from_packed_refs(base: Path, ref: str) -> str:
    """分支被 pack 进 `packed-refs` 时（`git gc` 之后很常见）从那里找。"""

    try:
        for line in (base / "packed-refs").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    except OSError:
        pass
    return ""


def _read_ref(git: Path, common: Path, ref: str) -> str:
    """一个 ref 的 sha：worktree 自己的 git 目录 → 共享目录 → 两边的 `packed-refs`。

    顺序是宽松的（都读不到才算失败）：**跟错目录的代价是"提交号丢了"**，
    而这个文件存在的理由正是"全绿要能被绑定到某个提交"。
    """

    bases = [git] if common == git else [git, common]
    for base in bases:
        try:
            sha = (base / ref).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if sha:
            return sha
    for base in bases:
        sha = _from_packed_refs(base, ref)
        if sha:
            return sha
    return ""


def git_head(root: Path) -> tuple[str, str]:
    """`(短 sha, 分支名)`；取不到就是 `("unknown", "unknown")`。

    **直接读 `.git` 下的文件，不起子进程**：验证入口可能在受限沙箱里跑，
    那时 `subprocess` 抓输出会失败——而"记不下来 sha"不该让留痕整个失效。

    `HEAD` 读**这个 worktree 自己的** git 目录（每个 worktree 各一份），
    分支 ref 走 `_read_ref()`（它要跟 `commondir`，见那里的说明）。
    """

    git = _git_dir(root)
    if git is None:
        return "unknown", "unknown"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "unknown", "unknown"
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        branch = ref.rsplit("/", 1)[-1]
        sha = _read_ref(git, _common_dir(git), ref)
        return (sha[:7] or "unknown"), branch
    return (head[:7] or "unknown"), "(detached)"


def tree_is_dirty(root: Path) -> str:
    """工作区脏不脏：`"yes"` / `"no"` / `"?"`（取不到就如实说不知道）。

    这一步**需要 git 命令**（`status` 没法靠读文件算），所以在受限环境里可能拿不到——
    那就返回 `"?"`。**不要**因为拿不到就写 `"no"`：那会把"不知道为什么"说成"干净"。
    """

    import subprocess

    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                             capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001 - 拿不到就说不知道
        return "?"
    if out.returncode != 0:
        return "?"
    return "yes" if out.stdout.strip() else "no"


def _log_dir(root: Path) -> Path:
    """日志目录：跟着底层的 `dev_config.data_dir()` 走（面板也能读同一份口径）。"""

    override = os.environ.get("QQBOT_VERIFY_LOG_DIR", "").strip()
    if override:
        return Path(override)
    try:
        from . import dev_config

        return Path(dev_config.data_dir()) / "logs"
    except Exception:  # noqa: BLE001 - 连配置都读不到就退到仓库 data/
        return root / "data" / "logs"


def record_run(root: Path, *, label: str, ran: int, failures: int, errors: int,
               skipped: int, ok: bool, output: str = "") -> Path | None:
    """记下一次验证运行。返回摘要日志的路径；留痕失败返回 None（**不改退出码**）。

    `label` 是这次跑的是什么（例如 `offline`），这样以后加别的验证（离线冒烟、
    真机干跑）可以共用同一份日志而不互相覆盖。
    """

    try:
        directory = _log_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        sha, branch = git_head(root)
        dirty = tree_is_dirty(root)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = (f"{stamp} label={label} sha={sha} branch={branch} dirty={dirty} "
                f"ran={ran} failed={failures} errors={errors} skipped={skipped} "
                f"result={'PASS' if ok else 'FAIL'}")
        summary = directory / SUMMARY_NAME
        with summary.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        if output:
            (directory / LAST_OUTPUT_NAME).write_text(output, encoding="utf-8", newline="\n")
        logger.info("验证留痕: %s", line)
        return summary
    except Exception:  # noqa: BLE001 - 留痕失败绝不能改变验证结论
        logger.warning("验证留痕失败（不影响测试结论）", exc_info=True)
        return None


def read_runs(root: Path, *, label: str | None = None) -> list[dict[str, object]]:
    """读回摘要日志（给"核对某次声明"用）。

    一行坏掉只跳过那一行——**半份日志也比没有强**，不该因为一行格式问题就读不出来。
    """

    path = _log_dir(root) / SUMMARY_NAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    runs: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        stamp, _, rest = line.partition(" ")
        fields: dict[str, object] = {"at": stamp, "raw": line}
        for token in rest.split():
            key, _, value = token.partition("=")
            if not key or not value:
                continue
            fields[key] = int(value) if value.isdigit() else value
        if label is not None and fields.get("label") != label:
            continue
        runs.append(fields)
    return runs


def last_run(root: Path, *, label: str | None = None) -> dict[str, object] | None:
    """最近一次运行；没有就返回 None。"""

    runs = read_runs(root, label=label)
    return runs[-1] if runs else None


def as_json(run: dict[str, object] | None) -> str:
    """给日志/面板展示用（去掉原始行以免重复）。"""

    if not run:
        return "{}"
    return json.dumps({k: v for k, v in run.items() if k != "raw"},
                      ensure_ascii=False, sort_keys=True)
