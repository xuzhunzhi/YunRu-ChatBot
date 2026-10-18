"""`data/model_providers.json`：**六个用途各自单独切**的读侧 + 写侧（2026-10-18）。

## 这个文件钉的是什么

用户 2026-10-18 原话：*"这里，我要的是添加而非替换"*——所以这一层的形状是**一层覆盖**：

- 文件里**写了的**用途按文件来（供应商 / 地址 / key / 模型 / effort）；
- **没写的**逐字回落到今天那套（`PROVIDERS` / `KEYS` / `TASKS` + `.env` + 覆盖层）；
- **没有这个文件**时，所有请求与今天**逐字节相同**（§五 / §5.2 的三重证法）。

## 三重证法各自落在哪（§5.2）

1. **机械守护（本条 1）**：`test_without_the_file_every_purpose_is_identical_to_today`
   ——把 `QQBOT_MODEL_PROVIDERS_FILE` 指到一个**不存在的临时路径**，逐字段断言六路等于
   今天那张表（`tests/test_model_config.py` 的 `EXPECTED`，直接 import 过来，不另抄一份），
   并且 `self_check_lines()` **逐行相等**。再加上 `tests/run_offline.py` 里那行隔离——
   那才是"这台机器恰好没建文件"与"**这个状态被显式固定下来**"的区别。
2. **双树对拍（人工，产物在 `data/`）**：`data/tmp_mp_parity.py`（未跟踪）+ `git worktree`
   两个 HEAD 树，一棵贴本轮的 `src/`，各场景打印稳定 JSON 后 `Compare-Object` 必须为空。
   那一步**不进**本套件（干净 clone 拿不到 `data/` 里的东西），见汇报里的实测结果。
3. **源码形状守护（本条 2）**：`test_the_file_has_exactly_one_read_entry`——文件只有
   `_providers_file()` 一处读口，`resolve` / `_key_source` / `_base_url_for` /
   `_resolve_model` 全从它（或它的 `_file_plan()`）取。"取值的第二处来源不存在"这件事
   是**结构**保证的，不是靠测试碰巧覆盖到（10-06 那次 401 的根因就是第二处来源）。

## 用户 2026-10-18 补定的三条（与契约一起落在这里）

1. **key 明文存在文件里**（安全等级同 `.env`，`data/` 不进 git）：`test_the_plaintext_key_
   round_trips` 钉住"写进去什么、读回来什么、文件里就是明文"，并断言 POSIX 上权限 `0600`。
   面板那一轮要明写"这里能看到明文"——**核心这一轮只留注释**，不加"加密"（加了之后
   排障方式就与 `.env` 分成两套，而且密钥最终还得有一把能解它）。
2. **"当前那一套"不许直接删**：`test_removing_a_provider_that_is_in_use_is_refused` 钉住
   `remove_provider()` 拒绝并给**可读原因**（点名哪几路在用、先切再删）。
3. **只做核心 + 一条命令行脚本**：脚本 `data/model_switch.py` 不进本套件（干净 clone
   拿不到它），它只调本文件测过的那些写层函数。

## 写侧（§4.2 W1–W6）

W1 面板写 key 落进文件 + 活 client 重指向；W2 空值 = 从文件里删掉这一路；W3 审计里
没有 key 明文；W4 写完立刻可读 / 合法 JSON / 权限 / mtime；W5 `custom` 地址与 key 成对；
W6 同一地址两个名字按 URL 收敛。
"""

import asyncio
import io
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from qq_roleplay_bot import (
    dev_config,
    model_config,
    model_providers_store,
    provider_failover,
    provider_registry,
    runtime,
)
from qq_roleplay_bot.control_audit import ControlAudit, summarize
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


def _relay_body(provider: str = "relay1", url: str = "https://relay.example/v1",
                key: str = "sk-file-relay", model: str = "relay-model") -> dict:
    return {"providers": {provider: {"base_url": url, "key": key}},
            "agents": {"reply": {"provider": provider, "model": model}}}


