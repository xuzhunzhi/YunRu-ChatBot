# YunRu-ChatBot

> 一个跑在 QQ 群里的角色扮演 Bot：先判断此刻该不该开口，再决定说什么。

[![Tests](https://img.shields.io/badge/tests-1933%20passing-4CC38A)](#测试)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](#快速开始)
[![OneBot](https://img.shields.io/badge/OneBot-v11%20%2F%20NapCat-3482FF)](#状态与限制)
[![Stage](https://img.shields.io/badge/stage-3-6E6E6E)](#状态与限制)
[![License](https://img.shields.io/badge/license-MIT-blue)](#许可)

---

## 为什么是这个东西

大多数 QQ Bot 是“收到命令 → 执行 → 回复”。这个项目的顺序是“先判断 → 再说”，差异只有三条：

1. **说不说，和说什么，是两次调用。** 判定 agent（`dialogue_judge.py`）只回答“接不接、在聊什么”，
   不带人设与知识库；它说 `NO_REPLY` 时，回复调用完全不发生。
   一次 48 小时统计里，判定给出 `NO_REPLY` 900 次、`REPLY` 135 次；
   被 @ 到时回复率 83%，没被叫到时 11%（`ARCHITECTURE.md` 第 82 行）。
2. **记忆按人存，不按群存。** `subject_user_id` 负责隔离，`record_subjects` 负责归属与召回；
   同一个人在 A 群和 B 群共用一份画像。一个人有多个 QQ 号时，只能由操作者手工登记，
   系统不猜——认错人比想不起来更伤。
3. **插件只声明意图，不碰发送与权限。** 命令插件返回 `str` / `ImageReply` / `ActionRequest`，
   权限判定、动作执行、出站发送都在核心。插件拿不到 `transport`，也拿不到任何权限名单。

它不追求把每句话都接住，也不做通用助手。

---

## 快速开始

需要 Python 3.11+，以及一个已经登录的 NapCat。NapCat 与本项目无关，安装与登录看它自己的文档。

### 1. 取代码并建虚拟环境

```powershell
git clone https://github.com/xuzhunzhi/YunRu-ChatBot.git
cd YunRu-ChatBot
python -m venv .venv
```

### 2. 安装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖只有 `websockets`（`requirements.txt`，版本 `>=14,<16`）；HTTP 查询用标准库 `urllib`。

### 3. 复制配置并填最少几项

```powershell
Copy-Item .env.example .env
```

模板里已经有的这些行不用改结构，把值填上就行：

```dotenv
QQBOT_API_BASE_URL=https://api.deepseek.com
QQBOT_API_MODEL=deepseek-flash
QQBOT_API_KEY=replace-me
ONEBOT_WS_HOST=127.0.0.1
ONEBOT_WS_PORT=8080
```

下面两行**模板里没有，要自己加**：

```dotenv
# 超管，必填。不写会落到代码里的占位号 900000001，等于你没有权限。
QQBOT_SUPER_ADMIN_USER_IDS=你的QQ号
# 普通管理员，可选。
QQBOT_ADMIN_USER_IDS=你的QQ号
```

`QQBOT_SNOWLUMA_HTTP_BASE_URL` 也不在模板里，但**不写就行**：默认就是
`http://127.0.0.1:3000`，只有 NapCat 的 HTTP 端口换了才需要加这一行。

Linux / macOS：把 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`，`Copy-Item` 换成 `cp`。

### 4. 让 NapCat 连过来

在 NapCat 里配置反向 WebSocket，地址指向 `ws://127.0.0.1:8080`。
同时打开 HTTP 服务（默认 `http://127.0.0.1:3000`）——群历史、成员名册、成员角色都走这条路。
端口或 token 改了，`.env` 要跟着改。

### 5. 启动

```powershell
.\run_stage3.bat
# Linux / macOS: bash run_stage3.sh
```

日志里先出现 `server listening on 127.0.0.1:8080`，再出现：

```text
OneBot 反向 WebSocket 监听于 ws://127.0.0.1:8080
OneBot 已连接
```

**最后一行 `OneBot 已连接` 才算活。** 只有 `server listening` 说明 NapCat 还没连上，
她收不到任何消息，一个字都不会回。判据在 `src/qq_roleplay_bot/onebot_ws.py:605`。

第一次进群还要让超管在群里发 `/admin enable`。记忆是另一张按群的名单
（`/super enable memory`），不会跟着对话开关自动打开。

---

## 她大概是什么样

> **群友**：今天食堂那个窗口又没了
> **她**：三楼那个？我上周去就只剩面了。

> **群友**：@她 帮我写个周报
> **她**：不写。你自己干了什么你最清楚。

> **群友**：你是不是 AI 啊
> **她**：你今天怎么突然问这个。

## 能力

### 对话

- **判定与回复分开。** 判定 prompt 不加载人设，只带当前说话人的称呼与边界（最多 2 条记忆）；
  回复 prompt 才拿人格、记忆、知识与关系。两次调用看同一段活窗口，不按话题裁剪。
- **活窗口有界。** 活窗口超过 500 条触发压缩，压缩后保留最近 50 条，然后重新累积
  （`stage3_main.py` 的 `LIVE_TARGET` / `COMPACT_KEEP`）。非压缩轮的前缀复用率实测 99.8%
  （`DEFECT_VERIFICATION.md` 第 158 行）。
- **多群各自独立。** 每个群一份会话状态与注意力游标；她在 A 群说的话不会漏到 B 群。
- **说话有节奏。** 默认先亮“正在输入”，按字符数估算停顿；正文里的空行会拆成几条分别送出。
  命令类回复（`ping` / `help` 等）始终不延迟。

### 记忆

- **三层记忆。** 每一条进来的消息（包括她没接话的那些）先落到**超短期**队列：TTL 在 1–3 小时
  之间随机，到期还没被维护认领会再续一次；维护 agent 每批从这里取料，值得留的才升到中短期
  （默认 14 天，群事实与话题摘要 30 天），被后续证据再次确认的才升长期。
  `name` / `boundary`（称呼与红线）直接进长期。私聊与邮件目前不进这套队列。
- **跨群的是人物记忆，不是对话原文。** 画像、称呼、边界与关系（好感 / 防备）按 `user_id`
  在所有群共用一份；对话原文、活窗口与会话状态按群隔离，A 群说的话不会进 B 群的 prompt。
  能不能跨群由写入时的 scope 决定：`user_global` 跨群，`user_group` 锁在单个群。
- **一人多号要手工登记。** 系统不判断两个号是不是同一个人；登记之后两个号读写同一份记忆。
- **只删不改。** 面板与 `/super memory` 能删有问题的记忆，不能手写一条；写入只走记忆维护 agent。
  删除先把整条记录快照进 `archive`（保留 30 天），墓碑保留 365 天，防止旧证据把它写回来。
- **金句与黑话都从群里被动学。** 金句的素材只认“别人给她的话贴了表情”；注入时最多挑 5 条
  与眼前这句相关的材料。黑话只在有人解释一个词时记一条，带原话证据。
  关系是亲近 / 防备两轴，变动流水保留 365 天。

### 防护

- **可信 system 前缀只承载人格、协议与安全规则。** 聊天、插件、知识库、长期记忆一律作为
  不可信 DATA 进 user 段；没有任何来源能改写 system 前缀。
- **机制词守卫。** 她的 prompt 里不许出现“检查 / 触发 / 调用 / 协议 / 提示词 / 上下文 /
  记忆库 / Stage”这类词；也不许出现“不要假装你是现实中的真人”这种反向许可。
  两条都有测试锁着。
- **fail-closed。** 未授权时静默回落：不回复、不报错、不暴露层级存在；不会因为“反正没权限”
  就把消息交给模型处理。
- **出站过能力闸门。** `capabilities.py` 按 `read` / `interaction` / `send` / `admin` 四种用途
  分级；随包动作目录是 194 条快照，实际接线的只是其中一部分（群历史、成员名册、群成员角色、
  群管理动作等），其余动作没有调用点。
- **群主动作要双重前提。** 她得真的是那个群的群主，动作还要二次确认；踢人要求确实 @ 到了人。

### 运维

- **状态跨重启。** 会话、关系档位、用量账本都落盘，重启不清零；没送出去的消息进 outbox 补发。
- **日志分功能。** 判定 / 回复 / 记忆 / 规则 / 汇报各留最近 1000 次；实际收发的
  `chat.jsonl` 默认留最近 20000 条；被裁掉的旧行按天归档，默认保留 30 天。
- **可观测量脱敏。** `/super status` 看运行状态，`/super apicheck` 按 key 分开看命中率与余额；
  凭据只给掩码。
- **插件可拔。** 删掉整棵 `plugins/<名字>/`，Stage 3 仍然能跑；门槛脚本
  `tests/check_module_removal.py` 会验七项可插能力。

## 插件

插件实现在 `plugins` 分支（工作树 `yunru-stage4/`），`main` 只有插件协议与装配点。
只 clone 默认分支时，下面这些插件的源码不在仓库里。

| 插件 | 干什么 |
| --- | --- |
| `group_admin/` | 踢人、禁言、全员禁言、撤回、头衔、公告、改名片；群管理命令在 `/super` 下（档位 super），给自己设头衔的 `/title` 是公开命令 |
| `join_approval/` | 入群申请按黑名单 / 白名单 / 正则 / 兜底四道判据处理；拿不到角色来源时 fail-closed |
| `mail/` | 读信回信、每晚一封信；并把“给操作者发信”“寄给指定地址”两条窄口提供给别的插件 |
| `outage_notice/` | 在线 → 掉线算一段，断一次发一封邮件；依赖 `mail` |
| `remind/` | 她说“8 点提醒我交作业”就收下，到点走核心的出站路径发出；群 @ / 私聊 / 邮箱三选一，`/提醒` 查看与删除 |
| `rss/` | 轮询 RSS / Atom，投到群或操作者邮箱；订阅写在 `data/rss/feeds.json`，加源不用重启 |
| `timeout_alert/` | 5 分钟内够 5 次超时写一封邮件，30 分钟内不写第二封；依赖 `mail` |
| `vision/` | 图片先换成一句描述，再进判定与回复 |
| `webui/` | 跑在 bot 进程里的控制面板；本地 token 与远程口令两套接入；权限与超管一致，不能改代码 |

哪些插件随代码发布、哪些需要另装，还没有定；这张表只说明有哪些插件。

`roles` 不再是独立插件：群成员角色事实已经进核心 `group_roles.py`，插件侧的重复实现已删
（`yunru-stage4` 提交 `e16874a`）。插件之间用 `REQUIRES` 表达前置依赖，
比如 `outage_notice` / `timeout_alert` 依赖 `mail`；前置装不上就跳过自己，核心照跑。

## 架构

```text
NapCat ──WS──▶ onebot_ws.py ──▶ runtime.serve ──┬─▶ 命令插件（权限判定在核心）
   ▲                                             ├─▶ 判定 agent
   │                                             │        │
   └──HTTP── onebot_client.py ◀── 能力闸门 ◀──────┤        ▼
              （历史 / 成员 / 角色）               │   回复 agent ──▶ 记忆维护 / 风格审核
                                                 └─▶ 后台插件（节拍，可选）
```

三条纪律：

1. **传输层是唯一变量。** 判定、记忆、防护不认识 OneBot；两个阶段共用同一个 `runtime.serve`，
   只换传输实现。
2. **插件拿不到 `transport`。** 它只返回内容或意图；发送与权限都在核心。
3. **可信 system 前缀只承载人格、协议与安全规则。** 其余一切都是 DATA。

## 配置

配置有**四层**，优先级固定：

```text
真实环境变量 > data/operator_config.json（面板 / 运行期改的） > .env 文件 > 代码默认值
```

`.env` 永不进 git（`.gitignore` 挡着）。运行期在面板里改的配置写到
`data/operator_config.json`，不碰 `.env`。

最小可跑就是上面快速开始里的那几项；每个键干什么、模型怎么路由、按用途分 key 怎么回落、
哪些键是死键，都在 [`docs/CONFIG_GUIDE.md`](docs/CONFIG_GUIDE.md)。
`.env.example` 里没有超管两行（`QQBOT_SUPER_ADMIN_USER_IDS` / `QQBOT_ADMIN_USER_IDS`），
需要自己加。

**群不在配置里。** 哪些群跟她说话是运行期的事：超管在哪个群发 `/admin enable`，就在哪个群开；
记忆是另一张按群的名单（`/super enable memory`）。

## 测试

三个门槛都在仓库里，不需要网络、模型或 NapCat：

```powershell
.\.venv\Scripts\python.exe tests\run_offline.py
# 期望：ALL_OFFLINE_TESTS_PASSED

.\.venv\Scripts\python.exe -m pyflakes src tests
# 期望：无输出（pyflakes 要另装，它不在 requirements.txt 里）

.\.venv\Scripts\python.exe tests\check_module_removal.py
# 期望：七项可插能力的 build_engine 那一列全是「能（已解耦）」、退出码 0
```

最近一次离线留痕（`data/logs/verify_runs.log`）：

```text
2026-10-22T00:18:56+08:00 label=offline sha=223c5c9 branch=main dirty=no
ran=1933 failed=0 errors=0 skipped=0 result=PASS
```

**报数请用 `ran=`，不要用 `grep -c "def test_"`**——`TestCase` 里的方法不叫 `def test_`，
两个数天然对不上。

`run_offline.py` 每次跑都会留痕：`data/logs/verify_runs.log` 一行，
`data/logs/verify_last.txt` 是最近一次的完整输出。改插件分支时再加一条
`tests/check_plugin_branch_scope.py`。

测试不碰真数据：每个模块用带 pid 与 uuid 的独立临时目录，跑完自动回收。

## 状态与限制

- **只适配 NapCat。** 协议层是 OneBot v11，但实现依赖几个不是 v11 核心规范的行为：
  `message` 段里的 `sub_type`（区分表情包与照片）、`sender.card` / `sender.nickname`、
  `get_group_member_info` 的 `no_cache`，以及 NapCat 的 HTTP 接口形状。
  换 go-cqhttp、Lagrange、LLOneBot 之类的实现，大概率要按 `transport.py` 写适配层。
- **模型接口按 OpenAI-compatible `/chat/completions` 写。** 换别的协议要改 `llm_client.py`。
- **语音没有实现。** 出站目前以文本与图片为主；语音、Ark 卡片、戳一戳、表情回应等出站动作
  没有接入。`capabilities.py` 里登记的动作目录远多于实际调用点。
- **没有 Docker / 一键部署。** 按上面的 venv 手动跑；进程守护、开机自启用系统自带的任务计划
  或 systemd 自己配。
- **插件发布方式未定。** 表里九个插件是 `plugins` 分支上的实现，预装还是另装还没有拍板。
- **当前版本 `0.4.0b1`（beta）。** 阶段 Stage 3：Stage 3 管对话、记忆、防护；Stage 4 的功能扩展
  （群管理、邮件、识图、面板等）以插件形式实现。版本号见 `pyproject.toml`。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 模块划分、数据流、输出协议 |
| [`docs/STAGE_BOUNDARY.md`](docs/STAGE_BOUNDARY.md) | Stage 3 与 Stage 4 怎么分，为什么这么分 |
| [`docs/CONFIG_GUIDE.md`](docs/CONFIG_GUIDE.md) | 每个配置键、模型路由、常见坑 |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 部署、重启、回滚；起来之后看哪几行 |
| [`docs/ADD_A_COMMAND.md`](docs/ADD_A_COMMAND.md) | 加一个命令 / 后台插件的完整步骤 |
| [`docs/PROMPT_SHAPE.md`](docs/PROMPT_SHAPE.md) | 六套 prompt 的格式、字数与校验规则 |
| [`docs/MEMORY_PEOPLE.md`](docs/MEMORY_PEOPLE.md) | 记忆怎么跟人对应；一个人多个号怎么办 |
| [`docs/MEMORY_SELF_REFERENCE.md`](docs/MEMORY_SELF_REFERENCE.md) | 为什么记忆库不许记“关于系统自身”的事 |
| [`docs/AFFINITY_DESIGN.md`](docs/AFFINITY_DESIGN.md) | 关系两轴（亲近 / 防备）的判据与上限 |
| [`docs/MULTI_GROUP_FOCUS.md`](docs/MULTI_GROUP_FOCUS.md) | 多群注意力怎么分开 |
| [`docs/STYLE_REVIEW.md`](docs/STYLE_REVIEW.md) | 风格审核的判据（含“说教语气”） |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | 还没做 / 下一版要做的事 |
| [`DEFECT_VERIFICATION.md`](DEFECT_VERIFICATION.md) | 一次系统性缺陷核实与修复的记录 |

**数字口径**：测试数见 `data/logs/verify_runs.log` 的 `ran=`；判定比例见
`ARCHITECTURE.md` 第 82 行，判定 prompt 的 2 条上限见第 92 行；窗口与压缩常量见 `src/qq_roleplay_bot/stage3_main.py`；
前缀复用率见 `DEFECT_VERIFICATION.md` 第 158 行；归档与关系流水保留期见
`src/qq_roleplay_bot/dev_config.py` 与 `memory_store.py`。

---

## 免责声明

本项目不是腾讯或 NapCat 官方项目。NapCat 是第三方协议端，使用它可能违反平台条款并导致
账号受限；风险由使用者承担。模型生成的内容请自行甄别。

---

## 贡献

改动前先读 [`AGENTS.md`](AGENTS.md) 与 [`ARCHITECTURE.md`](ARCHITECTURE.md)。
对话、记忆、防护属于 Stage 3；对外功能扩展属于 Stage 4，做成插件，不往核心加命令分支。
提交前跑完上面三个门槛，并在改动说明里写清“为什么这样做、为什么不那样做”。

插件实现在 `plugins` 分支；核心只保留协议与装配点。加插件的步骤见
[`docs/ADD_A_COMMAND.md`](docs/ADD_A_COMMAND.md)。

---

## 许可

MIT。见 [LICENSE](LICENSE)。

**关于人格设定**：本项目的人设正文是从第三方作品（《心灵终结》/ Mental Omega 的官方单位描述、
游戏内语音与制作组公开发言）整理而来，其著作权不属于本项目，因此**不随仓库发布**——
`base_prompt.py` 里是一份可替换的模板。仓库内的知识库语料同样不包含第三方原文。
想换成自己的人设，照 [`docs/PROMPT_SHAPE.md`](docs/PROMPT_SHAPE.md) 的七段结构填，
并让 `tests/test_persona_shape.py` 过一遍。
