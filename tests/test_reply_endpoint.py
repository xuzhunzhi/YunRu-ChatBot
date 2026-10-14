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

> ⚠️ **2026-10-14 又改了 reply 那条 key 的指向（如实交代）**：用户给了新地址与 key
> （*"这里面有gemini flash模型，给云茹接上"*）——`tasks["reply"]` 从"借 judge 那条"
> 改成 **reply 自己那条**，`KEYS["reply"]` 的 provider 改成 `xkiro`。于是：
> 地址 / key / 模型**三样同源**（`QQBOT_REPLY_API_KEY` 不再闲置），
> 任务级覆盖（`QQBOT_REPLY_API_BASE_URL` / `QQBOT_REPLY_API_KEY` / `QQBOT_REPLY_API_MODEL`）
> 照旧生效；**judge 那条 key 从此与 reply 无关**（10-09 的形状作废）。
> 为什么必须这样改：只填 `QQBOT_REPLY_API_BASE_URL` 时解析出来的 key 仍是 judge 那把
> ——等于把 deepseek 的 key 递给新地址（实测过）。本文件的期望值已按新路由更新。
>
> ⚠️ 同一天留下的一个**休眠的坑**（写在这里，别当它不存在）：如果哪天
> `QQBOT_REPLY_API_KEY` 被删掉，reply 会按 `fallback` 回落到"主 key 那一对"，
> 而**模型名仍然跟着 `tasks` 走**（`google/gemini-3.8-flash`）——那就成了
> "主家的 key + 主家的地址 + gemini 的模型名"。生产 `.env` 里那条 key 在，
> 所以这条路现在走不到；自检那一行会同时印 provider / model，真踩到了看得出来。

> ⚠️ **同一天同一条指令还动了判定（如实交代，用户原话："你把这个也换成gemini flash吧"）**：
> `tasks["judge"]` 改成**借 `reply` 那条 key**（xkiro / `google/gemini-3.8-flash`）。
> 于是**判定那条 key（`QQBOT_JUDGE_API_KEY`，deepseek）与 reply 彻底无关**，
> 而 `QQBOT_REPLY_API_KEY` 同时服务 reply 与 judge（judge 是借它）。
> 本文件里凡是"判定那把 key 属于 deepseek"的断言都因此换了对象：**仍然名副其实的
> 两路是 `letter`（借 judge 那条 deepseek key）与 `memory`（自己那条）**，
> 每一处都写清了为什么，别把它读成"随便换一路都行"。
> （判定那一行自己的期望值在 `tests/test_model_config.py` 的 `EXPECTED` 里钉着。）

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
# `QQBOT_API_PROVIDER`（"主 key 是哪家的"）同理：本文件里有 `provider` 那次热更，
# 它会写这个变量；不跟着管起来，跑完就会留给后面的文件。
GLOBAL_VARS = ("QQBOT_API_BASE_URL", "QQBOT_API_KEY", "QQBOT_API_MODEL",
               "QQBOT_API_PROVIDER")
# `QQBOT_JUDGE_API_KEY` 也要管起来，但**理由与 10-09 那版相反**：
# 那时 reply 借的是 judge 那条 key（所以它有值会改变 reply 的解析）；
# 2026-10-14 起 reply 用**自己那条**（`QQBOT_REPLY_API_KEY`，已在 REPLY_VARS 里），
# 而**判定借的是 reply 那条**——本文件里比较判定 client 的用例仍然依赖"这台机器
# 上的 judge 那把是什么"，不钉住就会跟着 `.env` 漂。
# （两个变量都必须管：一个管 reply 自己，一个管借它的人。）
JUDGE_VARS = ("QQBOT_JUDGE_API_KEY",)


