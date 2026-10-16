# 云茹 Bot 配置教程

> 面向测试群同学。所有键名都与仓库里的 `.env.example` / `.env.bot.example` 一致，
> 照抄即可；不确定的先别改，**默认值基本都是能跑的**。

---

## 0. 五分钟最小可用

```powershell
# 1) 需要 Python >= 3.11
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 复制配置模板，填 4 个键（见第 2、3、7 节）
copy .env.example .env

# 3) 启动（它会自动设好 PYTHONPATH）
.\run_stage3.bat
```

**起来了的标志**（日志里出现这几行）：

```
server listening on 127.0.0.1:8080
OneBot 反向 WebSocket 监听于 ws://127.0.0.1:8080
OneBot 已连接
运行状态已恢复：groups=… sessions=… admins=…
```

---

## 1. 配置有三层，优先级是固定的

```
真实环境变量  >  .env 文件  >  代码里的默认值
```

- **`.env` 永不进 git**（已在 `.gitignore` 里），凭据放这儿。
- `QQBOT_ENV_FILE` 是**整体替换** `.env`，**不是合并**。所以换文件时必须写全，
  只写一个 API Key 会让别的配置全部退回默认值。

---

## 2. 模型与凭据

| 键 | 说明 |
| --- | --- |
| `QQBOT_API_BASE_URL` | 服务商地址，默认 `https://api.deepseek.com` |
| `QQBOT_API_MODEL` | 模型名，默认 `deepseek-chat` |
| `QQBOT_API_KEY` | **必填**。回复用的 key |
| `QQBOT_REPLY_API_BASE_URL` | 可选。**只给回复 agent** 的地址（2026-10-05 起可以有）；不配回落全局地址 |
| `QQBOT_REPLY_API_MODEL` | 可选。只给回复 agent 的模型；不配回落全局模型 |
| `QQBOT_REPLY_API_KEY` | 可选。**只给回复 agent** 的 key。**2026-10-16 起回复那一路住在 deepseek**（借 `QQBOT_JUDGE_API_KEY` 那条，见下面"手动切档"），所以这一项**现在闲置**——2026-10-14～10-16 那段它承载的是中转 xkiro 那把 Gemini key；那把 key 与 `model_config.KEYS["reply"]`（provider = `xkiro`）都**保留**，将来切回 Gemini 要用 |
| `QQBOT_JUDGE_API_KEY` | 可选，判定用；不配回落主 key。**2026-10-16 起判定与回复两路都借这一条**（两路都是 `deepseek-flash`），所以它现在是"判定 + 回复"两路共用的那把 |
| `QQBOT_MEMORY_API_KEY` | 可选，记忆维护用；不配回落主 key |
| `QQBOT_REVIEW_API_KEY` | 可选，风格审核用；**配了它就自动开**（也可回落主 key） |

**每个 agent 各用各的地址**：`QQBOT_REPLY_API_*` 只影响回复那一路（含按会话新建的
client），判定 / 记忆 / 审核 / 写信仍然走全局那一套。这三项在面板上也是 live 的，
留空 = 清掉覆盖、回落全局。

⚠️ **"换家"不是只填三个键**（2026-10-14 与 2026-10-16 各踩过一次"换了一半"）：
`QQBOT_<用途>_API_KEY` 只有在 `model_config.TASKS[用途]["key"]` 指向**该用途自己那条
key 条目**时才真的生效。2026-10-16（用户原话："切换回ds。gemini好蠢。"，同日早先
那句是"都改ds"）之后 `TASKS` 是这样：

