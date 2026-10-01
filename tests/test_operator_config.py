"""运行配置覆盖层：优先级、损坏降级、白名单、不覆盖真实环境变量。

由来（2026-10-01 用户）："面板权限……可以修改 api key，可以切换供应商"。
这些改动要落盘，但**不能写 `.env`**（真实凭据 + 注释 + 顺序，脚本改一次就可能写坏），
所以新增一层覆盖：`operator_config.json > 真实环境变量 > .env > 代码默认`。
"""
import json
import os
import tempfile
from pathlib import Path

from qq_roleplay_bot.operator_config import (
    SETTINGS, ConfigRejected, OperatorConfig, normalize,
)


def _cfg(tmp: str) -> OperatorConfig:
    return OperatorConfig(Path(tmp) / "operator_config.json")


def test_unknown_keys_are_rejected() -> None:
    """白名单：不在 SETTINGS 里的键一律拒绝（"面板能改任何环境变量"是后门）。"""

    for key in ("path", "api_base_url_extra", "QQBOT_API_KEY", ""):
        try:
            normalize(key, "x")
        except ConfigRejected:
            continue
        raise AssertionError(f"{key!r} 不该被接受")


def test_normalize_shapes() -> None:
    assert normalize("judge_enabled", True) == "1"
    assert normalize("judge_enabled", "off") == "0"
    assert normalize("approve_whitelist", ["1", " 2 ", ""]) == "1,2"
    assert normalize("api_key", "  sk-x  ") == "sk-x"
    assert normalize("api_key", "") == ""       # 空 = 清掉这一项、回落主 key
    try:
        normalize("judge_enabled", "maybe")
    except ConfigRejected:
        pass
    else:
        raise AssertionError("含糊的布尔值该被拒")


def test_every_setting_declares_env_and_applies() -> None:
    """每个可改项都要说清"写进哪个环境变量"和"什么时候生效"。"""

    for key, spec in SETTINGS.items():
        assert spec["env"].startswith("QQBOT_"), key
        assert spec["applies"] in {"live", "restart"}, key
        assert spec["kind"] in {"bool", "str", "csv", "secret"}, key


def test_save_and_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        first = _cfg(tmp)
        first.set("api_model", "deepseek-chat")
        first.set("judge_enabled", False)
        second = _cfg(tmp)
        values = second.load()
        assert values["api_model"] == "deepseek-chat"
        assert values["judge_enabled"] == "0"


def test_corrupt_file_falls_back_to_empty() -> None:
    """一份写坏的 JSON 不该让 bot 起不来：整份忽略 + 回落。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_config.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        config = OperatorConfig(path)
        assert config.load() == {}
        assert config.last_error == "invalid_json"


def test_unsupported_version_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_config.json"
        path.write_text(json.dumps({"version": 99, "values": {"api_model": "x"}}),
                        encoding="utf-8")
        config = OperatorConfig(path)
        assert config.load() == {}
        assert config.last_error == "unsupported_version"


def test_unknown_stored_keys_are_dropped() -> None:
    """文件里混进不认识的键（手写、旧版本）时只保留认识的。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_config.json"
        path.write_text(json.dumps({
            "version": 1, "values": {"api_model": "m", "something_else": "1"},
        }, ensure_ascii=False), encoding="utf-8")
        values = OperatorConfig(path).load()
        assert values == {"api_model": "m"}


def test_empty_secret_in_file_is_ignored() -> None:
    """文件里存的**空凭据**不是覆盖，启动时必须忽略（否则回复会全 401）。

    2026-10-01 现场事故：`inject()` 把 `QQBOT_API_KEY=""` 写进环境之后，
    `load_env_file()` 因为"这个键已存在"就不再填 `.env` 里那把真 key——
    回复 agent 的 key 变成空串，模型一律 401，而她只是"不说话"（命令不走模型，
    照常响应，很难一眼看出是配置问题）。
    """

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_config.json"
        path.write_text(json.dumps({
            "version": 1,
            "values": {"api_key": "", "api_model": "deepseek-chat"},
        }, ensure_ascii=False), encoding="utf-8")
        config = OperatorConfig(path)
        values = config.load()
        assert "api_key" not in values, "空凭据不该被当成覆盖"
        assert values["api_model"] == "deepseek-chat"

        name = "QQBOT_API_KEY"
        saved = os.environ.pop(name, None)
        try:
            config.inject()
            assert os.environ.get(name) != "", "环境里被塞了空串"
        finally:
            os.environ.pop(name, None)
            if saved is not None:
                os.environ[name] = saved


def test_non_secret_empty_values_are_still_kept() -> None:
    """非凭据类的空值是**有意义的设置**（例如"不改模型"），不能一起丢掉。"""

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "operator_config.json"
        path.write_text(json.dumps({
            "version": 1, "values": {"api_model": "", "approve_pattern": ""},
        }, ensure_ascii=False), encoding="utf-8")
        values = OperatorConfig(path).load()
        assert values == {"api_model": "", "approve_pattern": ""}


def test_inject_does_not_override_real_environment() -> None:
    """真实环境变量优先于覆盖层：显式设过的值是"这次运行的特殊要求"。"""

    with tempfile.TemporaryDirectory() as tmp:
        config = _cfg(tmp)
        config.set("api_model", "from-panel")
        name = "QQBOT_API_MODEL"
        saved = os.environ.get(name)
        os.environ[name] = "from-shell"
        try:
            config.inject()
            assert os.environ[name] == "from-shell"
        finally:
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved


def test_inject_fills_missing_environment() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = _cfg(tmp)
        config.set("api_model", "from-panel")
        name = "QQBOT_API_MODEL"
        saved = os.environ.pop(name, None)
        try:
            injected = config.inject()
            assert os.environ[name] == "from-panel"
            assert injected >= 1
        finally:
            os.environ.pop(name, None)
            if saved is not None:
                os.environ[name] = saved


def test_inject_is_skipped_in_tests() -> None:
    """`QQBOT_STATE_PERSIST=0`（测试/干跑）时覆盖层不注入：测试不读部署配置。"""

    from qq_roleplay_bot import operator_config as module

    saved = os.environ.get("QQBOT_STATE_PERSIST")
    os.environ["QQBOT_STATE_PERSIST"] = "0"
    try:
        assert module.inject() == 0
    finally:
        if saved is None:
            os.environ.pop("QQBOT_STATE_PERSIST", None)
        else:
            os.environ["QQBOT_STATE_PERSIST"] = saved
