"""模型原始 I/O 追踪测试。离线；追踪默认关闭，测试里显式打开。

注意：追踪会保留完整 prompt 与聊天内容，因此这里同时验证"默认关闭"这一安全属性。
"""
import json
import os
from pathlib import Path

from qq_roleplay_bot.model_trace import ModelTrace, trace_enabled, trace_file_path
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"

# 这个模块必须把自己的追踪输出隔离掉。
# 原因：dev_config 会把 .env 里的 QQBOT_DEBUG_MODEL_IO_FILE 注入 os.environ，
# 测试进程继承后就会往**正在运行的 Bot 的追踪文件**里写。之前就发生过一次，
# 导致 Bot 的追踪文件里混进大量测试数据。这里在导入时固定到测试专用路径。
_SAVED_TRACE_FILE = os.environ.get("QQBOT_DEBUG_MODEL_IO_FILE")
_TEST_TRACE_FILE = (
    Path(__file__).resolve().parents[1] / ".tmp_test_run" / "test_model_trace.jsonl"
)
os.environ["QQBOT_DEBUG_MODEL_IO_FILE"] = str(_TEST_TRACE_FILE)

# 追踪文件是追加写的，测试写的内容体量不小（单次运行约 2.5 MB）。
# 不在导入时清掉的话，这个文件会随着每次运行无限增长。
# 只删自己这个测试专用文件，绝不碰 QQBOT_DEBUG_MODEL_IO_FILE 原本指向的路径。
try:
    _TEST_TRACE_FILE.unlink()
except FileNotFoundError:
    pass
except OSError:  # pragma: no cover - 文件被占用时不该让整个测试套件失败
    pass


def _request(system: str = "SYS", user: str = "USR") -> list[dict[str, str]]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _with_env(**values):
    """临时设置环境变量，返回恢复函数。"""

    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def restore():
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return restore


def _message(message_id: str = "m1") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{GROUP}",
        user_id="100",
        text="@YunRu 在吗",
        target=MessageTarget(group_id=GROUP),
        is_bot_mentioned=True,
    )


# --- 默认关闭（安全属性） --------------------------------------------------

def test_trace_is_disabled_by_default() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO=None, QQBOT_DEBUG_MODEL_IO_FILE=None)
    try:
        assert trace_enabled() is False
        assert trace_file_path() is None
        trace = ModelTrace()
        assert trace.enabled is False
        assert trace.record(_request(), "raw", session_id="s", trigger="mention") is None
        assert len(trace) == 0
    finally:
        restore()


def test_enabled_by_explicit_flag() -> None:
    for value in ("1", "true", "YES", "on"):
        restore = _with_env(QQBOT_DEBUG_MODEL_IO=value)
        try:
            assert trace_enabled() is True, value
        finally:
            restore()
    for value in ("0", "false", "off", ""):
        restore = _with_env(QQBOT_DEBUG_MODEL_IO=value)
        try:
            assert trace_enabled() is False, value
        finally:
            restore()


def test_trace_file_path_from_env() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO_FILE=r"C:\tmp\trace.jsonl")
    try:
        assert str(trace_file_path()).endswith("trace.jsonl")
        assert trace_file_path().is_absolute()
    finally:
        restore()


def test_relative_trace_path_is_resolved_against_project_root() -> None:
    """相对路径必须变成绝对路径。

    否则文件会写到进程当前目录下；更糟的是文件被外部删除后，进程仍握着已删除的
    句柄继续写，数据静默丢失。
    """

    restore = _with_env(QQBOT_DEBUG_MODEL_IO_FILE=".tmp_test_run/rel.jsonl")
    try:
        resolved = trace_file_path()
        assert resolved is not None and resolved.is_absolute()
        assert resolved.name == "rel.jsonl"
    finally:
        restore()


def test_tests_never_write_into_the_bot_trace_file() -> None:
    """回归保护：本模块的追踪路径不能指向 Bot 实际使用的文件。"""

    path = trace_file_path()
    assert path is not None
    assert path.name == "test_model_trace.jsonl", path


# --- 记录内容 -------------------------------------------------------------