| 用途 | `TASKS` 那一行 | key 从哪来 | 地址 / 模型 |
| --- | --- | --- | --- |
| reply | `{"key": "judge", "model": "deepseek-flash"}` | `QQBOT_JUDGE_API_KEY` | `deepseek` / `deepseek-flash` |
| judge | `{"key": "judge", "model": "deepseek-flash"}` | `QQBOT_JUDGE_API_KEY` | `deepseek` / `deepseek-flash` |
| letter | `{"key": "judge", "model": "deepseek-v4-pro"}` | `QQBOT_JUDGE_API_KEY` | `deepseek` / `deepseek-v4-pro` |
| memory | `{"key": "memory", "model": "deepseek-flash"}` | `QQBOT_MEMORY_API_KEY`（不配回落 `QQBOT_JUDGE_API_KEY`） | `deepseek` |
| review | `{"key": "review", "model": "deepseek-flash"}` | `QQBOT_REVIEW_API_KEY`（同上回落） | `deepseek` |
| vision | `{"key": "vision", "model": "deepseek-flash"}` | `QQBOT_VISION_API_KEY`（同上回落） | `deepseek` |

所以换家时**必须连 `TASKS` 那一行一起改**（地址、key、模型三样同源）。只填
`QQBOT_<用途>_API_BASE_URL` / `_API_MODEL` 只会换掉地址与模型名、**key 还是原来那把**
——那就是"A 家 key 打 B 家地址"的形状。这是实测过的：10-09～10-14 之间 reply 借的
是 `judge` 那条 key，那时只填 `QQBOT_REPLY_API_BASE_URL` + `_API_MODEL`，解析出来的
key **仍然是 judge 那把**（等于把 deepseek 的 key 递给新地址）。

**手动切档：deepseek ⇄ xkiro（中转的 Gemini Flash）**

`xkiro`（`https://api.xkiro.com/v1`）现在是**闲置档**：`model_config.PROVIDERS["xkiro"]`、
`model_config.KEYS["reply"]`（provider = `xkiro`、env = `QQBOT_REPLY_API_KEY`）与
`.env` 里那把 key 都**保留着**（没删，将来一键切回要用）。

| 层 | 切回 Gemini Flash | 切回 deepseek（现状） |
| --- | --- | --- |
| `model_config.TASKS["reply"]` | `{"key": "reply", "model": "google/gemini-2.5-flash"}` | `{"key": "judge", "model": "deepseek-flash"}` |
| `model_config.TASKS["judge"]` | `{"key": "reply", "model": "google/gemini-2.5-flash"}` | `{"key": "judge", "model": "deepseek-flash"}` |
| `model_config.KEYS["reply"]["provider"]` | `xkiro` | 不动（**别**跟着改成 deepseek——`KEYS["letter"]["fallback"]` 还指它） |
| `.env` 的 `QQBOT_REPLY_API_KEY` | xkiro 那把 | 留着不管（闲置） |

切到中继时会**丢掉 `reasoning_effort`**：那一项只在 `provider ∈ {"", "deepseek"}`
时才发（`llm_client._complete_sync` 里的 `deepseek_fields`），provider 是 `xkiro` 时
发不出去 ⇒ 只能吃 DeepSeek 自己的默认档。这是 2026-10-16 回退的两条依据之一；
另一条是中继的 Google 上游当时整段 503（`/v1/models` 通、`chat/completions` 全 503，
25 分钟探针全挂）。

⚠️ **一个从来没生效过的键：`QQBOT_REPLY_REASONING_EFFORT`（死键）**

思考深度那一项不是"用途名 → 变量名"的映射表，而是**直接拿 client 的 `usage_role`
当键名拼**：`llm_client` 里那句是 `reasoning_effort(self.usage_role)`，展开就是
`QQBOT_<USAGE_ROLE>_REASONING_EFFORT`，**中间不做任何映射**。而回复那一路的
`usage_role` 历史名是 **`dialogue`**（那张对照表见 `llm_client._SAMPLING_ROLE_TO_TASK`），
所以真正要写的是：

| 想改哪一路 | **真正生效的键** | 写了没用（死键） |
| --- | --- | --- |
| 回复 | `QQBOT_DIALOGUE_REASONING_EFFORT` | `QQBOT_REPLY_REASONING_EFFORT` |
| 判定 | `QQBOT_JUDGE_REASONING_EFFORT` | —— |
| 写信 / 记忆 / 审核 / 识图 | `QQBOT_LETTER_...` / `QQBOT_MEMORY_...` / `QQBOT_REVIEW_...` / `QQBOT_VISION_REASONING_EFFORT` | —— |

