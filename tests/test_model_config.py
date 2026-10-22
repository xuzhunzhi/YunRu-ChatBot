"""**模型 / 凭据 / 用途**三层配置的形状（2026-10-06 重做）。

由来是一次真实的现场事故：`run/.env` 里全局地址指着 mimo、主 key 是 mimo 那把，
而判定 / 记忆 / 审核的 key 是 **deepseek** 那把。于是那三个子系统拿 deepseek 的
key 去打 mimo，**全 401**。根因不是"谁配错了"，是**形状**——只要"地址"和"key"
还能各自单独被覆盖，这种错就写得出来。

现在形状是三层（`model_config`）：

    providers  只放连接信息（地址 / client_type），**不放 key**；
    keys       **按用途分开**，每条**显式绑定属于哪家** + 从哪个环境变量取值；
    tasks      用途 → 哪条 key + 哪个模型名。

一个客户端的三个字段**永远由同一条 key 条目解析出来**，所以
"A 家 key 打 B 家地址"**写不出来**。这个文件把那件事钉住：

1. 每个用途解析出的 `(provider, base_url, model, key 变量名)` **逐字段**对得上定下的表；
2. **改一条 key 的 provider → 地址跟着变**（证明成对取自同一条）；
3. 新配置**整份缺失** → 与改动前逐字相同（只配一把 key 的部署照旧能跑）；
4. key 缺失 / 变量为空 → **明确报出来**（而不是静默拿一把别的 key 去打）;
5. 启动自检那一行**不含 key 值**（只出现变量名与"有值没有"）。

> ⚠️ **2026-10-16 的如实交代（用户原话："切换回ds。gemini好蠢。"，同日早先那句是
> "都改ds"）**：判定与回复两路**都回到 deepseek**——`TASKS["reply"]` 与
> `TASKS["judge"]` 都改成 `{"key": "judge", "model": "deepseek-flash"}`，也就是
> 2026-10-14 那两笔（1834fce / d62446a）**之前**的形状；那两笔之后加进来的
> "借 `reply` 那条 key（xkiro）"的期望值因此一并回退。依据除了这句口径，还有两条
> 实测（记在 `model_config.TASKS` 的注释里）：中继的 Google 上游整段 503，且
> `provider=xkiro` 时 `reasoning_effort` 发不出去 ⇒ 首字压不到闸以下。
>
> 本文件因此要分清两类东西，改的时候别混：
>
> - **不变式**（与本条路由无关，一个字都没松）：地址与 key **成对**取自同一条来源；
>   任务之间互不影响（改一路不许动另一路）；没 key 时 **fail-closed**（`require`
>   明确报错，不许拿别的 key 顶上）；自检那一行不含 key 值。
> - **随路由更新的期望值**（跟着 `TASKS` / `KEYS` 走）：`EXPECTED` 里每个用途的
>   `(provider, base_url, model, key 变量名)`，以及"哪一路在用哪条 key 条目"。
>   这些值**移了就要跟着改**；判定的家回到 deepseek 之后，"deepseek 那一路"的例子
>   也回到 **`judge`**——2026-10-14～10-16 那段它住 xkiro 时才需要绕开。

（测试里用的都是**假 key**：真实值由部署方写在 `run/.env` 里，不进任何仓库文件。）
"""
import os
from contextlib import contextmanager

from qq_roleplay_bot import dev_config, model_config, runtime
from qq_roleplay_bot.model_config import (
    MissingCredential, TaskEndpoint, UnknownTask,
)

