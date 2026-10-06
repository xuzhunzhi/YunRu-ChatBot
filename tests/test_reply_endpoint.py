"""**每个 agent 各用各的 base URL / key**（2026-10-05 用户要求）。

由来：原来只有**一套** `QQBOT_API_BASE_URL` / `QQBOT_API_KEY` / `QQBOT_API_MODEL`
（`dev_config`），判定 / 回复 / 记忆 / 审核 / 写信全按它组装，各自再配自己的 key。
于是"把回复 agent 单独换到别家模型"做不到：换全局地址会把判定与记忆一起换走。

现在多出一节**只给回复 agent 的可选覆盖**：

    QQBOT_REPLY_API_BASE_URL / QQBOT_REPLY_API_KEY / QQBOT_REPLY_API_MODEL

2026-10-06 补：这一节现在是 `model_config` 里 **reply 那个用途**的任务级覆盖
（见 `tests/test_model_config.py` 里那张完整的解析表）。本文件继续钉两件事：

1. **配了就生效**：回复那一路（含按会话新建的 client 与 `build_engine` 里那个兜底
   client）拿到专属值；**判定 / 记忆 / 审核不受影响**；
2. **没配就与从前逐字相同**：三个都没配时回复走的就是"主 key 那一对"
   （`QQBOT_API_KEY` + `QQBOT_API_BASE_URL`）——只配一把 key 的部署、干净 clone、
   测试全都照旧。

> ⚠️ **2026-10-06 改了三条断言（如实交代）**：这个文件里原来有三处断言
> "回复没配专属值 → 等于 `dev_config.API_BASE_URL` / `API_MODEL`"，
> 而那两个常量是**全局兼容档**的兜底值。新的形状里地址**只跟着 key 那条走**，
> 所以 reply 没配覆盖时用的是 **reply 那条 key 所绑的那家**（mimo），不是
> "全局代码默认"（deepseek）。这正是要根治的那个混搭，不能反过来钉住它。
> 另外 `judge` 那条不再回落到 **reply 的 key**（那是跨家的组合），
> 它回落主 key（`QQBOT_API_KEY`）——见 `model_config.KEYS` 里的表。

（测试里用的都是**假地址与假 key**：目标值由部署方写在 `run/.env` 里，
不进任何仓库文件。）
"""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qq_roleplay_bot import dev_config, model_config, runtime
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.operator_config import SETTINGS, OperatorConfig
from qq_roleplay_bot.runtime import apply_overrides

# 这些是"回复 agent 专属"的三个名字。
REPLY_VARS = ("QQBOT_REPLY_API_BASE_URL", "QQBOT_REPLY_API_KEY", "QQBOT_REPLY_API_MODEL")
# 全局那一套（测试里可能被动过，跑完要还回去）。
# `QQBOT_API_KEY` 也在里面：**同一个套件里别的测试文件会把它设上**（它们跑完不清理），
# 带着它就等于"这台机器配了主 key"，reply 那一趟就不会走"自己那条"了。
GLOBAL_VARS = ("QQBOT_API_BASE_URL", "QQBOT_API_KEY", "QQBOT_API_MODEL")


@contextmanager
def _env(*, env_file_key: str | None = None, **values):
    """临时设/清这些环境变量，跑完原样还回去。`None` = 清掉，空串是**有效值**。

    `env_file_key` 顺便管 `dev_config.API_KEY`（`.env` 里那把主 key）——
    `model_config` 的兼容档会看它，验"什么都没配"时要连它一起清掉。
    """

    names = REPLY_VARS + GLOBAL_VARS
    saved = {name: os.environ.get(name) for name in names}
    saved_key = dev_config.API_KEY
    for name in names:
        os.environ.pop(name, None)
    if env_file_key is not None:
        dev_config.API_KEY = env_file_key
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        dev_config.API_KEY = saved_key
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


def _judge_key_now() -> str:
    """判定那把 key 现在应当解析成什么（**与 `model_config` 同一个判据**）。"""

    return model_config.resolve("judge").api_key


# --- 缺省行为：与改动前**逐字相同** ------------------------------------------


def test_without_reply_overrides_the_reply_agent_uses_the_main_key_pair() -> None:
    """三个都没配 → 回复 agent 用的就是"主 key 那一对"（这是"不回归"那一条）。

    "那一对" = `QQBOT_API_KEY` + `QQBOT_API_BASE_URL`：历史部署里它们本来就是
    **同一份配置**（只配一把 key 的机器），所以行为与改动前逐字相同。
    """

    with _env(QQBOT_API_KEY="sk-main-fake"):
        assert dev_config.reply_api_key() == "sk-main-fake"
        client = runtime._dialogue_client_factory("group:1")
        assert (client.base_url, client.api_key, client.model) == (
            dev_config.reply_api_base_url(), dev_config.reply_api_key(),
            dev_config.reply_api_model())
        # 地址与 key 同源：这一对的含义就是"主 key 那条"（兼容档）
        assert model_config.resolve("reply").key_name == model_config.MAIN_KEY
        assert runtime._reply_endpoint() == (
            dev_config.reply_api_base_url(), dev_config.reply_api_key(),
            dev_config.reply_api_model())