对比一下**采样参数那一处是有映射的**：`_sampling_params_for` 先过
`_SAMPLING_ROLE_TO_TASK = {"dialogue": "reply"}`，再去读 `QQBOT_REPLY_TEMPERATURE`
那一族。两套名字不一致是历史遗留（回复的账本角色叫 dialogue、配置键叫 reply），
不是笔误。**写错的那个键既不报错也不打日志，只是永远不生效**——动它之前先确认键名。

**实测口径（2026-10-16，`deepseek` / `deepseek-flash`，走本仓库自己的 client，流式）**：

| 口径 | 指的是什么 | 实测 |
| --- | --- | --- |
| 第一条完整响应行（闸） | 首字闸（`llm_client.STREAM_STALL_SECONDS`，3 秒）盯的那个事件：服务端吐回**第一整行** SSE | **0.15~0.31s** |
| 第一个 content | 真正吐出第一段正文的时刻 | `reasoning_effort=none` 档 **0.51~1.13s**；`low` / `high` 档 **1.87~17.03s** |

（这两个数**不是一个东西**：撞 3 秒那道闸看的是"第一条完整响应行"，不是"第一个
content"。以前这里只写"首字/总"两个词，读的人拿哪个数去对闸都不知道；那一版 A/B
数字因此作废，以这张表为准。）

⚠️ **同一个账号下的多把 key 不隔离并发限额与缓存容量**——那两样是账号级的。
分 key 的意义是"出问题只吊销那一把"。想让账单彻底分开，得用**不同账号**的 key。

### 想让 Bot 用独立账户（账单分开）

```powershell
copy .env.bot.example .env.bot     # 填 Bot 专用 Key
.\run_stage3.bat                   # 它会自动使用 .env.bot
```

注意两点：`.env.bot` 会**整体替换** `.env`（所以要把 Bot 需要的配置写全）；
直接 `python -m qq_roleplay_bot.stage3_main` 启动**不会**走 `.env.bot`。

---

## 3. 传输：NapCat / OneBot

Stage 3 用的是**反向 WebSocket**：**Bot 监听，NapCat 连进来**。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `ONEBOT_WS_HOST` | `127.0.0.1` | Bot 监听地址 |
| `ONEBOT_WS_PORT` | `8080` | Bot 监听端口 |
| `ONEBOT_ACCESS_TOKEN` | 空 | 非空时 NapCat 侧要填一样的 |

NapCat 侧把反向 WS 地址填成 **`ws://127.0.0.1:8080`**（端口要和这里一致）。

**端口被占就起不来**：

```powershell
netstat -ano | findstr :8080     # 看是谁占了
```

---