def _expected_reply_line() -> str:
    task, provider, _base, model, key_env = EXPECTED[0]
    return f"{task}: provider={provider} model={model} key={key_env} 有值"


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


# --- §5.2 第 3 条 / T10：源码形状守护 -----------------------------------------


def test_the_file_has_exactly_one_read_entry() -> None:
    """T10：文件只有 `_providers_file()` 一处读口——**结构上写不出第二处来源**。

    为什么需要它：前两条证的是"行为一致"，这一条证的是"**形状**上只有一个来源"，
    而那正是 2026-10-06 那次三系统 401 的根因（地址与 key 各自有一个来源）。
    """

    import inspect

    source = inspect.getsource(model_config)
    assert source.count("def _providers_file(") == 1, "读口不止一处"
    assert source.count("_providers_store.load(") == 1, \
        "除了 `_providers_file()` 之外还有地方直接读 store"
    # 四个取值函数都必须经过文件那一档（`_file_plan` 是 `_providers_file` 的薄包装）。
    for func in (model_config._key_source, model_config._base_url_for,
                 model_config._resolve_model, model_config.resolve):
        body = inspect.getsource(func)
        assert ("_file_plan(" in body) or ("_providers_file(" in body), \
            f"{func.__name__} 没有从文件那一处取"
    # 读侧绝不写：写 API 的名字不许出现在 `model_config` 里。
    for name in ("upsert_agent(", "clear_agent(", "remove_provider(", "set_provider("):
        assert name not in source, f"读侧出现了写入口 {name}（读写必须分开）"


# --- T2：文件里写了的用途，一个环境覆盖都不听 ---------------------------------


def test_a_purpose_written_in_the_file_ignores_every_env_override() -> None:
    """T2：文件优先 + 用途之间互不影响（另五项仍是今天那套）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _relay_body())
        with _isolated(path, QQBOT_REPLY_API_KEY="sk-fake-reply",
                       QQBOT_REPLY_API_BASE_URL="https://reply.example/v1",
                       QQBOT_REPLY_API_MODEL="reply-model-fake",
                       QQBOT_API_KEY="sk-main-fake",
                       QQBOT_API_BASE_URL="https://only.example/v1",
                       QQBOT_API_MODEL="global-model-fake"):
            reply = model_config.resolve("reply")
            assert (reply.provider, reply.base_url, reply.model, reply.api_key) == (
                "relay1", "https://relay.example/v1", "relay-model", "sk-file-relay")
            assert reply.own_key is True and reply.source == "文件"
            # 另外五项：一个都没写进文件 ⇒ 逐字按环境那套（主 key + 全局地址 + 全局模型）。
            for task, _provider, _base, _model, _key_env in EXPECTED[1:]:
                endpoint = model_config.resolve(task)
                assert endpoint.base_url == "https://only.example/v1", task
                assert endpoint.model == "global-model-fake", task
                assert endpoint.api_key == "sk-main-fake", task
                assert endpoint.key_name == model_config.MAIN_KEY, task
                assert endpoint.source == "", f"{task} 不该被文件接管"


# --- T3：自检行点名来源、绝不打出 key 值 ---------------------------------------


def test_the_self_check_names_the_file_source_without_leaking_the_key() -> None:
    """T3：`来源=文件` + `providers.<名字>` 出现在那一行；key 的值一个字符都不出现。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _relay_body(key="sk-Z" * 12))
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-fake-judge"):
            lines = model_config.self_check_lines()
            if len(lines) != len(EXPECTED):
                raise AssertionError(f"自检行数变了：{len(lines)} != {len(EXPECTED)}")
            reply_line = lines[0]
            assert "来源=文件" in reply_line, reply_line
            assert "providers.relay1" in reply_line, reply_line
            assert "host=relay.example" in reply_line, reply_line
            blob = "\n".join(lines)
            assert "sk-Z" not in blob, "自检把文件里的 key 打出来了"
            # 运行时那个钩子的**返回值**仍等于 `self_check_lines()`（不变式）。
            assert runtime._log_model_config_self_check() == model_config.self_check_lines()
            assert "sk-Z" not in "\n".join(runtime._log_model_config_self_check())