#: 这张表就是"用户定下的形状"（`model_config` 里的三层把它写死了一遍）。
#:
#: `(用途, provider, base_url, 模型名, key 变量名)`
EXPECTED: tuple[tuple[str, str, str, str, str], ...] = (
    # 2026-10-16（用户原话："切换回ds。gemini好蠢。"，同日早先那句是"都改ds"）：
    # 回复那一路**从 xkiro 改回 deepseek** —— 借 `judge` 那条 key（deepseek /
    # `deepseek-flash`）。这一条推翻 2026-10-14 的"自己那条 key（xkiro）"，依据是
    # 用户这句口径**加上两条实测**（2026-10-16）：①中继的 Google 上游整段 503
    # （`/v1/models` 通、`chat/completions` 全 503，25 分钟探针全挂）；
    # ②`reasoning_effort` 只在 provider ∈ {"", "deepseek"} 时才发（`llm_client` 的
    # `deepseek_fields`），provider=xkiro 时发不出去 ⇒ 只能吃默认档，正文最慢实测
    # 13.9s；回到 ds 之后 `reasoning_effort=none` 能把"第一个 content"从 1.1~14.5s
    # 压到 0.51~1.13s、过闸（第一条完整响应行）0.15~0.31s。
    # `KEYS["reply"]` 那条 xkiro 条目**保留**（闲置，将来一键切回要用）。
    ("reply", "deepseek", "https://api.deepseek.com", "deepseek-flash",
     "QQBOT_JUDGE_API_KEY"),
    # 2026-10-12 用户拍板"信用v4pro写"：letter 与 reply 同一个形状——要的模型挂在
    # 哪一家就借哪家的 key，`QQBOT_LETTER_API_KEY`（mimo 那把）因此在正常路径上
    # 闲置，只在"主家余额耗尽切兜底"那条链上被借走（见 `FALLBACK_TASKS`）。
    ("letter", "deepseek", "https://api.deepseek.com", "deepseek-v4-pro",
     "QQBOT_JUDGE_API_KEY"),
    # 2026-10-16（同一条口径："切换回ds。gemini好蠢。"）：判定**也回到 deepseek**，
    # 用它自己那条 key（`QQBOT_JUDGE_API_KEY`）与 `deepseek-flash`——即 2026-10-14
    # 之前那一版。依据同 reply 那条（上游 503 + `reasoning_effort` 发不出去 ⇒
    # 首字撞"3 秒"的闸）。⚠️ `KEYS["judge"]` 的 provider 仍然**不能**改成 xkiro：
    # letter 借它、memory/review/vision 回落到它，一改就会把 `deepseek-v4-pro` /
    # `deepseek-flash` 这些模型名打到 xkiro 上。
    ("judge", "deepseek", "https://api.deepseek.com", "deepseek-flash",
     "QQBOT_JUDGE_API_KEY"),
    # **知识块判档**（2026-10-18 用户："这块不行也加个agent，用dsflash，effort调none"）：
    # 与 judge 同一个形状——同一把 key（`QQBOT_JUDGE_API_KEY`）、同一个模型，**不新增
    # key 项**。思考深度写死在 `dev_config.FIXED_REASONING_EFFORT`（`none`），不读环境。
    ("relevance", "deepseek", "https://api.deepseek.com", "deepseek-flash",
     "QQBOT_JUDGE_API_KEY"),
    ("memory", "deepseek", "https://api.deepseek.com", "deepseek-flash",
     "QQBOT_MEMORY_API_KEY"),
    ("review", "deepseek", "https://api.deepseek.com", "deepseek-flash",
     "QQBOT_REVIEW_API_KEY"),
    ("vision", "deepseek", "https://api.deepseek.com", "deepseek-flash",
     "QQBOT_VISION_API_KEY"),
)

#: 会被"每用途解析"读到的环境变量（测试要能整份清空，跑完原样还回去）。
ALL_VARS: tuple[str, ...] = (
    "QQBOT_API_KEY", "QQBOT_API_BASE_URL", "QQBOT_API_MODEL", "QQBOT_API_PROVIDER",
    "QQBOT_REPLY_API_KEY", "QQBOT_REPLY_API_BASE_URL", "QQBOT_REPLY_API_MODEL",
    "QQBOT_LETTER_API_KEY", "QQBOT_LETTER_API_BASE_URL", "QQBOT_LETTER_API_MODEL",
    "QQBOT_MAIL_API_KEY", "QQBOT_MAIL_MODEL",
    "QQBOT_JUDGE_API_KEY", "QQBOT_JUDGE_API_BASE_URL", "QQBOT_JUDGE_MODEL",
    "QQBOT_MEMORY_API_KEY", "QQBOT_MEMORY_API_BASE_URL", "QQBOT_MEMORY_MODEL",
    "QQBOT_REVIEW_API_KEY", "QQBOT_REVIEW_API_BASE_URL", "QQBOT_REVIEW_MODEL",
    "QQBOT_VISION_API_KEY", "QQBOT_VISION_API_BASE_URL", "QQBOT_VISION_MODEL",
    # 兜底那条 key（主家余额耗尽时切过去的那一路，见 `FALLBACK_KEY`）。
    "QQBOT_FALLBACK_API_KEY",
)