## 4. 长期记忆

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_MEMORY_ENABLED` | `1` | 关掉后记忆维护不再跑 |
| `QQBOT_MEMORY_INTERVAL_SECONDS` | `600` | 每 10 分钟巡检一次 |
| `QQBOT_MEMORY_RETENTION_DAYS` | `7` | 收件箱里消息的保留天数 |
| `QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS` | `365` | **关系流水**（每一次好感/防备变动）保留多少天。范围夹在 30..3650，坏值回落 365。只按时间清**旧行**，近期一行不动；清掉的条数在 `/super status` 的记忆那行显示 |
| `QQBOT_TOMBSTONE_RETENTION_DAYS` | `365` | **墓碑**（删掉的记忆的键与时间判据，**不含内容**）保留多少天。范围夹在 30..3650，坏值回落 365。同样只清旧行、条数可见 |
| `QQBOT_MEMORY_MODEL` | 空 | 不写就复用 `QQBOT_API_MODEL` |

记忆库在 `data/memory/memory.sqlite3`。**删记忆等于删她的经历**，动手前先备份。

---

## 5. 行为开关

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_GROUP_LISTEN` | `1` | `1` = 群里每条消息都送进模型判断，她能看见别人之间的对话；`0` = 只跟对话对象说话 |
| `QQBOT_TYPING_SIM` | `1` | `1` = 先亮"正在输入"、按字数停顿、长回复拆成几条；`0` = 立刻一次性发出 |
| `QQBOT_VISION` | `1` | 识图：有图的消息先看一眼再判定 |
| `QQBOT_STYLE_REVIEW` | 跟随 | 回复出口前**只看不改**的审核（六类：事实错误/越权/出戏自指/伤人/不成句/空回复；输出认不出会重试一次）；配了审核 key 时默认开。判不过就让回复 agent 重写一次 |
| `QQBOT_DUAL_AGENT` | `1` | 判定与回复分离；判定不接就不发生回复调用 |
| `QQBOT_TURN_TIMEOUT_SECONDS` | `90` | **一条消息从进来到出结果的总预算**（秒）。范围夹在 5..600，坏值回落 90。超时 = 这一条没有回复（`/super status` 上有计数），**那条消息仍留在她的历史里**。它管的是"某一腿卡住时会不会把整个 bot 卡住"——接收循环是串行的，而每腿自己的网只是**停顿网**（首字 3 秒 + 连续 `QQBOT_<用途>_TIMEOUT` 秒无字节），相加可以远超这个预算，所以这一层是**最后一道网** |
| `QQBOT_COMPACTION` | `1` | 对话压缩：`0` = 长对话**不再压成摘要**（一次压缩请求都不发，历史原样留着，靠活窗口滚动）。面板上那条「对话压缩」开关改的就是它 |
| `QQBOT_LOCAL_TZ_OFFSET_HOURS` | `8` | **"群里这边"的时区偏移**（小时）。报给她的"现在"与 `/super status` 那些时刻都按它渲染，**不读部署机器的时区**——UTC 云主机上写 `0` 就对了。范围夹在 `-12..14`，坏值回落 8 |
| `QQBOT_PERSONA_OFFSET_HOURS` | `3` | "她那边"比"群里这边"快多少小时；两句时刻每次都一起印给她（*"她那边 = 北京 + 3 小时"*） |

> **每个模型用途的超时**（`QQBOT_REPLY_TIMEOUT` / `QQBOT_JUDGE_TIMEOUT` / …）从 2026-10-13 起
> **不再是"整趟上限"，而是停顿网**：首字 3 秒不来就放掉这一轮（`llm_client.STREAM_STALL_SECONDS`，
> **不可配**——用户拍板就是 3 秒），首字来了就让她说完，只有"连着这么多秒一个字节都不吐"
> 才当连接死了。同一天起**不重试**（`DEFAULT_MAX_ATTEMPTS = 1`）：一次失败 = 这一轮放掉
> （判定不出声、回复不发、审核按原稿发），理由见 `docs/audits/` 之外的代码注释。

---

