"""`data/model_providers.json` v2：**一个 key 一行** + 三件套绑定（2026-10-18 第二轮）。

## 用户定的形状（原话照抄，别自己发明）

> 「我倾向于**一个key一行**而不是一个供应商一行外加好多key。**agent那边先选供应商再选
> 对应的key最后选供应商提供的模型**。」

所以文件长这样（`STORE_VERSION = 2`）：

    {"version": 2,
     "providers": {名字: {"base_url": …, "client_type"?: …,
                          "models": [这家提供的模型名, …],
                          "keys": [{"id": "k1", "label": "主号"?, "value": "sk-…"}, …]}},
     "agents": {用途: {"provider": 名字, "key": id?, "model": …, "reasoning_effort"?}}}

## 这一份钉的是什么

1. **没有文件 = 今天**（§5.2 第 1 条机械守护）：六路逐字段、自检行逐行相等；
2. **一处读口**（§5.2 第 3 条源码形状守护）：`_providers_file()`；
3. **四层回落链**（写死的顺序，每一层一例）：
   `agents.<用途>.key` 指名的那把 → 该供应商的第一把 → `.env` 这一路 → `.env` 主 key；
4. **`status()` 说清每一路落在哪一层**（面板要显示）；
5. **`letter` 也在这张表里**（用户：插件读的是"核心里配好的"那份，不自己配）；
6. **写侧的几条纪律**：id 稳定、删"在用"的被拒、同名/同地址不覆盖、明文落盘但不进日志。

## 三重证法各自落在哪（§5.2）

1. **机械守护**：`test_without_the_file_every_purpose_is_identical_to_today`；
2. **双树对拍（人工，产物在 `data/`）**：`data/tmp_mp_parity.py` + `git worktree` 两个
   提交树各跑一遍，`sha256` 必须相等。那一步**不进**本套件（干净 clone 拿不到 `data/`）；
3. **源码形状守护**：`test_the_file_has_exactly_one_read_entry`。
"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from qq_roleplay_bot import (
    dev_config,
    model_config,
    model_providers_store,
    provider_failover,
    provider_registry,
    runtime,
)
from qq_roleplay_bot.control_audit import ControlAudit
from qq_roleplay_bot.llm_client import OpenAICompatibleClient
from qq_roleplay_bot.model_providers_store import ModelProvidersRejected
from qq_roleplay_bot.operator_config import OperatorConfig

#: "今天那套"的期望值。**直接 import**，不另抄一份：抄一份就会在两处漂移，而这条
#: 用例的全部意义就是"文件不在时 == 那张表"。
from test_model_config import ALL_VARS, EXPECTED, FAKE  # noqa: E402 - 见上面的理由

#: 文件路径变量（`model_providers_store.PATH_ENV` 的名字，写死一份让测试也当契约读）。
FILE_ENV = "QQBOT_MODEL_PROVIDERS_FILE"

#: 这张表之外的、本文件会改的环境变量（思考深度、审核开关）。`ALL_VARS` 是"三层配置"
#: 那份清单，本文件还要碰几个不属于它的，单独列出来一起存/一起还。
EXTRA_VARS: tuple[str, ...] = (
    FILE_ENV,
    "QQBOT_STYLE_REVIEW",
    "QQBOT_REPLY_REASONING_EFFORT",
    "QQBOT_DIALOGUE_REASONING_EFFORT",
    "QQBOT_JUDGE_REASONING_EFFORT",
    "QQBOT_LETTER_REASONING_EFFORT",
    "QQBOT_MEMORY_REASONING_EFFORT",
    "QQBOT_REVIEW_REASONING_EFFORT",
    "QQBOT_VISION_REASONING_EFFORT",
)

#: 一个**足够长**的假 key：短 key 会因为掩码露首尾而让"明文有没有漏出去"的断言变得有歧义。
KEY_K1 = "sk-v2-test-K1-ATTENTION-1234567890"
KEY_K2 = "sk-v2-test-K2-ATTENTION-0987654321"
RELAY_URL = "https://relay.example/v1"


@contextmanager
def _isolated(path: Path, *, env_file_key: str = "", **values):
    """清空这批变量、把文件指到 `path`，跑完原样还回去。

    `env_file_key` 管的是 `dev_config.API_KEY`（`.env` 里那把主 key）：`model_config`
    的兼容档会看它，所以要想验"什么凭据都没有"，得连它一起清掉（与
    `tests/test_model_config.py::_env` 同一条约定）。`""` 是有效值，`None` = 保持清空。
    """

    names = tuple(ALL_VARS) + EXTRA_VARS
    saved = {name: os.environ.get(name) for name in names}
    saved_key = dev_config.API_KEY
    for name in names:
        os.environ.pop(name, None)
    dev_config.API_KEY = env_file_key
    os.environ[FILE_ENV] = str(path)
    for name, value in values.items():
        if value is not None:
            os.environ[name] = value
    model_providers_store.reset_cache()
    provider_failover.reset_for_tests()
    try:
        yield
    finally:
        model_providers_store.reset_cache()
        provider_failover.reset_for_tests()
        dev_config.API_KEY = saved_key
        for name in names:
            os.environ.pop(name, None)
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def _write_file(path: Path, body: object) -> None:
    """直接写文件（**只许测试这么做**：业务写一律走 store 的写层）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    model_providers_store.reset_cache()