#: 只给某一用途的假 key（**每个用途一把，互不相同**，这样"拿错 key"会当场看出来）。
FAKE: dict[str, str] = {
    "QQBOT_REPLY_API_KEY": "sk-fake-reply",
    "QQBOT_LETTER_API_KEY": "sk-fake-letter",
    "QQBOT_JUDGE_API_KEY": "sk-fake-judge",
    "QQBOT_MEMORY_API_KEY": "sk-fake-memory",
    "QQBOT_REVIEW_API_KEY": "sk-fake-review",
    "QQBOT_VISION_API_KEY": "sk-fake-vision",
}


@contextmanager
def _env(*, env_file_key: str | None = None, **values):
    """把这批变量清空后按 `values` 设好，跑完原样还回去。

    **`""` 是有效值**（空串 = 没配，这正是要测的那些情况之一）；只有 `None`
    才表示"保持清空"——与 `test_reply_endpoint._env` 的约定一致。

    `env_file_key` 管的是**另一档**：`dev_config.API_KEY`（`.env` 里那把主 key）。
    `model_config` 在兼容档那一档会看它（"主 key 只在 `.env` 里"的机器靠它跑起来），
    所以要想验真正的"什么都没配"，得连它一起清掉。
    """

    saved = {name: os.environ.get(name) for name in ALL_VARS}
    saved_key = dev_config.API_KEY
    for name in ALL_VARS:
        os.environ.pop(name, None)
    if env_file_key is not None:
        dev_config.API_KEY = env_file_key
    for name, value in values.items():
        if value is not None:
            os.environ[name] = value
    try:
        yield
    finally:
        dev_config.API_KEY = saved_key
        for name in ALL_VARS:
            os.environ.pop(name, None)
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def _all_fake_keys() -> dict[str, str]:
    return dict(FAKE)


# --- ① 解析表逐字段对得上 -----------------------------------------------------


def test_every_purpose_resolves_to_the_table_we_agreed_on() -> None:
    """每个用途的 `(provider, base_url, model, key 变量名)` 逐字段对表。"""

    with _env(**_all_fake_keys()):
        for task, provider, base_url, model, key_env in EXPECTED:
            endpoint = model_config.resolve(task)
            assert isinstance(endpoint, TaskEndpoint)
            assert (endpoint.task, endpoint.provider, endpoint.base_url,
                    endpoint.model, endpoint.key_env) == (
                task, provider, base_url, model, key_env), f"{task} 那一行对不上表"
            assert endpoint.api_key == FAKE[key_env], f"{task} 拿到的不是自己那把 key"
            assert endpoint.own_key is True, f"{task} 该用自己那条 key"


def test_the_three_layers_are_layered() -> None:
    """**providers 里没有 key**；keys 每条都绑了 provider；tasks 指向存在的 key。"""

    for name, entry in model_config.PROVIDERS.items():
        assert set(entry) == {"base_url", "client_type"}, f"{name} 那一层混进了别的东西"
        assert "key" not in entry and "env" not in entry, "providers 里出现了凭据相关的字段"

    for name, entry in model_config.KEYS.items():
        assert set(entry) >= {"provider", "env"}, f"{name} 缺少 provider / env"
        assert entry["provider"] in model_config.PROVIDERS, f"{name} 绑了不认识的供应商"
        assert entry["env"].startswith("QQBOT_"), f"{name} 的 env 不是环境变量名"

    for task, fields in model_config.TASKS.items():
        assert fields["key"] in model_config.KEYS, f"{task} 指向了不存在的 key 条目"
        assert fields["model"], f"{task} 没有模型名"
        # 模型名要写**真名**：`deepseek-chat` 是随时会没的别名（实测现在只有
        # `deepseek-flash` 与 `deepseek-v4-pro`）。
        assert fields["model"] != "deepseek-chat", f"{task} 还在用别名"


