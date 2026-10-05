"""**每个 agent 各用各的 base URL / key**（2026-10-05 用户要求）。

由来：原来只有**一套** `QQBOT_API_BASE_URL` / `QQBOT_API_KEY` / `QQBOT_API_MODEL`
（`dev_config`），判定 / 回复 / 记忆 / 审核 / 写信全按它组装，各自再配自己的 key。
于是"把回复 agent 单独换到别家模型"做不到：换全局地址会把判定与记忆一起换走。

现在多出一节**只给回复 agent 的可选覆盖**：

    QQBOT_REPLY_API_BASE_URL / QQBOT_REPLY_API_KEY / QQBOT_REPLY_API_MODEL

这个文件钉两件事（缺一不可）：

1. **配了就生效**：回复那一路（含按会话新建的 client 与 `build_engine` 里那个兜底
   client）拿到专属值；**判定 / 记忆 / 审核不受影响**；
2. **没配就与从前逐字相同**：三个值一律回落到全局那一套——只配一把 key 的部署、
   干净 clone、测试的行为一个字节都没变。

（测试里用的都是**假地址与假 key**：目标值由部署方写在 `run/.env` 里，
不进任何仓库文件。）
"""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qq_roleplay_bot import dev_config, runtime
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.operator_config import SETTINGS, OperatorConfig
from qq_roleplay_bot.runtime import apply_overrides

# 这些是"回复 agent 专属"的三个名字。
REPLY_VARS = ("QQBOT_REPLY_API_BASE_URL", "QQBOT_REPLY_API_KEY", "QQBOT_REPLY_API_MODEL")
# 全局那一套（测试里可能被动过，跑完要还回去）。
GLOBAL_VARS = ("QQBOT_API_BASE_URL", "QQBOT_API_KEY", "QQBOT_API_MODEL")


@contextmanager
def _env(**values):
    """临时设/清这些环境变量，跑完原样还回去。`None` = 清掉，空串是**有效值**。"""

    names = REPLY_VARS + GLOBAL_VARS
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


class _Engine:
    """面板热更要碰的那几个属性（`test_runtime_overrides` 用的同一形状）。"""

    def __init__(self) -> None:
        self.usage_store = None
        self.group_roles = None


class _FakeTransport:
    """够 `build_engine` 装配的最小传输层（与 `test_optional_capabilities` 同形）。"""

    async def call_api(self, action, params=None):
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": "9", "nickname": "n"}}
        return {"status": "ok", "retcode": 0, "data": {"role": "owner"}}

    async def send(self, target, text, **kwargs):  # pragma: no cover - 装配不需要
        return None

    async def start(self):  # pragma: no cover
        return None


def _client(role: str, key: str = "sk-old", model: str = "old-model") -> OpenAICompatibleClient:
    return OpenAICompatibleClient("https://old.example", key, model,
                                  user_id="u", usage_role=role)


def _operator(directory) -> OperatorConfig:
    return OperatorConfig(Path(directory) / "operator_config.json")


# --- 缺省行为：与改动前**逐字相同** ------------------------------------------


def test_without_reply_overrides_the_reply_agent_uses_the_global_set() -> None:
    """三个都没配 → 回复 agent 用的就是全局那一套（这是"不回归"那一条）。"""

    with _env():
        assert dev_config.reply_api_key() == dev_config.API_KEY
        assert dev_config.reply_api_model() == dev_config.API_MODEL
        assert dev_config.reply_api_base_url() == dev_config.API_BASE_URL
        assert runtime._reply_endpoint() == (
            dev_config.API_BASE_URL, dev_config.API_KEY, dev_config.API_MODEL)

        client = runtime._dialogue_client_factory("group:1")
        assert (client.base_url, client.api_key, client.model) == (
            dev_config.API_BASE_URL, dev_config.API_KEY, dev_config.API_MODEL)
        judge = runtime._judge_client_factory("group:1")
        assert (judge.base_url, judge.api_key) == (dev_config.API_BASE_URL,
                                                   dev_config.JUDGE_API_KEY)


def test_an_empty_reply_value_means_not_configured() -> None:
    """空串 = 没配（环境里留一个空值会把 `.env` 的填充挡在门外，这是踩过的坑）。"""

    with _env(QQBOT_REPLY_API_BASE_URL="", QQBOT_REPLY_API_MODEL="",
              QQBOT_REPLY_API_KEY="", QQBOT_API_KEY="sk-main-fake"):
        assert dev_config.reply_api_base_url() == dev_config.API_BASE_URL
        assert dev_config.reply_api_model() == dev_config.API_MODEL
        assert dev_config.reply_api_key() == "sk-main-fake", "空值要回落主 key，不是变成空 key"
        client = runtime._dialogue_client_factory("group:1")
        assert client.api_key == "sk-main-fake"


# --- 配了就生效：只有回复那一路换 --------------------------------------------