def _read_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _body(*, provider: str = "relay1", url: str = RELAY_URL,
          model: str = "relay-model", keys=None, agents=None,
          models=None) -> dict:
    """一份 v2 文件（默认：一套 relay1 + 两把 key + 回复绑第一把）。"""

    keys = [{"id": "k1", "label": "主号", "value": KEY_K1},
            {"id": "k2", "label": "备用", "value": KEY_K2}] if keys is None else keys
    entry = {"base_url": url, "keys": keys}
    if models is not None:
        entry["models"] = models
    agent_map = {"reply": {"provider": provider, "key": "k1", "model": model}} \
        if agents is None else agents
    return {"version": 2, "providers": {provider: entry}, "agents": agent_map}


class _Engine:
    """`apply_overrides` 只需要引擎上这几个属性（路由到文件时不碰引擎）。"""

    usage_store = None
    group_roles = None


# --- §5.2 第 1 条 / T1：没有文件 = 今天 ---------------------------------------


def test_without_the_file_every_purpose_is_identical_to_today() -> None:
    """T1：**不存在的路径** + 六把假 key → 六路逐字段等于今天，自检行逐行相等。

    这条是"没有这个文件时逐字节与今天相同"的**机械守护**：它把"没有文件"这个状态
    显式固定下来，而不是靠"这台机器恰好没建文件"（§5.2 的第 1 条证法）。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        assert not path.exists()
        with _isolated(path, **FAKE):
            status = model_config.status()
            assert status["present"] is False, "这个用例的前提是文件不存在"
            assert status["last_error"] == "", "没有文件不是错误"
            for task, provider, base_url, model, key_env in EXPECTED:
                endpoint = model_config.resolve(task)
                assert (endpoint.task, endpoint.provider, endpoint.base_url,
                        endpoint.model, endpoint.key_env, endpoint.own_key,
                        endpoint.source) == (
                    task, provider, base_url, model, key_env, True, ""), \
                    f"{task} 在没有文件时与今天不一样了"
                assert endpoint.key_present() is True
                assert endpoint.api_key == FAKE[key_env], f"{task} 拿错了 key"
            # 自检行**逐行相等**（不是"包含"）：连措辞都不许因为这一层而变。
            lines = model_config.self_check_lines()
            assert len(lines) == len(EXPECTED)
            for (task, provider, _base, model, key_env), line in zip(EXPECTED, lines):
                assert line == f"{task}: provider={provider} model={model} key={key_env} 有值", \
                    line
            assert "来源=" not in "\n".join(lines), "没写文件时不该多出一个来源后缀"
            # 文件状态那几行是**另一个函数**（不然"每个用途一行"的条数不变式会破）。
            assert len(model_config.providers_file_lines()) == 1
            assert "不存在" in model_config.providers_file_lines()[0]
            # 每一路都报"文件没管它"（`env`），面板据此显示"实际走 .env"。
            assert {item["layer"] for item in status["key_layers"].values()} == {"env"}


# --- §5.2 第 3 条 / T10：源码形状守护 -----------------------------------------


def test_the_file_has_exactly_one_read_entry() -> None:
    """T10：内容只有 `_providers_file()` 一处读口——**结构上写不出第二处来源**。

    为什么需要它：前两条证的是"行为一致"，这一条证的是"**形状**上只有一个来源"，
    而那正是 2026-10-06 那次三系统 401 的根因（地址与 key 各自有一个来源）。
    """

    import inspect

    for name in ("_file_plan", "_key_source", "_base_url_for", "_resolve_model",
                 "file_agent_entry", "file_provider_specs"):
        body = inspect.getsource(getattr(model_config, name))
        assert ("_file_plan(" in body) or ("_providers_file(" in body), \
            f"{name} 没有从文件那一处读口取数"
    # 反过来：除了那三层，不许有谁自己去 `load()` 文件。
    for name in ("resolve", "require", "endpoints"):
        body = inspect.getsource(getattr(model_config, name))
        assert "_providers_store.load(" not in body, f"{name} 绕开了唯一读口"


# --- 四层回落链（用户 2026-10-18 拍定的顺序，一层一例）-------------------------


def test_layer_1_the_key_named_by_the_purpose_is_used() -> None:
    """① `agents.<用途>.key` 指名的那把 —— 用户那句"先选供应商再选对应的 key"。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = _body(agents={"reply": {"provider": "relay1", "key": "k2",
                                       "model": "relay-model"}})
        _write_file(path, body)
        with _isolated(path):
            endpoint = model_config.resolve("reply")
            layer = model_config.task_key_layer("reply")
        assert endpoint.api_key == KEY_K2, "没有用 agents.reply.key 指名的那把"
        assert endpoint.base_url == RELAY_URL
        assert layer["layer"] == "own" and layer["key_id"] == "k2", layer
        assert "k2" in layer["layer_label"], layer["layer_label"]