def test_the_letter_purpose_uses_the_pro_model_through_the_judge_key() -> None:
    """用户原话（2026-10-12）："信用v4pro写"——写信那一路要 pro 那档。

    `deepseek-v4-pro` 只在 deepseek 那一家（与 `deepseek-flash` 不同档），所以这一路
    跟 reply 一个形状：借 `judge` 那条 key。它自己那条（`QQBOT_LETTER_API_KEY`）是
    mimo 的，闲置但**没废**——它是兜底链借走的那把（见 `FALLBACK_TASKS`）。
    """

    assert model_config.TASKS["letter"]["model"] == "deepseek-v4-pro"
    assert model_config.TASKS["letter"]["key"] == "judge", "模型在哪家就借哪家的 key"
    letter = model_config.KEYS["letter"]
    assert letter["provider"] == "mimo"
    assert letter["env"] == "QQBOT_LETTER_API_KEY"
    assert letter["fallback"] == "reply", \
        "回落规则要写清：先自己那条（mimo），再沿 `reply` 那条借"
    # ⚠️ 已知的**跨家**之处（2026-10-16 如实记下，没有顺手"修"）：`KEYS["reply"]`
    # 自 2026-10-14 起 provider = xkiro，而这条回落说的是 letter（mimo）→ reply。
    # 也就是说：letter 自己那条与主 key 都没配、而 `QQBOT_REPLY_API_KEY` 有值时，
    # 信会拿 xkiro 的 key 去打 xkiro 的地址（模型名仍是 `deepseek-v4-pro`）——
    # 这正是本模块要根治的"A 家 key 打 B 家模型"的形状。它今天走不到（生产 `.env`
    # 里 `QQBOT_LETTER_API_KEY` 与主 key 都在）。这是"`TASKS` 回退、`KEYS["reply"]`
    # 按用户口径留在 xkiro"两件事并存的代价，改回 Gemini 之前要先看它一眼。


def test_the_letter_key_entry_still_serves_the_mimo_fallback() -> None:
    """`QQBOT_LETTER_API_KEY` 那条条目**没废**：主家余额耗尽时信切回它。

    2026-10-12 之前这条测的是"写信用途自己回落 reply 那把 mimo key"——那条规则连同
    "信跑在 mimo 上"一起作废了（用途现在借 judge 那条）。它自己那条 mimo key 仍然
    生效，只是生效的地方换到了**兜底链**上：兜底那条 key（mimo / `QQBOT_FALLBACK_API_KEY`）
    自己没配，按回落链借到 letter 这把。
    """

    with _env(QQBOT_JUDGE_API_KEY="sk-fake-judge",
              QQBOT_LETTER_API_KEY="sk-fake-letter"):
        fallback = model_config.resolve_fallback("letter")
        assert fallback is not None, "letter 从 2026-10-12 起在兜底表里"
        assert fallback.triple() == (
            "https://api.xiaomimimo.com/v1", "sk-fake-letter", "mimo-v2.6-pro")
        assert fallback.key_env == "QQBOT_LETTER_API_KEY"
        assert fallback.own_key is False, "兜底那一路永远是借来的 key"


# --- ② 改一条 key 的 provider → 地址跟着变（成对取自同一条） ------------------


def test_changing_a_key_provider_moves_the_address_with_it() -> None:
    """**这条是结构保证的判据**：地址只能跟着"哪条 key"走。

    把 judge 那条 key 的 provider 从 deepseek 换成 mimo，**借它的那一路**的地址
    必须跟着换；换回去也要跟着回去。写死地址、或从全局读地址的实现会在这里红。

    驱动用的用途是 **`judge`**（借自己的那条）。2026-10-14～10-16 那段判定借 `reply`
    那条 key（xkiro）、`KEYS["judge"]` 与它无关，所以那时这条用例改用 `letter` 驱动；
    2026-10-16 用户「切换回ds」之后判定回到 deepseek、又借 `judge` 那条，驱动随之回到
    判定本人（这次回退**不是**为了让用例变绿，是因为它重新成为这条不变式的直接证据）。
    """

    with _env(QQBOT_JUDGE_API_KEY="sk-fake-judge"):
        before = model_config.resolve("judge")
        assert before.key_name == "judge", "judge 借的就是自己那条 key"
        assert before.base_url == "https://api.deepseek.com"

        original = model_config.KEYS["judge"]["provider"]
        try:
            model_config.KEYS["judge"]["provider"] = "mimo"
            after = model_config.resolve("judge")
            assert after.base_url == "https://api.xiaomimimo.com/v1", \
                "换了 key 的 provider，地址没跟着走——base 与 key 不成对了"
            assert after.provider == "mimo"
            assert after.api_key == "sk-fake-judge", "key 本身不该变"
        finally:
            model_config.KEYS["judge"]["provider"] = original

        assert model_config.resolve("judge").base_url == "https://api.deepseek.com"


