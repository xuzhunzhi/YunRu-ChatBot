"""运行状态持久化与脱敏指标的离线覆盖。"""
import asyncio
import atexit
import json
import os
import shutil
import uuid
from pathlib import Path

from qq_roleplay_bot.metrics import LatencyWindow, RuntimeMetrics
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.state_store import STATE_VERSION, RuntimeStateStore
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OTHER = "999999999"
# 会话级临时根目录，进程退出时整体回收；带 pid 与 uuid 以免并发运行互相干扰。
_SCRATCH = (
    Path(__file__).resolve().parents[1] / ".tmp_test_run"
    / f"persist-{os.getpid()}-{uuid.uuid4().hex[:8]}"
)
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)


def _scratch_dir(name: str) -> Path:
    target = _SCRATCH / name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _message(message_id: str, text: str = "你好", *, group: str = GROUP, mentioned: bool = True):
    return IncomingMessage(
        message_id=message_id,
        session_id=f"group:{group}",
        user_id="100",
        text=text,
        target=MessageTarget(group_id=group),
        is_bot_mentioned=mentioned,
    )


class _EchoClient:
    def __init__(self, reply: str = "我在") -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        return f"<decision>REPLY</decision><reply>{self.reply}</reply>"


# --- 状态存储本身 ---------------------------------------------------------

def test_missing_state_file_falls_back_to_defaults() -> None:
    store = RuntimeStateStore(_scratch_dir("missing") / "runtime_state.json")
    persisted = store.load()
    assert persisted.enabled is True
    assert persisted.enabled_group_ids == ()
    assert persisted.sessions == ()


def test_corrupt_state_file_falls_back_instead_of_raising() -> None:
    path = _scratch_dir("corrupt") / "runtime_state.json"
    path.write_text("{not json", encoding="utf-8")
    store = RuntimeStateStore(path)
    assert store.load().enabled_group_ids == ()
    assert store.last_error == "invalid_json"


def test_unsupported_version_is_rejected() -> None:
    path = _scratch_dir("version") / "runtime_state.json"
    path.write_text(json.dumps({"version": 99, "enabled_group_ids": [GROUP]}), encoding="utf-8")
    store = RuntimeStateStore(path)
    assert store.load().enabled_group_ids == ()
    assert store.last_error == "unsupported_version"


def test_invalid_group_ids_are_filtered_on_load() -> None:
    path = _scratch_dir("groups") / "runtime_state.json"
    path.write_text(json.dumps({
        "version": STATE_VERSION,
        "enabled_group_ids": [GROUP, "123", "abcdef", "99999999999999", 717151356],
    }), encoding="utf-8")
    assert RuntimeStateStore(path).load().enabled_group_ids == (GROUP,)


def test_save_is_atomic_and_round_trips() -> None:
    directory = _scratch_dir("roundtrip")
    store = RuntimeStateStore(directory / "runtime_state.json")
    assert store.save({"enabled": False, "enabled_group_ids": [GROUP], "sessions": []}) is True
    persisted = store.load()
    assert persisted.enabled is False
    assert persisted.enabled_group_ids == (GROUP,)
    # 原子写入不应留下临时文件。
    assert [p.name for p in directory.iterdir()] == ["runtime_state.json"]


# --- 引擎层：群名单与短期会话 --------------------------------------------

def test_group_allowlist_survives_restart() -> None:
    path = _scratch_dir("restart") / "runtime_state.json"
    first = DialogueEngine(_EchoClient(), state_store=RuntimeStateStore(path))
    assert first.enable_group(OTHER) is True
    assert first.disable_group(GROUP) is True

    second = DialogueEngine(_EchoClient(), state_store=RuntimeStateStore(path))
    restored = second.restore_state()
    assert OTHER in second.enabled_group_ids
    assert GROUP not in second.enabled_group_ids
    assert restored["groups"] == 1


def test_sessions_survive_restart_including_bot_reply_history() -> None:
    path = _scratch_dir("sessions") / "runtime_state.json"
    engine = DialogueEngine(_EchoClient(), state_store=RuntimeStateStore(path))
    asyncio.run(engine.handle(_message("m1", "@YunRu 你好")))
    assert engine.persist_state() is True

    restarted = DialogueEngine(_EchoClient(), state_store=RuntimeStateStore(path))
    restored = restarted.restore_state()
    assert restored["sessions"] == 1
    state = restarted.sessions.state(f"group:{GROUP}")
    assert [m.text for m in state.recent()] == ["@YunRu 你好", "我在"]
    assert state.mode.value == "active"


def test_engine_without_state_store_does_not_touch_disk() -> None:
    engine = DialogueEngine(_EchoClient())
    assert engine.persist_state() is False
    assert engine.restore_state() == {"groups": 0, "sessions": 0, "admins": 0}


