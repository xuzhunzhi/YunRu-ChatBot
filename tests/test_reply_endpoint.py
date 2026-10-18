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

> ⚠️ **2026-10-09 又改了 reply 那条 key 的指向（如实交代）**：用户拍板把 reply
> 换回 deepseek（mimo-v2.6-flash 同样提示词中位 10 秒，ds 1.1 秒）——
> `tasks["reply"]` 从"reply 那条（mimo）"改成 **judge 那条**。于是：
> 地址 / 模型的任务级覆盖（`QQBOT_REPLY_API_BASE_URL` / `QQBOT_REPLY_API_MODEL`）
> **照旧生效**；**key 跟 judge 那条走**，`QQBOT_REPLY_API_KEY`（mimo 那把）在解析里
> **闲置**。本文件的期望值已按新路由更新。

> ⚠️ **2026-10-16 又改了 reply 那条 key 的指向（如实交代，用户原话："切换回ds。
> gemini好蠢。"，同日早先那句是"都改ds"）**：`tasks["reply"]` 从"reply 自己那条"
> （xkiro / Gemini Flash）**改回借 `judge` 那条**（deepseek / `deepseek-flash`）——
> 与 2026-10-09 那一版逐字相同，也就是 2026-10-14 那笔（1834fce）**之前**的形状。
> 依据是用户这句口径 **加上两条实测**（2026-10-16）：①中继的 Google 上游整段 503
> （`/v1/models` 通、`chat/completions` 全 503，25 分钟探针全挂）；②`reasoning_effort`
> 只在 provider ∈ {"", "deepseek"} 时才发（`llm_client` 的 `deepseek_fields`），
> provider=xkiro 时发不出去 ⇒ 只能吃默认档，正文最慢实测 13.9s。于是：地址 / 模型的
> 任务级覆盖（`QQBOT_REPLY_API_BASE_URL` / `QQBOT_REPLY_API_MODEL`）**照旧生效**；
> **key 跟 judge 那条走**，`QQBOT_REPLY_API_KEY`（xkiro 那把）在解析里**再次闲置**
> ——但**保留**：`.env` 里那把 key 与 `KEYS["reply"]` 那条条目都没删，中继恢复后
> "一键切回"要用它们。本文件的期望值已按新路由回退。
>
> ⚠️ 同一天顺手记下**休眠的坑**（方向与 10-14 那版相反）：reply 借 judge 那条 key、
> 而**模型名仍然跟着 `tasks` 走**——谁要是只把模型名换成 Gemini 的（或让
> `QQBOT_REPLY_API_MODEL` 指着别家的模型），就会出现"deepseek 的 key + 别家的模型名"。
> 自检那一行同时印 provider / model，真踩到了看得出来。

> ⚠️ **2026-10-14 那一天的判定改动也一并回退了（同一条口径："切换回ds"）**：
> `tasks["judge"]` 曾改成**借 `reply` 那条 key**（xkiro / `google/gemini-2.5-flash`），
> 2026-10-16 起回到 **`{"key": "judge", "model": "deepseek-flash"}`**。于是
> **判定那条 key（`QQBOT_JUDGE_API_KEY`，deepseek）与 reply 的关系回到"reply 借它"**，
> 而 `QQBOT_REPLY_API_KEY` 谁都不借了。本文件里"判定那把 key 属于 deepseek"的断言
> 因此回到以**判定本人**为例——那正是它 2026-10-16 之后的真实身份。
> （判定那一行自己的期望值在 `tests/test_model_config.py` 的 `EXPECTED` 里钉着。）