@contextmanager
def _env(*, env_file_key: str | None = None, **values):
    """临时设/清这些环境变量，跑完原样还回去。`None` = 清掉，空串是**有效值**。

    `env_file_key` 顺便管 `dev_config.API_KEY`（`.env` 里那把主 key）——
    `model_config` 的兼容档会看它，验"什么都没配"时要连它一起清掉。
    """

    names = REPLY_VARS + GLOBAL_VARS + JUDGE_VARS
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
    """三个都没配（judge 那条也没有）→ 回复 agent 用的就是"主 key 那一对"。

    "那一对" = `QQBOT_API_KEY` + `QQBOT_API_BASE_URL`：历史部署里它们本来就是
    **同一份配置**（只配一把 key 的机器），所以行为与改动前逐字相同。

    ⚠️ 2026-10-14 起"没配专属值"的回落链只有一条：reply 的 key 来源是**它自己那条**
    （`QQBOT_REPLY_API_KEY`，xkiro / Gemini Flash），没有就落"主 key 那一对"。
    下面第二段钉住新形状：**判定的那把 key 搬不动 reply 了**（10-09 那种"借 judge"
    的形状作废——它在换家时会造出"deepseek 的 key 打 xkiro 的地址"）。
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

    # 2026-10-14：judge 那把 key 与 reply **无关**了——配上它也照样走主 key 那一对。
    with _env(QQBOT_API_KEY="sk-main-fake", QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        endpoint = model_config.resolve("reply")
        assert (endpoint.key_name, endpoint.key_env, endpoint.api_key) == (
            model_config.MAIN_KEY, "QQBOT_API_KEY", "sk-main-fake")
        # 供应商仍按**本用途自带那家**算（reply 那条 key 绑的是 xkiro）——
        # 兼容档那一对"是哪家"由这一条自己那家决定，见 `_main_provider`。
        assert (endpoint.provider, endpoint.base_url) == (
            "xkiro", model_config.provider_base_url("xkiro"))


def test_an_empty_reply_value_means_not_configured() -> None:
    """空串 = 没配（环境里留一个空值会把 `.env` 的填充挡在门外，这是踩过的坑）。

    ⚠️ 与改动前的一处**有意不同**：那时 reply 没配就"回落到全局地址/模型"
    （`dev_config.API_BASE_URL` = deepseek）。现在地址只跟着 key 那条走——
    设了 `QQBOT_API_KEY` 而它属于别家时，reply 用它**那一对**，不再拼出
    "A 家的 key + B 家的地址"。见本文件头部那段交代。

    ⚠️ 2026-10-14 起 `QQBOT_REPLY_API_KEY` **不再闲置**（reply 的 key 来源就是
    它自己那条，xkiro / Gemini Flash）——所以这里"空串等于没配"验的正是它本身：
    三个都设成空串时，回落的必须是**主 key 那一对**，而不是把 key 弄成空串。
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
    """一条 key 都没有时：reply 报它自己那条，地址取那家的默认。

    成对的意思就是"有 key 才有那一对"——所以兼容档的地址（`QQBOT_API_BASE_URL`）
    在这里**不被采用**，用它就会造出"空 key + 别家的地址"。
    （reply 自己那条自 2026-10-14 用户改口径起 = `QQBOT_REPLY_API_KEY`，
    绑的是 **xkiro**；10-09 那版"自己那条 = judge 那条"已作废。）
    """

    with _env(env_file_key=""):
        endpoint = model_config.resolve("reply")
        assert endpoint.key_env == "QQBOT_REPLY_API_KEY", \
            "没有 key 时要报它自己那条（2026-10-14 起 = QQBOT_REPLY_API_KEY）"
        assert endpoint.key_present() is False, "没有 key 时要在自检里报出来"
        assert endpoint.base_url == model_config.provider_base_url("xkiro")
        assert "**缺**" in endpoint.safe_summary()

    with _env(env_file_key="", QQBOT_API_KEY="",
              QQBOT_API_BASE_URL="https://global.example/v1"):
        endpoint = model_config.resolve("reply")
        # **有地址、没 key 的全局配置构成不了"兼容档那一对"**，所以 reply 用自己那条：
        # 一个空 key 配上一个别家地址，正是今晚那个 401 的形状，这里不许再出现。
        assert endpoint.key_present() is False
        assert endpoint.base_url == model_config.provider_base_url("xkiro"), \
            "没有 key 的全局地址不该被采用（那会拼出'空 key + 别家地址'）"
        assert "**缺**" in endpoint.safe_summary()


# --- 配了就生效：只有回复那一路换 --------------------------------------------


