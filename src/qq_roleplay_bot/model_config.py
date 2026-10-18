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

这张表说的是 **key 条目**之间的回落；"哪个**用途**用哪条 key"由 `TASKS` 定。
`reply` 与 `letter` 这两路现在都借 `judge` 那条（它们要的模型都在 deepseek 上）。
`reply` 自己那条（`QQBOT_REPLY_API_KEY`）2026-10-14 被改成 xkiro 那把、2026-10-16
用户拍板回 ds（原话："切换回ds。gemini好蠢。"，同日早先那句是"都改ds"）之后
**闲置但保留**（将来一键切回 Gemini 要用，见 `KEYS`）；`letter` 自己那条是 mimo 的。
两把在正常路径上都闲置，只在"主家余额耗尽切兜底"那条链上被借走（见 `FALLBACK_TASKS`）。

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
from urllib.parse import urlsplit

from . import model_providers_store as _providers_store

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
    # 2026-10-14（用户给的地址 + key）：一个 OpenAI 兼容中转，里面有 Gemini Flash。
    # 用户原话："这里面有gemini flash模型，给云茹接上" ⇒ 当时挂在**回复**那一路。
    # 实测 `google/gemini-2.5-flash` 首字 1.77s、总 1.77s（同一句群聊 prompt，走本仓库
    # 自己的 client）；`google/gemini-3.8-flash` 首字超时。
    # ⚠️ 2026-10-16（用户原话："切换回ds。gemini好蠢。"，同日早先那句是"都改ds"）：
    # `TASKS` 里已经没有用途指向**这家**了——原因（整段 Google 上游 503、且这家发不出
    # `reasoning_effort`）写在 `TASKS` 的 reply / judge 两条上。这条 `PROVIDERS` 条目
    # 与 `KEYS["reply"]` 一起**保留**：将来中继恢复时，"一键切回"要用的就是这份连接信息。
    "xkiro": {
        "base_url": "https://api.xkiro.com/v1",
        "client_type": "openai",
    },
}