def test_record_captures_system_user_and_raw_output() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace(capacity=5)
        entry = trace.record(_request("SYSTEMPROMPT", "USERCONTENT"), "<decision>REPLY</decision>",
                             session_id="group:1", trigger="mention")
        assert entry is not None
        assert entry.system_prompt == "SYSTEMPROMPT"
        assert entry.user_content == "USERCONTENT"
        assert entry.raw_output == "<decision>REPLY</decision>"
        assert entry.sequence == 1
    finally:
        restore()


def test_error_calls_are_recorded_without_output() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace()
        entry = trace.record(_request(), "", session_id="s", trigger="mention",
                             error="http_error: status=429")
        assert entry is not None
        assert entry.raw_output == ""
        assert "429" in entry.error
    finally:
        restore()


def test_ring_buffer_is_bounded_and_keeps_latest() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace(capacity=3)
        for index in range(5):
            trace.record(_request(user=f"u{index}"), f"raw{index}", session_id="s", trigger="t")
        assert len(trace) == 3
        kept = [entry.user_content for entry in trace.entries()]
        assert kept == ["u2", "u3", "u4"]
    finally:
        restore()


def test_redacted_view_hides_full_text() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace()
        secret = "SECRET-" + "x" * 2000
        trace.record(_request(secret, secret), secret, session_id="s", trigger="t")
        redacted = trace.recent_redacted(1)[0]
        # 脱敏视图只有长度与前缀，不携带完整 system prompt。
        assert redacted["system_chars"] == len(secret)
        assert len(str(redacted["raw_preview"])) <= 600
        assert secret not in json.dumps(redacted["raw_preview"])
        assert "system_prompt" not in redacted
        assert "user_content" not in redacted
    finally:
        restore()


def test_snapshot_reports_state_without_content() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace(capacity=7)
        trace.record(_request("S", "U"), "RAW", session_id="s", trigger="t")
        snapshot = trace.snapshot()
        assert snapshot["enabled"] is True
        assert snapshot["capacity"] == 7
        assert snapshot["recorded"] == 1
        assert snapshot["kept"] == 1
        assert "RAW" not in json.dumps(snapshot)
    finally:
        restore()


def test_raw_output_is_truncated_at_limit() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace()
        entry = trace.record(_request(), "y" * 50_000, session_id="s", trigger="t")
        assert entry is not None
        assert len(entry.raw_output) == 20000
    finally:
        restore()


def test_file_dump_appends_jsonl(tmp_path=None) -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    directory = Path(__file__).resolve().parents[1] / ".tmp_test_run" / "trace-test"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "trace.jsonl"
    if target.exists():
        target.unlink()
    try:
        trace = ModelTrace(file_path=target)
        trace.record(_request("S", "U"), "RAW-ONE", session_id="s", trigger="t")
        trace.record(_request("S", "U"), "RAW-TWO", session_id="s", trigger="t")
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["raw_output"] == "RAW-ONE"
        assert json.loads(lines[1])["raw_output"] == "RAW-TWO"
        assert trace.snapshot()["writing_to_file"] is True
    finally:
        restore()
        import shutil
        shutil.rmtree(directory, ignore_errors=True)


def test_unwritable_file_does_not_raise() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        # 用一个不可能创建的路径（把文件当目录用）。
        blocked = Path(__file__).resolve().parents[1] / "pyproject.toml" / "nested" / "trace.jsonl"
        trace = ModelTrace(file_path=blocked)
        entry = trace.record(_request(), "RAW", session_id="s", trigger="t")
        assert entry is not None
        assert trace.write_failures == 1
    finally:
        restore()


def test_clear_empties_buffer() -> None:
    restore = _with_env(QQBOT_DEBUG_MODEL_IO="1")
    try:
        trace = ModelTrace()
        trace.record(_request(), "RAW", session_id="s", trigger="t")
        assert len(trace) == 1
        trace.clear()
        assert len(trace) == 0
    finally:
        restore()



# 引擎接线的那几条测试搬到了 `tests/test_feature_log.py`：引擎现在用的是按功能分开的
# 日志（`FeatureLogs`），`model_trace` 只是还留着的旧模块，不再挂在引擎上。