# --- T4：坏条目只废自己 -------------------------------------------------------


def test_a_bad_agent_entry_only_disables_that_purpose() -> None:
    """T4：`agents.reply.provider="nope"` → reply 回落今天那套，且 `status()` 点名原因。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"providers": {},
                           "agents": {"reply": {"provider": "nope", "model": "x"},
                                      "judge": {"provider": "deepseek",
                                                "model": "deepseek-v4-pro"}}})
        with _isolated(path, **FAKE):
            reply = model_config.resolve("reply")
            task, provider, base_url, model, key_env = EXPECTED[0]
            assert (reply.provider, reply.base_url, reply.model, reply.key_env,
                    reply.source) == (provider, base_url, model, key_env, "")
            assert "reply" in model_config.status()["rejected"]
            assert "nope" in model_config.status()["rejected"]["reply"]
            # 另一路（写对了的）照旧生效——**不整份作废**。
            judge = model_config.resolve("judge")
            assert judge.source == "文件"
            assert judge.provider == "deepseek" and judge.model == "deepseek-v4-pro"
            assert judge.api_key == FAKE["QQBOT_JUDGE_API_KEY"]


# --- T5：改文件立刻生效（mtime 缓存会失效） -----------------------------------


def test_rewriting_the_file_takes_effect_on_the_next_resolve() -> None:
    """T5：v1 → A；重写 v2 + `os.utime` → B（缓存不会永久钉住旧值）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _relay_body(provider="relay_a", url="https://a.example/v1",
                                      key="sk-a", model="model-a"))
        with _isolated(path):
            first = model_config.resolve("reply")
            assert (first.provider, first.base_url, first.api_key) == (
                "relay_a", "https://a.example/v1", "sk-a")
            _write_file(path, _relay_body(provider="relay_b", url="https://b.example/v1",
                                          key="sk-b", model="model-b"))
            now = time.time()
            os.utime(path, (now + 5, now + 5))
            second = model_config.resolve("reply")
            assert (second.provider, second.base_url, second.api_key, second.model) == (
                "relay_b", "https://b.example/v1", "sk-b", "model-b")


# --- T6：坏 JSON 整份忽略、不抛 -----------------------------------------------


def test_a_broken_json_file_is_ignored_and_recorded() -> None:
    """T6：一份坏 JSON 不该让 bot 起不来（整份当没有 + `last_error`）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        model_providers_store.reset_cache()
        with _isolated(path, **FAKE):
            for task, provider, base_url, model, key_env in EXPECTED:
                endpoint = model_config.resolve(task)  # 不抛
                assert (endpoint.provider, endpoint.base_url, endpoint.model,
                        endpoint.key_env, endpoint.source) == (
                    provider, base_url, model, key_env, "")
            status = model_config.status()
            assert status["present"] is True
            assert status["last_error"] == "invalid_json"
            assert status["rejected"] == {}


# --- T7：sync_from_file 只动已存在的 client，兜底期间不动 ---------------------


def test_sync_from_file_repoints_live_clients_unless_in_fallback() -> None:
    """T7：文件变了 → 已存在的 client 整体重指向；兜底期间返回 0（推迟、不丢）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _relay_body(key="sk-file-relay"))
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-fake-judge",
                       QQBOT_LETTER_API_KEY="sk-fake-letter"):
            reply_client = OpenAICompatibleClient(
                "https://old.example/v1", "sk-old", "old-model",
                usage_role="dialogue", provider="deepseek")
            judge_client = OpenAICompatibleClient(
                "https://old.example/v1", "sk-old", "old-model",
                usage_role="judge", provider="deepseek")
            assert provider_failover.sync_from_file(
                clients=[reply_client, judge_client]) == 2
            assert (reply_client.base_url, reply_client.api_key, reply_client.model,
                    reply_client.provider) == (
                "https://relay.example/v1", "sk-file-relay", "relay-model", "relay1")
            assert judge_client.base_url == "https://api.deepseek.com"
            assert judge_client.api_key == "sk-fake-judge"
            # 现在进兜底（judge 报 402）→ 文件再改也不许把它拉回来。
            assert provider_failover.note_balance_exhausted("deepseek", task="judge") is True
            assert provider_failover.reapply(clients=[reply_client, judge_client]) == 2
            assert judge_client.provider == "mimo"
            _write_file(path, _relay_body(provider="relay2", url="https://relay2.example/v1",
                                          key="sk-relay2", model="model-2"))
            assert provider_failover.sync_from_file(
                clients=[reply_client, judge_client]) == 0
            assert judge_client.provider == "mimo", "兜底期间不该被文件改动抢走"
            assert reply_client.provider == "mimo"