def test_layer_2_the_first_key_is_used_when_nothing_is_named() -> None:
    """② 该供应商的**第一把** —— `agents.<用途>` 有 provider、没写 key。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body(agents={"reply": {"provider": "relay1",
                                                  "model": "relay-model"}}))
        with _isolated(path):
            endpoint = model_config.resolve("reply")
            layer = model_config.task_key_layer("reply")
        assert endpoint.api_key == KEY_K1, "没写 key 时应当用列表里的第一把"
        assert layer["layer"] == "first" and layer["key_id"] == "k1", layer


def test_layer_3_the_env_key_of_that_purpose() -> None:
    """③ `.env` 里**这一路**的 key —— 供应商没写 key 时往环境那套回落。

    用 `judge` 驱动：`KEYS["judge"]` 自带的那条 key 属于 deepseek，与文件写的 deepseek
    同家（"同家"这道闸是 D1 ② 的既有纪律，不是这一轮新加的）。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"version": 2,
                           "providers": {"deepseek": {"base_url": "https://api.deepseek.com",
                                                      "keys": []}},
                           "agents": {"judge": {"provider": "deepseek",
                                                "model": "judge-model"}}})
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-env-judge"):
            endpoint = model_config.resolve("judge")
            layer = model_config.task_key_layer("judge")
        assert endpoint.api_key == "sk-env-judge"
        assert endpoint.key_env == "QQBOT_JUDGE_API_KEY"
        assert layer["layer"] == "env_task", layer
        assert ".env" in layer["layer_label"], layer["layer_label"]


def test_layer_4_the_env_main_key() -> None:
    """④ `.env` 的主 key —— 这一路自己那条 key 没有时要落到它。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"version": 2,
                           "providers": {"deepseek": {"base_url": "https://api.deepseek.com",
                                                      "keys": []}},
                           "agents": {"judge": {"provider": "deepseek",
                                                "model": "judge-model"}}})
        with _isolated(path, QQBOT_API_KEY="sk-env-main",
                       QQBOT_API_PROVIDER="deepseek"):
            endpoint = model_config.resolve("judge")
            layer = model_config.task_key_layer("judge")
        assert endpoint.api_key == "sk-env-main"
        assert layer["layer"] == "env_main", layer
        assert "主 key" in layer["layer_label"], layer["layer_label"]


def test_status_says_which_layer_every_purpose_sits_on() -> None:
    """`status()["key_layers"]` 逐路回答"现在实际用的是哪一层"（面板要显示）。

    六路各给一处：own / first / env_task / env_main / env（没写进文件）/ 被拒。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = {
            "version": 2,
            "providers": {"relay1": {"base_url": RELAY_URL,
                                     "keys": [{"id": "k1", "value": KEY_K1},
                                              {"id": "k2", "value": KEY_K2}]}},
            "agents": {
                "reply": {"provider": "relay1", "key": "k2", "model": "m-reply"},
                "judge": {"provider": "relay1", "model": "m-judge"},
                # memory 指着一把不存在的 key ⇒ 这一路被拒（悬空 id 不许静默兜底）
                "memory": {"provider": "relay1", "key": "k9", "model": "m-memory"},
            },
        }
        _write_file(path, body)
        with _isolated(path, QQBOT_REVIEW_API_KEY="sk-review"):
            status = model_config.status()
            layers = status["key_layers"]
            assert layers["reply"]["layer"] == "own"
            assert layers["judge"]["layer"] == "first"
            assert layers["memory"]["layer"] == "broken", "写错的那一路要报 broken"
            assert "k9" in status["rejected"]["memory"], status["rejected"]["memory"]
            assert layers["review"]["layer"] == "env", "没写进文件的那一路报 env"
            assert layers["letter"]["layer"] == "env"
            # 报的是**层**与**有没有值**，**不是** key 的值。
            blob = json.dumps(status, ensure_ascii=False)
            assert KEY_K1 not in blob and KEY_K2 not in blob
            assert "sk-review" not in blob