#: 用途 → 哪条 key + 哪个模型名。
#:
#: 模型名写**真名**，不写随时会没的别名：实测 deepseek 侧现在只有
#: `deepseek-flash` 与 `deepseek-v4-pro`（`deepseek-chat` 是别名，已全部换掉）。
#: reply 的钥匙从 2026-10-09 起用 judge 那条（deepseek），2026-10-14 一度改成
#: 自己那条（xkiro 的 Gemini Flash），2026-10-16 用户拍板"切换回ds"之后**又回到**
#: 借 judge 那条（见这一行自己的注释）。地址跟着钥匙走（judge → deepseek），
#: `QQBOT_REPLY_API_KEY`（现在是 xkiro 那把）因此再次闲置、原值保留在 `.env` 里。
#: 要换回 Gemini：把这一行改回 {"key": "reply", "model": "google/gemini-2.5-flash"}。
#: letter 是同一个形状（2026-10-12 用户拍板"信用v4pro写"）：`deepseek-v4-pro` 只挂在
#: deepseek 那一家，所以借 `judge` 那条 key。它自己那条（`QQBOT_LETTER_API_KEY`）是
#: mimo 的，因此闲置——但**没废**：主家余额耗尽时信会切回它（见 `FALLBACK_TASKS`）。
#: 要换回去：把 letter 那一行改回 {"key": "letter", "model": "mimo-v2.6-pro"}。
TASKS: dict[str, dict[str, str]] = {
    # 2026-10-16（用户原话："切换回ds。gemini好蠢。"，同日早先那句是"都改ds"）：
    # 回复那一路从 xkiro 的 Gemini Flash **改回** deepseek——借 `judge` 那条 key
    # （deepseek / `deepseek-flash`），与 2026-10-09 那一版逐字相同。为什么不是
    # "用 .env 覆盖"：`QQBOT_REPLY_*` 只覆盖地址与模型名，**不改 provider**；而
    # `reasoning_effort` 只在 provider ∈ {"", "deepseek"} 时才发（`llm_client._complete_sync`
    # 的 `deepseek_fields`），provider 是 xkiro 时那一项根本发不出去 ⇒ 只能吃 DeepSeek
    # 自己的默认档。依据（2026-10-16 实测）：中继的 Google 上游整段 503（`/v1/models`
    # 通、`chat/completions` 全 503，25 分钟探针全挂）；`reasoning_effort=none` 把
    # "第一个 content"从 1.1~14.5s 压到 0.51~1.13s、过闸（第一条完整响应行）
    # 0.15~0.31s（`provider=xkiro` 时这档发不出去，正文最慢实测 13.9s）。
    # 什么时候改回去：中继的 Google 恢复了就改回
    # {"key": "reply", "model": "google/gemini-2.5-flash"}——`KEYS["reply"]` 那条
    # xkiro 条目与 `.env` 里那把 key 都留着，其余不动。
    "reply": {"key": "judge", "model": "deepseek-flash"},
    "letter": {"key": "judge", "model": "deepseek-v4-pro"},
    # 2026-10-16（同一条口径："切换回ds。gemini好蠢。"）：判定**也回到 deepseek**，
    # 用它自己那条 key（`KEYS["judge"]`，deepseek）与 `deepseek-flash`——即 2026-10-14
    # 之前那一版。依据与 reply 同一条，不是"顺带改的"：①中继的 Google 上游整段 503
    # （实测 25 分钟探针全挂）；②provider=xkiro 时 `reasoning_effort` 发不出去
    # （闸在 `llm_client`），只能吃默认档（实测"第一个 content" 1.1~14.5s、正文最慢
    # 13.9s），最慢那几次直接撞"首字 3 秒"的闸；回到 ds 后 `none` 档把它压到
    # 0.51~1.13s。⚠️ 仍然别把 `KEYS["judge"]` 的 provider 改成 xkiro：letter 借它、
    # memory/review/vision 回落到它，一改就会把 `deepseek-v4-pro` / `deepseek-flash`
    # 这些模型名打到 xkiro 上。什么时候改回去：中继 Google 恢复后改
    # {"key": "reply", "model": "google/gemini-2.5-flash"}。
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

#: **兜底那条** key 条目的名字（主家余额耗尽时整条切过去的那一家）。
#:
#: 它跟上面六条不一样：**不绑定任何用途**，而是"主家整条挂了"时所有用途共用的
#: 那一份来源。哪个用途能在它上面跑由 `FALLBACK_TASKS` 决定；用哪家地址由它
#: 自己那条的 `provider` 决定（`resolve_fallback` 只取那一个地址）。
#: 操作者可以用 `QQBOT_FALLBACK_API_KEY` 配一把专用兜底 key；没配就沿
#: `fallback` 链借**同家**那条——按本机 `.env` 的实际配置，落到的是那把实测
#: 还活着的 mimo 写信 key（`letter`）。
FALLBACK_KEY = "fallback"

#: key 条目 → **它属于哪家** + 从哪个环境变量取值 + 不配时回落哪一条。
#:
#: 回落的含义是"**整份来源**一起换"（值 + 地址），见模块说明。所以它只会指向
#: 同家的另一条，或者指向兼容档那一对（`main`）。
KEYS: dict[str, dict[str, str]] = {
    # 2026-10-14：回复那一路从"借 judge 那把 deepseek key"改回**自己这条**
    # （`QQBOT_REPLY_API_KEY`），因为它现在打的是 xkiro（Gemini Flash，见 `PROVIDERS`）。
    # 借别人 key 的那套（10-09 的形状）在这里不成立：地址换了、key 必须跟着换，
    # 否则就是把 deepseek 的 key 递给 xkiro（实测：只填 `QQBOT_REPLY_API_BASE_URL`
    # 时解析出来的 key 仍是 judge 那把——`QQBOT_REPLY_API_KEY` 被 `TASKS["reply"]["key"]`
    # 架空，`docs/CONFIG_GUIDE.md` 那句"填三个键就能换"因此只对地址与模型成立）。
    # ⚠️ 2026-10-16（用户："切换回ds。gemini好蠢。"）：这一段历史**整条保留**，但这条
    # 条目现在又**闲置**了——`TASKS["reply"]` 改回借 `judge` 那条，所以它在正常路径上
    # 一分钱不算。**没删的理由**：将来中继的 Google 恢复、要"一键切回 Gemini"时，
    # 要连同 `TASKS["reply"]["key"]` 一起指回这里（只改 `TASKS` 不改这里的 provider
    # 就对了：地址与 key 必须同源，这是 10-14 实测过的坑）。
    "reply": {"provider": "xkiro", "env": "QQBOT_REPLY_API_KEY", "fallback": MAIN_KEY},
    "letter": {"provider": "mimo", "env": "QQBOT_LETTER_API_KEY", "fallback": "reply"},
    "judge": {"provider": "deepseek", "env": "QQBOT_JUDGE_API_KEY", "fallback": MAIN_KEY},
    "memory": {"provider": "deepseek", "env": "QQBOT_MEMORY_API_KEY", "fallback": "judge"},
    "review": {"provider": "deepseek", "env": "QQBOT_REVIEW_API_KEY", "fallback": "judge"},
    "vision": {"provider": "deepseek", "env": "QQBOT_VISION_API_KEY", "fallback": "judge"},
    # 兜底那条（见 `FALLBACK_KEY`）：`TASKS` 里没有谁指向它，它只在
    # "主家余额耗尽"这一条保命路径上被 `resolve_fallback` 用到。
    FALLBACK_KEY: {"provider": "mimo", "env": "QQBOT_FALLBACK_API_KEY", "fallback": "letter"},
}

#: 主家余额耗尽、整条切到兜底那一家时：**哪些用途接得住、各用什么模型名**。
#:
#: "这一家接不住这个用途" = 那个用途**不在表里**——那时如实失败，不硬凑。
#:
#: 有实测证据的模型名只有一个：`mimo-v2.6-pro`（`letter` 从建起来一直用它，到
#: 2026-10-12 为止 28 次调用；2026-10-12 又把真实 judge 形状在它上面重放过，
#: 5/5 出 `<route>`+`<topic>`）。
#: `letter` 是 2026-10-12 新进这张表的：它的**主家**换成了 deepseek（见 `TASKS`），
#: 于是"主家余额耗尽 → 切回 mimo"这条路对它成立了（切过去用的是它自己那把 mimo
#: key，走 `FALLBACK_KEY` 的回落链）。
#: 不在表里的只剩 `vision`：要传图片，mimo 那一路**没实测过**收不收图片，按
#: "没证据就别写进生产表"的纪律先不写。主家挂着的时候这一路如实失败。
# TODO(mimo-vision): 实测过 mimo 的图片入参之后再决定要不要加。
# TODO(mimo-flash): `mimo-v2.6-flash` 在本仓库还没有实测记录，先不写进生产表。
FALLBACK_TASKS: dict[str, str] = {
    "reply": "mimo-v2.6-pro",
    "letter": "mimo-v2.6-pro",
    "judge": "mimo-v2.6-pro",
    "memory": "mimo-v2.6-pro",
    "review": "mimo-v2.6-pro",
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


# --- 文件层：`data/model_providers.json`（2026-10-18 核心契约）-----------------
#
# 这一段的形状是**一层覆盖**，不是第二套配置：文件里**写了的**用途按文件来
# （供应商 / 地址 / key / 模型 / effort）；**没写的**逐字回落到今天那套
# （`PROVIDERS` / `KEYS` / `TASKS` + `.env` + `data/operator_config.json`）。
# 所以"没有这个文件"是这套代码里一个**必须保持可测试**的状态——它等价于今天
# （契约 §五 / §5.2 的三重证法）。
#
# 三条写死的纪律（改之前先读，别让它们漂）：
#
# 1. **文件只有一处读口**：`_providers_file()`。`resolve` / `_key_source` /
#    `_base_url_for` / `_resolve_model` 全从它（或它的 `_file_plan()`）取——那正是
#    2026-10-06 那次 401 的根因：同一个值有第二处来源，就能拼出混搭。
#    `tests/test_model_providers_file.py` 按源码形状钉住这一条。
# 2. **文件里的用途一定成对取**：地址与 key 来自**同一个** `providers.<名字>` 条目
#    （契约 D13）。所以文件这一档要么整份给出（地址 + key），要么拒绝这一路。
# 3. **没写进文件的用途看不见文件里的 provider 覆盖**：文件的 `providers.deepseek`
#    改了地址，也不会搬走环境中走 deepseek 的判定（那一路没写进文件）。否则"只配了
#    回复那一路"会悄悄改到另外五路的地址——§5.1 的"没写的用途一字不改"就破了。
#    ⇒ 环境那一条链路只认内置 `PROVIDERS`（`_builtin_base_url`）。


@dataclass(frozen=True)
class _FilePlan:
    """文件给某个用途**算出来的一整份来源**（地址 / key / 模型一起，不许拆开取）。"""

    task: str
    provider: str
    base_url: str
    model: str
    api_key: str
    key_name: str
    key_env: str
    own_key: bool
    reasoning_effort: str = ""
    #: 这一份是从哪一档来的（自检行里打出来，人一眼能看出"文件到底生效没有"）。
    origin: str = ""
    #: 这把 key 来自**回落链的第几层**（`KEY_LAYER_*`；空串 = 这条不在那几层里）。
    #:
    #: 为什么要一个字段而不是从 `key_env` 的字符串里猜：`_FilePlan` 的另一半消费者是
    #: `status()` 与面板那一页的小字（"这一路现在实际用的是哪一层"），靠字符串前缀猜
    #: 会在下一个人改措辞时**静默**变成"这一路没有来源"。
    key_layer: str = ""
    #: 给人看的那句话（"agents.reply 指定的那把（k1）" / ".env 主 key" …）。
    key_layer_label: str = ""
    #: 这一路绑到的那把 key 的 id（走文件那两层的才有；空串 = 没绑）。
    key_id: str = ""


@dataclass(frozen=True)
class _FileView:
    """某一刻文件的**只读视图**：剥掉 key 的连接信息 + 原始 agents 条目。"""

    snapshot: object
    #: 供应商名 → `{base_url, client_type}`（**剥掉 key**，与 `PROVIDERS` 同形状）。
    specs: dict[str, dict[str, str]]
    #: 供应商名 → **这家声明的模型名**（面板第三级下拉框就是它）。
    models: dict[str, tuple[str, ...]]
    #: 供应商名 → **key 列表**（`ProviderKey`：id / label / 明文值）。
    #:
    #: 明文值**只在本模块内用来解析**，不许进日志/自检（`status()` 只报 id）。
    keys: dict[str, tuple[object, ...]]
    #: 用途名 → 原始条目（未校验；`_file_plan` 负责校验并给原因）。
    agents: dict[str, dict[str, str]]
    issues: tuple[str, ...] = ()


_VIEW: "_FileView | None" = None
#: 与 `_VIEW` 配套的缓存键 `(路径, mtime_ns, size, last_error, version_ok)`。
_VIEW_KEY: object = None


def _build_file_view(snapshot: object) -> _FileView:
    providers = getattr(snapshot, "providers", {}) or {}
    specs: dict[str, dict[str, str]] = {}
    for name, entry in providers.items():
        spec = {"base_url": str(entry.get("base_url", "") or "")}
        client_type = str(entry.get("client_type", "") or "")
        spec["client_type"] = client_type or "openai"
        specs[name] = spec
    return _FileView(snapshot=snapshot, specs=specs,
                     models=dict(getattr(snapshot, "models", {}) or {}),
                     keys=dict(getattr(snapshot, "keys", {}) or {}),
                     agents=dict(getattr(snapshot, "agents", {}) or {}),
                     issues=tuple(getattr(snapshot, "issues", ()) or ()))


def _providers_file() -> _FileView:
    """**唯一**读口（见上面纪律 1）。按 `(路径, mtime_ns, size)` 缓存一份视图。"""

    global _VIEW, _VIEW_KEY
    snapshot = _providers_store.load()
    key = (str(snapshot.path), snapshot.token, snapshot.last_error, snapshot.version_ok)
    view = _VIEW
    if view is not None and _VIEW_KEY == key:
        return view
    view = _build_file_view(snapshot)
    _VIEW = view
    _VIEW_KEY = key
    return view


def _host_of(url: str) -> str:
    """只取 `scheme://host[:port]`：自检里要能看出"名字与地址对不对得上"。

    为什么不用整条 URL：地址的 path / query 里可能塞着 token（中转站很常见），
    自检行会进日志。
    """

    try:
        parts = urlsplit(str(url or ""))
    except ValueError:  # pragma: no cover - urlsplit 很少抛，抛了也不该拖垮自检
        return ""
    return parts.netloc or ""


# --- 一路 key 的**四层回落链**（2026-10-18 用户拍定，写死在这里）------------------
#
# 用户原话：「agent那边先选供应商再选对应的key最后选供应商提供的模型」，以及"key 是
# 一个供应商下的列表"。于是"这一路实际用的是哪把 key"有**固定的四层**，顺序不许改、
# 也不许加第五层：
#
#   ① `agents.<用途>.key`       —— 文件里**指名**的那把（`agents.<用途>` 这一条写全了）
#   ② 该供应商的 `第一把 key`   —— `agents.<用途>.provider` 有、但没写 `key`
#   ③ `.env` 里**这一路**的 key  —— 供应商都没指定（这一路没写进文件）
#   ④ `.env` 的**主 key**       —— 今天那套的兜底
#
# ③ 里面还留着今天那套自己的两小步（`_env_key_source` 的"同家另一条"），**没有**把它
# 拆成第五、第六层：那不是这一轮定的事，改了就是顺手改今天的行为（T1 那条"没有文件时
# 逐字节相同"会当场红）。所以 ③ 报出来的名字可能是"借来的那一条"（例如 letter 借
# judge），`key_env` / 小字标签里看得见。
#
# 每一层都要能回答"这一路现在实际用的是哪一层"（面板「各路绑定」每行的小字就是它）——
# 所以这些 `KEY_LAYER_*` 常量是**契约**，别只当字符串用。
KEY_LAYER_OWN = "own"                 # agents.<用途> 指名的那把 key
KEY_LAYER_FIRST = "first"             # 该供应商的第一把 key
KEY_LAYER_ENV_TASK = "env_task"       # .env 的{用途} key
KEY_LAYER_ENV_MAIN = "env_main"       # .env 主 key
#: 这一路**没写进文件**（整条走今天那套）——不是上面那几层，是"文件没管它"。
KEY_LAYER_ENV = "env"
#: 这一路**写错了**（`agents.<用途>.key` 指了一个不存在的 id）。既不是某一层，也不是
#: "没写"：要原样把原因报出来，让人去改那个 id——绝不能悄悄换成"第一把"（换一把 =
#: 拿另一个账号的 key 去打，2026-10-06 那次 401 的同一类形状）。
KEY_LAYER_BROKEN = "broken"

#: 用途 → 面板/小字里的中文说法。`letter` 是**插件那一路**（mail 写信），
#: 它跟其余五路同一张表（用户 2026-10-18：插件读的是"核心里配好的"那份）。
TASK_LABELS: dict[str, str] = {
    "reply": "对话",
    "judge": "判定",
    "memory": "记忆",
    "review": "审核",
    "vision": "识图",
    "letter": "写信",
}

#: 顺带给面板那一页的"格子里有哪些行"：`main` 之外就是这六路（顺序即面板顺序）。
KEY_GRID_TASKS: tuple[str, ...] = ("reply", "judge", "memory", "review", "vision", "letter")


def _layer_label(task: str, layer: str, key_id: str = "") -> str:
    """一层的**人话**（面板小字 / `status()` / 拒绝原因都用它，只此一份）。"""

    name = TASK_LABELS.get(task, task)
    if layer == KEY_LAYER_OWN:
        return f"{name}绑定的那一把 key" + (f"（{key_id}）" if key_id else "")
    if layer == KEY_LAYER_FIRST:
        return "该供应商的第一把 key" + (f"（{key_id}）" if key_id else "")
    if layer == KEY_LAYER_ENV_TASK:
        return f".env 的{name} key"
    if layer == KEY_LAYER_ENV_MAIN:
        return ".env 主 key"
    if layer == KEY_LAYER_ENV:
        return ".env 那套（这一路没写进文件）"
    return ""


@dataclass(frozen=True)
class _KeyPick:
    """一次"这一路该用哪把 key"的结果：**哪一层 + 值 + id + 来源名 + （不能用时）为什么**。"""

    layer: str
    label: str
    value: str
    #: 与 `_FilePlan.key_name` / `key_env` 同一个口径（`文件:providers.X.k1` / 变量名）。
    name: str
    env: str
    own: bool
    key_id: str = ""
    reason: str = ""


def _provider_key(view: _FileView, provider: str, key_id: str) -> object | None:
    """这一家里 id 为 `key_id` 的那把 key；没有就 `None`。"""

    for item in view.keys.get(provider, ()) or ():
        if str(getattr(item, "id", "") or "") == key_id:
            return item
    return None


def _provider_key_ids(view: _FileView, provider: str) -> list[str]:
    return [str(getattr(item, "id", "") or "") for item in view.keys.get(provider, ()) or ()]


def _file_key_pick(provider: str, task: str, entry: dict[str, str],
                   view: _FileView) -> _KeyPick:
    """按**四层回落链**算这一路（`agents.<用途>` 这一条 × 那一套）的 key。

    ①② 是纯文件（地址与 key 出自同一条 `providers.<名字>`，天然"成对"）；③④ 是借
    `.env`，所以**必须**过"同家"那道闸（D1 ②：不许让文件改了地址却借 `.env` 里别家的
    key——那正是 2026-10-06 那次三系统 401 的形状）。闸不过不是"没有 key"，而是"这一层
    用不了"，`reason` 里说清是哪一层、属于谁。

    这个函数是**唯一**算这几层的地方：`_plan_from_entry`（解析）与 `_task_layer()`
    （`status()` / 面板小字）都调它。两份实现必然漂移，而漂移的结果是"面板说在用 A、
    实际打的是 B"。
    """

    task = str(task or "").strip().casefold()
    wanted = str(entry.get("key", "") or "").strip()
    if wanted:
        found = _provider_key(view, provider, wanted)
        if found is None:
            return _KeyPick(KEY_LAYER_BROKEN, "", "", "", "", False, reason=(
                f"agents.{task}.key = {wanted}，但 providers.{provider} 里没有这个 id"
                f"（它现在有：{'、'.join(_provider_key_ids(view, provider)) or '一把都没有'}）"))
        return _KeyPick(KEY_LAYER_OWN, _layer_label(task, KEY_LAYER_OWN, wanted),
                        str(getattr(found, "value", "") or ""),
                        f"文件:providers.{provider}.{wanted}",
                        f"文件:providers.{provider}.{wanted}", True, key_id=wanted)
    first = _provider_key(view, provider, _provider_key_ids(view, provider)[0]) \
        if _provider_key_ids(view, provider) else None
    if first is not None:
        first_id = str(getattr(first, "id", "") or "")
        return _KeyPick(KEY_LAYER_FIRST, _layer_label(task, KEY_LAYER_FIRST, first_id),
                        str(getattr(first, "value", "") or ""),
                        f"文件:providers.{provider}.{first_id}",
                        f"文件:providers.{provider}.{first_id}", True, key_id=first_id)
    source = _env_key_source(task)
    if not source.value:
        return _KeyPick("", "", "", "", "", False, reason=(
            f"agents.{task} 只写了供应商 {provider}，它没有 key，"
            "环境里也没有这一路可用的 key"))
    if source.is_main:
        declared, _stated = _main_provider(task)
        if declared != provider:
            return _KeyPick("", "", "", "", "", False, reason=(
                f"环境里的主 key 属于 {declared}，与 agents.{task} 写的 {provider} "
                "不是同一家（地址与 key 必须成对）"))
        return _KeyPick(KEY_LAYER_ENV_MAIN, _layer_label(task, KEY_LAYER_ENV_MAIN),
                        source.value, source.name, source.env, False)
    if source.provider != provider:
        return _KeyPick("", "", "", "", "", False, reason=(
            f"环境里那条 key 属于 {source.provider}，与 agents.{task} 写的 {provider} "
            "不是同一家（地址与 key 必须成对）"))
    return _KeyPick(KEY_LAYER_ENV_TASK, _layer_label(task, KEY_LAYER_ENV_TASK),
                    source.value, source.name, source.env, False)


def _file_plan(task: str) -> _FilePlan | None:
    """这个用途在文件里有没有一份**可用**的计划；没有（没写 / 写坏了）返回 `None`。

    写坏了不是"整份作废"：只废这一路，原因由 `_file_rejection()` 报给 `status()`。
    """

    view = _providers_file()
    entry = view.agents.get(task)
    if entry is None:
        return None
    plan, _why = _plan_from_entry(task, entry, view)
    return plan


def _file_rejection(task: str) -> str:
    """这个用途被文件拒绝的原因；没写或可用时是空串（`status()` 与自检行用）。"""

    view = _providers_file()
    entry = view.agents.get(task)
    if entry is None:
        return ""
    plan, why = _plan_from_entry(task, entry, view)
    return "" if plan is not None else why


def _plan_from_entry(task: str, entry: dict[str, str],
                     view: _FileView) -> tuple[_FilePlan | None, str]:
    """把一个 `agents.<用途>` 条目算成一份来源，或者给出**为什么不能用**。

    顺序就是契约 D1（"宽松带闸"）叠上 2026-10-18 那四层回落链：
    ① `agents.<用途>.key` 指名的那把 → 用它（地址与 key 出自同一条，成对）；
    ② 没写 `key` → 该供应商的**第一把**（同样是文件里的，成对）；
    ③ 这一家没有 key → 只有地址与内置那条**完全相同**时，才允许借 `.env`（挡死
       "文件改了地址却借 .env 的 key"——那是 2026-10-06 的形状）；借的时候按
       `.env 这一路` → `.env 主 key` 两层次序，且必须同家；
    ④ 这一家**不在**文件里（只写了"用哪家"）→ 走内置表 + 环境变量（= 今天）。
    """

    provider = str(entry.get("provider", "") or "").strip().casefold()
    model = str(entry.get("model", "") or "").strip()
    effort = str(entry.get("reasoning_effort", "") or "").strip()
    if not provider:
        return None, "缺 provider"
    if not model:
        return None, "缺 model（文件里写了的用途必须自己给模型名）"
    if provider in view.specs:
        base_url = view.specs[provider]["base_url"].rstrip("/")
        pick = _file_key_pick(provider, task, entry, view)
        if pick.layer in (KEY_LAYER_OWN, KEY_LAYER_FIRST):
            # ①②：key 与地址出自**同一条** `providers.<名字>` ⇒ 成对，直接用。
            return _FilePlan(task=task, provider=provider, base_url=base_url, model=model,
                             api_key=pick.value, key_name=pick.name, key_env=pick.env,
                             own_key=True, reasoning_effort=effort, origin="文件",
                             key_layer=pick.layer, key_layer_label=pick.label,
                             key_id=pick.key_id), ""
        if pick.layer == KEY_LAYER_BROKEN:
            # 这一路**写错了**（指名了一把不存在的 key）。先报这个原因：说成"没有 key、
            # 地址又不是内置那一条"会把人引到地址上去，而实际要改的是那个 id。
            return None, pick.reason
        # ③：要借 `.env` 了。地址不是内置那一条 ⇒ 不许借（否则就是"文件改地址、
        # 借 .env 的 key"那种混搭）。这条闸排在家门闸之前，与改动前逐字一致。
        builtin = PROVIDERS.get(provider)
        if builtin is None or builtin["base_url"].rstrip("/") != base_url:
            return None, (f"providers.{provider} 还没有 key，而地址又不是内置那一条"
                          "（不许让文件改了地址却借 .env 里的 key）")
        if not pick.value:
            return None, pick.reason
        return _FilePlan(task=task, provider=provider, base_url=base_url, model=model,
                         api_key=pick.value, key_name=pick.name, key_env=pick.env,
                         own_key=False, reasoning_effort=effort,
                         origin="文件·借环境 key",
                         key_layer=pick.layer, key_layer_label=pick.label), ""
    if provider in PROVIDERS:
        source = _env_key_source(task)
        base_url = _env_base_url_for(task, source)
        actual = source.provider or _default_provider(task)
        # 这一档 key 全来自 `.env`（文件只写了"用哪家"）：层号按 `.env` 那两层报。
        layer = KEY_LAYER_ENV_MAIN if source.is_main else KEY_LAYER_ENV_TASK
        return _FilePlan(task=task, provider=actual, base_url=base_url, model=model,
                         api_key=source.value, key_name=source.name, key_env=source.env,
                         own_key=source.own, reasoning_effort=effort,
                         origin="文件·家回落",
                         key_layer=layer, key_layer_label=_layer_label(task, layer)), ""
    return None, f"不认识的供应商 {provider}（文件 providers 与内置表里都没有）"


def providers_file_token() -> tuple[int, int] | None:
    """文件的 `(mtime_ns, size)`；没有文件时是 `None`（`provider_failover` 用它判"变没变"）。"""

    return _providers_file().snapshot.token  # type: ignore[union-attr]


def file_provider_specs() -> dict[str, dict[str, str]]:
    """文件里那几家供应商的连接信息（**剥掉 key**；`provider_registry` 合并视图用）。"""

    return {name: dict(spec) for name, spec in _providers_file().specs.items()}


def file_has_agent(task: str) -> bool:
    """这个用途**真的**由文件管着（文件里写了、而且那一份能用）。"""

    return _file_plan(str(task or "").strip().casefold()) is not None


def file_has_key(task: str) -> bool:
    """这个用途的 key **来自文件**（`agents.<用途>` 指名的那把，或该供应商的第一把）。

    为什么把"借 `.env`"排除在外（这一格的两个调用方都不许它算进来）：
    `dev_config.review_enabled()` 用这条判"review 配了没有"，它刻意不算"借 judge 那把"
    ——算进来会让"只配了一把主 key"的机器**突然自动开风格审核**（每条回复多一次调用），
    正是那条用例明确否掉的形状。`runtime._build_client_overrides()` 用它是给面板回
    "这一项（按路的）配了没有"。所以：**文件里这一路真的有 key** 才算。
    """

    plan = _file_plan(str(task or "").strip().casefold())
    return plan is not None and plan.key_layer in (KEY_LAYER_OWN, KEY_LAYER_FIRST)


def file_agent_entry(task: str) -> dict[str, str]:
    """文件里那个用途的原始条目（没写返回空 dict）。写侧拿它兜"沿用现有的模型名/key"。"""

    return dict(_providers_file().agents.get(str(task or "").strip().casefold()) or {})


def file_provider_has_key(name: str) -> bool:
    """文件里这一家**有没有 key**（写侧判"改地址时地址与 key 成不成对"用；不返回值）。"""

    return bool(_providers_file().keys.get(str(name or "").strip().casefold()))


def file_provider_models(name: str) -> tuple[str, ...]:
    """文件里这一家**声明的模型名**（面板第三级下拉框用；没声明返回空元组）。"""

    return tuple(_providers_file().models.get(str(name or "").strip().casefold(), ()))


def file_provider_key_ids(name: str) -> tuple[str, ...]:
    """文件里这一家的 key **id 列表**（顺序即"第一把"的顺序；不含值）。"""

    return tuple(str(getattr(item, "id", "") or "")
                 for item in _providers_file().keys.get(
                     str(name or "").strip().casefold(), ()) or ())


def task_key_layer(task: str) -> dict[str, object]:
    """**这一路**现在实际用的是哪一层 key（`status()` 与面板每行的小字都用它）。

    也是"面板说的与实际打的是不是同一把"这条判据的唯一来源——所以它**不另算一遍**，
    而是调解析用的那个 `_file_key_pick()`。

    **绝不返回 key 的值**：这里只回答"哪一层、有没有值、来源名、为什么用不了"。
    面板要显示"填过没有"由 runtime 那侧的掩码/长度负责（它从 store 读，不从这拿）。
    """

    task_name = str(task or "").strip().casefold()
    view = _providers_file()
    entry = view.agents.get(task_name)
    if entry is None:
        return {"task": task_name, "label": TASK_LABELS.get(task_name, task_name),
                "layer": KEY_LAYER_ENV, "layer_label": _layer_label(task_name, KEY_LAYER_ENV),
                "present": bool(_key_source(task_name).value), "key_id": "",
                "source": "", "reason": ""}
    pick = _file_key_pick(str(entry.get("provider", "") or "").strip().casefold(),
                          task_name, entry, view)
    return {
        "task": task_name,
        "label": TASK_LABELS.get(task_name, task_name),
        "layer": pick.layer or "",
        "layer_label": pick.label,
        "present": bool(pick.value),
        "key_id": pick.key_id,
        "source": pick.env,
        "reason": pick.reason,
    }


def providers_file_path() -> str:
    """文件的路径（自检 / 回执里用；读侧的唯一出口仍是 `_providers_file()`）。"""

    return str(_providers_file().snapshot.path)  # type: ignore[union-attr]


def file_reasoning_effort(task: str) -> str:
    """文件给这个用途的 `reasoning_effort`；没写返回空串（由 `dev_config` 接着往环境看）。"""

    plan = _file_plan(str(task or "").strip().casefold())
    return plan.reasoning_effort if plan is not None else ""


def providers_file_lines() -> list[str]:
    """文件状态那 1~3 行（**不并进 `self_check_lines()`**：那一份的条数被用例钉死）。

    照 `fallback_self_check_lines()` 的先例单开一个函数：文件状态是"另一件事"
    （哪几路被文件接管、哪几路被文件拒了），混进"每个用途一行"的表会把那条不变式弄红。
    """

    view = _providers_file()
    snapshot = view.snapshot
    if not snapshot.present:  # type: ignore[union-attr]
        return [f"模型供应商文件: {snapshot.path} 不存在（= 今天那套：.env + operator_config）"]  # type: ignore[union-attr]
    if snapshot.last_error:  # type: ignore[union-attr]
        return [f"模型供应商文件: {snapshot.path} 整份忽略（{snapshot.last_error}）：按今天那套跑"]  # type: ignore[union-attr]
    effective = [task for task in TASK_NAMES if _file_plan(task) is not None]
    lines = [f"模型供应商文件: {snapshot.path} 生效 {len(effective)}/{len(TASK_NAMES)} 用途"  # type: ignore[union-attr]
             f"（{'、'.join(effective) or '无'}）；providers={len(view.specs)}"]
    if view.specs:
        # host 只到 `scheme://host`（§6.3：名字与地址对不对得上，一眼看得出来）。
        lines.append("模型供应商文件·地址: " + "、".join(
            f"{name}@{_host_of(spec['base_url']) or '?'}"
            for name, spec in sorted(view.specs.items())))
    if view.keys:
        # 只报 **key 的 id 与"谁是第一把"**，绝不打值（多 key 之后这一行就是"哪一家有
        # 几把 key、第一把是谁"的现场证据；打值等于一次泄露好几把）。
        lines.append("模型供应商文件·key: " + "、".join(
            f"{name}[{'/'.join(str(getattr(item, 'id', '') or '') for item in items)}]"
            for name, items in sorted(view.keys.items())))
    rejected = {task: why for task in TASK_NAMES if (why := _file_rejection(task))}
    if rejected:
        lines.append("模型供应商文件·被拒: " + "；".join(
            f"{task}（{why}）" for task, why in rejected.items()))
    if view.issues:
        lines.append("模型供应商文件·坏条目: " + "；".join(view.issues))
    return lines


def status() -> dict[str, object]:
    """文件 + 解析结果的读数（自检 / 面板 / 测试用；**不含任何 key 值**）。

    `rejected` 是"文件里写了但这一路用不了"的用途 → 原因。为什么不在
    `model_providers_store.status()` 里算它：store **不认识 `TASK_NAMES`**（契约 §3.4，
    两份用途名清单必然漂移），所以"哪几路生效/被拒"只能在这一层算。

    `key_layers` 是"这一路的 key 现在实际来自哪一层"（见 `KEY_LAYER_*`）；没写进文件的
    那几路报 `env`（"文件没管它"），不是那几层里的任何一层。
    `key_ids` 报的是**key 的 id**（哪一家有哪几把），**不是值**。
    """

    view = _providers_file()
    snapshot = view.snapshot
    effective = [task for task in TASK_NAMES if _file_plan(task) is not None]
    rejected = {task: why for task in TASK_NAMES if (why := _file_rejection(task))}
    return {
        "path": str(snapshot.path),  # type: ignore[union-attr]
        "present": bool(snapshot.present),  # type: ignore[union-attr]
        "mtime": round(float(snapshot.mtime), 3),  # type: ignore[union-attr]
        "token": list(snapshot.token) if snapshot.token else None,  # type: ignore[union-attr]
        "last_error": str(snapshot.last_error),  # type: ignore[union-attr]
        "version_ok": bool(snapshot.version_ok),  # type: ignore[union-attr]
        "providers": sorted(view.specs),
        "agents": sorted(view.agents),
        "effective": effective,
        "rejected": rejected,
        "unknown_agents": sorted(set(view.agents) - set(TASK_NAMES)),
        #: 用途 → `{layer, label, from_file, present, key_id, source}`（"key 来自哪一层"）。
        "key_layers": {task: _task_layer(task) for task in TASK_NAMES},
        #: 供应商 → **key 的 id 列表**（顺序即"第一把"的顺序；不是值）。
        "key_ids": {name: [str(getattr(item, "id", "") or "") for item in items]
                    for name, items in sorted(view.keys.items())},
        #: 供应商 → 它声明的模型名。
        "provider_models": {name: list(names)
                            for name, names in sorted(view.models.items())},
        "issues": list(getattr(snapshot, "issues", ()) or ()),
        "notes": list(getattr(snapshot, "duplicate_urls", ()) or ()),
    }


def _task_layer(task: str) -> dict[str, object]:
    """`status()["key_layers"]` 的一行：这一路**实际**用的那把 key 来自哪一层。"""

    layer = task_key_layer(task)
    plan = _file_plan(str(task or "").strip().casefold())
    return {"layer": layer["layer"], "label": layer["layer_label"],
            "from_file": plan is not None, "present": bool(layer["present"]),
            "key_id": str(layer.get("key_id") or ""), "source": str(layer.get("source") or "")}


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
    #: 这一趟的值是从哪来的：`"文件"` = `data/model_providers.json`；空串 = 今天那套。
    #:
    #: 为什么要这个字段而不是"看 `key_env` 里有没有'文件'字样"：把语义藏在字符串里，
    #: 下一个人改一个措辞就断了（契约 §3.2）。
    source: str = ""

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
        line = (f"{self.task}: provider={self.provider} model={self.model} "
                f"key={self.key_env}{source} {mark}")
        if self.source:
            # 文件那一路额外说清"来源 + host"：§6.3 要一眼看出"名字与地址对不对得上"
            # （host 只到 `scheme://host`，不带 path/query——防止地址里塞的 token 进日志）。
            line += f" 来源={self.source} host={_host_of(self.base_url) or '?'}"
        return line


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


def _builtin_base_url(provider: str) -> str:
    """**内置**供应商的地址（环境那一条链路只看它，看不见文件里的覆盖）。

    为什么环境链路必须看不见文件（契约 §5.1 的"没写的用途一字不改"）：文件里
    `providers.deepseek` 改成中继地址，只该影响**写进文件的那几路**；判定没写进文件，
    它仍然打 deepseek 官方地址。否则"只配了回复那一路"会悄悄搬走另外五路——
    这正是 10-06 那次的形状（地址与 key 来自两个地方）。文件里的新家（如 `relay1`）
    由 `_file_plan()` 自己认，不靠这里。
    """

    spec = PROVIDERS.get(str(provider or "").strip().casefold())
    if spec is None:
        raise UnknownProvider(f"不认识的供应商：{provider}")
    return spec["base_url"]


def _provider_spec(name: str) -> dict[str, str]:
    """供应商的连接信息（**剥掉 key**）：文件里有就用文件的，否则内置表。

    `PROVIDERS` 的字面量一个字都不改（`tests/test_model_config.py::test_the_three_layers_
    are_layered` 钉着 `set(entry) == {"base_url", "client_type"}`）；文件条目带 `key`，
    并进那张表就会把凭据混进"只放连接信息"的那一层。
    """

    key = str(name or "").strip().casefold()
    spec = _providers_file().specs.get(key)
    if spec is not None:
        return spec
    builtin = PROVIDERS.get(key)
    if builtin is None:
        raise UnknownProvider(f"不认识的供应商：{name}")
    return builtin


def provider_base_url(provider: str) -> str:
    """供应商的地址（文件优先，内置兜底）。不认识的供应商**明确报错**，不"就近找相似的"。"""

    return _provider_spec(provider)["base_url"]


def builtin_provider_base_url(provider: str) -> str:
    """**内置表**那一份地址（写侧判"文件里这一家的地址是不是就是内置那条"用）。"""

    return _builtin_base_url(provider)


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
        return _known_compat_provider(declared, COMPAT_PROVIDER_ENV), True
    return _default_provider(task), False


def _known_compat_provider(provider: str, where: str) -> str:
    """**声明"主 key 属于哪一家"**时认不认识这个名字：内置表 ∪ 文件里的家。

    为什么这一处允许认文件（而 `_known_provider` 只认内置）：`QQBOT_API_PROVIDER` 是
    操作者**显式写下的声明**，它不会因为文件里恰好有个同名条目就被悄悄改写；而
    `KEYS` / `fallback` 那几条链路是**代码里的表**，让它们认文件会让不写进文件的用途
    也被搬走（§5.1 不允许）。契约 §3.5 明确要求这一处走合并视图。
    """

    key = str(provider or "").strip().casefold()
    if key in _providers_file().specs or key in PROVIDERS:
        return key
    raise UnknownProvider(f"{where} 绑了一个不认识的供应商：{provider}")


def _key_source(task: str) -> _KeySource:
    """这条用途的 key 来源：**文件第一**（写进文件的用途押过一切），否则今天那三档。

    文件那一档的判据是 `_file_plan()`（它已经算好"地址与 key 出自同一条"）；这一层
    只把那份计划翻译成 `_KeySource`，不再自己读环境——两处取值的形状正是要消灭的东西。
    """

    plan = _file_plan(task)
    if plan is not None:
        return _KeySource(name=plan.key_name, env=plan.key_env, value=plan.api_key,
                          provider=plan.provider, own=plan.own_key)
    return _env_key_source(task)


def _env_key_source(task: str) -> _KeySource:
    """**今天那套**的 key 来源（环境变量 / `.env` / 兼容档）。三档，顺序写死在这里：

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
    """**内置**表里认不认识这一家（`KEYS` / 兼容档 / 兜底这三条链路只认内置表）。

    契约 §3.2 原来要求它改走 `_provider_spec()`（= 也认文件里的新家）。**没这么做**，
    因为它只被"今天那套"的链路调用（`KEYS` 的字面量 + `QQBOT_API_PROVIDER`），那些
    地方永远只会写内置名字；让它认文件等于让文件里的同名条目**改写没写进文件的用途**
    （文件 `providers.deepseek` 改了地址 → 没写进文件的判定也被搬走），那是 §5.1 明确
    不许的。文件里的新家由 `_file_plan()` 直接认（它看 `providers` ∪ `PROVIDERS`）。
    """

    key = str(provider or "").strip().casefold()
    if key not in PROVIDERS:
        raise UnknownProvider(f"{where} 绑了一个不认识的供应商：{provider}")
    return key


def _fallback_source() -> _KeySource | None:
    """兜底那条 key 的来源：自己那条有值就用它，没有就沿 `fallback` 链借同家那条。

    与其它所有来源一样（见模块说明）：**值与地址从同一条解析出来**，
    `resolve_fallback` 只取 `provider_base_url(source.provider)`。

    链子走回兼容档那一对时（那时 `source.provider` 是空串）**当作没有**：兼容档
    就是主家那一对，正是要躲开的那一趟——把兜底 key 往 `QQBOT_API_BASE_URL` 上
    打，就是 2026-10-06 那个三系统 401 的形状。
    """

    entry = _own_entry(FALLBACK_KEY)
    if entry.value:
        return entry
    source = _follow(FALLBACK_KEY)
    if source is None or not source.provider:
        return None
    return source


def _base_url_for(task: str, source: _KeySource) -> str:
    """地址：文件里写了的用途**只**取文件那条 provider 的地址；否则今天那套。

    文件这一档就是"成对取自同一条"的落点：地址与 key 出自**同一个**
    `providers.<名字>`（契约 D13）。没有它，地址就会退回"全局"那个来源——那正是
    2026-10-06 那次三系统 401 的形状。
    """

    plan = _file_plan(task)
    if plan is not None:
        return plan.base_url
    return _env_base_url_for(task, source)


def _env_base_url_for(task: str, source: _KeySource) -> str:
    """**今天那套**的地址：任务级覆盖 → **这份来源自己的地址**（内置表那一份）。

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
        if provider in PROVIDERS:
            # 内置的家：地址取**内置**那一份（文件里同名条目改了地址也不搬走它——
            # 那条只对"写进文件的那几路"生效，§5.1）。
            default_url = _builtin_base_url(provider)
        else:
            # 声明的是一家**只在文件里**的家（`QQBOT_API_PROVIDER` 指向它）：它的地址
            # 只能来自文件（契约 §3.5：这一处走合并视图）。
            default_url = str(_providers_file().specs.get(provider, {}).get("base_url", ""))
        return (_read(COMPAT_BASE_URL_ENV) or default_url).rstrip("/")
    return _builtin_base_url(source.provider)


def _default_provider(task: str) -> str:
    """某个用途**代码默认**绑的那家（兼容档没配地址时用它兜底）。"""

    return _known_provider(str(KEYS[TASKS[task]["key"]]["provider"]), TASKS[task]["key"])


def _resolve_model(task: str) -> str:
    """模型名：文件里写了的用途**只**取 `agents.<用途>.model`；否则今天那套。"""

    plan = _file_plan(task)
    if plan is not None:
        return plan.model
    return _env_resolve_model(task)


def _env_resolve_model(task: str) -> str:
    """**今天那套**的模型名：任务级覆盖（含迁移期老名字）→ 兼容档 → `tasks` 里的真名。"""

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
    plan = _file_plan(key)
    source = _key_source(key)
    if plan is not None:
        # 文件里写了的用途：provider / 地址 / key / 模型全部出自那一份计划（成对）。
        provider = plan.provider
    elif source.is_main and not source.own:
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
        source="文件" if plan is not None else "",
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


def resolve_fallback(task: str) -> TaskEndpoint | None:
    """把某个用途解析成**兜底那一家**上的一个客户端；那一家接不住就返回 `None`。

    只用在本模块之外那条保命路径上（主家余额耗尽 → 整条切走，见
    `provider_failover`）。四道闸，顺序写死在这里：

    ① 这个用途**不在 `FALLBACK_TASKS`** → `None`（那一家接不住它）；
    ② 这个用途的主家来源是**兼容档那一对**（`_key_source(task).is_main`）→ `None`：
       那时操作者只配了一把 key，没有"另一家"可言；而且兼容档的地址是
       `QQBOT_API_BASE_URL`，兜底 key 打它正是那次 401 的形状；
    ③ 兜底那条 key（含它的同家回落链）**没有值** → `None`；
    ④ 有值 → 地址**只**取 `_builtin_base_url(source.provider)`。

    ④ 那一行是这个函数存在的理由：地址**绝不**再读一次 `BASE_URL_ENV[task]`。
    任务级地址覆盖属于**主家那一路**（`_base_url_for` 的第一档），在这里读它
    就等于"兜底 key 打主家地址"。模型名同理只取 `FALLBACK_TASKS` 里的字面量，
    不读 `MODEL_ENV`（那是主家那个模型名）。缺 key **不抛**，返回 `None` ——
    调用方要的就是"接不住就别切"，不是启动期报错。
    """

    key = str(task or "").strip().casefold()
    if key not in TASKS:
        raise UnknownTask(f"不认识的用途：{task}")
    model = str(FALLBACK_TASKS.get(key, "") or "")
    if not model:
        return None
    if _key_source(key).is_main:
        return None
    source = _fallback_source()
    if source is None:
        return None
    return TaskEndpoint(
        task=key,
        provider=source.provider,
        declared_provider=source.provider,
        base_url=_builtin_base_url(source.provider),
        api_key=source.value,
        model=model,
        key_name=source.name,
        key_env=source.env,
        # 兜底永远是"借来的那一条"：`own_key=False` 让自检那一行写明回落，
        # 也提醒后来的人"这不是这个用途自己那条 key"。
        own_key=False,
    )


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


def fallback_self_check_lines() -> list[str]:
    """兜底那一路的自检行：供应商 / 模型名 / 变量名 + 有值没有，**绝不含 key 值**。

    为什么不并进 `self_check_lines()`：那一份是"**每个用途一行**"，条数被用例
    `test_the_self_check_prints_names_not_values` 钉死
    （`len(lines) == len(EXPECTED)`）。兜底是"另一家"的额外一路，混进去会让那条
    不变式不再成立，所以单独一份、由调用方自己决定打在哪。
    """

    source = _fallback_source()
    provider = str(KEYS[FALLBACK_KEY]["provider"])
    env = str(KEYS[FALLBACK_KEY]["env"])
    mark = "**缺**"
    origin = ""
    if source is not None:
        mark = "有值"
        env = source.env
        if source.name != FALLBACK_KEY:
            origin = f"（回落到 {source.name}）"
    lines = [f"兜底: provider={provider} key={env}{origin} {mark}"]
    for task in TASK_NAMES:
        model = str(FALLBACK_TASKS.get(task, "") or "")
        if model:
            lines.append(f"兜底·{task}: provider={provider} model={model} {mark}")
        else:
            lines.append(f"兜底·{task}: 不在表里（主家挂了这一路如实失败）")
    return lines