def test_offline_test_runner_is_isolated_from_real_state() -> None:
    """离线测试绝不能写真实的 data/runtime_state.json。"""

    import os

    from qq_roleplay_bot.runtime import state_persistence_enabled

    # run_offline.py 必须设置这两个变量。
    assert os.environ.get("QQBOT_STATE_PERSIST") == "0"
    assert state_persistence_enabled() is False
    state_file = os.environ.get("QQBOT_STATE_FILE", "")
    assert state_file.endswith("runtime_state.json")
    assert ".tmp_test_run" in state_file
    # 默认路径必须被显式覆盖：生产环境的 data/ 目录可能由正在运行的 Bot 创建，
    # 不能靠"文件不存在"来判断隔离是否生效。
    from qq_roleplay_bot.state_store import state_path_default

    saved = {key: os.environ.pop(key) for key in ("QQBOT_STATE_FILE", "QQBOT_DATA_DIR") if key in os.environ}
    try:
        default_path = state_path_default()
    finally:
        os.environ.update(saved)
    # 未覆盖时落在项目 data/ 下，而测试用的路径在 .tmp_test_run/ 下。
    assert "data" in default_path.parts
    assert ".tmp_test_run" in Path(state_file).parts
    assert Path(state_file).resolve() != default_path.resolve()


def test_snapshot_marks_persistence_and_redacts() -> None:
    path = _scratch_dir("snapshot") / "runtime_state.json"
    engine = DialogueEngine(_EchoClient(), state_store=RuntimeStateStore(path))
    asyncio.run(engine.handle(_message("m1", "@YunRu 你好")))
    snapshot = engine.snapshot()
    assert snapshot.persistence_enabled is True
    assert snapshot.sessions[0].persisted is True
    # 快照只暴露计数与摘要，不包含聊天正文或凭据。
    metrics_text = json.dumps(snapshot.metrics, ensure_ascii=False).lower()
    assert "api_key" not in metrics_text
    assert "secret" not in metrics_text
    assert all("@YunRu" not in session.topic for session in snapshot.sessions)


def test_reply_to_index_is_resolved_to_real_message_id() -> None:
    class Client:
        async def complete(self, request):
            return ("<decision>REPLY</decision><reply_to>current</reply_to>"
                    "<reply>引用你上一句</reply>")

    engine = DialogueEngine(Client())
    result = asyncio.run(engine.handle(_message("m1", "@YunRu 你好")))
    assert result is not None
    assert result.reply_to_message_id == "m1"


def test_unknown_reply_to_is_dropped() -> None:
    class Client:
        async def complete(self, request):
            return ("<decision>REPLY</decision><reply_to>no-such-index</reply_to>"
                    "<reply>正常回复</reply>")

    engine = DialogueEngine(Client())
    result = asyncio.run(engine.handle(_message("m1", "@YunRu 你好")))
    assert result is not None
    assert result.reply_to_message_id == ""


def test_reply_to_none_means_no_quote() -> None:
    class Client:
        async def complete(self, request):
            return "<decision>REPLY</decision><reply_to>none</reply_to><reply>普通回复</reply>"

    engine = DialogueEngine(Client())
    result = asyncio.run(engine.handle(_message("m1", "@YunRu 你好")))
    assert result is not None
    assert result.reply_to_message_id == ""


# --- 指标 ---------------------------------------------------------------

def test_metrics_counts_triggers_decisions_and_errors() -> None:
    metrics = RuntimeMetrics()
    metrics.record_trigger("mention")
    metrics.record_trigger("not-a-kind")
    metrics.record_decision("reply")
    metrics.record_decision("unexpected")
    metrics.record_model_error("http_error")
    metrics.record_send_failure()
    metrics.record_extension_failure()
    snapshot = metrics.snapshot()
    assert snapshot["triggers"]["mention"] == 1
    assert snapshot["triggers"]["unknown"] == 1
    assert snapshot["decisions"]["reply"] == 1
    assert snapshot["decisions"]["no_reply"] == 1
    assert snapshot["model_errors"] == {"http_error": 1}
    assert snapshot["send_failures"] == 1
    assert snapshot["extension_failures"] == 1


def test_latency_window_is_bounded_and_summarised() -> None:
    window = LatencyWindow(window=4)
    assert window.snapshot()["count"] == 0
    for value in (1.0, 2.0, 3.0, 4.0, 5.0):
        window.observe(value)
    snapshot = window.snapshot()
    # 只保留最近 window 个样本。
    assert snapshot["count"] == 4
    assert snapshot["max"] == 5.0
    assert snapshot["p50"] <= snapshot["p95"]
    window.observe(-1.0)
    assert window.snapshot()["count"] == 4


def test_engine_records_model_latency_and_decision() -> None:
    engine = DialogueEngine(_EchoClient())
    asyncio.run(engine.handle(_message("m1", "@YunRu 你好")))
    metrics = engine.snapshot().metrics
    assert metrics["decisions"]["reply"] == 1
    assert metrics["triggers"]["mention"] == 1
    assert metrics["model_latency"]["count"] == 1