def test_a_dangling_key_id_is_refused_instead_of_borrowing_another() -> None:
    """`agents.<用途>.key` 指了一把不存在的 id → **拒这一路**，不许悄悄换成第一把。

    静默换一把是这类配置里最坏的失败：面板与日志都会显示"配好了"，而实际打出去的
    是另一个账号的 key（2026-10-06 那次 401 的同一类形状）。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body(agents={"reply": {"provider": "relay1", "key": "k9",
                                                  "model": "relay-model"}}))
        with _isolated(path):
            endpoint = model_config.resolve("reply")
            assert endpoint.source == "", "被拒的那一路不该报成由文件接管"
            assert endpoint.api_key == "", "不该拿别的 key 顶上"
            reason = model_config.status()["rejected"]["reply"]
            assert "k9" in reason and "k1" in reason, reason


def test_the_letter_purpose_goes_through_the_same_table() -> None:
    """`letter`（插件那一路）与其余五路**同一张表、同一条链**。

    用户 2026-10-18：「插件读的是核心里配好的那份」——所以写信那一路能被文件覆盖，
    而且它**不需要**插件自己配 key（`plugins/mail/wire.py` 只从接缝拿工厂）。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = _body(agents={"letter": {"provider": "relay1", "key": "k2",
                                        "model": "letter-model"}})
        _write_file(path, body)
        with _isolated(path):
            endpoint = model_config.resolve("letter")
            layer = model_config.task_key_layer("letter")
        assert (endpoint.provider, endpoint.model, endpoint.api_key) == (
            "relay1", "letter-model", KEY_K2)
        assert layer["layer"] == "own"
        assert model_config.TASK_LABELS["letter"] == "写信"


# --- T2：文件里写了的用途，一个环境覆盖都不听 ---------------------------------


def test_a_purpose_written_in_the_file_ignores_every_env_override() -> None:
    """T2：文件里给了 key 的那一路，`QQBOT_*` 的地址/模型覆盖一律不听。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path, QQBOT_API_KEY="sk-main",
                       QQBOT_REPLY_API_KEY="sk-reply-env",
                       QQBOT_REPLY_API_BASE_URL="https://env.example/v1",
                       QQBOT_REPLY_API_MODEL="env-model",
                       QQBOT_API_MODEL="compat-model"):
            endpoint = model_config.resolve("reply")
        assert (endpoint.provider, endpoint.base_url, endpoint.api_key, endpoint.model) == (
            "relay1", RELAY_URL, KEY_K1, "relay-model")
        assert endpoint.source == "文件"


# --- T3：自检行点名来源、绝不打出 key 值 ---------------------------------------


def test_the_self_check_names_the_file_source_without_leaking_the_key() -> None:
    """T3：自检行说清"来自文件 + host"，而且**一个字节的 key 都不出现**。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path):
            lines = model_config.self_check_lines()
            files = model_config.providers_file_lines()
        line = next(item for item in lines if item.startswith("reply:"))
        assert "来源=文件" in line and "host=relay.example" in line, line
        blob = "\n".join(lines + files)
        assert KEY_K1 not in blob and KEY_K2 not in blob, "自检行里出现了 key 明文"
        # 文件那几行报的是 **key 的 id**，不是值。
        assert any("k1" in item and "k2" in item for item in files), files


# --- T4：坏条目只废自己 -------------------------------------------------------


