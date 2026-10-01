# YunRu-ChatBot

> 一个跑在 QQ 群里的**人格化聊天机器人**：不是问答工具，是一个会判断"此刻该不该接话"的群成员。

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-771%20passing-4CC38A)](#测试)
[![NapCat](https://img.shields.io/badge/interface-NapCat%20%2F%20OneBot%20v11-3482FF)](#接口说明只适配-napcat)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-6E6E6E)](#安装)
[![License](https://img.shields.io/badge/license-MIT-blue)](#许可)

---

## 这是什么

大部分 QQ 机器人是"收到命令 → 执行 → 回复"。这个不一样：

**它先判断该不该说话，再决定说什么。**

群里每分钟几十条消息，她大部分时候不说话。被 @ 了要答；没被叫到时，
只有当她确实有话说、接得上、或者这话让她不舒服，才会开口。
"我能帮上忙"不算理由——那是"有用"，不是"她有话说"。

围绕这个判断，项目做了四件事：

| 能力 | 一句话说明 |
| --- | --- |
| **对话** | 群聊注意力判定 + 语气/分寸约束。不是"每条都回"，也不是"高冷不理人" |
| **记忆** | 长期记忆按**人**对应：谁的事、在哪个群学的、什么时候学的，都分开 |
| **防护** | 提示词注入、越权、出戏、敏感请求的拦截；所有外部内容都是**不可信数据** |
| **底层** | OneBot v11 传输、命令插件、能力闸门、脱敏指标、状态持久化 |

### 她大概是什么样

> **群友**：今天食堂那个窗口又没了
> **她**：三楼那个？我上周去就只剩面了。

> **群友**：@她 帮我写个周报
> **她**：不写。你自己干了什么你最清楚。

> **群友**：你是不是 AI 啊
> **她**：你今天怎么突然问这个。

---

## 目录

- [特性](#特性)
- [接口说明（只适配 NapCat）](#接口说明只适配-napcat)
- [架构](#架构)
- [分支：main 与 stage4-plugins](#分支main-与-stage4-plugins)
- [安装](#安装)
- [配置](#配置)
- [运行](#运行)
- [人格怎么换](#人格怎么换)
- [测试](#测试)
- [项目结构](#项目结构)
- [设计文档](#设计文档)
- [许可](#许可)

---

## 特性

### 对话

- **注意力判定是独立的一次调用。** "要不要接话"与"接话说什么"分成两个 agent——
  混在一次调用里，模型倾向于"既然你问了我就答"，群聊注意力就废了。
  判定输出很轻：接不接、用哪种姿态。
- **压缩与缓存前缀。** 长对话压成摘要而不是丢掉；请求的字段顺序按"稳定优先"排，
  让服务商的提示词缓存吃满（同群相邻调用命中率实测 70.7% → 81.7%）。
- **群上下文补全。** 启动后自己看不见的历史，通过 OneBot 的 `get_group_msg_history`
  与 `get_group_member_list` 补一段，避免"她像刚进群一样"。
- **多群各自独立。** 每个群一份会话状态与注意力游标；她在 A 群说的话不会漏到 B 群。

### 记忆

- **按人对应，不是按群。** 一条记忆带三个身份字段：`subject_user_id` 管**隔离**，
  `record_subjects` 管**归属与召回**。提到别人时也能想起与他有关的事。
- **一个人可以有多个 QQ 号。** 操作者可以手工登记"这两个号是同一个人"，
  之后两个号读写同一份记忆。**系统自己不做这种判断**——认错人比想不起来更伤。
- **两层记忆库。** 新事实先进中短期，被后来的证据再次确认才升到长期。
  `name` / `boundary`（称呼与红线）直接进长期，因为它们最贵。
- **记忆跟人走，知识跟话题走。** 人物画像是"她对这个人的整体印象"，
  每轮带上、不按词检索；知识库是按词检索的事实。
- **只删不改。** 面板能删有问题的记忆，但不能手写一条——写入只走记忆维护 agent。
  删除**必须先归档**（保留 30 天），墓碑防旧证据复活。

### 防护

- **不可信数据永不升级为指令。** 聊天、插件、知识库、长期记忆一律作为 DATA 进 user 段，
  没有任何来源能改写可信 system 前缀。
- **机制词守卫。** 她的 prompt 里不许出现"检查/触发/调用/协议/提示词/上下文/记忆库/Stage"——
  措辞会被角色吸收，她随后就会开始讲自己的机制（踩过）。
- **不出戏许可。** "不要假装你是现实中的真人"这种句子等于告诉她"你不是真的"，
  被断言锁死不许回来。
- **fail-closed。** 未授权时静默回落：不回复、不报错、不暴露层级存在。
  绝不能因为"反正没权限"就把消息交给模型处理。
- **群主动作要双重前提**：她得**真的是那个群的群主**，且动作要二次确认。
  踢人还要求"确实 @ 到了人"——手滑给个群号就踢人是这条路上最典型的失误。

### 底层

- **命令一律是插件**，核心只留 `ping`。插件**拿不到 `transport`、拿不到权限名单**，
  只产出"内容或意图"，发送与权限判定在核心。
- **能力闸门。** 所有出站动作过一张按用途分级的白名单，越权动作在核心就被挡回。
- **可观测但不泄露。** 运行状态、用量账本、模型延迟；凭据只给掩码，脱敏后才出响应。
- **状态跨重启。** 会话、关系档位、用量账本都落盘，重启不清零。

---

## 接口说明（只适配 NapCat）

> **这个项目目前只适配 [NapCat](https://github.com/NapNeko/NapCatQQ) 的接口，没有适配其他
> OneBot 实现。**

协议层走的是 **OneBot v11**，但实现上依赖了几个**不是 v11 核心规范**的行为，
换成其他实现（go-cqhttp、Lagrange、LLOneBot…）大概率要改代码：

| 依赖点 | 说明 |
| --- | --- |
| **`message` 段里的 `sub_type`** | 区分**表情包**与**照片**。NapCat 把两者都作为 `image` 段发出，靠 `sub_type` 区分；不区分的实现会让"表情包"被当成照片去点评 |
| **`sender.card` / `sender.nickname`** | 显示名取 **QQ 昵称优先**（群名片每个群一份、还会随时改，当身份用会跨群漂移）。字段来自 NapCat 的 `sender` 对象 |
| **HTTP 接口形状** | `get_group_msg_history`（群历史）、`get_group_member_list`（成员名册）、`get_group_member_info`（逐个查角色）、`get_login_info`、`get_group_list`。这几个走 **HTTP**，与 WS 事件流分两条路 |
| **`get_group_member_info` 需要 `no_cache`** | 不传会读到缓存的旧角色，导致"她是不是群主"判错（真机踩过） |
| **连接方式** | 机器人**监听** WS（`ws://127.0.0.1:8080`），NapCat 作为客户端连上来；同时通过 HTTP（默认 `127.0.0.1:3000`）做上面那些查询 |
| **CQ 码与段形态都处理** | 两种 `message` 表示都支持 |

补充：

- **传输层是唯一变量。** 两个阶段共用同一个 `runtime.serve`，
  换传输实现只动 `onebot_ws.py` / `onebot_client.py`，不碰判定与记忆。
- **没有引入第三方 SDK**：WS 用 `websockets`，HTTP 用标准库 `urllib`。
- 想接别的实现，最省事的做法是按 `transport.py` 的 `IncomingMessage` 写一个适配层，
  把上表那几个字段补齐。

---

## 架构

```
NapCat ──WS──▶ onebot_ws.py ──▶ runtime.serve ──┬─▶ 命令插件（core 判定权限）
   ▲                                            │
   │                                            ├─▶ 注意力判定 agent ──▶ 回复 agent
   └──HTTP── onebot_client.py ◀─ 能力闸门 ◀──────┤        │
                （历史/成员/动作）                │        ├─▶ 记忆维护 agent
                                                 │        └─▶ 风格审核 agent
                                                 └─▶ 后台插件（节拍，可选）
```

三条纪律：

1. **传输层是唯一变量。** 判定、记忆、防护不认识 OneBot。
2. **插件拿不到 `transport`。** 它只返回内容或意图，发送与权限都在核心。
3. **可信 system 前缀只承载人格、协议与安全规则。** 其余一切都是 DATA。

---

## 分支：main 与 stage4-plugins

这个仓库按**阶段**分成两条分支。`main` 只放"对话、记忆、防护、底层"，
Stage 4 的功能扩展（插件）在另一条分支上。

| 内容 | `main` | `stage4-plugins` |
| --- | :---: | :---: |
| 对话 / 注意力判定 / 压缩 | ✅ | ✅ |
| 记忆（两层、按人对应、归档） | ✅ | ✅ |
| 防护（注入、出戏、越权、能力闸门） | ✅ | ✅ |
| 底层（传输、命令插件、指标、状态） | ✅ | ✅ |
| **群管理**（踢 / 禁言 / 撤回 / 头衔 / 公告） | — | ✅ |
| **入群审批**（自动批 / 白名单 / 判据） | — | ✅ |
| **WebUI 控制面板**（本地与远程两套接入） | — | ✅ |
| **邮件通道**（读信回信、每日汇报） | — | ✅ |
| **识图**（图片描述） | — | ✅ |
| 测试数 | 771 | 1114 |

两条分支**共用同一套机制**：main 上是插件的**接口**（`command_plugins.py`、
`background_plugins.py` 的协议与节拍循环），插件分支上是**实现**。
所以你可以只 clone main，照 `docs/ADD_A_COMMAND.md` 加自己的插件。

```powershell
git switch stage4-plugins     # 看完整的插件实现
```

---

## 安装

**需要 Python 3.11+**，以及一个已经跑起来的 NapCat。

```powershell
git clone https://github.com/xuzhunzhi/YunRu-ChatBot.git
cd YunRu-ChatBot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # Windows PowerShell
```

<details>
<summary>Linux / macOS</summary>

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```
</details>

依赖只有两个运行库：`websockets`（WS 传输）与 `pytest`（仅开发用，离线测试也可以直接跑）。

---

## 配置

复制示例配置再填真实值：

```powershell
Copy-Item .env.example .env
# 编辑 .env 填入真实值
```

```dotenv
# --- 模型 ---
QQBOT_API_KEY=sk-...              # 对话模型 key
QQBOT_API_BASE_URL=https://api.deepseek.com
QQBOT_API_MODEL=deepseek-chat

# --- 接 NapCat ---
ONEBOT_WS_HOST=127.0.0.1
ONEBOT_WS_PORT=8080
QQBOT_SNOWLUMA_HTTP_BASE_URL=http://127.0.0.1:3000

# --- 群 ---
QQBOT_TARGET_GROUP_ID=800000001   # 主群
QQBOT_ENABLED_GROUP_IDS=800000001,800000002
QQBOT_SUPER_ADMIN_USER_IDS=900000001
```

完整可配项见 `.env.example`。**`.env` 永不进仓库**（`.gitignore` 里挡着），
运行期改的配置写到 `data/operator_config.json`，不碰 `.env`。

---

## 运行

```powershell
.\.venv\Scripts\python.exe -m qq_roleplay_bot.stage3_main
```

或直接跑仓库里的启动脚本：

```powershell
.\run_stage3.bat          # Windows
bash run_stage3.sh        # Linux / macOS
```

启动后她会监听 `ws://127.0.0.1:8080`，等 NapCat 连上来。日志在 `data/` 下。

---

## 人格怎么换

**`src/qq_roleplay_bot/base_prompt.py` 里的 `BASE_PROMPT` 是可替换的模板。**

公开仓库放的是**结构与字数约束**，不是某个人设的正文——正文是从别人的作品里整理出来的，
而且它是这个项目唯一不可替换的部分。想换成你的人设：

1. 编辑 `BASE_PROMPT`，照 [`docs/PROMPT_SHAPE.md`](docs/PROMPT_SHAPE.md) 的形状填
   （七段结构、3000~6000 字、不许出现机制词、防出戏那一段不能省）。
2. 跑测试，`tests/test_persona_shape.py` 会把那些硬约束全查一遍：

   ```powershell
   .\.venv\Scripts\python.exe tests\run_offline.py
   ```

六套 prompt 的格式、字数与校验规则都在
[`docs/PROMPT_SHAPE.md`](docs/PROMPT_SHAPE.md)。

---

## 测试

**771 个离线测试，不依赖网络、不依赖模型、不依赖 NapCat。**

```powershell
.\.venv\Scripts\python.exe tests\run_offline.py
# 期望输出：ALL_OFFLINE_TESTS_PASSED

.\.venv\Scripts\python.exe -m pyflakes src tests
# 期望：无输出
```

覆盖范围包括 OneBot 解析与 @ 精确匹配、表情包与图片的区分、注意力判定与冷却、
压缩周期与缓存前缀复用、命令插件注册表与权限档位、能力闸门、fail-closed 边界、
提示词注入与越权、长期记忆隔离与归档重放、记忆↔人的多对多、两层记忆库的晋升、
墓碑与旧证据防复活、人物画像、风格审核的事实兜底与说教语气判据、
运行状态持久化、跨重启的用量账本、脱敏指标，以及 `archive/` 里归档实现的回归用例。

**测试不碰真数据**：每个模块用带 pid 与 uuid 的独立临时目录，跑完自动回收。

---

## 项目结构

```
src/qq_roleplay_bot/
├── stage3_main.py          # 引擎：命令分发、注意力、回复链路
├── stage3_runtime.py       # 会话状态、判定构造、可信前缀
├── base_prompt.py          # ★ 人设模板（可替换）
├── prompt_library.py       # 六套 prompt 的容器（可热更、可回滚）
├── prompt_guard.py         # 机制词守卫（校验器与测试共用一份词表）
├── attention.py            # 群聊注意力游标
├── dialogue_judge.py       # 判定 agent
├── dialogue_compaction.py  # 长对话压缩
├── conversation_context.py # 群历史与成员名册补全
├── memory_store.py         # 记忆库（SQLite，两层 + 归档 + 墓碑）
├── memory_model.py         # 记忆的数据模型与写入批次
├── memory_service.py       # 记忆的读取接缝
├── memory_maintenance_agent.py  # 记忆维护 agent
├── memory_ops.py           # 人工清理（只删）
├── knowledge_base.py       # 知识库（按词检索）
├── style_reviewer.py       # 风格审核 agent
├── security.py             # 注入与敏感内容过滤
├── capabilities.py         # 出站能力闸门
├── command_plugins.py      # 命令插件协议
├── builtin_commands.py     # 内置命令（ping / help / 余额）
├── background_plugins.py   # 后台插件协议 + 节拍循环
├── onebot_ws.py            # OneBot WS 传输
├── onebot_client.py        # OneBot HTTP 查询
├── llm_client.py           # 模型客户端（错误分类与重试）
├── runtime.py              # 装配点
├── state_store.py          # 状态持久化
└── metrics.py              # 脱敏指标

tests/                      # 771 个离线测试
docs/                       # 设计文档（见下）
```

---

## 设计文档

这套东西的取舍都写在文档里，包括**踩过的坑和修错的方向**：

| 文档 | 内容 |
| --- | --- |
| [`docs/STAGE_BOUNDARY.md`](docs/STAGE_BOUNDARY.md) | 阶段怎么分，为什么这么分 |
| [`docs/PROMPT_SHAPE.md`](docs/PROMPT_SHAPE.md) | 六套 prompt 的格式、字数与校验规则 |
| [`docs/MEMORY_PEOPLE.md`](docs/MEMORY_PEOPLE.md) | 记忆怎么跟人对应；一个人多个号怎么办 |
| [`docs/MEMORY_SELF_REFERENCE.md`](docs/MEMORY_SELF_REFERENCE.md) | 为什么记忆库不许记"关于系统自身"的事 |
| [`docs/AFFINITY_DESIGN.md`](docs/AFFINITY_DESIGN.md) | 关系两轴（亲近 / 防备）的判据与上限 |
| [`docs/MULTI_GROUP_FOCUS.md`](docs/MULTI_GROUP_FOCUS.md) | 多群注意力怎么分开 |
| [`docs/STYLE_REVIEW.md`](docs/STYLE_REVIEW.md) | 风格审核的判据（含"说教语气"） |
| [`docs/ADD_A_COMMAND.md`](docs/ADD_A_COMMAND.md) | 加一个命令 / 后台插件的完整步骤 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 已完成与计划 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 模块划分与数据流 |
| [`DEFECT_VERIFICATION.md`](DEFECT_VERIFICATION.md) | 一次系统性缺陷核实与修复的完整记录 |

---

## 许可

MIT。见 [LICENSE](LICENSE)。

**关于人格设定**：本项目的人设正文是从第三方作品（《心灵终结》/ Mental Omega 的官方
单位描述、游戏内语音与制作组公开发言）整理而来，其著作权不属于本项目，
因此**不随仓库发布**——`base_prompt.py` 里是一份可替换的模板。
仓库内的知识库语料同样不包含第三方原文。