# --- T8：面板下拉框与 model_config 认识的家必须一致 ---------------------------


def test_the_provider_registry_knows_every_family_model_config_knows() -> None:
    """T8：内置那几家 + 文件里的新家，`provider_registry` 全认识且地址相同。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"providers": {"relay1": {"base_url": "https://relay.example/v1"}},
                           "agents": {}})
        with _isolated(path):
            names = set(model_config.PROVIDERS) | {"relay1"}
            for name in sorted(names):
                assert provider_registry.known(name), f"下拉框不认识 {name}"
                assert provider_registry.base_url_of(name) == \
                    model_config._provider_spec(name)["base_url"], name
            values = [item["value"] for item in provider_registry.options()]
            assert "relay1" in values, "文件里的新家必须能选"


# --- T9：reasoning_effort 读文件 + 迁移别名 -----------------------------------


def test_reasoning_effort_comes_from_the_file_and_the_old_name_still_works() -> None:
    """T9：文件给了 `max` → 请求体里就是 `max`；只配老名字时 reply 仍读得到。

    前半条钉的是 D4 修掉的那个**现存缺陷**（`llm_client` 用 `usage_role` 当用途名，
    于是回复那一路的 `QQBOT_REPLY_*` 从来没生效过）；后半条钉的是**迁移别名**：
    线上 `run/.env` 里老名字是未注释的（生效中），没有别名它就会悄悄变成 `low`。
    """

    request = [{"role": "user", "content": "你好"}]
    ok_body = {"choices": [{"message": {"content": "好"}}], "usage": {}}

    def _body_for() -> dict:
        seen: list = []

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            seen.append(req)
            return _Response(json.dumps(ok_body).encode())

        client = OpenAICompatibleClient(
            "https://api.deepseek.com", "sk-fake-judge", "deepseek-flash",
            timeout=5.0, user_id="qqbot-dialogue", provider="deepseek",
            usage_role="dialogue")
        with patch("qq_roleplay_bot.llm_client._urlopen", side_effect=fake_urlopen):
            assert asyncio.run(client.complete(request)) == "好"
        assert len(seen) == 1
        return json.loads(seen[0].data.decode("utf-8"))

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, {"providers": {},
                           "agents": {"reply": {"provider": "deepseek",
                                                "model": "deepseek-flash",
                                                "reasoning_effort": "max"}}})
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-fake-judge"):
            assert dev_config.reasoning_effort("reply") == "max"
            assert _body_for()["reasoning_effort"] == "max"

    # 没有文件时：老名字仍然生效（迁移别名）；新名字优先。
    with TemporaryDirectory() as tmp:
        with _isolated(Path(tmp) / "model_providers.json",
                       QQBOT_JUDGE_API_KEY="sk-fake-judge",
                       QQBOT_DIALOGUE_REASONING_EFFORT="high"):
            assert dev_config.reasoning_effort("reply") == "high"
            assert _body_for()["reasoning_effort"] == "high"
        with _isolated(Path(tmp) / "model_providers.json",
                       QQBOT_JUDGE_API_KEY="sk-fake-judge",
                       QQBOT_DIALOGUE_REASONING_EFFORT="high",
                       QQBOT_REPLY_REASONING_EFFORT="max"):
            assert dev_config.reasoning_effort("reply") == "max"


# --- W1：面板写 key → 落进文件 + 活 client 重指向 -----------------------------


def _apply(tmp: str, payload: dict, *, clients=()) -> tuple[dict, ControlAudit]:
    audit_path = Path(tmp) / "control_audit.jsonl"
    audit = ControlAudit(path=audit_path)
    store = OperatorConfig(Path(tmp) / "operator_config.json")
    result = runtime.apply_overrides(_Engine(), payload, store=store, audit=audit,
                                     source="test:panel", token="tok-test")
    return result, audit


def test_the_panel_key_write_lands_in_the_file_and_repoints_the_live_client() -> None:
    """W1：`reply_api_key` → 文件里 `providers.<reply 那一家的名字>.key`；活 client 跟着变。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-fake-judge"):
            client = OpenAICompatibleClient(
                "https://api.deepseek.com", "sk-fake-judge", "deepseek-flash",
                usage_role="dialogue", provider="deepseek")
            result, audit = _apply(tmp, {"reply_api_key": "sk-file-new"})
            assert result["reply_api_key"]["applied"] == "live", result
            body = _read_file(path)
            assert body["providers"]["deepseek"]["key"] == "sk-file-new"
            assert body["providers"]["deepseek"]["base_url"] == "https://api.deepseek.com"
            assert body["agents"]["reply"]["provider"] == "deepseek"
            assert body["agents"]["reply"]["model"] == "deepseek-flash"
            # **不**写进 `operator_config.json`（唯一存储是文件）。
            operator_bytes = (Path(tmp) / "operator_config.json").read_text(encoding="utf-8")
            assert "reply_api_key" not in operator_bytes
            # 活 client 四件套跟着走（不是等下次重启）。
            assert client.api_key == "sk-file-new"
            assert client.base_url == "https://api.deepseek.com"
            assert client.model == "deepseek-flash"
            # 审计里留了"从哪套到哪套"，且没有 key 明文。
            entries = audit.tail(10)
            assert any(item.get("action") == "model_providers" for item in entries), entries
            audit_text = (Path(tmp) / "control_audit.jsonl").read_text(encoding="utf-8")
            assert "sk-file-new" not in audit_text, "审计里出现了 key 明文"