def test_the_reply_agent_can_use_its_own_endpoint() -> None:
    reply_base, reply_key, reply_model = ("https://reply.example/v1", "sk-reply-fake",
                                          "reply-model-fake")
    # 2026-10-14 用户改口径（"这里面有gemini flash模型，给云茹接上"）：reply 的 key
    # 来源 = **它自己那条**（`QQBOT_REPLY_API_KEY`），与地址、模型**三样同源**；
    # 任务级覆盖照旧只落在这一路。judge 那把 key 一并配上，专门钉住"它搬不动 reply"。
    with _env(QQBOT_REPLY_API_BASE_URL=reply_base, QQBOT_REPLY_API_KEY=reply_key,
              QQBOT_REPLY_API_MODEL=reply_model, QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        endpoint = model_config.resolve("reply")
        assert endpoint.key_env == "QQBOT_REPLY_API_KEY", \
            "reply 的 key 来源自 2026-10-14 起是它自己那条"
        assert endpoint.api_key == "sk-reply-fake", \
            "judge 那把（deepseek）不该再进 reply 客户端"
        assert runtime._reply_endpoint() == (reply_base, "sk-reply-fake", reply_model)

        client = runtime._dialogue_client_factory("group:1")
        assert (client.base_url, client.api_key, client.model) == (
            reply_base, "sk-reply-fake", reply_model)
        # 判定照旧走它自己那一条（专属值只给回复）。判定现在**借 reply 那条 key**
        # （2026-10-14 起），所以这里用"与解析结果比"而不是写死一个值——写死就会
        # 把"判定住哪家"再钉成一次会漂的期望。
        judge = runtime._judge_client_factory("group:1")
        assert judge.base_url == model_config.resolve("judge").base_url
        assert judge.api_key == _judge_key_now()
        assert judge.model == model_config.resolve("judge").model


def test_build_engine_wires_the_reply_endpoint_into_the_dialogue_client() -> None:
    """装配点也要接上：`engine.client` 是回复那一趟真正用的 client。

    key 是**回复自己那条**（2026-10-14 起，`QQBOT_REPLY_API_KEY`）；地址 / 模型的
    任务级覆盖照旧生效、且只落在回复这一路。
    """

    reply_base, reply_key, reply_model = ("https://reply.example/v1", "sk-reply-fake",
                                          "reply-model-fake")
    with _env(QQBOT_REPLY_API_BASE_URL=reply_base, QQBOT_REPLY_API_KEY=reply_key,
              QQBOT_REPLY_API_MODEL=reply_model, QQBOT_JUDGE_API_KEY="sk-judge-fake"):
        engine = runtime.build_engine(_FakeTransport())
    assert (engine.client.base_url, engine.client.api_key, engine.client.model) == (
        reply_base, "sk-reply-fake", reply_model)


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
    """**成对取自同一条**：某一路有自己的 key 时，改全局地址不该把它搬到别家去。

    这条是 2026-10-06 那个现场事故的回归：全局地址指向 mimo、而那一把 key 属于
    deepseek —— 全局一动，那个子系统就会拿着 deepseek 的 key 去打 mimo。

    ⚠️ **2026-10-14 换了举例子那两路（如实交代）**：用户当天说 *"你把这个也换成
    gemini flash吧"*，判定的 key 来源在解析表里改成了**借 `reply` 那条**（xkiro），
    判定因此不再是"deepseek 那一把 key"的例子。现在两侧用的是**仍然名副其实的
    两路**：`memory`（自己那条 deepseek key）与 `reply`（自己那条 xkiro key）——
    口径移了，不是"哪一路都行"。
    """

    with tempfile.TemporaryDirectory() as tmp, _env(
            env_file_key="", QQBOT_MEMORY_API_KEY="sk-memory-fake",
            QQBOT_REPLY_API_KEY="sk-reply-fake", QQBOT_MEMORY_API_BASE_URL=None):
        memory, dialogue = _client("memory"), _client("dialogue")
        apply_overrides(_Engine(), {"api_base_url": "https://api.xiaomimimo.com/v1"},
                        store=_operator(tmp))
        assert memory.base_url == model_config.provider_base_url("deepseek"), \
            "记忆那把 key 属于 deepseek，地址不该被全局改成 mimo"
        assert dialogue.base_url == model_config.provider_base_url("xkiro"), \
            "回复那把 key 属于 xkiro，地址同样不该被全局改变"
        assert model_config.resolve("reply").base_url == \
            model_config.provider_base_url("xkiro")
        assert model_config.resolve("memory").base_url == \
            model_config.provider_base_url("deepseek")


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