def test_a_bad_agent_entry_only_disables_that_purpose() -> None:
    """T4：一条坏 `agents.<用途>` 只废那一路，别路照跑。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = _body(agents={
            "reply": {"provider": "relay1", "key": "k1", "model": "relay-model"},
            "judge": {"provider": "relay1"},  # 缺 model
        })
        _write_file(path, body)
        with _isolated(path, **FAKE):
            assert model_config.resolve("reply").api_key == KEY_K1
            assert "model" in model_config.status()["rejected"]["judge"]
            # 没写进文件的另外四路逐字等于今天那套。
            for task, provider, _base, model, key_env in EXPECTED:
                if task in {"reply", "judge"}:
                    continue
                endpoint = model_config.resolve(task)
                assert (endpoint.provider, endpoint.model, endpoint.key_env) == (
                    provider, model, key_env), task


# --- T5：改文件立刻生效（mtime 缓存会失效） -----------------------------------


def test_rewriting_the_file_takes_effect_on_the_next_resolve() -> None:
    """T5：改文件（mtime 变了）→ 下一次 `resolve()` 立刻看到新值，不用重启。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path):
            assert model_config.resolve("reply").api_key == KEY_K1
            time.sleep(0.01)
            body = _body(keys=[{"id": "k1", "value": "sk-changed"}])
            _write_file(path, body)
            assert model_config.resolve("reply").api_key == "sk-changed"


# --- T6：坏 JSON 整份忽略、不抛 -----------------------------------------------


def test_a_broken_json_file_is_ignored_and_recorded() -> None:
    """T6：坏 JSON → 整份忽略 + 记原因，六路回到今天那套（不抛、不崩）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        path.write_text("{not json", encoding="utf-8")
        model_providers_store.reset_cache()
        with _isolated(path, **FAKE):
            status = model_config.status()
            assert status["last_error"] == "invalid_json"
            for task, provider, base_url, model, key_env in EXPECTED:
                endpoint = model_config.resolve(task)
                assert (endpoint.provider, endpoint.base_url, endpoint.model) == (
                    provider, base_url, model), task


def test_version_one_files_are_ignored_instead_of_guessed() -> None:
    """v1（一个供应商一把 `key`）**整份忽略**并记原因，不去猜它想表达什么。

    依据：v1 从未部署过（三棵树里都没有这份文件，实测），而"猜一份看不懂的文件"比
    "忽略它并说清楚"危险得多。要迁移是单独一笔。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"version": 1,
                           "providers": {"relay1": {"base_url": RELAY_URL, "key": KEY_K1}},
                           "agents": {"reply": {"provider": "relay1", "model": "m"}}})
        with _isolated(path, **FAKE):
            status = model_config.status()
            assert status["last_error"] == "unsupported_version"
            assert status["version_ok"] is False
            task, provider, _base, model, key_env = EXPECTED[0]
            endpoint = model_config.resolve("reply")
            assert (endpoint.provider, endpoint.model, endpoint.key_env) == (
                provider, model, key_env), "v1 文件不许参与解析"


# --- T7：sync_from_file 只动已存在的 client，兜底期间不动 ---------------------


def test_sync_from_file_repoints_live_clients_unless_in_fallback() -> None:
    """T7：文件变了 → `sync_from_file()` 重指活 client；**兜底期间**不动（推迟、不丢）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path):
            client = OpenAICompatibleClient(RELAY_URL, KEY_K1, "relay-model",
                                            usage_role="dialogue", provider="relay1")
            time.sleep(0.01)
            _write_file(path, _body(keys=[{"id": "k1", "value": "sk-k1-new"},
                                          {"id": "k2", "value": KEY_K2}]))
            changed = provider_failover.sync_from_file([client])
            assert changed >= 0
            assert client.api_key == "sk-k1-new", "活 client 没跟着文件走"


# --- T7b：主 key 可以声明属于文件里的新家 -------------------------------------


def test_the_main_key_can_be_declared_as_a_file_only_family() -> None:
    """T7b：`QQBOT_API_PROVIDER` 指向文件里的新家时，走主 key 的那几路按**这一家**算。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body(agents={}))
        with _isolated(path, QQBOT_API_KEY="sk-main", QQBOT_API_PROVIDER="relay1"):
            assert provider_registry.base_url_of("relay1") == RELAY_URL
            endpoint = model_config.resolve("judge")
        assert endpoint.provider == "relay1"
        assert endpoint.api_key == "sk-main"


# --- T8：面板下拉框与 model_config 认识的家必须一致 ---------------------------