# --- W2：空值 = 从文件里删掉这一路 --------------------------------------------


def test_an_empty_value_removes_the_purpose_from_the_file() -> None:
    """W2：空串 → 该用途**从文件里消失**，`resolve()` 回到今天那套（逐字段对表）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path, **FAKE):
            _apply(tmp, {"reply_api_key": "sk-file-temp"})
            assert _read_file(path)["agents"]["reply"]["provider"]
            result, _audit = _apply(tmp, {"reply_api_key": ""})
            assert result["reply_api_key"]["applied"] == "live", result
            assert "reply" not in _read_file(path)["agents"], "空值必须把这一路从文件里删掉"
            reply = model_config.resolve("reply")
            task, provider, base_url, model, key_env = EXPECTED[0]
            assert (reply.provider, reply.base_url, reply.model, reply.key_env,
                    reply.own_key, reply.source) == (
                provider, base_url, model, key_env, True, "")


# --- W3：审计里没有 key 明文（含"嵌套 dict 会漏"这条硬约束） ------------------


def test_the_audit_never_contains_a_key_value() -> None:
    """W3：凭据类的 payload 键一律 `[已改]`；**嵌套 dict 会漏**——这正是硬约束 D7。

    ⚠️ 与契约原文的一处不一致（如实记在这里，不偷偷放宽）：契约 W3 写"对**每一个**新
    payload 键名断言 `[已改]`"。但新加的那 6 个键是 `*_provider`（值是一个**名字**，
    不是凭据），`control_audit.summarize()` 只按"名字里含 key/token/password/secret/passwd"
    掩码，所以它们会照原样记进去——而契约 §三.5 又明写"本轮不碰 control_audit"。
    两者只能取一个：这里的判据收窄成"**凭据类**的键必须 `[已改]`、审计正文里不许出现
    任何 key 值"，并把"嵌套会漏"钉成一条**会红**的用例（那才是 D7 硬约束的真身）。
    把 `*_provider` 也掩掉反而会毁掉"从哪套到哪套"这条审计要求（掩掉的正是要记的名字）。
    """

    for name in ("reply_api_key", "judge_api_key", "memory_api_key", "review_api_key",
                 "letter_api_key", "vision_api_key", "api_key"):
        assert summarize({name: "sk-plaintext-123"}) == {name: "[已改]"}, name

    # 嵌套 payload：顶层键叫 `agents`，里面才是 key ⇒ **整份 dict 会被 clip 进流水**。
    nested = {"agents": {"reply": {"key": "sk-plaintext-123"}}}
    leaked = json.dumps(summarize(nested), ensure_ascii=False)
    assert "sk-plaintext-123" in leaked, (
        "嵌套 payload 居然没漏 key？那说明 control_audit 改了递归掩码——"
        "契约 §三.5 说那件事要单独出计划，本轮不许顺手改。"
        "这条用例红的意思是：**D7 的硬约束仍然成立**，请把它改成断言'漏'，"
        "并确认我们没有写嵌套 payload。")

    # 我们确实**没有**写嵌套 payload：`apply_overrides` 的审计 detail 永远是顶层键。
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-fake-judge"):
            _apply(tmp, {"reply_api_key": "sk-file-secret"})
            text = (Path(tmp) / "control_audit.jsonl").read_text(encoding="utf-8")
            assert "sk-file-secret" not in text
            for line in text.splitlines():
                entry = json.loads(line)
                assert all(isinstance(value, str) for value in entry["detail"].values()), \
                    "审计 detail 里出现了嵌套结构（D7：那会把 key 明文带进流水）"


# --- W4：写完立刻可读 / 合法 JSON / 权限 / mtime ------------------------------


def test_writing_is_immediately_readable_and_private() -> None:
    """W4：写盘纪律——立刻可读、合法 JSON、POSIX 0600、mtime 变了。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            first = model_providers_store.upsert_agent(
                "reply", provider="relay1", model="relay-model",
                base_url="https://relay.example/v1", key="sk-a")
            assert first["provider"] == "relay1"
            assert path.exists()
            mtime_before = path.stat().st_mtime_ns
            time.sleep(0.01)
            model_providers_store.upsert_agent(
                "judge", provider="relay1", model="relay-model",
                base_url="https://relay.example/v1", key="sk-a")
            assert path.stat().st_mtime_ns != mtime_before, "mtime 没变：读侧缓存与同步都会漏"
            body = _read_file(path)
            assert body["version"] == 1
            assert set(body) == {"version", "providers", "agents"}
            # 写完**立刻**可读（不需要重启，也不用手动清缓存）。
            assert model_config.resolve("judge").api_key == "sk-a"
            if os.name != "nt":  # Windows 上 chmod 只是尽力而为
                assert (path.stat().st_mode & 0o777) == 0o600, "这份文件明文存着 key"