（测试里用的都是**假地址与假 key**：目标值由部署方写在 `run/.env` 里，
不进任何仓库文件。）
"""
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from qq_roleplay_bot import (
    dev_config,
    model_config,
    model_providers_store,
    provider_failover,
    runtime,
)
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.operator_config import SETTINGS, OperatorConfig
from qq_roleplay_bot.runtime import apply_overrides

# 这些是"回复 agent 专属"的三个名字。
REPLY_VARS = ("QQBOT_REPLY_API_BASE_URL", "QQBOT_REPLY_API_KEY", "QQBOT_REPLY_API_MODEL")
# 全局那一套（测试里可能被动过，跑完要还回去）。
# `QQBOT_API_KEY` 也在里面：**同一个套件里别的测试文件会把它设上**（它们跑完不清理），
# 带着它就等于"这台机器配了主 key"，reply 那一趟就不会走"自己那条"了。
# `QQBOT_API_PROVIDER`（"主 key 是哪家的"）同理：本文件里有 `provider` 那次热更，
# 它会写这个变量；不跟着管起来，跑完就会留给后面的文件。
GLOBAL_VARS = ("QQBOT_API_BASE_URL", "QQBOT_API_KEY", "QQBOT_API_MODEL",
               "QQBOT_API_PROVIDER")
# `QQBOT_JUDGE_API_KEY` 也要管起来：reply 的 key 来源自 2026-10-09 起（2026-10-16
# 用户「切换回ds」之后再一次）是 **judge 那条**，所以这个文件里凡是钉 reply 解析的
# 用例必须连它一起管起来——`.env` 带进来的真值会把"该走主 key 那对 / 该报缺"的
# 用例全部带偏。
JUDGE_VARS = ("QQBOT_JUDGE_API_KEY",)


@contextmanager
def _env(*, env_file_key: str | None = None, **values):
    """临时设/清这些环境变量，跑完原样还回去。`None` = 清掉，空串是**有效值**。

    `env_file_key` 顺便管 `dev_config.API_KEY`（`.env` 里那把主 key）——
    `model_config` 的兼容档会看它，验"什么都没配"时要连它一起清掉。

    **模型供应商文件**（`QQBOT_MODEL_PROVIDERS_FILE`）也在这里管起来：2026-10-18 起
    `reply_api_*` 这三个键**改投文件**（契约 §3.5）。这条守卫生成一份**本次用例专用**的
    临时路径——否则它们会写进 `tests/run_offline.py` 那条全局隔离路径，留给后面的用例
    一份"文件存在"的状态（那会把"没有文件 = 今天那套"整条论证弄脏）。
    """

    names = REPLY_VARS + GLOBAL_VARS + JUDGE_VARS + ("QQBOT_MODEL_PROVIDERS_FILE",)
    saved = {name: os.environ.get(name) for name in names}
    saved_key = dev_config.API_KEY
    for name in names:
        os.environ.pop(name, None)
    if env_file_key is not None:
        dev_config.API_KEY = env_file_key
    temp_dir = tempfile.TemporaryDirectory()
    os.environ["QQBOT_MODEL_PROVIDERS_FILE"] = str(
        Path(temp_dir.name) / "model_providers.json")
    model_providers_store.reset_cache()
    provider_failover.reset_for_tests()
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
        model_providers_store.reset_cache()
        provider_failover.reset_for_tests()
        temp_dir.cleanup()


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
    """三个都没配（judge 那条也没有）→ 回复 agent 用的就是"主 key 那一对"。

    "那一对" = `QQBOT_API_KEY` + `QQBOT_API_BASE_URL`：历史部署里它们本来就是
    **同一份配置**（只配一把 key 的机器），所以行为与改动前逐字相同。

    2026-10-16 用户「切换回ds」之后，"没配专属值"的前提多了一条：reply 的 key 来源
    现在是 **judge 那条**（deepseek），它有值时 reply 跟它走、不再走主 key 那一对
    ——那个新默认在下面第二段钉住。（2026-10-14～10-16 那段 reply 用自己那条 key，
    所以那时这一段是反过来的：判定那把 key 搬不动它。）
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

    # 2026-10-16 用户「切换回ds」：judge 那条有值时，reply 跟它走（deepseek）。
    with _env(QQBOT_API_KEY="sk-main-fake", QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        endpoint = model_config.resolve("reply")
        assert (endpoint.key_env, endpoint.api_key) == (
            "QQBOT_JUDGE_API_KEY", "sk-judge-fake")
        assert (endpoint.provider, endpoint.base_url) == (
            "deepseek", model_config.provider_base_url("deepseek"))


def test_an_empty_reply_value_means_not_configured() -> None:
    """空串 = 没配（环境里留一个空值会把 `.env` 的填充挡在门外，这是踩过的坑）。

    ⚠️ 与改动前的一处**有意不同**：那时 reply 没配就"回落到全局地址/模型"
    （`dev_config.API_BASE_URL` = deepseek）。现在地址只跟着 key 那条走——
    设了 `QQBOT_API_KEY` 而它属于别家时，reply 用它**那一对**，不再拼出
    "A 家的 key + B 家的地址"。见本文件头部那段交代。

    ⚠️ 2026-10-16 用户「切换回ds」后，`QQBOT_REPLY_API_KEY` 本身在解析里
    **又闲置**（reply 的 key 来源是 judge 那条）——这里要验的仍然是"空串等于
    没配、不许把 key 弄成空串"；judge 那条也没配时，回落的是主 key 那一对。
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
    """一条 key 都没有时：reply 报它自己那条（= judge 那条），地址取那家的默认。

    成对的意思就是"有 key 才有那一对"——所以兼容档的地址（`QQBOT_API_BASE_URL`）
    在这里**不被采用**，用它就会造出"空 key + 别家的地址"。
    （reply 自己那条自 2026-10-09 起就是 **judge 那条**：deepseek；2026-10-16 用户
    「切换回ds」之后又回到这里——2026-10-14～10-16 那版"自己那条 = 回复那条（xkiro）"
    已作废。）
    """

    with _env(env_file_key=""):
        endpoint = model_config.resolve("reply")
        assert endpoint.key_env == "QQBOT_JUDGE_API_KEY", \
            "没有 key 时要报它自己那条（= judge 那条）"
        assert endpoint.key_present() is False, "没有 key 时要在自检里报出来"
        assert endpoint.base_url == model_config.provider_base_url("deepseek")
        assert "**缺**" in endpoint.safe_summary()

    with _env(env_file_key="", QQBOT_API_KEY="",
              QQBOT_API_BASE_URL="https://global.example/v1"):
        endpoint = model_config.resolve("reply")
        # **有地址、没 key 的全局配置构成不了"兼容档那一对"**，所以 reply 用自己那条：
        # 一个空 key 配上一个别家地址，正是今晚那个 401 的形状，这里不许再出现。
        assert endpoint.key_present() is False
        assert endpoint.base_url == model_config.provider_base_url("deepseek"), \
            "没有 key 的全局地址不该被采用（那会拼出'空 key + 别家地址'）"
        assert "**缺**" in endpoint.safe_summary()


# --- 配了就生效：只有回复那一路换 --------------------------------------------


def test_the_reply_agent_can_use_its_own_endpoint() -> None:
    reply_base, reply_key, reply_model = ("https://reply.example/v1", "sk-reply-fake",
                                          "reply-model-fake")
    # 2026-10-16 用户「切换回ds」：reply 的 key 来源又回到 **judge 那条**。地址与模型的
    # 任务级覆盖（QQBOT_REPLY_API_BASE_URL / QQBOT_REPLY_API_MODEL）照旧生效；
    # QQBOT_REPLY_API_KEY（xkiro 那把）在解析里闲置——配了也不进客户端（下面钉住）。
    with _env(QQBOT_REPLY_API_BASE_URL=reply_base, QQBOT_REPLY_API_KEY=reply_key,
              QQBOT_REPLY_API_MODEL=reply_model, QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        endpoint = model_config.resolve("reply")
        assert endpoint.key_env == "QQBOT_JUDGE_API_KEY", \
            "reply 的 key 来源自 2026-10-16 起又回到 judge 那条"
        assert endpoint.api_key == "sk-judge-fake", \
            "QQBOT_REPLY_API_KEY（xkiro 那把）已闲置，不该再进 reply 客户端"
        assert runtime._reply_endpoint() == (reply_base, "sk-judge-fake", reply_model)

        client = runtime._dialogue_client_factory("group:1")
        assert (client.base_url, client.api_key, client.model) == (
            reply_base, "sk-judge-fake", reply_model)
        # 判定照旧走它自己的那一条（专属值只给回复）
        judge = runtime._judge_client_factory("group:1")
        assert judge.base_url == model_config.resolve("judge").base_url
        assert judge.api_key == _judge_key_now()
        assert judge.model == model_config.resolve("judge").model


def test_build_engine_wires_the_reply_endpoint_into_the_dialogue_client() -> None:
    """装配点也要接上：`engine.client` 是回复那一趟真正用的 client。

    key 跟 judge 那条走（2026-10-16 用户「切换回ds」后回到这一版）；地址 / 模型的任务级
    覆盖照旧生效、且只落在回复这一路。
    """

    reply_base, reply_key, reply_model = ("https://reply.example/v1", "sk-reply-fake",
                                          "reply-model-fake")
    with _env(QQBOT_REPLY_API_BASE_URL=reply_base, QQBOT_REPLY_API_KEY=reply_key,
              QQBOT_REPLY_API_MODEL=reply_model, QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        engine = runtime.build_engine(_FakeTransport())
    assert (engine.client.base_url, engine.client.api_key, engine.client.model) == (
        reply_base, "sk-judge-fake", reply_model)


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


def test_the_panel_can_change_only_the_reply_model() -> None:
    """面板只动回复这一路（判据没变；**能动的项变了**）。

    ⚠️ 2026-10-18（契约 §3.5 → 第二轮"一个key一行"）：`reply_api_key` /
    `reply_api_base_url` / `reply_provider` 这三项**已经搬到面板「模型与凭据」页**
    （那一页是"每路一行：供应商 → key → 模型"）。留在这里的只有**模型名**：
    `agents.reply.model` 仍然是"只属于回复这一路"的一项，所以它照旧走 `apply_overrides`
    → 文件。判据一个字没松：**只动回复这一路**、另外几路一字不动、立刻生效。

    被搬走的那几项在下面**当场拒绝**并指向新页面——静默无效比拒绝坏得多。
    """

    with tempfile.TemporaryDirectory() as tmp, _env(env_file_key=""):
        dialogue, judge = _client("dialogue"), _client("judge")
        moved = apply_overrides(_Engine(), {
            "reply_api_base_url": "https://reply.example/v1",
            "reply_api_key": "sk-reply-fake",
            "reply_provider": "deepseek",
        }, store=_operator(tmp))
        for key in ("reply_api_base_url", "reply_api_key", "reply_provider"):
            assert moved[key]["applied"] == "rejected", moved
            assert "模型与凭据" in moved[key]["detail"], moved[key]
            assert "sk-reply-fake" not in moved[key]["detail"], "拒绝理由里带出了明文"
        # 被拒的那几项**一个字节都没写**（文件不该被建出来）。
        assert not Path(os.environ["QQBOT_MODEL_PROVIDERS_FILE"]).exists()

        # 一份能让回复那一路**真的解析出来**的文件：一个供应商 + 一把 key + 绑定。
        Path(os.environ["QQBOT_MODEL_PROVIDERS_FILE"]).write_text(json.dumps({
            "version": 2,
            "providers": {"deepseek": {"base_url": "https://api.deepseek.com",
                                       "keys": [{"id": "k1", "value": "sk-file-reply"}]}},
            "agents": {"reply": {"provider": "deepseek", "key": "k1",
                                 "model": "old-model"}},
        }), encoding="utf-8")
        model_providers_store.reset_cache()

        applied = apply_overrides(_Engine(), {"reply_api_model": "reply-model-fake"},
                                  store=_operator(tmp))
        assert applied["reply_api_model"]["applied"] == "live", applied
        # 模型换了 ⇒ 回复那个活 client 跟着变（地址与 key 仍按 `resolve()` 那一套）。
        assert dialogue.model == "reply-model-fake"
        assert (dialogue.base_url, dialogue.api_key) == (
            model_config.resolve("reply").base_url, model_config.resolve("reply").api_key)
        # 判定那一路一个字都没动
        assert (judge.base_url, judge.api_key, judge.model) == (
            "https://old.example", "sk-old", "old-model")
        # 落进**文件**（唯一存储）：只有 `agents.reply.model` 变了，绑定一个字没动。
        body = json.loads(Path(os.environ["QQBOT_MODEL_PROVIDERS_FILE"]).read_text("utf-8"))
        assert body["agents"]["reply"] == {"provider": "deepseek", "key": "k1",
                                           "model": "reply-model-fake"}
        assert body["agents"].keys() == {"reply"}, "顺手把别路写进文件了"
        assert body["providers"]["deepseek"]["keys"] == [{"id": "k1",
                                                          "value": "sk-file-reply"}]
        # **不再**写覆盖层与环境变量（"唯一存储"这句的机械判据）。
        stored = OperatorConfig(Path(tmp) / "operator_config.json").load()
        assert "reply_api_model" not in stored
        assert "QQBOT_REPLY_API_MODEL" not in os.environ


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

    驱动用的用途是 **`judge`**（2026-10-16 用户「切换回ds」之后它又借 `judge` 那条 key、
    回到 deepseek）。2026-10-14～10-16 那段它借 `reply` 那条（xkiro）、不再"属于
    deepseek"，所以那时这条用例由 `memory` + `reply` 驱动——回退是因为判定重新属于
    deepseek，不是因为用例要变绿。
    """

    with tempfile.TemporaryDirectory() as tmp, _env(
            QQBOT_JUDGE_API_KEY="sk-judge-fake", QQBOT_JUDGE_API_BASE_URL=None):
        judge = _client("judge")
        apply_overrides(_Engine(), {"api_base_url": "https://api.xiaomimimo.com/v1"},
                        store=_operator(tmp))
        assert judge.base_url == model_config.provider_base_url("deepseek"), \
            "判定那把 key 属于 deepseek，地址不该被全局改成 mimo"
        # reply 的 key 来源自 2026-10-16 起也是 judge 那条（此处有值），所以全局地址
        # 同样搬不动它——它与判定同一家（deepseek）。
        assert model_config.resolve("reply").base_url == \
            model_config.provider_base_url("deepseek")
        assert model_config.resolve("judge").base_url == \
            model_config.provider_base_url("deepseek")


def test_clearing_the_reply_model_falls_back_to_the_env_layer() -> None:
    """清空模型名 = 把回复这一路**从文件里删掉**，回落到今天那套（D12 的同一条语义）。

    2026-10-18（第二轮）：能在「调参」页动的那一项只剩 `reply_api_model`。文件里没有
    "只有 provider 没有模型"这种形状，所以清空模型名不是"写一个空模型名"，而是**删掉
    `agents.reply`**——于是它回到 `.env` + 兼容档那一套。判据"回落之后与 `resolve()`
    一致、环境里没留空串、覆盖层里没留那条"一个字没松。
    """

    with tempfile.TemporaryDirectory() as tmp, _env(env_file_key="",
                                                    QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        dialogue = _client("dialogue")
        store = _operator(tmp)
        path = Path(os.environ["QQBOT_MODEL_PROVIDERS_FILE"])
        path.write_text(json.dumps({
            "version": 2,
            "providers": {"deepseek": {"base_url": "https://api.deepseek.com",
                                       "keys": [{"id": "k1", "value": "sk-file-reply"}]}},
            "agents": {"reply": {"provider": "deepseek", "key": "k1",
                                 "model": "reply-model-fake"}},
        }), encoding="utf-8")
        model_providers_store.reset_cache()
        assert model_config.resolve("reply").model == "reply-model-fake"
        assert model_config.resolve("reply").api_key == "sk-file-reply"
        applied = apply_overrides(_Engine(), {"reply_api_model": ""}, store=store)
        assert applied["reply_api_model"]["applied"] == "live"
        # 清掉之后回到**环境那套**：judge 那条 key（deepseek）。
        assert dialogue.model == model_config.resolve("reply").model
        assert dialogue.model == "deepseek-flash"
        assert dialogue.base_url == "https://api.deepseek.com"
        assert dialogue.api_key == "sk-judge-fake"
        body = json.loads(path.read_text("utf-8")) if path.exists() else {"agents": {}}
        assert "reply" not in body["agents"], "清空之后文件里还留着这一路"
        assert "QQBOT_REPLY_API_BASE_URL" not in os.environ, "环境里留下了空串"
        assert "reply_api_base_url" not in store.values, "覆盖层里还留着那条"
