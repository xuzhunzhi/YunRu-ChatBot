"""验证留痕（`qq_roleplay_bot.verify_log`）——底层能力，自己也要被测。

由来（2026-10-02 一次外部观察）：`AGENTS.md` 声称"1139 个全绿、干净 clone 上也可复现"，
但磁盘上**找不到对应那次运行的记录**，所以那句话无法被独立核对。用户指出
"你是没做好配套日志吗，**这属于底层**"——所以留痕不是"下次记得存一下"，
而是底层提供、由唯一验证入口无条件调用的能力。

这个文件钉住它的三件事：
1. 记的东西**够核对**（时间、提交、工作区脏不脏、四个计数、结论）；
2. 结论**如实**（失败记 FAIL，工作区脏就写 `dirty=yes`——脏的时候那次运行的
   内容不等同于任何提交，凭证效力不同）；
3. 留痕本身出错**不能改变验证结论**（不抛异常、不改退出码）。
"""
import pathlib
import tempfile


def _modules():
    import sys
    sys.path.insert(0, "src")
    from qq_roleplay_bot import verify_log as V

    return V


def test_records_a_line_that_can_be_read_back() -> None:
    """记一行、能读回来，而且字段够核对。"""

    V = _modules()
    root = pathlib.Path(".").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["QQBOT_VERIFY_LOG_DIR"] = tmp
        try:
            path = V.record_run(root, label="unit", ran=1139, failures=0, errors=0,
                                skipped=1, ok=True, output="saved output\n")
            assert path is not None and path.is_file()
            run = V.last_run(root, label="unit")
            assert run is not None
            # 够核对：时间、提交、脏不脏、四个计数、结论
            for key in ("at", "sha", "branch", "dirty", "ran", "failed",
                        "errors", "skipped", "result"):
                assert key in run, f"摘要里缺 {key}：{run}"
            assert run["ran"] == 1139 and run["skipped"] == 1
            assert run["result"] == "PASS"
            assert len(str(run["sha"])) == 7 or run["sha"] == "unknown"
            assert run["dirty"] in {"yes", "no", "?"}
            # 整份输出也存了（失败时不必让人重跑一遍）
            assert (pathlib.Path(tmp) / V.LAST_OUTPUT_NAME).read_text(
                encoding="utf-8") == "saved output\n"
        finally:
            os.environ.pop("QQBOT_VERIFY_LOG_DIR", None)


def test_a_failed_run_is_recorded_as_fail() -> None:
    """**失败也要留痕**，而且结论必须如实是 FAIL。

    只记成功的那次，等于把留痕变成宣传材料——那就白做了。
    """

    V = _modules()
    root = pathlib.Path(".").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["QQBOT_VERIFY_LOG_DIR"] = tmp
        try:
            V.record_run(root, label="unit", ran=1139, failures=2, errors=1,
                         skipped=0, ok=False, output="boom\n")
            run = V.last_run(root, label="unit")
            assert run is not None
            assert run["result"] == "FAIL"
            assert run["failed"] == 2 and run["errors"] == 1
        finally:
            os.environ.pop("QQBOT_VERIFY_LOG_DIR", None)


def test_appends_instead_of_overwriting() -> None:
    """只追加：历史不能丢（"上周那次是绿的"本身也是证据）。"""

    V = _modules()
    root = pathlib.Path(".").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["QQBOT_VERIFY_LOG_DIR"] = tmp
        try:
            for i in range(3):
                V.record_run(root, label="unit", ran=i, failures=0, errors=0,
                             skipped=0, ok=True)
            runs = V.read_runs(root, label="unit")
            assert len(runs) == 3
            assert [r["ran"] for r in runs] == [0, 1, 2]
        finally:
            os.environ.pop("QQBOT_VERIFY_LOG_DIR", None)


def test_label_filters_runs() -> None:
    """以后加别的验证（冒烟、真机干跑）时共用同一份日志，每条能被分开取。"""

    V = _modules()
    root = pathlib.Path(".").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["QQBOT_VERIFY_LOG_DIR"] = tmp
        try:
            V.record_run(root, label="offline", ran=1, failures=0, errors=0, skipped=0, ok=True)
            V.record_run(root, label="smoke", ran=2, failures=0, errors=0, skipped=0, ok=True)
            assert V.last_run(root, label="offline")["ran"] == 1
            assert V.last_run(root, label="smoke")["ran"] == 2
            assert len(V.read_runs(root)) == 2, "不带 label 时取全部"
        finally:
            os.environ.pop("QQBOT_VERIFY_LOG_DIR", None)


def test_a_broken_line_does_not_break_reading_the_rest() -> None:
    """半份日志也比没有强：一行坏掉只跳过那一行。"""

    V = _modules()
    root = pathlib.Path(".").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        import os

        os.environ["QQBOT_VERIFY_LOG_DIR"] = tmp
        try:
            V.record_run(root, label="unit", ran=1, failures=0, errors=0, skipped=0, ok=True)
            log = pathlib.Path(tmp) / V.SUMMARY_NAME
            with log.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("这不是一行合法摘要\n\n")
            V.record_run(root, label="unit", ran=2, failures=0, errors=0, skipped=0, ok=True)
            runs = V.read_runs(root, label="unit")
            assert [r["ran"] for r in runs] == [1, 2], runs
        finally:
            os.environ.pop("QQBOT_VERIFY_LOG_DIR", None)


def test_logging_failure_returns_none_instead_of_raising() -> None:
    """**留痕失败不能改变验证结论**：返回 None、不抛异常、不打乱退出码。

    否则"日志写不进去"会伪装成"测试失败"（或反过来）——那比没有日志更糟。
    """

    V = _modules()
    root = pathlib.Path(".").resolve()
    import os

    # 指到一个**不可能写成目录**的路径：把一个已存在的文件当目录用。
    with tempfile.TemporaryDirectory() as tmp:
        blocker = pathlib.Path(tmp) / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        os.environ["QQBOT_VERIFY_LOG_DIR"] = str(blocker / "logs")
        try:
            assert V.record_run(root, label="unit", ran=1, failures=0, errors=0,
                                skipped=0, ok=True) is None
        finally:
            os.environ.pop("QQBOT_VERIFY_LOG_DIR", None)


def test_git_head_reads_without_spawning_git() -> None:
    """`git_head` 靠**读 `.git` 下的文件**拿提交号，不起子进程。

    为什么要在意：验证入口可能在受限沙箱里跑，那时 `subprocess` 抓输出会失败——
    而"记不下来 sha"不该让整条留痕失效。这里顺带钉住它返回的形状（7 位短 sha）。
    """

    V = _modules()
    root = pathlib.Path(".").resolve()
    sha, branch = V.git_head(root)
    assert sha == "unknown" or (len(sha) == 7 and all(c in "0123456789abcdef" for c in sha)), sha
    assert branch, "分支名不该是空串（取不到时是 unknown / (detached)）"
    # 与 git 自己的答案对照（起子进程只在这条测试里，且失败就跳过对照）
    import subprocess

    real = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                          capture_output=True, text=True)
    if real.returncode == 0:
        assert sha == real.stdout.strip()[:7]