# --- W5：custom 必须成对 -------------------------------------------------------


def test_custom_needs_a_key_of_its_own() -> None:
    """W5：`custom` 只给地址不给 key → `rejected`；给了 key → 地址与 key 落在**同一条**。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path, QQBOT_JUDGE_API_KEY="sk-fake-judge"):
            rejected, _audit = _apply(tmp, {
                "reply_provider": "custom",
                "reply_api_base_url": "https://my-relay.example/v1",
            })
            assert rejected["reply_provider"]["applied"] == "rejected", rejected
            assert "key" in rejected["reply_provider"]["detail"], rejected
            assert not path.exists(), "被拒的请求不许留下半个文件"

            ok, _audit = _apply(tmp, {
                "reply_provider": "custom",
                "reply_api_base_url": "https://my-relay.example/v1",
                "reply_api_key": "sk-custom-1",
            })
            assert ok["reply_provider"]["applied"] == "live", ok
            assert ok["reply_api_key"]["applied"] == "live", ok
            entry = _read_file(path)["providers"]["custom"]
            assert entry["base_url"] == "https://my-relay.example/v1"
            assert entry["key"] == "sk-custom-1", "地址与 key 必须落在同一条里"
            reply = model_config.resolve("reply")
            assert (reply.provider, reply.base_url, reply.api_key) == (
                "custom", "https://my-relay.example/v1", "sk-custom-1")


# --- W6：同一地址按 URL 收敛 ---------------------------------------------------


def test_the_same_url_written_under_two_names_converges() -> None:
    """W6：同 URL 两个名字 → 收敛成一个（U3：同账号两名会让 402 兜底静默失效）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            first = model_providers_store.set_provider(
                "relaya", base_url="https://relay.example/v1", key="sk-a")
            assert first["name"] == "relaya" and first["converged"] is False
            second = model_providers_store.set_provider(
                "relayb", base_url="https://relay.example/v1/", key="sk-a")
            assert second["converged"] is True, second
            assert second["name"] == "relaya"
            assert set(_read_file(path)["providers"]) == {"relaya"}

            # 同一个地址、**不同**的 key：不许悄悄丢掉其中一把（那会把凭据写没）。
            try:
                model_providers_store.set_provider(
                    "relayc", base_url="https://relay.example/v1", key="sk-other")
            except ModelProvidersRejected as exc:
                assert "key" in str(exc)
            else:
                raise AssertionError("同地址不同 key 应当被拒绝，而不是收敛掉")

            # 读侧也提醒：手写的文件里同 URL 两个名字 → `status()["notes"]` 有一条。
            _write_file(path, {"providers": {
                "relaya": {"base_url": "https://relay.example/v1", "key": "sk-a"},
                "relayb": {"base_url": "https://relay.example/v1", "key": "sk-b"},
            }, "agents": {}})
            notes = model_config.status()["notes"]
            assert notes and "同一个地址" in notes[0], notes