## 6. 运行数据与状态

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_DATA_DIR` | 项目下 `data/` | 所有运行数据、状态、日志、记忆的根目录 |
| `QQBOT_STATE_PERSIST` | 开启 | `0` 关闭运行状态持久化 |
| `QQBOT_STATE_FILE` | 由上面推导 | 指定 `runtime_state.json` 的完整路径 |
| `QQBOT_PERSISTED_HISTORY` | `200` | 每个会话落盘带多少条最近消息。范围夹在 50..2000，坏值回落 200。**不是性能开关**：它只决定"还没进摘要的那段活窗口"在重启后能恢复多少（超出的部分只在内存里，重启就没了；更早的内容已经在摘要里） |
| `QQBOT_SESSION_CACHE_MAX_SESSIONS` | `200` | 内存里最多同时留多少个会话的短期状态。范围夹在 10..2000，坏值回落 200。超了就淘汰**最久不活跃**的那个会话（它的两个按会话 client 也一起丢掉）；淘汰数在 `/super status` 的"短期会话"那行显示。**与开不开对话无关**：`/admin disable` 只等于"不回复"，历史与记忆照记 |
| `QQBOT_CHAT_LOG` | 开启 | **对话日志**（实际收发，`data/logs/chat.jsonl`）。`0` 关掉整份 |
| `QQBOT_CHAT_LOG_MAX` | `20000` | 对话日志保留多少条（按条滚动，不是按天）。范围兜在 10..1000000，**不会无限增长** |
| `QQBOT_CHAT_LOG_DIR` | 空 | 空 = 与模型日志同一个 `data/logs/`；要单独搬走就写路径 |
| `QQBOT_RAW_EVENTS` | 开启 | **原始事件日志**（`data/logs/raw_events.jsonl`）：**没被处理**的入站事件（通知/请求/其它 post_type）原样落盘。只为查"NapCat 到底推不推某种事件"（如表情回应）。`0` 关掉整份 |
| `QQBOT_RAW_EVENTS_MAX` | `5000` | 原始事件日志保留多少条（按条滚动）。范围兜在 10..1000000，**不会无限增长** |
| `QQBOT_RAW_EVENTS_DIR` | 空 | 空 = 与另外两份日志同一个 `data/logs/`；要单独搬走就写路径 |

**调试用（生产建议保持注释掉）**：`QQBOT_DEBUG_MODEL_IO=1` 会把模型的完整
输入输出（含 system prompt 与聊天正文）落盘到 `data/traces/`。

**到点提醒（插件 `plugins/remind/`）**——他在群里说一句"8 点提醒我交作业"就调起，
到点由核心发。它自己的数据在 `data/remind/` 下（**都是 jsonl，删了就真没了**）：

| 文件 | 装什么 |
| --- | --- |
| `data/remind/people.jsonl` | 每个人一条：他选的叫他方式、**核心递过来的那个邮箱地址**、"问过他没有" |
| `data/remind/reminders.jsonl` | 还没发 / 发过 / 取消的那几条提醒（发完 7 天后自动清掉） |
| `data/remind/mail_sent.jsonl` | 邮件那一路的账本：**谁、哪天**（只记这个，不记正文），保留 7 天 |

邮件那一路是**可选**的：没有 mail 插件（或它没提供"寄给某个地址"那条口子）时，
问他"怎么叫你"就只列**群里 @ / 私聊**两条，其余照常。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_REMIND_MAIL_PER_PERSON_PER_DAY` | `1` | **一个人一天最多几封**提醒邮件。范围夹在 0..20；`0` = 这个人一封都不寄 |
| `QQBOT_REMIND_MAIL_PER_DAY` | `5` | **全局一天最多几封**。范围夹在 0..50；两个都是 `0` = 邮件那一路整个关掉 |

> 为什么默认这么保守：**任何能触发提醒的人**都能让操作者的邮箱替他寄信
> （主题/正文模板固定，但"提醒什么"是可控的）。超限时不寄、并回他一句人话
> （不烧重试、也不会安静地挂着）。这两个键是**插件读的环境变量**——它们不住在
> `dev_config.py`（那是核心的文件）。

> `data/` 不在 git 里。**删了就真没了。**

---

## 7. 权限（这一段最容易踩坑）

| 键 | 说明 |
| --- | --- |
| `QQBOT_SUPER_ADMIN_USER_IDS` | 超管 QQ 号，逗号分隔。**不配则用代码里的占位号 `900000001`** |
| `QQBOT_ADMIN_USER_IDS` | 普通管理员，逗号分隔 |
| `QQBOT_PRIVATE_DEBUG_USER_IDS` | 谁能私聊她；不配取超管 |
| `QQBOT_BALANCE_PRIVATE_USER_IDS` | `/balance` 私聊白名单；不配取超管 |
| `QQBOT_BALANCE_GROUP_IDS` | 允许在哪些群查余额；**留空 = 群里不可用**（fail-closed） |