def test_the_address_never_comes_from_a_second_place() -> None:
    """全局地址只对"没有自己那条 key"的用途有效——有 key 的用途钉在自己那家。

    这条正面打今晚那个形状：`QQBOT_API_BASE_URL` 指着 mimo、判定拿的是 deepseek 的
    key。判定的地址必须是 deepseek。

    2026-10-14～10-16 之间判定借 `reply` 那条 key（xkiro），那段时间"该钉在 deepseek
    上"的例子换成了 `letter`；2026-10-16 用户「切换回ds」把判定送回 deepseek 之后，
    例子回到 **`judge`**（它重新是这条不变式的直接证据，不是因为用例要变绿）。
    """

    with _env(QQBOT_API_BASE_URL="https://api.xiaomimimo.com/v1",
              QQBOT_JUDGE_API_KEY="sk-fake-judge",
              QQBOT_MEMORY_API_KEY="sk-fake-memory"):
        # 这个用例模拟的正是今晚那份 `.env` 的形状：全局地址指着 mimo，
        # 而判定 / 记忆各自拿 deepseek 的 key。
        judge = model_config.resolve("judge")
        memory = model_config.resolve("memory")
        assert judge.base_url == "https://api.deepseek.com"
        assert memory.base_url == "https://api.deepseek.com"
        assert judge.key_env == "QQBOT_JUDGE_API_KEY"
        assert memory.key_env == "QQBOT_MEMORY_API_KEY"
        # 全局地址只出现在"这一趟用的就是兼容档那一对"的时候
        for endpoint in (judge, memory):
            assert endpoint.key_env != model_config.COMPAT_KEY_ENV


def test_the_pairing_survives_a_panel_provider_switch() -> None:
    """面板"换供应商"不许把有自己 key 的用途搬走（热更路径上的同一条保证）。

    驱动用的是 **`judge`** 那一路（它从 2026-10-16 用户「切换回ds」起又用自己的
    deepseek key）。2026-10-14～10-16 那段它借 `reply` 那条 key（xkiro），
    那段时间这条用例由 `memory` 驱动——回退是因为判定重新属于 deepseek，
    不是因为用例要变绿。
    """

    import tempfile
    from pathlib import Path

    from qq_roleplay_bot.llm_client import OpenAICompatibleClient
    from qq_roleplay_bot.operator_config import OperatorConfig

    class _Engine:
        usage_store = None
        group_roles = None

    with tempfile.TemporaryDirectory() as tmp, _env(
            QQBOT_JUDGE_API_KEY="sk-fake-judge"):
        judge = OpenAICompatibleClient("https://old.example", "sk-fake-judge",
                                       "deepseek-flash", user_id="u", usage_role="judge")
        try:
            runtime.apply_overrides(
                _Engine(), {"provider": "mimo"},
                store=OperatorConfig(Path(tmp) / "operator_config.json"))
            assert judge.base_url == "https://api.deepseek.com", \
                "判定那把 key 属于 deepseek，换全局供应商不该把它搬走"
        finally:
            judge.base_url = "https://old.example"


# --- ③ 新配置整份缺失 → 与改动前逐字相同 -------------------------------------


def test_a_completely_unconfigured_machine_still_runs_the_old_way() -> None:
    """**一个变量都没有**（连 `.env` 那份也没有）时：地址取这一条的代码默认那家。

    那时没有任何凭据，所以自检里每个用途都报 `**缺**`；地址取这一条的代码默认那家
    （letter = mimo，其余 = deepseek——reply 自 2026-10-09 用户拍板换回 ds 起
    跟 judge 那条走，没配时连它报的 key 变量名也是 `QQBOT_JUDGE_API_KEY`）。
    """

    with _env(env_file_key=""):
        for task, provider, base_url, _model, _key_env in EXPECTED:
            endpoint = model_config.resolve(task)
            assert endpoint.api_key == "", task
            assert endpoint.key_present() is False, task
            assert endpoint.provider == provider, task
            assert endpoint.base_url == base_url, task
            assert endpoint.own_key is True, task
        blob = "\n".join(model_config.self_check_lines())
        assert blob.count("**缺**") == len(EXPECTED), "没有 key 的用途必须逐个报缺"


