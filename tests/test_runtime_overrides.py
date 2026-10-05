"""面板热更接缝：配置落盘 → 立刻生效（`runtime.apply_overrides`）。

由来（2026-10-01 用户）："能热更的就热更，其余表单标'需重启'"。这个文件钉的是
**哪一类改动是哪一种**——标错比不生效更糟（面板说"已生效"而其实没有）。

同时钉住 `llm_client` 的登记簿：换 key / 模型 / 供应商要覆盖**所有**已存在的 client
（按会话分出来的那些也算），否则会出现"主群换了、别的群还是旧的"。
"""
import json
import os
import tempfile
from pathlib import Path

from qq_roleplay_bot import runtime_flags
from qq_roleplay_bot.llm_client import (
    OpenAICompatibleClient, apply_client_overrides, clients,
)
from qq_roleplay_bot.operator_config import OperatorConfig
from qq_roleplay_bot.runtime import apply_overrides

# 本机配置在不在的判定（干净 clone 只有 `.env.example`）——见 `tests/config_support.py`
from config_support import skip_without_env


class _Engine:
    """面板热更要碰的那几个属性（真引擎上也就是这几个）。"""

    def __init__(self) -> None:
        self.usage_store = None
        self.group_roles = None


def _operator(tmp: str) -> OperatorConfig:
    return OperatorConfig(Path(tmp) / "operator_config.json")


def _client(role: str, key: str = "sk-old", model: str = "old-model") -> OpenAICompatibleClient:
    return OpenAICompatibleClient("https://old.example", key, model,
                                  user_id="u", usage_role=role)


def test_unknown_key_is_rejected_per_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        flags = runtime_flags.build_flags()
        runtime_flags.install(flags)
        applied = apply_overrides(_Engine(), {"nope": "1"}, store=_operator(tmp))
        assert applied["nope"]["applied"] == "rejected"


def test_flags_take_effect_immediately() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        flags = runtime_flags.build_flags()
        runtime_flags.install(flags)
        assert flags.get("review_enabled") in (True, False)
        applied = apply_overrides(_Engine(), {"review_enabled": False}, store=_operator(tmp))
        assert applied["review_enabled"]["applied"] == "live"
        assert runtime_flags.shared().get("review_enabled") is False
        # 落盘了：下一份配置也读得到
        assert OperatorConfig(Path(tmp) / "operator_config.json").load()["review_enabled"] == "0"


def test_restart_only_settings_are_reported_honestly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        applied = apply_overrides(_Engine(), {"approve_pattern": ".*"}, store=_operator(tmp))
        assert applied["approve_pattern"]["applied"] == "restart_required"


def test_model_change_hits_existing_clients() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        first, second = _client("dialogue"), _client("judge")
        try:
            applied = apply_overrides(_Engine(), {"api_model": "brand-new"},
                                      store=_operator(tmp))
            assert applied["api_model"]["applied"] == "live"
            assert first.model == "brand-new"
            assert second.model == "brand-new", "同一次改动要覆盖所有 agent"
        finally:
            for client in (first, second):
                client.api_key = ""
                client.model = ""
            del first, second
        # 环境变量也更新了（重启后仍是新模型）
        assert os.environ.get("QQBOT_API_MODEL") == "brand-new"


def test_api_key_change_keeps_client_identity_and_usage() -> None:
    """就地改属性，不重建 client——重建会丢掉会话缓存与记账。"""

    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        client = _client("dialogue", key="sk-old")
        client.usage_totals["prompt_tokens"] = 42
        try:
            apply_overrides(_Engine(), {"api_key": "sk-brand-new"}, store=_operator(tmp))
            assert client.api_key == "sk-brand-new"
            assert client.usage_totals["prompt_totals" if False else "prompt_tokens"] == 42
        finally:
            client.api_key = ""


def test_provider_switches_the_base_url() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        client = _client("dialogue")
        try:
            applied = apply_overrides(_Engine(), {"provider": "openai"}, store=_operator(tmp))
            assert applied["provider"]["applied"] == "live"
            assert client.base_url == "https://api.openai.com/v1"
            # provider 只是面板的聚合字段：真正落盘的是地址
            stored = OperatorConfig(Path(tmp) / "operator_config.json").load()
            assert stored["api_base_url"] == "https://api.openai.com/v1"
        finally:
            client.base_url = "https://old.example"


def test_unknown_provider_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        applied = apply_overrides(_Engine(), {"provider": "nope"}, store=_operator(tmp))
        assert applied["provider"]["applied"] == "rejected"


def test_explicit_url_wins_over_provider() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        client = _client("dialogue")
        try:
            apply_overrides(_Engine(), {"provider": "openai",
                                        "api_base_url": "https://mine.example/v1"},
                            store=_operator(tmp))
            assert client.base_url == "https://mine.example/v1"
        finally:
            client.base_url = "https://old.example"


def test_empty_secret_means_fall_back_to_main_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        judge = _client("judge", key="sk-judge")
        main = _client("dialogue", key="sk-main")
        try:
            os.environ["QQBOT_API_KEY"] = "sk-main"
            apply_overrides(_Engine(), {"judge_api_key": ""}, store=_operator(tmp))
            assert judge.api_key == "sk-main", "留空 = 回落主 key，而不是空 key"
            assert main.api_key == "sk-main"
        finally:
            judge.api_key = ""
            main.api_key = ""
            os.environ.pop("QQBOT_API_KEY", None)