def test_the_provider_registry_knows_every_family_model_config_knows() -> None:
    """T8：`provider_registry` 的合并视图里有文件里的新家（下拉框不许少一项）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path):
            options = {item["value"]: item for item in provider_registry.options()}
            assert options["relay1"]["base_url"] == RELAY_URL
            assert model_config.provider_base_url("relay1") == RELAY_URL


# --- T9：reasoning_effort 读文件 + 迁移别名 -----------------------------------


def test_reasoning_effort_comes_from_the_file_and_the_old_name_still_works() -> None:
    """T9：`agents.<用途>.reasoning_effort` 优先；没有时回落到环境（含迁移期老名字）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = _body(agents={"reply": {"provider": "relay1", "key": "k1",
                                       "model": "relay-model",
                                       "reasoning_effort": "low"}})
        _write_file(path, body)
        with _isolated(path, QQBOT_DIALOGUE_REASONING_EFFORT="high"):
            assert model_config.file_reasoning_effort("reply") == "low"
            assert dev_config.reasoning_effort("reply") == "low"
        # 没写进文件的那一路照旧听环境。
        with _isolated(path, QQBOT_DIALOGUE_REASONING_EFFORT="high"):
            assert model_config.file_reasoning_effort("judge") == ""


# --- 写侧：一套供应商 + 它下面那几把 key ---------------------------------------


def test_a_new_set_lands_address_models_and_keys_together() -> None:
    """添加一套 = 地址 + 声明的模型 + 若干 key，**一次落盘**；id 由 store 给且稳定。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            model_providers_store.set_provider("relay1", base_url=RELAY_URL,
                                               models=["m-a", "m-b", "m-a"])
            first = model_providers_store.put_provider_key("relay1", label="主号",
                                                           value=KEY_K1)
            second = model_providers_store.put_provider_key("relay1", value=KEY_K2)
        body = _read_file(path)
        assert body["version"] == 2
        assert body["providers"]["relay1"]["base_url"] == RELAY_URL
        assert body["providers"]["relay1"]["models"] == ["m-a", "m-b"], "模型名要去重保序"
        assert [item["id"] for item in body["providers"]["relay1"]["keys"]] == ["k1", "k2"]
        assert first["id"] == "k1" and second["id"] == "k2"
        assert body["providers"]["relay1"]["keys"][0] == {"id": "k1", "label": "主号",
                                                          "value": KEY_K1}


def test_one_key_is_enough_and_zero_keys_is_refused_at_add_time() -> None:
    """「不强制最少几把 key」：一把能存；但**添加一套**时一把都没有要拒（空壳）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            model_providers_store.set_provider("relay1", base_url=RELAY_URL)
            model_providers_store.put_provider_key("relay1", value=KEY_K1)
            assert len(_read_file(path)["providers"]["relay1"]["keys"]) == 1
            try:
                model_providers_store.put_provider_key("relay1", value="")
            except ModelProvidersRejected as exc:
                assert "值" in str(exc)
            else:
                raise AssertionError("空值不该能加出一把 key")
            # 读侧一样：这一家有一把 key，这一路就能用。
            model_providers_store.upsert_agent("reply", provider="relay1",
                                               model="relay-model")
            assert model_config.resolve("reply").api_key == KEY_K1


def test_a_second_key_does_not_move_the_first() -> None:
    """加第二把 key 之后，"第一把"仍然是 k1（顺序稳定 = 回落链稳定）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            model_providers_store.set_provider("relay1", base_url=RELAY_URL)
            model_providers_store.put_provider_key("relay1", value=KEY_K1)
            model_providers_store.upsert_agent("reply", provider="relay1",
                                               model="relay-model")
            assert model_config.resolve("reply").api_key == KEY_K1
            model_providers_store.put_provider_key("relay1", value=KEY_K2)
            assert model_config.resolve("reply").api_key == KEY_K1, "第一把被挪走了"


def test_changing_a_key_value_keeps_its_id() -> None:
    """改一把 key 的值 = 同 id、新值；绑定它的那几路读到的就是新值。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            model_providers_store.set_provider("relay1", base_url=RELAY_URL)
            model_providers_store.put_provider_key("relay1", value=KEY_K1)
            model_providers_store.upsert_agent("reply", provider="relay1",
                                               key_id="k1", model="relay-model")
            model_providers_store.put_provider_key("relay1", key_id="k1",
                                                   value="sk-k1-rotated")
            assert model_config.resolve("reply").api_key == "sk-k1-rotated"
            body = _read_file(path)
            assert body["agents"]["reply"]["key"] == "k1", "改值不该换 id"