def test_the_reply_agent_can_use_its_own_endpoint() -> None:
    reply_base, reply_key, reply_model = ("https://reply.example/v1", "sk-reply-fake",
                                          "reply-model-fake")
    with _env(QQBOT_REPLY_API_BASE_URL=reply_base, QQBOT_REPLY_API_KEY=reply_key,
              QQBOT_REPLY_API_MODEL=reply_model):
        assert runtime._reply_endpoint() == (reply_base, reply_key, reply_model)

        client = runtime._dialogue_client_factory("group:1")
        assert (client.base_url, client.api_key, client.model) == (
            reply_base, reply_key, reply_model)
        # 判定照旧走全局那一套（专属值只给回复）
        judge = runtime._judge_client_factory("group:1")
        assert judge.base_url == dev_config.API_BASE_URL
        assert judge.api_key == dev_config.JUDGE_API_KEY
        assert judge.model == (os.environ.get("QQBOT_JUDGE_MODEL") or dev_config.API_MODEL)


def test_build_engine_wires_the_reply_endpoint_into_the_dialogue_client() -> None:
    """装配点也要接上：`engine.client` 是回复那一趟真正用的 client。"""

    reply_base, reply_key, reply_model = ("https://reply.example/v1", "sk-reply-fake",
                                          "reply-model-fake")
    with _env(QQBOT_REPLY_API_BASE_URL=reply_base, QQBOT_REPLY_API_KEY=reply_key,
              QQBOT_REPLY_API_MODEL=reply_model):
        engine = runtime.build_engine(_FakeTransport())
    assert (engine.client.base_url, engine.client.api_key, engine.client.model) == (
        reply_base, reply_key, reply_model)


# --- 面板那一层（白名单 + 热更） ---------------------------------------------


def test_the_three_reply_settings_are_in_the_whitelist() -> None:
    """面板只能写白名单里的键；这三个要**按现有写法**接进去（否则等于没配）。"""

    expected = {
        "reply_api_base_url": "QQBOT_REPLY_API_BASE_URL",
        "reply_api_model": "QQBOT_REPLY_API_MODEL",
        "reply_api_key": "QQBOT_REPLY_API_KEY",
    }
    for key, env in expected.items():
        spec = SETTINGS[key]
        assert spec["env"] == env
        assert spec["applies"] == "live", f"{key} 标注的生效方式不对"
    assert SETTINGS["reply_api_key"]["kind"] == "secret", "key 要按凭据处理（不回显）"
    assert SETTINGS["reply_api_base_url"]["kind"] == "str"


def test_the_panel_can_point_only_the_reply_agent_elsewhere() -> None:
    with tempfile.TemporaryDirectory() as tmp, _env():
        dialogue, judge = _client("dialogue"), _client("judge")
        applied = apply_overrides(_Engine(), {
            "reply_api_base_url": "https://reply.example/v1",
            "reply_api_key": "sk-reply-fake",
            "reply_api_model": "reply-model-fake",
        }, store=_operator(tmp))
        assert applied["reply_api_base_url"]["applied"] == "live"
        assert (dialogue.base_url, dialogue.api_key, dialogue.model) == (
            "https://reply.example/v1", "sk-reply-fake", "reply-model-fake")
        # 判定那一路一个字都没动
        assert (judge.base_url, judge.api_key, judge.model) == (
            "https://old.example", "sk-old", "old-model")
        # 落盘了：重启之后还是这一套
        stored = OperatorConfig(Path(tmp) / "operator_config.json").load()
        assert stored["reply_api_base_url"] == "https://reply.example/v1"
        assert stored["reply_api_model"] == "reply-model-fake"
        assert os.environ["QQBOT_REPLY_API_BASE_URL"] == "https://reply.example/v1"


def test_a_global_change_does_not_clobber_the_reply_endpoint() -> None:
    """全局 = 默认值，reply 专属 = 覆盖它：改全局不许把专属值在内存里冲掉。"""

    with tempfile.TemporaryDirectory() as tmp, _env(
            QQBOT_REPLY_API_BASE_URL="https://reply.example/v1"):
        dialogue, judge = _client("dialogue"), _client("judge")
        apply_overrides(_Engine(), {"api_base_url": "https://global.example/v1"},
                        store=_operator(tmp))
        assert judge.base_url == "https://global.example/v1"
        assert dialogue.base_url == "https://reply.example/v1", "回复专属地址被全局改掉了"


def test_a_provider_switch_still_reaches_a_reply_client_without_a_specific_url() -> None:
    """**没配**专属地址时，换全局供应商照旧换到回复 client（这条是不回归）。"""

    with tempfile.TemporaryDirectory() as tmp, _env():
        dialogue = _client("dialogue")
        apply_overrides(_Engine(), {"provider": "openai"}, store=_operator(tmp))
        assert dialogue.base_url == "https://api.openai.com/v1"


def test_clearing_the_reply_endpoint_falls_back_to_the_global_one() -> None:
    with tempfile.TemporaryDirectory() as tmp, _env():
        dialogue = _client("dialogue")
        store = _operator(tmp)
        apply_overrides(_Engine(), {"reply_api_base_url": "https://reply.example/v1"},
                        store=store)
        assert dialogue.base_url == "https://reply.example/v1"
        applied = apply_overrides(_Engine(), {"reply_api_base_url": ""}, store=store)
        assert applied["reply_api_base_url"]["applied"] == "live"
        assert dialogue.base_url == dev_config.API_BASE_URL, "清掉之后要回落全局地址"
        assert "QQBOT_REPLY_API_BASE_URL" not in os.environ, "环境里留下了空串"
        assert "reply_api_base_url" not in store.values, "覆盖层里还留着那条"