def test_a_main_key_that_only_lives_in_the_env_file_still_works() -> None:
    """主 key 只在 `.env` 里（环境里没有这一项）时，"只配一把 key"的部署照旧能跑。

    这是 `run/.env` 那种部署的形状：`.env` 里有 `QQBOT_API_KEY`，面板 / 环境里没有。
    地址必须与那把 key **同源**（兼容档那一对），不是"reply 那条自己那家"。
    """

    with _env():
        saved = dev_config.API_KEY
        dev_config.API_KEY = "sk-from-env-file"
        try:
            for task in model_config.TASK_NAMES:
                endpoint = model_config.resolve(task)
                assert endpoint.api_key == "sk-from-env-file", task
                assert endpoint.key_name == model_config.MAIN_KEY, task
        finally:
            dev_config.API_KEY = saved


def test_a_single_main_key_still_serves_every_purpose() -> None:
    """只配一把 key 的机器：每个用途都拿那一把，地址就是**那一对里的**全局地址。

    （改动前就是这个行为：`agent_api_key` 与 `reply_api_key` 都回落主 key，
    地址取全局地址。这是"不许弄坏现在能跑的"那一条。）
    """

    with _env(QQBOT_API_KEY="sk-only-key", QQBOT_API_BASE_URL="https://only.example/v1"):
        for task in model_config.TASK_NAMES:
            endpoint = model_config.resolve(task)
            assert endpoint.api_key == "sk-only-key", task
            assert endpoint.base_url == "https://only.example/v1", task
            assert endpoint.key_name == model_config.MAIN_KEY, task
            assert endpoint.own_key is False, task


def test_the_compat_layer_matches_the_old_accessors() -> None:
    """`dev_config` 那几个老读数与新的解析结果**说的是同一件事**。"""

    with _env(**{**_all_fake_keys(),
                 "QQBOT_API_MODEL": "global-model-fake"}, env_file_key=""):
        endpoint = model_config.resolve("reply")
        assert dev_config.reply_api_base_url() == endpoint.base_url
        assert dev_config.reply_api_model() == endpoint.model
        assert dev_config.reply_api_key() == endpoint.api_key
        # 老常量是** import 时求值一次**的（改动前就是这个语义），所以这里只钉
        # "它仍然在、而且是字符串"，不去要求它跟着测试里后来的改动变。
        assert isinstance(dev_config.API_BASE_URL, str) and dev_config.API_BASE_URL
        assert isinstance(dev_config.API_MODEL, str) and dev_config.API_MODEL
        # 判定"有自己的 key 就用自己那条"（模型名被全局覆盖了，key 不受影响）
        # 2026-10-16 用户「切换回ds」之后判定回到 deepseek、重新用 `judge` 那条 key；
        # 2026-10-14～10-16 那段它借 `reply` 那条（xkiro），那段时间这里报的是
        # `reply` / `sk-fake-reply`——期望值跟着 `TASKS` 回退，不是为了让用例变绿。
        judge = model_config.resolve("judge")
        assert judge.key_name == "judge"
        assert judge.api_key == "sk-fake-judge"


def test_the_compat_bundle_is_used_as_a_pair_when_the_main_key_exists() -> None:
    """兼容档那一对**成对**：主 key 有值时才连它的地址一起用。

    （主 key 只写在 `.env`、环境里没有这一项的机器，就是靠这条跑起来的。）
    """

    with _env(QQBOT_API_BASE_URL="https://global.example/v1"):
        saved = dev_config.API_KEY
        dev_config.API_KEY = "sk-from-env-file"
        try:
            for task in model_config.TASK_NAMES:
                endpoint = model_config.resolve(task)
                assert endpoint.api_key == "sk-from-env-file", task
                assert endpoint.key_name == model_config.MAIN_KEY, task
                assert endpoint.base_url == "https://global.example/v1", task
        finally:
            dev_config.API_KEY = saved