def test_removing_a_key_that_a_purpose_uses_is_refused() -> None:
    """删"在用"的那把 key → 拒绝并点名哪几路（先换一把，再删）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body(agents={"reply": {"provider": "relay1", "key": "k2",
                                                  "model": "relay-model"}}))
        with _isolated(path):
            try:
                model_providers_store.remove_provider_key("relay1", key_id="k2")
            except ModelProvidersRejected as exc:
                message = str(exc)
                assert "agents.reply" in message and "先" in message, message
            else:
                raise AssertionError("删掉还在用的那把 key 居然成功了")
            # 没人用的那把可以删。
            removed = model_providers_store.remove_provider_key("relay1", key_id="k1")
            assert removed["removed"] is True
        assert [item["id"] for item in _read_file(path)["providers"]["relay1"]["keys"]] == ["k2"]


def test_a_binding_can_be_cleared_back_to_the_env_layer() -> None:
    """`key_id=""` = 解开绑定（回到"这一套的第一把"→`.env`），不写一把空 key。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path):
            model_providers_store.upsert_agent("reply", provider="relay1",
                                               key_id="", model="relay-model")
            assert model_config.task_key_layer("reply")["layer"] == "first"
            assert "key" not in _read_file(path)["agents"]["reply"]


def test_removing_a_provider_that_is_in_use_is_refused() -> None:
    """用户补定：删除操作**必须先切到别的套、再删**；核心删除入口拒掉并给可读原因。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path):
            try:
                model_providers_store.remove_provider("relay1")
            except ModelProvidersRejected as exc:
                message = str(exc)
                assert "agents.reply" in message, message
                assert "先" in message and "再删" in message, message
            else:
                raise AssertionError("删掉还在用的那一套居然成功了")
            # 先解绑，再删——这一次允许。
            model_providers_store.clear_agent("reply")
            removed = model_providers_store.remove_provider("relay1")
            assert removed["removed"] is True
        assert "relay1" not in _read_file(path)["providers"]


def test_the_same_url_written_under_two_names_converges() -> None:
    """同一地址写两个名字 → 收敛成一个（U3：同账号两名会让 402 兜底静默失效）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            first = model_providers_store.set_provider("relaya", base_url=RELAY_URL)
            assert first["name"] == "relaya" and first["converged"] is False
            second = model_providers_store.set_provider("relayb", base_url=RELAY_URL + "/")
            assert second["converged"] is True, second
            assert second["name"] == "relaya"
            assert set(_read_file(path)["providers"]) == {"relaya"}
            # 手写的文件里同 URL 两个名字 → `status()["notes"]` 提醒一条。
            _write_file(path, {"version": 2, "providers": {
                "relaya": {"base_url": RELAY_URL, "keys": [{"id": "k1", "value": KEY_K1}]},
                "relayb": {"base_url": RELAY_URL, "keys": [{"id": "k1", "value": KEY_K2}]},
            }, "agents": {}})
            notes = model_config.status()["notes"]
            assert notes and "同一个地址" in notes[0], notes


def test_a_custom_address_without_a_key_is_refused_by_the_reader() -> None:
    """地址与 key 必须成对（D1 ②）：文件改了地址、这一家却没有 key ⇒ 不许借 `.env`。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        # deepseek 是内置家，但文件把地址改成了中转；这一家又没有 key ⇒ 拒。
        _write_file(path, {"version": 2,
                           "providers": {"custom": {"base_url": "https://my-relay.example/v1",
                                                    "keys": []}},
                           "agents": {"reply": {"provider": "custom",
                                                "model": "relay-model"}}})
        with _isolated(path, QQBOT_API_KEY="sk-main"):
            assert model_config.resolve("reply").source == ""
            reason = model_config.status()["rejected"]["reply"]
            assert "key" in reason, reason


def test_the_model_list_is_declared_but_not_a_whitelist() -> None:
    """`providers.<名字>.models` 只是"这家声明提供哪些模型"，**不是白名单**。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body(models=["declared-a"]))
        with _isolated(path):
            assert model_config.file_provider_models("relay1") == ("declared-a",)
            # 填列表外的模型名照样能用（用户：可以手输，面板标注就行）。
            assert model_config.resolve("reply").model == "relay-model"