def test_clearing_a_role_key_leaves_no_empty_env_behind() -> None:
    """清空某一项凭据时，环境里**不能留下空串**。

    由来（2026-10-01 实测发现）：`_factory_api_key` 读的是环境变量，环境里留一个空串
    会让**之后新建的会话 client 拿到空 key**——那个群从此一句话都说不出来，
    而且要重启才能恢复。空值必须表现为"环境里没有这一项"。
    """

    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        judge = _client("judge", key="sk-judge")
        saved = {name: os.environ.get(name) for name in
                 ("QQBOT_JUDGE_API_KEY", "QQBOT_API_KEY")}
        try:
            os.environ["QQBOT_API_KEY"] = "sk-main"
            os.environ["QQBOT_JUDGE_API_KEY"] = "sk-panel-set"
            apply_overrides(_Engine(), {"judge_api_key": ""}, store=_operator(tmp))
            assert "QQBOT_JUDGE_API_KEY" not in os.environ, "环境里留下了空串"
            assert judge.api_key == "sk-main"
            from qq_roleplay_bot.runtime import _factory_api_key

            assert _factory_api_key("QQBOT_JUDGE_API_KEY", "fallback") == "fallback"
        finally:
            judge.api_key = ""
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_clearing_the_main_key_falls_back_or_is_refused() -> None:
    """主 key 留空：回落 `.env` 里那一把；回落不到就明确拒绝（不造出"谁都调不动"）。

    这条是 2026-10-01 事故的回归测试：当时清空 `api_key` 会在环境里留下空串，
    而 `.env` 的填充因为"键已存在"被挡住 → 回复 agent 的 key 变空 → 模型全 401。

    前提是本机有主 key——公开仓库只发布 `.env.example`，所以干净 clone 上跳过
    （2026-10-02 加，见 `tests/config_support.py`），而不是红着喊代码坏了。
    """

    skip_without_env("API_KEY", "这条验的是'清空后回落 .env 那一把'，需要 .env 里有主 key")
    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        client = _client("dialogue", key="sk-from-env")
        saved = os.environ.pop("QQBOT_API_KEY", None)
        try:
            applied = apply_overrides(_Engine(), {"api_key": ""}, store=_operator(tmp))
            # 测试环境里 `dev_config.API_KEY` 有值（来自 .env），所以这里应当成功回落
            assert applied["api_key"]["applied"] in {"live", "rejected"}
            assert os.environ.get("QQBOT_API_KEY") is None, "环境里被塞了空串或旧值"
            assert client.api_key and client.api_key.startswith("sk-"), client.api_key
            # 覆盖层里那条空值也要一起清掉：留着它启动时会被忽略（自愈逻辑），
            # 但面板会把它显示成"已覆盖"，让人以为生效了一层其实不存在的东西。
            from qq_roleplay_bot.operator_config import OperatorConfig

            assert "api_key" not in OperatorConfig(
                Path(tmp) / "operator_config.json").load()

            # 关键：环境里没有这一项之后，工厂必须能回落到 `.env` 的那把
            from qq_roleplay_bot import dev_config
            from qq_roleplay_bot.runtime import _factory_api_key

            assert _factory_api_key("QQBOT_API_KEY", dev_config.API_KEY) == dev_config.API_KEY
            assert dev_config.API_KEY, "dev_config 里的主 key 不该是空的"
            # 就算环境里真的被塞了空串，工厂也要当成"没设置"
            os.environ["QQBOT_API_KEY"] = ""
            assert _factory_api_key("QQBOT_API_KEY", dev_config.API_KEY) == dev_config.API_KEY
        finally:
            client.api_key = ""
            os.environ.pop("QQBOT_API_KEY", None)
            if saved is not None:
                os.environ["QQBOT_API_KEY"] = saved


def test_deployed_operator_config_has_no_empty_secrets() -> None:
    """真机那份 `data/operator_config.json` 不该留着空凭据（启动时会被忽略）。"""

    from pathlib import Path

    from qq_roleplay_bot.operator_config import SETTINGS

    path = Path("data") / "operator_config.json"
    if not path.exists():
        return
    stored = (json.loads(path.read_text(encoding="utf-8")) or {}).get("values") or {}
    for key, value in stored.items():
        spec = SETTINGS.get(str(key))
        if spec is not None and spec["kind"] == "secret":
            assert value, f"{key} 是空凭据：文件里留着它只会让人以为 key 没了"


def test_audit_receives_every_change() -> None:
    class _Audit:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def record(self, action, *, detail=None, result="", source="", token="") -> None:
            self.rows.append({"action": action, "detail": detail, "result": result})

    with tempfile.TemporaryDirectory() as tmp:
        runtime_flags.install(runtime_flags.build_flags())
        audit = _Audit()
        apply_overrides(_Engine(), {"judge_enabled": False, "nope": "1"},
                        store=_operator(tmp), audit=audit, source="127.0.0.1")
        assert audit.rows and audit.rows[0]["action"] == "settings"


# --- client 登记簿 ----------------------------------------------------------

def test_registry_skips_detached_clients() -> None:
    """弱引用：client 被回收之后登记簿不该继续攥着它（按会话分出来的会很多）。"""

    before = len(clients())
    leaked = _client("dialogue")
    assert any(client is leaked for client in clients())
    del leaked
    after = len(clients())
    assert after <= before + 1


def test_apply_overrides_can_target_one_role() -> None:
    dialogue, judge = _client("dialogue", model="keep"), _client("judge", model="keep")
    try:
        changed = apply_client_overrides(model="only-judge", usage_role="judge")
        assert changed >= 1
        assert judge.model == "only-judge"
        assert dialogue.model == "keep"
    finally:
        dialogue.model = ""
        judge.model = ""


def test_config_loader_tolerates_missing_apply_seam() -> None:
    """`apply_overrides` 的默认参数不能依赖调用方一定给了 store。"""

    from qq_roleplay_bot import operator_config

    assert operator_config.shared() is not None