def test_declaring_which_provider_the_main_key_belongs_to_fixes_the_addresses() -> None:
    """**`QQBOT_API_PROVIDER` 是这次事故的正面解**：写清"主 key 是哪家的"。

    配了它，兼容档那一对就成了一对**已知属于谁**的（地址与供应商都按它算）；
    没配时自检会显示"兼容档·未声明，按本用途自带那家算"——那是"我还没说清"的标志。
    """

    with _env(env_file_key="", QQBOT_API_KEY="sk-main-deepseek",
              QQBOT_API_PROVIDER="deepseek",
              QQBOT_API_BASE_URL="https://api.deepseek.com"):
        for task in model_config.TASK_NAMES:
            endpoint = model_config.resolve(task)
            assert endpoint.provider == "deepseek", task
            assert endpoint.base_url == "https://api.deepseek.com", task
            assert endpoint.api_key == "sk-main-deepseek", task
        blob = "\n".join(model_config.self_check_lines())
        assert "未声明" not in blob


def test_an_undeclared_compat_bundle_is_flagged_in_the_self_check() -> None:
    """没声明就走"按本用途自带那家算"，而自检那一行**必须说出来**。"""

    with _env(QQBOT_API_KEY="sk-main-fake"):
        blob = "\n".join(model_config.self_check_lines())
        assert "兼容档·未声明" in blob, "走兼容档却没声明哪家时，自检必须写明"


def test_an_unknown_declared_provider_is_rejected() -> None:
    """声明了一个不认识的供应商 → 明确报错，不"就近找相似的"。"""

    with _env(QQBOT_API_KEY="sk-main-fake", QQBOT_API_PROVIDER="nope"):
        try:
            model_config.resolve("judge")
        except model_config.UnknownProvider:
            pass
        else:
            raise AssertionError("声明了不认识的供应商却照样解析出来了")


def test_the_old_task_level_variables_still_win() -> None:
    """改动前那套 `QQBOT_<用途>_MODEL` 仍然是那一档（名字没变，语义没变）。"""

    with _env(**{**_all_fake_keys(),
                 "QQBOT_JUDGE_MODEL": "judge-model-fake",
                 "QQBOT_MEMORY_MODEL": "memory-model-fake",
                 "QQBOT_REVIEW_MODEL": "review-model-fake",
                 "QQBOT_VISION_MODEL": "vision-model-fake",
                 "QQBOT_MAIL_MODEL": "letter-model-fake"}):
        assert model_config.resolve("judge").model == "judge-model-fake"
        assert model_config.resolve("memory").model == "memory-model-fake"
        assert model_config.resolve("review").model == "review-model-fake"
        assert model_config.resolve("vision").model == "vision-model-fake"
        assert model_config.resolve("letter").model == "letter-model-fake"


# --- ④ key 缺失 / 变量为空 → 明确报出来 --------------------------------------


def test_an_empty_variable_counts_as_missing() -> None:
    """空串 = 没配（环境里留一个空值会把 `.env` 的填充挡在门外，这是踩过的坑）。"""

    with _env(env_file_key="", QQBOT_JUDGE_API_KEY=""):
        endpoint = model_config.resolve("judge")
        assert endpoint.api_key == ""
        assert endpoint.key_present() is False
        assert "**缺**" in endpoint.safe_summary()


def test_require_names_the_missing_variable_instead_of_using_another_key() -> None:
    """缺 key 时**明确报出来**，而不是"静默拿一把别的 key 去打"。

    驱动用的是 **`judge`**：2026-10-16 用户「切换回ds」之后它又用自己的那条 deepseek
    key（`QQBOT_JUDGE_API_KEY`），而这段环境里那条没配 —— 正好是"这一路一条 key
    都没有"的形状。2026-10-14～10-16 那段判定借 `reply` 那条 key（xkiro），配了
    reply 那把它就有 key 了，所以那时这条用例改用 `letter` 驱动。
    """

    with _env(env_file_key="", QQBOT_REPLY_API_KEY="sk-fake-reply"):
        # reply 那把（xkiro）在，但它与 judge 无关：判定要的是 `QQBOT_JUDGE_API_KEY`
        try:
            model_config.require("judge")
        except MissingCredential as exc:
            assert exc.task == "judge"
            # 报的是"这个用途该配的那一项"，并说清它是怎么回落的
            assert exc.env == "QQBOT_JUDGE_API_KEY", exc.env
            assert "回落" in str(exc), str(exc)
        else:
            raise AssertionError("judge 没有 key 时 `require` 却给了个客户端")