> ### ⚠️ 超管号**必须写 `.env`，不要写进代码文件**
>
> 代码文件（`src/qq_roleplay_bot/dev_config.py`）是**被 git 跟踪**的。
> 把真实号写在里面，只是一处未提交的本地修改——**一次 `git checkout` 就会把它
> 还原成占位号 `900000001`，超管当场失去全部权限，而且没有任何告警**（真实发生过）。
> 写在 `.env` 里则换分支、重装都冲不掉。

---

## 8. 多群焦点（一般不用改）

她同一时刻只在**一个群**当值，其余群排队。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_FOCUS_QUIET_SECONDS` | `45` | 话题静默多久释放当值 |
| `QQBOT_FOCUS_DUTY_SECONDS` | `300` | 单次当值上限 |
| `QQBOT_FOCUS_REPLY_LIMIT` | `50` | 连回多少条后交代一句再走 |
| `QQBOT_FOCUS_ACK_COOLDOWN_SECONDS` | `120` | 同一个群里"稍等"的冷却 |

---

## 9. 命令

- **`/super help`** —— 超管全部子命令（状态、进程、群管理、记忆查看、加管理员…）
- **`/admin help`** —— 群管理员子命令（开关群、清会话、转达…）
- `ping` / `help` —— 基础
- `/balance` —— 账户余额，**默认只在超管私聊可用**
- **`/提醒`** —— 到点提醒（插件 `plugins/remind/`）：`/提醒` 看还有哪些、
  `/提醒 删 2` 删掉第 2 条、`/提醒 换 2` 换"以后叫我"的方式（1 群里 @ / 2 私聊 / 3 邮箱）。
  **"8 点提醒我交作业"那句是对话调起的**，不走这条命令；这条命令只用来**看和改**。

权限判定在核心，失败时**静默**（不回复、不报错、不暴露层级存在）。

---

## 10. 常见问题

| 现象 | 排查 |
| --- | --- |
| 起不来，报 `No module named qq_roleplay_bot` | 没设 `PYTHONPATH`。用 `run_stage3.bat` 就自动设了 |
| 起不来，端口占用 | 看第 3 节；或改 `ONEBOT_WS_PORT` |
| NapCat 连不上 | 地址/端口/token 要和 `.env` 一致；**先起 Bot 再连** |
| 她在群里一直不说话 | 多半是判定压着（正常）。看 `data/logs/judge.jsonl` 里的 `<route>` |
| 记忆不写 | `QQBOT_MEMORY_ENABLED=1` 且记忆 key（或主 key）有值 |
| `/balance` 查不到 | fail-closed：必须显式配白名单，见第 7 节 |
| 想看她到底看了什么（模型那一半） | `data/logs/` 下按功能分的 `judge.jsonl` / `reply.jsonl` / `memory.jsonl` |
| 想查群里**实际**收到了什么、她到底发出去过什么 | `data/logs/chat.jsonl`（**对话日志**，实际收发；拆开的每一段各一条）。留多少条看 `QQBOT_CHAT_LOG_MAX`（默认 20000），关掉整份用 `QQBOT_CHAT_LOG=0` |
| 想知道 **NapCat 到底推不推某种事件**（例如"别人给她的话点了表情"） | `data/logs/raw_events.jsonl`（**原始事件日志**）：核心没认出来、历来直接丢掉的那些帧**原样**在这里一行一条（`{"at":…, "payload":{…原始…}}`，心跳不记）。排查时 `Select-String -Path data\logs\raw_events.jsonl -Pattern 'emoji'`。容量 `QQBOT_RAW_EVENTS_MAX`（默认 5000），关掉用 `QQBOT_RAW_EVENTS=0` |

---

## 11. 安全清单

- `.env` / `.env.bot` **永不提交**（已在 `.gitignore`）
- 生产环境**不要**开 `QQBOT_DEBUG_MODEL_IO`
- `data/`（含记忆库）、`backups/`、`.env` **都不在 git 里**——删了不可恢复
- 公开仓库里的示例一律用假号（`900000001`）；**别把真实号、真实群号写进代码或文档**