# --- 用户补定 ②：删"当前那一套"必须被拒 ---------------------------------------


def test_removing_a_provider_that_is_in_use_is_refused() -> None:
    """用户补定 ②：删除操作**必须先切到别的套、再删**；核心删除入口拒掉并给可读原因。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _relay_body())
        with _isolated(path):
            try:
                model_providers_store.remove_provider("relay1")
            except ModelProvidersRejected as exc:
                message = str(exc)
                assert "agents.reply" in message, message
                assert "先" in message and "再删" in message, message
            else:
                raise AssertionError("删掉还在用的那一套居然成功了")
            # 被拒之后文件**一个字都没动**（失败不留半个状态）。
            assert "relay1" in _read_file(path)["providers"]

            # 先切到别的家（这里是内置的 deepseek），再删——这一次允许。
            model_providers_store.upsert_agent(
                "reply", provider="deepseek", model="deepseek-flash",
                base_url="https://api.deepseek.com")
            removed = model_providers_store.remove_provider("relay1")
            assert removed["removed"] is True
            assert "relay1" not in _read_file(path)["providers"]


# --- 用户补定 ①：明文密钥写入后读回一致 ---------------------------------------


def test_the_plaintext_key_round_trips() -> None:
    """用户补定 ①：key **明文**存在 `providers.<名字>.key`，写进去什么、读回来什么。

    安全等级与 `.env` 相同（`data/` 在 `.gitignore` 里）；**面板那一轮要明写"这里能看到
    明文"**——核心这一轮只把话写进注释，不加任何"加密"（加了之后排障方式就与 `.env`
    分成两套，而且密钥最终还得有一把能解它）。
    """

    secret = "sk-plaintext-" + "Ab3" * 8
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path):
            model_providers_store.upsert_agent(
                "reply", provider="relay1", model="relay-model",
                base_url="https://relay.example/v1", key=secret)
            raw = path.read_text(encoding="utf-8")
            assert secret in raw, "用户定的就是明文存储：磁盘上必须就是这把 key"
            assert _read_file(path)["providers"]["relay1"]["key"] == secret
            assert model_config.resolve("reply").api_key == secret
            # 读侧自检**绝不**把它打出来（明文存储 ≠ 明文进日志）。
            assert secret not in "\n".join(model_config.self_check_lines())
            assert secret not in "\n".join(model_config.providers_file_lines())