def test_the_runtime_refuses_to_build_a_client_without_a_key() -> None:
    """装配点也必须拒绝：没有 key 就不许造出"拿着空 key 去请求"的客户端。"""

    with _env(env_file_key=""):
        for factory in (lambda: runtime._dialogue_client_factory("group:1"),
                        lambda: runtime._judge_client_factory("group:1"),
                        lambda: runtime._build_letter_client()):
            try:
                factory()
            except MissingCredential:
                pass
            else:
                raise AssertionError("没有 key 时不该造出客户端")


def test_an_unknown_purpose_is_rejected_loudly() -> None:
    with _env():
        try:
            model_config.resolve("nope")
        except UnknownTask:
            pass
        else:
            raise AssertionError("不认识的用途必须明确报错，不许就近找相似的")


# --- ⑤ 启动自检那一行不含 key 值 ---------------------------------------------


def test_the_self_check_prints_names_not_values() -> None:
    """自检那一行：provider 名 / 模型名 / key 变量名（有值没有）——**绝不打值**。

    reply 那一行的期望值跟着 `EXPECTED` 表走：2026-10-09 用户拍板换回 ds，
    它打印的是 `provider=deepseek` / `deepseek-flash` / `QQBOT_JUDGE_API_KEY`。
    """

    with _env(**_all_fake_keys()):
        lines = model_config.self_check_lines()
        assert len(lines) == len(EXPECTED)
        blob = "\n".join(lines)
        for task, provider, _base, model, key_env in EXPECTED:
            assert f"{task}: provider={provider}" in blob
            assert f"model={model}" in blob
            assert key_env in blob
            assert "有值" in blob
        # **key 的值一个字符都不许出现**
        for value in FAKE.values():
            assert value not in blob, f"自检那一行把 key 的值打出来了：{value}"
        assert "**缺**" not in blob


def test_the_self_check_marks_the_missing_purposes() -> None:
    """缺 key 的用途要在自检里带 `**缺**`（今晚那种事一眼看得出来）。

    2026-10-16 用户「切换回ds」之后 reply 与 judge **都借 judge 那条 key**（deepseek），
    所以"只配 judge 那一把"的机器上**六个用途全都有值**（要看 `**缺**` 得把 judge 也
    清掉——两半都钉在下面）。2026-10-14～10-16 那段 reply 走自己那条（xkiro），
    那时这一段钉的是"只配 judge 那把 ⇒ reply 与 judge 都报缺"。
    """

    with _env(env_file_key=""):
        blob = "\n".join(model_config.self_check_lines())
        assert "**缺**" in blob
        for task in ("reply", "letter", "judge"):
            assert f"{task}: " in blob

    with _env(env_file_key="", QQBOT_JUDGE_API_KEY="sk-fake-judge"):
        blob = "\n".join(model_config.self_check_lines())
        # judge 有自己的 key；reply / letter 都借 judge 那条，memory / review / vision
        # 回落到它（同家）——所以这一把 key 就够六个用途跑起来，一个 `**缺**` 都不该有。
        assert "judge: provider=deepseek" in blob
        assert "**缺**" not in blob, "reply 与 letter 借的都是 judge 那条，不该报缺"
        assert "letter: " in blob
        assert "reply: provider=deepseek" in blob, "reply 回到借 judge 那条（deepseek）"


def test_the_runtime_self_check_hook_returns_those_lines() -> None:
    """`runtime` 里那个启动自检钩子：把同一批行打出来（返回它，便于断言）。"""

    with _env(**_all_fake_keys()):
        lines = runtime._log_model_config_self_check()
        assert lines == model_config.self_check_lines()
        blob = "\n".join(lines)
        for value in FAKE.values():
            assert value not in blob


def test_the_self_check_never_leaks_a_real_key_from_the_environment() -> None:
    """拿一个**不像假值**的 key 塞进去，确认它不会出现在任何一行里。"""

    secret = "sk-" + "Z" * 40
    with _env(QQBOT_JUDGE_API_KEY=secret):
        blob = "\n".join(model_config.self_check_lines())
        assert secret not in blob
        assert "QQBOT_JUDGE_API_KEY" in blob