def test_an_empty_reply_value_means_not_configured() -> None:
    """空串 = 没配（环境里留一个空值会把 `.env` 的填充挡在门外，这是踩过的坑）。

    ⚠️ 与改动前的一处**有意不同**：那时 reply 没配就"回落到全局地址/模型"
    （`dev_config.API_BASE_URL` = deepseek）。现在地址只跟着 key 那条走——
    设了 `QQBOT_API_KEY` 而它属于别家时，reply 用它**那一对**，不再拼出
    "A 家的 key + B 家的地址"。见本文件头部那段交代。
    """

    with _env(QQBOT_REPLY_API_BASE_URL="", QQBOT_REPLY_API_MODEL="",
              QQBOT_REPLY_API_KEY="", QQBOT_API_KEY="sk-main-fake"):
        assert dev_config.reply_api_key() == "sk-main-fake", "空值要回落主 key，不是变成空 key"
        endpoint = model_config.resolve("reply")
        assert endpoint.key_env == "QQBOT_API_KEY"
        assert dev_config.reply_api_base_url() == endpoint.base_url
        assert dev_config.reply_api_base_url(), "reply 的地址不该是空的"
        client = runtime._dialogue_client_factory("group:1")
        assert client.api_key == "sk-main-fake"
        assert client.base_url == endpoint.base_url


def test_without_any_key_the_reply_uses_its_own_provider() -> None:
    """一条 key 都没有时：reply 报兼容档（缺 key），地址取**它自己那条**的供应商。

    成对的意思就是"有 key 才有那一对"——所以兼容档的地址（`QQBOT_API_BASE_URL`）
    在这里**不被采用**，用它就会造出"空 key + 别家的地址"。
    """

    with _env(env_file_key=""):
        endpoint = model_config.resolve("reply")
        assert endpoint.key_env == "QQBOT_REPLY_API_KEY", "没有 key 时要报它自己那条"
        assert endpoint.key_present() is False, "没有 key 时要在自检里报出来"
        assert endpoint.base_url == model_config.provider_base_url("mimo")
        assert "**缺**" in endpoint.safe_summary()

    with _env(env_file_key="", QQBOT_API_KEY="",
              QQBOT_API_BASE_URL="https://global.example/v1"):
        endpoint = model_config.resolve("reply")
        # **有地址、没 key 的全局配置构成不了"兼容档那一对"**，所以 reply 用自己那条：
        # 一个空 key 配上一个别家地址，正是今晚那个 401 的形状，这里不许再出现。
        assert endpoint.key_present() is False
        assert endpoint.base_url == model_config.provider_base_url("mimo"), \
            "没有 key 的全局地址不该被采用（那会拼出'空 key + 别家地址'）"
        assert "**缺**" in endpoint.safe_summary()


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
        # 判定照旧走它自己的那一条（专属值只给回复）
        judge = runtime._judge_client_factory("group:1")
        assert judge.base_url == model_config.resolve("judge").base_url
        assert judge.api_key == _judge_key_now()
        assert judge.model == model_config.resolve("judge").model


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
    """全局 = 默认值，reply 专属（任务级覆盖）= 覆盖它：改全局不许把专属值冲掉。"""

    with tempfile.TemporaryDirectory() as tmp, _env(
            QQBOT_REPLY_API_BASE_URL="https://reply.example/v1"):
        dialogue, judge = _client("dialogue"), _client("judge")
        apply_overrides(_Engine(), {"api_base_url": "https://global.example/v1"},
                        store=_operator(tmp))
        assert judge.base_url == "https://global.example/v1"
        assert dialogue.base_url == "https://reply.example/v1", "回复专属地址被全局改掉了"


def test_a_global_change_never_moves_a_task_that_has_its_own_key() -> None:
    """**成对取自同一条**：判定有自己的 key 时，改全局地址不该把它搬到别家去。

    这条是 2026-10-06 那个现场事故的回归：全局地址指向 mimo、而判定那把 key 属于
    deepseek —— 全局一动，判定就会拿着 deepseek 的 key 去打 mimo。
    """

    with tempfile.TemporaryDirectory() as tmp, _env(
            QQBOT_JUDGE_API_KEY="sk-judge-fake", QQBOT_JUDGE_API_BASE_URL=None):
        judge = _client("judge")
        apply_overrides(_Engine(), {"api_base_url": "https://api.xiaomimimo.com/v1"},
                        store=_operator(tmp))
        assert judge.base_url == model_config.provider_base_url("deepseek"), \
            "判定那把 key 属于 deepseek，地址不该被全局改成 mimo"
        # 而回复那一趟没有自己的 key，走的是主 key 那一对——它跟着全局走
        assert model_config.resolve("reply").base_url == "https://api.xiaomimimo.com/v1"


def test_clearing_the_reply_endpoint_falls_back_to_the_main_pair() -> None:
    with tempfile.TemporaryDirectory() as tmp, _env():
        dialogue = _client("dialogue")
        store = _operator(tmp)
        apply_overrides(_Engine(), {"reply_api_base_url": "https://reply.example/v1"},
                        store=store)
        assert dialogue.base_url == "https://reply.example/v1"
        applied = apply_overrides(_Engine(), {"reply_api_base_url": ""}, store=store)
        assert applied["reply_api_base_url"]["applied"] == "live"
        assert dialogue.base_url == model_config.resolve("reply").base_url, \
            "清掉之后要回落 reply 那条 key 自己那一对"
        assert "QQBOT_REPLY_API_BASE_URL" not in os.environ, "环境里留下了空串"
        assert "reply_api_base_url" not in store.values, "覆盖层里还留着那条"
