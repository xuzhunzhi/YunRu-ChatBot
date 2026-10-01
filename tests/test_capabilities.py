"""能力层测试：离线、可重复，依赖随包发布的 capability 目录。"""
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qq_roleplay_bot.capabilities import (
    FORBIDDEN_ACTIONS,
    INTERACTION_ACTIONS,
    READ_ACTIONS,
    SEND_ACTIONS,
    CapabilityDenied,
    CapabilityRegistry,
    load_catalog,
)

CATALOG = Path(__file__).resolve().parents[1] / "src" / "qq_roleplay_bot" / "data" / "snowluma_actions.json"


@contextmanager
def _temp_dir():
    """项目测试运行器不是 pytest，没有 tmp_path fixture，这里自带一个。"""

    path = tempfile.mkdtemp(prefix="cap-")
    try:
        yield Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_catalog_exists_and_loads() -> None:
    assert CATALOG.exists(), "SnowLuma capability 目录必须随包发布"
    catalog = load_catalog()
    assert len(catalog) > 150, f"目录条目过少: {len(catalog)}"


def test_catalog_covers_the_documented_194() -> None:
    catalog = load_catalog()
    assert len(catalog) == 194, f"期望 194 个 action，实际 {len(catalog)}"


def test_catalog_missing_file_degrades_to_empty() -> None:
    with _temp_dir() as directory:
        assert load_catalog(directory / "nope.json") == {}


def test_catalog_invalid_json_degrades_to_empty() -> None:
    with _temp_dir() as directory:
        bad = directory / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_catalog(bad) == {}


def test_risk_classification() -> None:
    registry = CapabilityRegistry()
    assert registry.get("get_group_msg_history").risk == "read"
    assert registry.get("get_credentials").risk == "forbidden"
    assert registry.get("set_group_kick").risk == "sensitive_write"
    assert registry.get("send_group_msg").risk == "write"
    # 未知 action 不崩。
    assert registry.get("no_such_action") is None
    assert "未知 action" in registry.describe("no_such_action")


def test_forbidden_actions_are_never_allowed() -> None:
    registry = CapabilityRegistry()
    for name in FORBIDDEN_ACTIONS:
        for purpose in ("read", "interaction", "send", "admin"):
            assert not registry.is_allowed(name, purpose=purpose), (name, purpose)
            try:
                registry.check(name, purpose=purpose)
            except CapabilityDenied:
                pass
            else:
                raise AssertionError(f"{name} 在用途 {purpose} 下不应被允许")


def test_credential_actions_are_in_the_forbidden_set() -> None:
    """凭证类接口必须显式禁用，不能只靠注释提醒。"""

    for name in ("get_credentials", "get_cookies", "get_clientkey", "get_csrf_token",
                 "request_decrypt_key", "send_packet"):
        assert name in FORBIDDEN_ACTIONS, name


def test_read_purpose_only_allows_read_actions() -> None:
    registry = CapabilityRegistry()
    assert registry.is_allowed("get_group_msg_history", purpose="read")
    assert registry.is_allowed("get_group_member_list", purpose="read")
    # 写操作不能在 read 用途下放行。
    assert not registry.is_allowed("send_group_msg", purpose="read")
    assert not registry.is_allowed("set_group_kick", purpose="read")


def test_send_and_interaction_purposes_are_separated() -> None:
    registry = CapabilityRegistry()
    assert registry.is_allowed("send_group_msg", purpose="send")
    assert not registry.is_allowed("send_group_msg", purpose="interaction")
    assert registry.is_allowed("set_msg_emoji_like", purpose="interaction")
    assert not registry.is_allowed("set_msg_emoji_like", purpose="send")


def test_admin_purpose_still_blocks_sensitive_and_forbidden() -> None:
    registry = CapabilityRegistry()
    assert registry.is_allowed("get_group_list", purpose="admin")
    assert not registry.is_allowed("set_group_kick", purpose="admin")
    assert not registry.is_allowed("delete_friend", purpose="admin")
    assert not registry.is_allowed("send_packet", purpose="admin")


def test_unknown_purpose_denies_everything() -> None:
    registry = CapabilityRegistry()
    assert not registry.is_allowed("get_group_msg_history", purpose="whatever")


def test_check_returns_metadata_for_allowed_action() -> None:
    registry = CapabilityRegistry()
    item = registry.check("get_group_msg_history", purpose="read")
    assert item.name == "get_group_msg_history"
    assert item.read_only is True
    assert "group_id" in item.param_names
    assert item.summary


def test_missing_required_detects_absent_params() -> None:
    registry = CapabilityRegistry()
    assert registry.missing_required("get_group_msg_history", {}) == ("group_id",)
    assert registry.missing_required("get_group_msg_history", {"group_id": 1}) == ()


def test_allowed_sets_only_reference_known_actions() -> None:
    """允许集合里出现目录中不存在的名字，说明清单写错了。"""

    catalog = load_catalog()
    for name in sorted(READ_ACTIONS | INTERACTION_ACTIONS | SEND_ACTIONS):
        assert name in catalog, f"{name} 不在 SnowLuma 目录里"


def test_read_actions_are_marked_read_only_in_catalog() -> None:
    """READ_ACTIONS 里不该混进写操作，否则闸门等于没设。"""

    catalog = load_catalog()
    offenders = [name for name in READ_ACTIONS if name in catalog and not catalog[name].read_only]
    assert offenders == [], f"只读集合里混入了写操作: {offenders}"


def test_summary_shape() -> None:
    summary = CapabilityRegistry().summary()
    assert summary["total"] == 194
    assert summary["by_risk"]["read"] > 0
    assert summary["by_risk"]["forbidden"] == len(FORBIDDEN_ACTIONS)
    assert "get_credentials" in summary["forbidden"]
