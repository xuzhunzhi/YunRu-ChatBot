"""模型 / 凭据 / 用途的**三层配置**：providers → keys → tasks。

## 为什么要重做这一层（2026-10-06 的现场事故）

`run/.env` 里曾经同时存在两份互相矛盾的东西：

    QQBOT_API_BASE_URL = https://api.xiaomimimo.com/v1   ← 全局地址指着 mimo
    QQBOT_API_KEY      = mimo 那把
    QQBOT_JUDGE_API_KEY / QQBOT_MEMORY_API_KEY / QQBOT_REVIEW_API_KEY = deepseek 那把

于是**地址与 key 来自两个不同的地方**：判定 / 记忆 / 审核拿的是 deepseek 的 key，
却按全局地址打到 mimo——一旦面板那层即时覆盖被重置，三个子系统一起 401。

根因不是"谁配错了"，是**形状**：只要"地址"和"key"还能各自单独被覆盖，
这种错就写得出来。这个模块把形状改掉：

    providers  每家供应商**只放连接信息**（base_url / client_type），**不放 key**；
    keys       **按用途分开**，每条**显式绑定它属于哪家**（`provider`）+ 从哪个
               环境变量取值（`env`）；
    tasks      用途 → 用哪条 key + 哪个模型名。

一个客户端的三个字段**永远由同一条 key 条目解析出来**：

    base_url = 该 key 那条的来源（任务级覆盖 → 兼容档 → **该 key 绑定的 provider 的地址**）
    api_key  = 那条 key 的 env 里的值
    model    = 该任务的模型名

地址只能跟着"哪条 key"走，没有第二个来源，所以"用 A 家的 key 打 B 家地址"
**写不出来**——`resolve(task)` 里没有那条路径。

## 环境变量（密钥仍然只在 `.env` 里）

配置里**只写变量名**，绝不写值。取值仍然只从 `os.environ` 读
（`operator_config` 与 `load_env_file` 都往那里填），这个模块不另造一套读取。

地址与模型的优先级（从高到低）：

1. **任务级覆盖**（可选的新名字，一个都不配也行）：
   `QQBOT_<用途>_API_BASE_URL` / `QQBOT_<用途>_API_MODEL`
   —— 只给那一个用途换地址或模型名；
2. **兼容档**（改动前就在用的名字，现在降级成**默认值**）：
   `QQBOT_API_BASE_URL` / `QQBOT_API_MODEL`；
3. **代码默认**：`providers` 里的地址、`tasks` 里的模型名。

`QQBOT_REPLY_API_BASE_URL` / `QQBOT_REPLY_API_MODEL` 是既有的一套（2026-10-05 起），
它在这里的含义就是 **reply 那个用途的任务级覆盖**，语义一字未变。

## key 的回落（不配也能跑，但**成对**）

每个用途各有自己的 key 变量；不配时按下面的表回落：

| 用途 | 自己的变量 | 不配时回落 |
| --- | --- | --- |
| reply | `QQBOT_REPLY_API_KEY` | `QQBOT_API_KEY` |
| letter | `QQBOT_LETTER_API_KEY` | `QQBOT_REPLY_API_KEY` |
| judge / memory / review / vision | `QQBOT_JUDGE_API_KEY` / `QQBOT_MEMORY_API_KEY` / `QQBOT_REVIEW_API_KEY` / `QQBOT_VISION_API_KEY` | `QQBOT_JUDGE_API_KEY` |

回落到最后那一条（"主 key 那条"，`QQBOT_API_KEY`）时，**地址也一起用兼容档**
（`QQBOT_API_BASE_URL`）——因为在历史部署里那个 key 与那个地址本来就是**同一份配置**：
只配一把 key 的机器，改动前后的行为因此**逐字相同**（有测试钉住）。
这是一个**成对的来源**，不是"地址走全局、key 走别的"那种混搭。

为什么要留这条迁移路：把判定那把 key 的回落直接砍掉 = 单 key 部署一升级就"判定没了"。
代价是"只有一把 key、而它其实属于别家"的机器仍会被它带回老形状——所以
**启动自检那一行里会显式打印 `provider=main（兼容档）`**，一眼能看出来。

地址与 key **永远取自同一个来源**（provider 条目、任务级覆盖、或兼容档三者之一）；
这条由 `tests/test_model_config.py::test_the_base_url_is_never_read_from_a_second_place`
按源码形状钉住。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: 供应商 → **只有连接信息**。这里绝不出现任何 key。
#:
#: `deepseek` 的地址要不要带 `/v1`：DeepSeek 两种写法都能用（它自己的文档写
#: `https://api.deepseek.com`，也接受 `/v1`）。这里保持与改动前**逐字相同**，
#: 免得"形状重做"顺手改了线协议。
PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "client_type": "openai",
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "client_type": "openai",
    },
}

#: 用途 → 哪条 key + 哪个模型名。
#:
#: 模型名写**真名**，不写随时会没的别名：实测 deepseek 侧现在只有
#: `deepseek-flash` 与 `deepseek-v4-pro`（`deepseek-chat` 是别名，已全部换掉）。
TASKS: dict[str, dict[str, str]] = {
    "reply": {"key": "reply", "model": "mimo-v2.6-flash"},
    "letter": {"key": "letter", "model": "mimo-v2.6-pro"},
    "judge": {"key": "judge", "model": "deepseek-flash"},
    "memory": {"key": "memory", "model": "deepseek-flash"},
    "review": {"key": "review", "model": "deepseek-flash"},
    "vision": {"key": "vision", "model": "deepseek-flash"},
}

#: 用途的顺序（自检按它打印；测试也按它遍历）。
TASK_NAMES: tuple[str, ...] = ("reply", "letter", "judge", "memory", "review", "vision")

#: 兼容档的名字（改动前就在用；现在是**默认值**那一档）。
COMPAT_BASE_URL_ENV = "QQBOT_API_BASE_URL"
COMPAT_MODEL_ENV = "QQBOT_API_MODEL"
COMPAT_KEY_ENV = "QQBOT_API_KEY"

#: **那把主 key 属于哪一家**（可选的显式声明）。
#:
#: 为什么需要它：兼容档是一对（`QQBOT_API_KEY` + `QQBOT_API_BASE_URL`），而
#: 一个用途的"自带供应商"可能跟这一对**不是同一家**。例如 `reply` 自带 mimo，
#: 而主 key 其实是 deepseek 的——那时 `reply` 会拿 deepseek 的 key 去打 mimo。
#: 那正是 2026-10-06 那个 401 的形状。
#:
#: 配了它就等于说清"主 key 是哪家的"：那一对的地址与供应商都按它算（地址仍取
#: `QQBOT_API_BASE_URL`），**成对**这件事因此仍然成立。
#: 不配也能跑（只配一把 key 的部署照旧），但那时启动自检里会出现
#: `provider=main（兼容档·未声明）` —— 一眼能看出"这一趟没写清是哪家"。
COMPAT_PROVIDER_ENV = "QQBOT_API_PROVIDER"

#: "主 key 那条"的名字。它不是一个 `KEYS` 条目：它代表**兼容档那一对**
#: （`QQBOT_API_KEY` + `QQBOT_API_BASE_URL`），两者成对使用。
MAIN_KEY = "main"

#: key 条目 → **它属于哪家** + 从哪个环境变量取值 + 不配时回落哪一条。
#:
#: 回落的含义是"**整份来源**一起换"（值 + 地址），见模块说明。所以它只会指向
#: 同家的另一条，或者指向兼容档那一对（`main`）。
KEYS: dict[str, dict[str, str]] = {
    "reply": {"provider": "mimo", "env": "QQBOT_REPLY_API_KEY", "fallback": MAIN_KEY},
    "letter": {"provider": "mimo", "env": "QQBOT_LETTER_API_KEY", "fallback": "reply"},
    "judge": {"provider": "deepseek", "env": "QQBOT_JUDGE_API_KEY", "fallback": MAIN_KEY},
    "memory": {"provider": "deepseek", "env": "QQBOT_MEMORY_API_KEY", "fallback": "judge"},
    "review": {"provider": "deepseek", "env": "QQBOT_REVIEW_API_KEY", "fallback": "judge"},
    "vision": {"provider": "deepseek", "env": "QQBOT_VISION_API_KEY", "fallback": "judge"},
}

#: 每个用途可选的**任务级地址覆盖**变量名。
#:
#: 为什么任务级也允许覆盖地址：本机 `.env` 就有 `QQBOT_REPLY_API_BASE_URL`，
#: 而且"换地址"这件事本来必然是按用途做的（只换一路）。它**不破坏**成对取自同一条：
#: 覆盖的是"这条 key 打哪个地址"，来源仍然是**那一条 key**。
BASE_URL_ENV: dict[str, str] = {
    "reply": "QQBOT_REPLY_API_BASE_URL",
    "letter": "QQBOT_LETTER_API_BASE_URL",
    "judge": "QQBOT_JUDGE_API_BASE_URL",
    "memory": "QQBOT_MEMORY_API_BASE_URL",
    "review": "QQBOT_REVIEW_API_BASE_URL",
    "vision": "QQBOT_VISION_API_BASE_URL",
}

#: 每个用途可选的**任务级模型覆盖**变量名（与 `BASE_URL_ENV` 同一条纪律）。
MODEL_ENV: dict[str, str] = {
    "reply": "QQBOT_REPLY_API_MODEL",
    "letter": "QQBOT_LETTER_API_MODEL",
    "judge": "QQBOT_JUDGE_MODEL",
    "memory": "QQBOT_MEMORY_MODEL",
    "review": "QQBOT_REVIEW_MODEL",
    "vision": "QQBOT_VISION_MODEL",
}

#: **迁移期**的老变量名（只认不值钱的模型名；凭据类一个都不加）。
#:
#: `QQBOT_MAIL_MODEL` 是写信那一路改动前就在读的名字（写信用的是 mail 那套名字）。
#: 它现在排在 `QQBOT_LETTER_API_MODEL` 之后，属于"没有新名字时照旧生效"。
MODEL_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "letter": ("QQBOT_MAIL_MODEL",),
}


class UnknownTask(ValueError):
    """问了一个不在表里的用途名。别名不许猜——凭据类配置最怕"改了却没生效"。"""


class UnknownProvider(ValueError):
    """绑定了一个不在 `PROVIDERS` 里的供应商。"""


class MissingCredential(RuntimeError):
    """这个用途**没有任何可用的 key**。

    明确报出来，而不是"静默拿一把别的 key 去打"——那正是这次要根治的东西。
    """

    def __init__(self, task: str, env: str) -> None:
        super().__init__(f"用途 {task} 没有可用的 key（{env} 与同家回落项都没值）")
        self.task = task
        self.env = env


@dataclass(frozen=True)
class TaskEndpoint:
    """一个用途解析出来的**一个客户端的三件套**，外加它的来源（给自检用）。

    `base_url` / `api_key` / `model` 就是 `OpenAICompatibleClient` 的三个位置参数；
    `provider` / `key_name` / `key_env` 只用于打印与断言，**不含任何 key 值**。
    """

    task: str
    #: 这一趟**实际**用的供应商（`main（兼容档）` 表示走的是迁移路径）。
    provider: str
    #: 这条 key 条目**声称**绑定的供应商（`provider` 是它、或它的回落来源）。
    declared_provider: str
    base_url: str
    api_key: str
    model: str
    key_name: str
    key_env: str
    #: 有值的那条 key 是不是"自己那条"（False = 走了回落）。
    own_key: bool

    def key_present(self) -> bool:
        return bool(self.api_key)

    def triple(self) -> tuple[str, str, str]:
        """`(base_url, api_key, model)`——客户端就是按这三样构造的。"""
        return self.base_url, self.api_key, self.model

    def safe_summary(self) -> str:
        """一行给人看的摘要：**只有供应商 / 模型 / 变量名与有没有值**，绝不含 key 值。

        走兼容档那一对时会写明"兼容档"、以及那一家是**声明的**还是"按本用途自带那家算"。
        """

        mark = "有值" if self.key_present() else "**缺**"
        if self.key_name == MAIN_KEY:
            stated = _read(COMPAT_PROVIDER_ENV).casefold()
            origin = "兼容档" if stated else f"兼容档·未声明，按{self.provider}算"
            source = f"（{origin}）"
        else:
            source = "" if self.own_key else "（回落到这一条）"
        return (f"{self.task}: provider={self.provider} model={self.model} "
                f"key={self.key_env}{source} {mark}")


def _read(name: str) -> str:
    """读环境变量。空串 = 没配（踩过的坑：环境里留一个空串会把 `.env` 的填充挡在门外）。"""

    return os.environ.get(name, "").strip()


def _main_key_value() -> str:
    """兼容档那把主 key 的**值**：环境 → `.env` 带入的那份 → 老式 `API_KEY`。

    为什么要看 `.env` 那一份（而不是只读 `os.environ`）：主 key 只在 `.env` 里、
    **环境里没有这一项**的机器是存在的（测试会显式删掉环境变量来验"回落"，
    `agent_api_key` 的注释也记着这条）。那时"只配一把 key"的部署仍然要能跑，
    而且**地址与 key 都必须取兼容档那一对**——所以这里只回答"有没有值"，
    地址那一侧由 `_key_source` 一起决定（成对，不是各取各的）。
    """

    value = _read(COMPAT_KEY_ENV)
    if value:
        return value
    return _read("API_KEY") or _env_file_main_key()


def _env_file_main_key() -> str:
    """`.env` 里那把主 key（`dev_config` 在 import 时读进来的常量）。

    只在运行期去问它（`dev_config` import 本模块，反向没有模块级 import，
    所以不成环）；拿不到就当没有——`model_config` 不许变成"删掉 `dev_config`
    就起不来"那种拴缚。
    """

    try:
        from . import dev_config

        return str(getattr(dev_config, "API_KEY", "") or "").strip()
    except Exception:  # noqa: BLE001 - 拿不到就当没有
        return ""


def _main_bundle_configured() -> bool:
    """兼容档那一对**成不成对**——判据是**那把主 key 有没有值**。

    为什么不看地址：`QQBOT_API_BASE_URL` 常常先被写上（历史遗留 / 面板换过供应商），
    而"这一对能不能真的拿去用"只取决于**有没有那把 key**。地址只决定这一对打哪儿。
    成对的意思正是"有 key 才有这一对"——所以"有地址、没 key"**不算配好**，
    那一趟走的是这一条自己的代码默认那家。靠这条才避开了今晚那个形状：
    地址指着 mimo、而判定拿 deepseek 的 key 时，判定走的是自己那条。
    """

    return bool(_main_key_value())


def provider_base_url(provider: str) -> str:
    """供应商的地址。不认识的供应商**明确报错**，不"就近找相似的"。"""

    spec = PROVIDERS.get(str(provider or "").strip().casefold())
    if spec is None:
        raise UnknownProvider(f"不认识的供应商：{provider}")
    return spec["base_url"]


@dataclass(frozen=True)
class _KeySource:
    """一条 key 解析出来的**整份来源**：名字、变量名、值、以及它属于谁。"""

    name: str
    env: str
    value: str
    #: `KEYS` 里那一条声称的供应商；兼容档是空串（它不代表某一家）。
    provider: str
    #: 是不是"自己那条"（False = 走了回落）。
    own: bool

    @property
    def is_main(self) -> bool:
        return self.name == MAIN_KEY


def _main_provider(task: str) -> tuple[str, bool]:
    """兼容档那一对属于哪一家：`(provider, 是显式声明的吗)`。

    - `QQBOT_API_PROVIDER` 有值 → 用它（那是操作者写下的"主 key 是哪家的"）；
      不认识的供应商**明确报错**，不"就近找相似的"；
    - 没写 → 取这个用途**自带的**那家，并且如实报告"没声明"（自检里会写出来）。
      这一档是为了"只配一把 key、单家"的部署照旧能跑；多供应商的机器应当显式声明。
    """

    declared = _read(COMPAT_PROVIDER_ENV).casefold()
    if declared:
        return _known_provider(declared, COMPAT_PROVIDER_ENV), True
    return _default_provider(task), False


def _key_source(task: str) -> _KeySource:
    """这条用途的 key 来源。三档，顺序写死在这里：

    ① **自己那条**（`KEYS[用途的 key]["env"]`）有值就用它；
    ② 表里写明的**同家回落**（例如 `letter → reply` 那把 mimo key）有值就用它；
    ③ **兼容档那一对**（`QQBOT_API_KEY` + `QQBOT_API_BASE_URL`）配过就用它
       ——值与地址**成对**取，所以"只配一把 key"的历史部署照旧能跑；
    ④ 都没有 → 这一条**自己的代码默认那家**（`own_key=True`）。

    为什么 ③ 要排在 ② 之后、而且只在**② 没结果**时才生效：`letter → reply` 是
    用户写明的规则（"长文用 mimo pro，key 先看自己的、没有就借 reply 那把"），
    而"某一路借另一路的 key"这种同家回落只该在连主 key 都没有的机器上发生。
    （`review` 也写了 `fallback: judge`，但主 key 配过时它会走 ③ —— 那两种写法
    都合法，判据是"**值可能有、地址与它同源**"，见本文件头部那段。）

    一个客户端的地址永远是这一趟选中的那份来源的地址，没有第二种取法。
    """

    fields = TASKS[task]
    entry_name = str(fields["key"])
    direct = _own_entry(entry_name)
    if direct.value:
        return direct
    # ③ **同家另一条**：只在"兼容档那一对没配过"时才走它。
    #
    #    为什么要加这道闸：主 key 是历史部署的**默认来源**，只要它配了，
    #    "某一路借另一路的 key"就不该发生（`review` 表里也写了 `fallback: judge`，
    #    但主 key 在时它应该用主 key）。而 `letter → reply` 这条**用户写明的规则**
    #    不受影响：那时兼容档通常也没配（reply 那条 mimo key 是自己那条）。
    if not _main_bundle_configured():
        source = _follow_same_family(entry_name)
        if source is not None:
            return source
    # ② **兼容档那一对**（`QQBOT_API_KEY` + `QQBOT_API_BASE_URL`）：历史部署的默认
    #    来源，值与地址**成对**取。
    if _main_bundle_configured():
        return _KeySource(name=MAIN_KEY, env=COMPAT_KEY_ENV,
                          value=_main_key_value(), provider="", own=False)
    # ④ 都没有：这一趟用**这一条自己的代码默认那家**（还没配凭据的机器）。
    return direct


def _own_entry(name: str) -> _KeySource:
    """这一条自己的来源（不管它有没有值）。"""

    if name == MAIN_KEY:
        return _KeySource(name=MAIN_KEY, env=COMPAT_KEY_ENV, value=_main_key_value(),
                          provider="", own=True)
    entry = KEYS.get(name)
    if entry is None:
        raise UnknownProvider(f"不认识的 key 条目：{name}")
    return _KeySource(name=name, env=str(entry["env"]), value=_read(str(entry["env"])),
                      provider=_known_provider(str(entry["provider"]), name), own=True)


def _follow(name: str) -> _KeySource | None:
    """沿着 `fallback` 找第一条**有值**的来源（兼容档也算一条）；没有就返回 `None`。

    `fallback` 只指向同家的另一条、或兼容档那一对（`main`）。走回来的
    `own=False`，这样自检那一行会写明"回落到这一条"。
    """

    chain: list[str] = []
    current = name
    while True:
        entry = KEYS.get(current)
        if entry is None:
            break
        fallback = str(entry.get("fallback", ""))
        if not fallback or fallback == current or fallback in chain:
            break
        chain.append(fallback)
        if fallback == MAIN_KEY:
            # 兼容档那一对**成对**用：值取 `QQBOT_API_KEY`（含 `.env` 带入的那份），
            # 地址由 `_base_url_for` 取 `QQBOT_API_BASE_URL`。
            value = _main_key_value()
            if value:
                return _KeySource(name=MAIN_KEY, env=COMPAT_KEY_ENV, value=value,
                                  provider="", own=False)
            break
        source = _own_entry(fallback)
        if source.value:
            return _KeySource(name=source.name, env=source.env, value=source.value,
                              provider=source.provider, own=False)
        current = fallback
    return None


def _follow_same_family(name: str) -> _KeySource | None:
    """只沿**同家另一条**找回落（**不走兼容档**）。

    用它来区分两件事：「这一路该借同家哪一条」与「主 key 那一对」。
    两者的优先级由 `_key_source` 定死（同家另一条 → 兼容档 → 自己的代码默认）。
    """

    entry = KEYS.get(name)
    if entry is None:
        return None
    fallback = str(entry.get("fallback", ""))
    if not fallback or fallback == name or fallback == MAIN_KEY:
        return None
    source = _own_entry(fallback)
    if source.value:
        return _KeySource(name=source.name, env=source.env, value=source.value,
                          provider=source.provider, own=False)
    return _follow_same_family(fallback)


def _known_provider(provider: str, where: str) -> str:
    key = str(provider or "").strip().casefold()
    if key not in PROVIDERS:
        raise UnknownProvider(f"{where} 绑了一个不认识的供应商：{provider}")
    return key


def _base_url_for(task: str, source: _KeySource) -> str:
    """地址：任务级覆盖 → **这份来源自己的地址**。

    最后那一档就是成对保证的落点——没有它，地址就会退回"全局"那个来源，
    那正是这次事故的形状。兼容档那一对的地址就是 `QQBOT_API_BASE_URL`
    （它的**供应商**由 `QQBOT_API_PROVIDER` 声明，见 `_main_provider`）。
    """

    specific = _read(BASE_URL_ENV[task])
    if specific:
        return specific.rstrip("/")
    if source.is_main:
        # 兼容档那一对：走到这里就意味着**主 key 有值**（见 `_main_bundle_configured`），
        # 所以地址与 key 成对。地址取 `QQBOT_API_BASE_URL`；没配地址时取
        # 这一对所属那家的默认地址（`QQBOT_API_PROVIDER` 声明的那家，或本用途自带那家）。
        provider, _declared = _main_provider(task)
        return (_read(COMPAT_BASE_URL_ENV) or provider_base_url(provider)).rstrip("/")
    return provider_base_url(source.provider)


def _default_provider(task: str) -> str:
    """某个用途**代码默认**绑的那家（兼容档没配地址时用它兜底）。"""

    return _known_provider(str(KEYS[TASKS[task]["key"]]["provider"]), TASKS[task]["key"])


def _resolve_model(task: str) -> str:
    """模型名：任务级覆盖（含迁移期老名字）→ 兼容档 → `tasks` 里的真名。"""

    for name in (MODEL_ENV[task],) + MODEL_ENV_ALIASES.get(task, ()):
        specific = _read(name)
        if specific:
            return specific
    compat = _read(COMPAT_MODEL_ENV)
    if compat:
        return compat
    return TASKS[task]["model"]


def resolve(task: str) -> TaskEndpoint:
    """把某个用途解析成**一个客户端**（地址 / key / 模型名取自同一份来源）。

    用途名不合法 → `UnknownTask`；某条 key 绑了不认识的供应商 → `UnknownProvider`。
    **key 缺失不在这里抛**：`resolve` 是面板 / 报告也用的只读接口，缺 key 要能
    被打印出来（`safe_summary` 里那个"**缺**"）。要"缺 key 就必须炸"的调用方用
    `require()`。
    """

    key = str(task or "").strip().casefold()
    if key not in TASKS:
        raise UnknownTask(f"不认识的用途：{task}")
    source = _key_source(key)
    if source.is_main and not source.own:
        # 兼容档那一对：它是**哪一家**由声明（或这一条自带那家）决定。
        provider, _stated = _main_provider(key)
    else:
        provider = source.provider or _default_provider(key)
    return TaskEndpoint(
        task=key,
        provider=provider,
        declared_provider=provider,
        base_url=_base_url_for(key, source),
        api_key=source.value,
        model=_resolve_model(key),
        key_name=source.name,
        key_env=source.env,
        own_key=source.own,
    )


def require(task: str) -> TaskEndpoint:
    """与 `resolve` 相同，但**没有可用 key 时抛 `MissingCredential`**。

    装配一个真正的客户端时用这个：宁可在启动时就明确报出"这个用途没有 key"，
    也不要带着空 key 去请求、然后在日志里看见一排 401。
    """

    endpoint = resolve(task)
    if not endpoint.api_key:
        raise MissingCredential(endpoint.task, endpoint.key_env)
    return endpoint


def endpoints() -> dict[str, TaskEndpoint]:
    """全部用途的解析结果（自检 / 报告 / 测试用）。"""

    return {name: resolve(name) for name in TASK_NAMES}


def self_check_lines() -> list[str]:
    """启动自检：每个用途一行，**只有名字与"有值没有"，绝不打 key 的值**。

    今晚那种事（A 家 key 打 B 家地址）以后一眼就能看出来：地址的来源就是
    key 那条所绑的供应商，所以那一行里的 provider 名与 key 变量名**必须同家**；
    缺 key 的那一行带一个 `**缺**`；走兼容档的那一行写明是**哪一家**
    （`QQBOT_API_PROVIDER` 声明的那家，或"未声明，按这一条自带那家算"）。
    """

    return [endpoint.safe_summary() for endpoint in endpoints().values()]