def test_writing_is_immediately_readable_and_private() -> None:
    """写盘纪律：立刻可读、合法 JSON、POSIX 0600、mtime 变了、key 是**明文**。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            model_providers_store.set_provider("relay1", base_url=RELAY_URL)
            model_providers_store.put_provider_key("relay1", value=KEY_K1)
            mtime_before = path.stat().st_mtime_ns
            time.sleep(0.01)
            model_providers_store.put_provider_key("relay1", value=KEY_K2)
            assert path.stat().st_mtime_ns != mtime_before, "mtime 没变：读侧缓存与同步都会漏"
            body = _read_file(path)
            assert body["version"] == 2
            assert set(body) == {"version", "providers", "agents"}
            raw = path.read_text(encoding="utf-8")
            assert KEY_K1 in raw, "用户定的就是明文存储：磁盘上必须就是这把 key"
            assert model_config.file_provider_key_ids("relay1") == ("k1", "k2")
            if os.name != "nt":  # Windows 上 chmod 只是尽力而为
                assert (path.stat().st_mode & 0o777) == 0o600, "这份文件明文存着 key"


# --- 面板那一条 apply_overrides：只剩"模型名"还投文件 -------------------------


def _apply(tmp: str, payload: dict) -> tuple[dict, ControlAudit]:
    audit_path = Path(tmp) / "control_audit.jsonl"
    audit = ControlAudit(path=audit_path)
    store = OperatorConfig(Path(tmp) / "operator_config.json")
    result = runtime.apply_overrides(_Engine(), payload, store=store, audit=audit,
                                     source="test:panel", token="tok-test")
    return result, audit


def test_the_panel_model_write_lands_in_the_file_and_repoints_the_live_client() -> None:
    """`memory_model` 这类**只改模型名**的键仍然投文件；供应商与 key 绑定一个字不动。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"version": 2,
                           "providers": {"relay1": {"base_url": RELAY_URL,
                                                    "keys": [{"id": "k1", "value": KEY_K1}]}},
                           "agents": {"memory": {"provider": "relay1", "key": "k1",
                                                 "model": "m-old"}}})
        with _isolated(path):
            result, audit = _apply(tmp, {"memory_model": "m-new"})
            assert result["memory_model"]["applied"] == "live", result
            body = _read_file(path)
            assert body["agents"]["memory"] == {"provider": "relay1", "key": "k1",
                                                "model": "m-new"}
            assert model_config.resolve("memory").model == "m-new"
            entries = audit.tail(10)
            assert any(item.get("action") == "model_providers" for item in entries), entries
            assert KEY_K1 not in (Path(tmp) / "control_audit.jsonl").read_text("utf-8")


def test_an_empty_model_removes_the_purpose_from_the_file() -> None:
    """空模型名 = 把这一路从文件里删掉（回到 `.env` 那套），不是写一个空模型名。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _body())
        with _isolated(path, **FAKE):
            result, _audit = _apply(tmp, {"reply_api_model": ""})
            assert result["reply_api_model"]["applied"] == "live", result
            assert "reply" not in _read_file(path)["agents"]
            task, provider, base_url, model, key_env = EXPECTED[0]
            endpoint = model_config.resolve("reply")
            assert (endpoint.provider, endpoint.base_url, endpoint.model) == (
                provider, base_url, model), "清空之后没有回到今天那套"


def test_the_moved_settings_are_refused_with_a_pointer_to_the_new_page() -> None:
    """`*_api_key` / `*_provider` / `*_api_base_url` 已经搬去「模型与凭据」页。

    在这里**明确拒绝**并说清去哪儿改，而不是"写进覆盖层但被文件压住"（那种静默无效
    是这类配置里最坏的一种）。被拒时**一个字都不许写**。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            for key in ("reply_api_key", "judge_api_key", "reply_provider",
                        "reply_api_base_url"):
                result, _audit = _apply(tmp, {key: "x" if not key.endswith("url")
                                              else "https://x.example/v1"})
                assert result[key]["applied"] == "rejected", (key, result)
                assert "模型与凭据" in result[key]["detail"], result[key]
            assert not path.exists(), "被拒的那几次不该建出文件来"


def test_the_audit_never_contains_a_key_value() -> None:
    """写侧的审计**没有 key 明文**（`value_changed` 只是布尔、label 只记 `[已改]`）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        audit_path = Path(tmp) / "control_audit.jsonl"
        audit = ControlAudit(path=audit_path)
        with _isolated(path):
            model_providers_store.set_provider("relay1", base_url=RELAY_URL, audit=audit,
                                               source="test")
            model_providers_store.put_provider_key("relay1", value=KEY_K1, audit=audit,
                                                   source="test")
            model_providers_store.put_provider_key("relay1", label="新标签",
                                                   value=KEY_K2, audit=audit,
                                                   source="test")
            model_providers_store.upsert_agent("reply", provider="relay1", key_id="k2",
                                               model="m", audit=audit, source="test")
            model_providers_store.remove_provider_key("relay1", key_id="k1",
                                                      audit=audit, source="test")
        text = audit_path.read_text(encoding="utf-8")
        assert KEY_K1 not in text and KEY_K2 not in text, "审计里出现了 key 明文"
        assert "新标签" not in text, "审计里出现了 label 原值（它可能是账号别名）"
