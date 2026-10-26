# 云茹 Bot 配置教程

> 面向测试群同学。`.env.example` 里有的键，名字与它**逐字一致**，照抄即可；
> `.env.example` 里**没有**的那些（插件自己读的邮件/提醒/面板/超时报警那几个键、
> 金句与黑话的开关）在第 6 节单独列了。不确定的先别改，**默认值基本都是能跑的**。
> 部署、重启、回滚见 [`DEPLOY.md`](DEPLOY.md)。

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

**起来了的标志**（日志里出现这几行，`data/logs/` 与 stdout 都算）：

```
模型配置 reply: provider=deepseek model=deepseek-flash key=QQBOT_JUDGE_API_KEY 有值
模型配置 letter: provider=mimo model=mimo-v2.6-pro key=QQBOT_LETTER_API_KEY 有值
…（六个用途各一行：reply / letter / judge / memory / review / vision）
运行状态已恢复：groups=… sessions=… admins=… enabled=…
server listening on 127.0.0.1:8080
OneBot 反向 WebSocket 监听于 ws://127.0.0.1:8080
OneBot 已连接
```

**最后一行才是判据**。只有 `server listening`、没有 `OneBot 已连接`，说明 NapCat 还没连上来
——她收不到任何消息，一个字都不会回。前面那几行 `模型配置` 是凭据自检：哪一行写着
`**缺**`，就是那一路没有可用的 key（见第 2 节）。

---

## 1. 配置有**四**层，优先级是固定的

```
真实环境变量  >  data/operator_config.json（面板 / 运行期改的）  >  .env 文件  >  代码里的默认值
```

（以前这里写的是"三层"——漏了 `operator_config.json` 那层。**层数是四，但顺序不是"覆盖层最高"**：
2026-10-18 读实现核出来的顺序就是上面这个。）

**为什么真实环境变量在最前**：覆盖层与 `.env` 用的是**同一条纪律**——不覆盖 `os.environ`
里已存在的键（`operator_config.py:216`、`dev_config.py:91`）。进程启动时，真实环境变量
**已经**在 `os.environ` 里了，所以谁也顶不掉它。覆盖层在 `dev_config.py:113` 注入、
`.env` 在 `:114` 才读，覆盖层早一步 ⇒ 它压得住 `.env`，压不住真实环境变量。

> ⚠️ **两处注释把顺序写反了**：`dev_config.py:99-101` 与 `operator_config.py:9` 都写成
> 「`operator_config.json` > 真实环境变量」，这与 `operator_config.py:216` 那道守卫、
> 以及它自己 `:15-16` 的说明（*"显式设过的环境变量是'这次运行的特殊要求'，面板的持久覆盖
> 不该把它顶掉"*）矛盾。**以代码为准**：同一个键**真实环境变量也设了**的时候，
> **面板改的那份不生效**。（那两行注释是源码改动，等用户点头再改准。）

- **`.env` 永不进 git**（已在 `.gitignore` 里），凭据放这儿。
- `QQBOT_ENV_FILE` 是**整体替换** `.env`，**不是合并**。所以换文件时必须写全，
  只写一个 API Key 会让别的配置全部退回默认值。
- 覆盖层（`data/operator_config.json`）与 `.env` 都遵守同一条纪律：**不覆盖已经存在的键**。
  所以"谁先进 `os.environ` 谁赢"——覆盖层是抢在 `.env` 之前注入的，它因此排在第一位。
  想把某一项退回去，就把覆盖层那一项清掉，别指望在 `.env` 里写一句更大的。

---

## 2. 模型与凭据

| 键 | 说明 |
| --- | --- |
| `QQBOT_API_BASE_URL` | 兼容档地址，默认 `https://api.deepseek.com`（`model_config.PROVIDERS["deepseek"]`） |
| `QQBOT_API_MODEL` | 兼容档模型名。**不配 = 用 `TASKS` 里那个真名**（现在 reply / judge / memory / review / vision 是 `deepseek-flash`，letter 是 `mimo-v2.6-pro`）。⚠️ 这里原来写"默认 `deepseek-chat`"——那是会消失的别名，代码里已经不用了 |
| `QQBOT_API_KEY` | 兼容档那把主 key。某个用途自己那条 key 有值时就轮不到它，但"只配一把 key"的部署靠它 |
| `QQBOT_API_PROVIDER` | 可选但建议写。说清"主 key 属于哪一家"（`deepseek` / `mimo` / `xkiro`）。不写也能跑，但自检那行会显示 `兼容档·未声明，按…算` |
| `QQBOT_REPLY_API_BASE_URL` | 可选。**只给回复 agent** 的地址；不配回落那条 key 自己的地址 |
| `QQBOT_REPLY_API_MODEL` | 可选。只给回复 agent 的模型名 |
| `QQBOT_REPLY_API_KEY` | 可选。**只给回复 agent** 的 key。**2026-10-16 起回复那一路住在 deepseek**（借 `QQBOT_JUDGE_API_KEY` 那条，见下面"手动切档"），所以这一项**现在闲置**——2026-10-14～10-16 那段它承载的是中转 xkiro 那把 Gemini key；那把 key 与 `model_config.KEYS["reply"]`（provider = `xkiro`）都**保留**，将来切回 Gemini 要用 |
| `QQBOT_JUDGE_API_KEY` | 判定用。**2026-10-22 起 reply / judge 两路借这一条**（都是 `deepseek-flash`）；写信（`letter`）同日改用**自己那条** mimo key（`QQBOT_LETTER_API_KEY`），不再借它 |
| `QQBOT_MEMORY_API_KEY` | 可选，记忆维护用。不配时的顺序：**主 key 没配** → 借同家的 `QQBOT_JUDGE_API_KEY`；**主 key 配了** → 直接用兼容档那一对（值 + 地址一起）。这是 `model_config._key_source` 里那道闸，不是"永远回落主 key" |
| `QQBOT_REVIEW_API_KEY` | 可选，风格审核用；**配了它就自动开**（`QQBOT_STYLE_REVIEW` 可以强行开关）。回落顺序同记忆那行 |
| `QQBOT_VISION_API_KEY` | 可选，识图用；回落顺序同记忆那行 |
| `QQBOT_LETTER_API_KEY` | 写信那一路**自己那条**（mimo）。**2026-10-22 起它就是写信在用的那把**（`TASKS["letter"] = {"key": "letter", "model": "mimo-v2.6-pro"}`），不再闲置。不配时按 `KEYS["letter"]["fallback"]` 借 reply 那条（跨家，风险写在 `model_config` 注释里） |
| `QQBOT_FALLBACK_API_KEY` | 可选，兜底那一家专用 key；不配就沿回落链借同家那条（本机落到写信那把 mimo key） |

**每个用途都能单独换地址与模型名**（键名就是用途名大写；两张表的形状不一样，别猜）：

| 用途 | 地址覆盖 | 模型名覆盖 |
| --- | --- | --- |
| reply | `QQBOT_REPLY_API_BASE_URL` | `QQBOT_REPLY_API_MODEL` |
| letter | `QQBOT_LETTER_API_BASE_URL` | `QQBOT_LETTER_API_MODEL`（老名字 `QQBOT_MAIL_MODEL` 仍然认，排在新名字之后） |
| judge | `QQBOT_JUDGE_API_BASE_URL` | `QQBOT_JUDGE_MODEL` |
| memory | `QQBOT_MEMORY_API_BASE_URL` | `QQBOT_MEMORY_MODEL` |
| review | `QQBOT_REVIEW_API_BASE_URL` | `QQBOT_REVIEW_MODEL` |
| vision | `QQBOT_VISION_API_BASE_URL` | `QQBOT_VISION_MODEL` |

⚠️ judge / memory / review / vision 的**模型**覆盖键**不带 `_API_`**（是 `QQBOT_JUDGE_MODEL`，
不是 `QQBOT_JUDGE_API_MODEL`），而**地址**覆盖键带。这不是笔误，是
`model_config.MODEL_ENV` / `BASE_URL_ENV` 两张表各自的形状。写错的那个键既不报错也不生效。

**每个 agent 各用各的地址**：`QQBOT_REPLY_API_*` 只影响回复那一路（含按会话新建的
client）；判定 / 记忆 / 审核 / 识图 / 写信各有自己那条 key，地址跟着**那条 key 绑的
供应商**走。这几个键在面板上也是 live 的，留空 = 清掉覆盖、回落该用途自己的来源。

⚠️ **"换家"不是只填三个键**（2026-10-14 与 2026-10-16 各踩过一次"换了一半"）：
`QQBOT_<用途>_API_KEY` 只有在 `model_config.TASKS[用途]["key"]` 指向**该用途自己那条
key 条目**时才真的生效。2026-10-16（用户原话："切换回ds。gemini好蠢。"，同日早先
那句是"都改ds"）之后 `TASKS` 是这样：

| 用途 | `TASKS` 那一行 | key 从哪来 | 地址 / 模型 |
| --- | --- | --- | --- |
| reply | `{"key": "judge", "model": "deepseek-flash"}` | `QQBOT_JUDGE_API_KEY` | `deepseek` / `deepseek-flash` |
| judge | `{"key": "judge", "model": "deepseek-flash"}` | `QQBOT_JUDGE_API_KEY` | `deepseek` / `deepseek-flash` |
| letter | `{"key": "letter", "model": "mimo-v2.6-pro"}` | `QQBOT_LETTER_API_KEY`（mimo 那把；不配才按 `KEYS["letter"]["fallback"]` 借 reply 那条） | `mimo` / `mimo-v2.6-pro` |
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

切到中继时**当年**会丢掉 `reasoning_effort`（2026-10-16 那条依据：`deepseek_fields` 只在
`provider ∈ {"", "deepseek"}` 时才发）。**这条依据 2026-10-20 起已经不成立了**：那道白名单
换成了黑名单 `REASONING_EFFORT_INCOMPATIBLE_PROVIDERS`（默认空集 = 对所有家都发，mimo 实测
收下不 400），另有按用途的强制开关 `QQBOT_<TASK>_SEND_REASONING_EFFORT=0/1`。所以 2026-10-16
回退的两条依据里**只剩一条还成立**：当时中继的 Google 上游整段 503（`/v1/models` 通、
`chat/completions` 全 503，25 分钟探针全挂）。

⚠️ **两个坑，都在 `reasoning_effort` 上**

**坑一：变量名是直接拿 `usage_role` 拼的，中间没有映射表。**
`llm_client._complete_sync` 里那句是 `reasoning_effort(self.usage_role)`，展开就是
`QQBOT_<USAGE_ROLE>_REASONING_EFFORT`。而回复那一路的 `usage_role` 历史名是
**`dialogue`**（那张对照表见 `runtime._TASK_TO_ROLE`），所以真正要写的是：

| 想改哪一路 | **真正生效的键** | 写了没用（死键） |
| --- | --- | --- |
| 回复（`usage_role=dialogue`） | `QQBOT_DIALOGUE_REASONING_EFFORT` | `QQBOT_REPLY_REASONING_EFFORT` |
| 判定（`judge`） | `QQBOT_JUDGE_REASONING_EFFORT` | —— |
| 写信（`letter`） | `QQBOT_LETTER_REASONING_EFFORT` | `QQBOT_MAIL_REASONING_EFFORT` |
| 记忆（`memory`） | `QQBOT_MEMORY_REASONING_EFFORT` | —— |
| 风格审核（`review`） | `QQBOT_REVIEW_REASONING_EFFORT` | —— |
| 识图（`vision`） | `QQBOT_VISION_REASONING_EFFORT` | —— |

**这六个键代码里都真的在读**（2026-10-18 逐个核过：写信那一路的 `usage_role` 是
`letter`，所以 `QQBOT_LETTER_REASONING_EFFORT` 有效，不是死键）。不配就走 **`low`**；
可选值 `none` / `low` / `high` / `max`，`minimal` 当成 `low`、`medium`/`xhigh` 当成 `high`，
其它值打一条 warning 并回落 `low`。

对比一下**采样参数那一处是有映射的**：`_sampling_params_for` 先过
`_SAMPLING_ROLE_TO_TASK = {"dialogue": "reply"}`，再去读 `QQBOT_REPLY_TEMPERATURE`
那一族。两套名字不一致是历史遗留（回复的账本角色叫 dialogue、配置键叫 reply），
不是笔误。**写错的那个键既不报错也不打日志，只是永远不生效**——动它之前先确认键名。

**坑二：发不发由"黑名单 + 按用途开关"决定，不再看 provider 是不是 deepseek。**
（2026-10-20 改的，原来这里写"只在 `provider ∈ {"", "deepseek"}` 时才发得出去"，**已作废**。）
`llm_client._send_reasoning_effort(task, provider)` 的顺序是：先看
`QQBOT_<TASK>_SEND_REASONING_EFFORT` 是不是 `0`/`1`（按用途强制），没配再看 provider 在不在
`REASONING_EFFORT_INCOMPATIBLE_PROVIDERS`（**默认空集**）。所以 mimo 这类中继现在**也会**收到
`reasoning_effort`；真要关某一家，把名字加进那个集合，别指望代码替你按家静默丢弃。
当年 2026-10-16 从 Gemini 切回 deepseek 的另一条依据（中继的 Google 上游整段 503：
`/v1/models` 通、`chat/completions` 全 503，25 分钟探针全挂）**仍然成立**。

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
| `QQBOT_MEMORY_MODEL` | 空 | 记忆那一路的模型名。不写 → 回落兼容档 `QQBOT_API_MODEL` → 再没有就用 `TASKS["memory"]` 里的真名（现在是 `deepseek-flash`） |

记忆库在 `data/memory/memory.sqlite3`。**删记忆等于删她的经历**，动手前先备份。
关系两轴的判据与上限见 [`AFFINITY_DESIGN.md`](AFFINITY_DESIGN.md)；记忆怎么跟人对应见
[`MEMORY_PEOPLE.md`](MEMORY_PEOPLE.md)。

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
| `QQBOT_LOG_ARCHIVE_DAYS` | `30` | 活日志按条轮转时被裁掉的旧行**不再丢弃**，而是追加到同目录下的 `archive/<label>-YYYYMMDD.jsonl`（按归档那一刻的本地日期分文件），这里控制归档保留多少天。**所有按条滚动的日志共用这一份实现**（模型日志 judge/reply/memory/security/mail、对话日志 chat、原始事件 raw_events、金句与黑话）。范围夹在 0..3650，坏值回落 30；`0` = **不归档**（回到"裁掉就丢"的旧行为）。清理**按归档文件名里的日期**严格保留最近 N 个自然日（含今天），不看 mtime；解析不出日期的文件不删 |
| `QQBOT_CHAT_LOG_DIR` | 空 | 空 = 与模型日志同一个 `data/logs/`；要单独搬走就写路径 |
| `QQBOT_RAW_EVENTS` | 开启 | **原始事件日志**（`data/logs/raw_events.jsonl`）：**没被处理**的入站事件（通知/请求/其它 post_type）原样落盘。只为查"NapCat 到底推不推某种事件"（如表情回应）。`0` 关掉整份 |
| `QQBOT_RAW_EVENTS_MAX` | `5000` | 原始事件日志保留多少条（按条滚动）。范围兜在 10..1000000，**不会无限增长** |
| `QQBOT_RAW_EVENTS_DIR` | 空 | 空 = 与另外两份日志同一个 `data/logs/`；要单独搬走就写路径 |

**调试用（生产建议保持注释掉）**：`QQBOT_DEBUG_MODEL_IO=1` 会把模型的完整
输入输出（含 system prompt 与聊天正文）落盘到 `data/traces/`。

### `data/` 里到底有什么

下面这张表是照**线上那一份**列的（2026-10-18 看 `run/data/`）。它不在 git 里，
所以"能不能删"是要紧事。

| 路径 | 是什么 | 能删吗 |
| --- | --- | --- |
| `memory/memory.sqlite3` | 记忆库：两层记忆、归档表、墓碑、关系档位与流水 | **不能**（删了等于删她的经历） |
| `knowledge/knowledge.sqlite3` | 知识库（按词检索）+ 面板改过的版本 | 不能 |
| `logs/chat.jsonl` | 实际收发（拆开的每一段各一条） | 不建议 |
| `logs/reply.jsonl` / `judge.jsonl` / `memory.jsonl` | 三路模型各自的输入输出 | 不建议 |
| `logs/mail.jsonl` / `quote.jsonl` / `slang.jsonl` / `security.jsonl` | 功能日志：写信每一步、金句学习、黑话、安全拦截 | 不建议 |
| `logs/raw_events.jsonl` | 核心没认出来的入站事件，原样落盘 | 可以 |
| `logs/verify_runs.log` / `verify_last.txt` | 离线测试的留痕（一行一次 / 最近一次的完整输出） | 可以 |
| `runtime_state.json` | 运行状态：会话、群启停、管理员、记忆开关、焦点队列 | **不能**（删了要重新配） |
| `api_usage.json` | 用量账本（跨重启累计） | 可以（账就断了） |
| `operator_config.json` | **面板 / 运行期改的配置覆盖层**（可能含凭据，见第 1 节） | 不能 |
| `mail_state.json` | 邮件通道的状态（上次汇报时间、没送出去的那封、回信计数、发过的信） | 不能（见下面"补发"） |
| `mail_outbox/` | 发信时给命令行工具读的正文文件（信是写给 CLI 的，不走 SMTP） | 可以 |
| `quote/profile.json` | 金句学习：笔记 + 按群的表情含义表 + 操作者覆盖 | 删了只是重新学 |
| `slang/entries.json` | 黑话词条 + 原话证据 | **删了就没了**（学不回来） |
| `remind/` | 到点提醒的 jsonl（见下；线上现在只有 people / reminders 两份，邮件那本的账还没生成） | 删了就没了 |
| `prompts/versions/` | 面板改过 / 回滚过的 prompt 版本 | 不能（回滚要用） |
| `private_docs/` | **不进公开仓库的私有材料**：真人设、事实表、写信人格、几份私有文档 | **不能** |
| `traces/` | 调试追踪（只有开了 `QQBOT_DEBUG_MODEL_IO` 才有内容） | 可以 |
| `webui_token` / `webui_password` | 面板凭据（**这两样都是凭据**） | 能删，会重新生成 |
| `models/` | 本地下载的向量 / 重排模型（bge 那几套） | 不能（要重新下） |

**最后那一列"能删"只在"你知道自己在干什么"时成立**：`/data/` 整个目录在 `.gitignore` 里
（锚定仓库根），删了没有任何 git 副本。`private_docs/` 与 `.env` 一样属于"删了就真没了"。

### 邮件通道（插件 `plugins/mail/`，另带两个后置插件；发信能力自 2026-10-22 M1 起在核心）

> M1 之后：核心 `qq_roleplay_bot/mail/` 在 `runtime.build_engine()` 里、`attach_plugins()` **之前**
> 提供"给操作者发一封普通邮件"与"寄给指定地址"两条窄口（`mail.provide_senders()`），
> 插件再提供时是 no-op。写信 / 日报 / 收信 / 邮箱状态仍在插件线 `plugins/mail/`。
> 下面这些键**照旧全在插件或核心两侧读取**，一个都没改名。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_MAIL_REPLY` | `1` | `0` = 不读信、不回信 |
| `QQBOT_MAIL_REPORT` | `1` | `0` = 关掉"每晚一封信"整条路 |
| `QQBOT_MAIL_REPORT_AT` | `23:00` | 每晚几点发（按 `QQBOT_LOCAL_TZ_OFFSET_HOURS` 那个时区；窗口是"距上次满 24 小时"，不是日历日） |
| `QQBOT_MAIL_REPORT_TO` | **空（源码里没有默认收件人）** | **收件人常量，不从对话 / 记忆 / 邮件内容里取**。换机器必须改这一行；**不填就不发信**（写信 / 日报都发不出去，启动自检那一行带 `**缺**`） |
| `QQBOT_MAIL_OWNER_FROM` | 回落上面那个 | 她只认这个地址的信是"主人写的"，其余按陌生人走判定 |
| `QQBOT_MAIL_SENDERS` | `any` | `any` = 谁都读、判定决定回不回；`owner` = 只读主人的；也可以给地址名单 |
| `QQBOT_MAIL_MAX_REPLIES_PER_SENDER` | `3` | 同一天对同一个人最多回几封（防自动回复来回刷） |
| `QQBOT_MAIL_MAX_REPLIES_PER_DAY` | `5` | 一天总共最多回几封 |
| `QQBOT_MAIL_MAX_AGE_HOURS` | `48` | 太旧的来信不回 |
| `QQBOT_MAIL_POLL_SECONDS` | `300` | 多久看一次信箱 |
| `QQBOT_MAIL_REPORT_MAX_CHARS` | `1500` | 信正文上限（比 CLI 自己的 1 MB 严得多） |
| `QQBOT_MAIL_CLI` | `agently-cli` | 发信走这个命令行工具；换机器改这里 |
| `QQBOT_MAIL_SELF_FROM` | 空 | 她自己的邮箱地址。填了就**绝不回自己的地址**——那是死循环 |
| `QQBOT_MAIL_OUTAGE_NOTICE` | `1` | 掉线通知：断一次发一封固定模板的信 |
| `QQBOT_MAIL_TIMEOUT_ALERT` | `1` | 超时报警总开关 |
| `QQBOT_MAIL_TIMEOUT_ALERT_WINDOW` / `_TIMES` / `_COOLDOWN` / `_POLL` | `300` / `5` / `1800` / `60` | 5 分钟够 5 次超时就写一封；写完 30 分钟内不再写；每 60 秒看一次 |

> 后两个插件（`outage_notice` / `timeout_alert`）**只报事实**：不调模型、不往群里发消息，
> 信里的话是手写定稿。它们的 `REQUIRES = ("mail",)` **声明没改、判定改了**（2026-10-22 M1）：
> `discover()._load` 先问 `PluginRegistry.has_capability("mail")`，为真直接放行（**删掉
> `plugins/mail/` 也装得上**），为假才退回"装同名前置插件"，装不上跳过自己（fail-closed）。

**"发失败会存着、下一次先补发"到底怎么走**（读 `daily_report.py` 核过，2026-10-18）：

1. 信写好之后**先存再发**（`_stash_pending`）：`data/mail_state.json` 里记下这一天、主题、正文。
   顺序是故意的——存下来再发，"发信那一刻断电或发送失败"都不会把信弄丢。
2. 存之前主题先洗一遍，存的就是**要发的那一份**，与真正发出去的逐字一致。
3. 真正发（`_send_draft`）：成功 → 清掉 pending、把信留一份、并告诉引擎"她刚发了什么"；
   失败 → 记一笔 `ok=False`，**那份 pending 留着**。
4. 下一次跑（`run_once`）**第一件事**就是看有没有"新鲜"的 pending：
   - 有 → `_resend_pending`：**原样再发**。不重新收集素材、不叫模型——同一段日子不该被她写两遍；
   - 没有、但有一份**过期**的（写出超过 3 天，`mail_state.PENDING_REPORT_MAX_AGE`）→ 丢掉重写。
5. **只留一封**：新的覆盖旧的。连着几天都没发出去，最后只剩最近那一封。

所以"掉线一晚上、第二天补发"是成立的，前提是 `mail_state.json` 还在、`QQBOT_MAIL_REPORT` 没关、
bot 又跑起来了。想看它有没有攒着：`/super mail` —— 里面 `pending_report` 那一栏就是
"写好了还没送出去的那封"（只报日子与主题，**不把正文搬进出站消息**）。

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
| `QQBOT_PRIVATE_REPLY_ALL` | 默认 `0`（关）。设 `1` 后**任何人**的私聊都进普通对话流程；是否回复仍由判定决定（不是"一定回"） |
| `QQBOT_BALANCE_PRIVATE_USER_IDS` | `/balance` 私聊白名单；不配取超管 |
| `QQBOT_BALANCE_GROUP_IDS` | 允许在哪些群查余额；**留空 = 群里不可用**（fail-closed） |

> ### ⚠️ 超管号**必须写 `.env`，不要写进代码文件**
>
> 代码文件（`src/qq_roleplay_bot/dev_config.py`）是**被 git 跟踪**的。
> 把真实号写在里面，只是一处未提交的本地修改——**一次 `git checkout` 就会把它
> 还原成占位号 `900000001`，超管当场失去全部权限，而且没有任何告警**（真实发生过）。
> 写在 `.env` 里则换分支、重装都冲不掉。

### 私聊申请制（默认做法，2026-10-25）

私聊默认**不是全开**：名单外的人只能走申请，超管点头后才放行。

1. 他先在**群里**发 `/yunru private`（别名 `/yunru 私聊` / `/yunru 私信`；`#`、裸 `yunru` 也认）。
   在私聊里发这条只会被回一句"在群里发"。
2. 这条命令只**登记待批申请**，不会直接开通；每个群只保留**最近一条**，30 分钟内有效。
3. 超管回那条消息发 `/super enable`（新写法 `/super private enable`）→ 申请人立刻进白名单，
   回复"已开通"。
4. 放行名单写在 `data/private_allow.json`（`dev_config.private_allow_file()`），
   每条只有 `user_id` / `by` / `at`，**不存聊天内容**；重启时自动读回。

**备用"全开"开关**：`QQBOT_PRIVATE_REPLY_ALL=1`（不设 = 关）。它让**所有人**的私聊都进
普通对话流程，绕过申请制；只有明确要全开时才打开，平时不要用。
名单外私聊默认仍然"进记忆、不回、不进会话历史、不调模型"（fail-closed）。

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

三层：公开（`/help`、`ping`）、管理员（`/admin …`）、超管（`/super …`）。
**权限判定在核心**，失败时**静默**（不回复、不报错、不暴露层级存在）。
完整菜单以 `/super help` / `/admin help` 发出来的那份为准；下面是**今天真的有**的那些
（对着 `memory_view.SUPER_HELP` 与插件里登记的命令核过，2026-10-18）。

| 命令 | 谁能用 | 干什么 |
| --- | --- | --- |
| `ping` / `help` | 公开 | `/help` 会画成图片（渲染失败退回文字） |
| `/admin help` | 管理员 | 开关本群对话、`/admin status`、`/admin clear`、`/admin echo`、转达 |
| `/balance` | 超管私聊 | 账户余额。群里默认**不可用**（要显式配 `QQBOT_BALANCE_GROUP_IDS`） |
| `/提醒` | 公开 | 到点提醒（插件）：`/提醒` 看还有哪些、`/提醒 删 2`、`/提醒 换 2` 换"以后叫我"的方式（群里 @ / 私聊 / 邮箱）。**"8 点提醒我交作业"那句是对话调起的**，这条命令只用来**看和改** |
| `/title 头衔` | 公开 | 她给自己设群头衔（**要在她当群主的群**才改得动）。别名 `#title` / `/yunru title` |
| `/yunru affinity` / `/yunru profile` | 公开 | 只看**你自己**的关系 / 人物画像（无需超管；命令里带 @ 或 QQ 号会被拒绝） |

`/super` 这一层（**只有超管**）：

| 子命令 | 干什么 |
| --- | --- |
| `/super help` | 打印完整菜单 |
| `/super status` | 本次重启后的概览：运行时长、内存、计数、连续失败、最近日志 |
| `/super apicheck` | 按 agent 对账：回复/判定/记忆/审核/识图/写信的命中率与余额 |
| `/super mail` | 邮箱与汇报状态：收件人、发送时间、上次/下次、今天还能重试几次、**攒着没发的那封** |
| `/super memory enable` / `memory disable` | **本群**记忆开关（在哪个群说就动哪个群；与 `/admin` 的对话开关是两张名单）。旧写法 `/super enable memory` 仍兼容 |
| `/yunru private` → `/super private enable` | 私聊申请制：他先在群里发 `/yunru private`，超管再发这一条放行（旧写法 `/super enable` 仍兼容） |
| `/super memory` + `records` / `inbox` / `audit` / `archive` | 记忆库概览与明细、待处理材料、维护流水、已删归档（保留 30 天） |
| `/super quote` | 金句学习学出来的**表情含义表 + 老习惯**；`/super quote <表情id> <方向> [说明]` 人工纠正、`auto` 撤销、`note <id> off\|on` 停用一条笔记 |
| `/super slang` | 她**被动听来的黑话词条**；`why <词>` 看原话证据、`set` / `del` / `wrong` 改删标错、`all` 看所有群 |
| `/super affinity @某人 [reset]` | 关系两轴现状（按回默认档） |
| `/super profile @某人` | 她对那个人的完整人物画像（不带人 = 你自己） |
| `/super addadmin @某人` / `/super deladmin @某人` | 加/撤**本群**管理员（也接受显式群号） |
| `/super admin list` | 管理员名单：超管全局，管理员按群列 |
| `/super processes [memory\|cpu\|gpu\|gpu-memory]` / `/super lan` / `/super fan` | 本机诊断（没暴露的会直说"采不到"） |
| `/super restart` | 重启 bot（几秒后回来）。**改配置要生效靠它** |
| `/super permit` | **引用**一条被拒的 admin 命令再发这句，替他执行那一条（只一次） |
| `/super kick` / `ban` / `unban` / `mute` / `unmute` / `recall` | 群管理（踢、禁言、全员禁言、撤回），都必须在本群发 |
| `/super qqadmin` / `unqqadmin` / `card` / `groupname` / `title` / `notice` | **群主专属**：她自己在那个群是群主才做得成 |

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
| 某一路模型全 401 / 报"没有可用的 key" | 看启动时那六行 `模型配置 …`：地址永远跟着 key 那条绑的供应商走，所以 `provider=` 与 `key=` 必须同家。哪一行是 `**缺**` 就是那一路没 key |
| 改了 `.env` 没反应 | 大部分键是**启动时读一次的模块常量**，要重启才生效；少数几个是**每次调用现读**（`reasoning_effort`、采样参数、各用途 `_TIMEOUT`、`QQBOT_TURN_TIMEOUT_SECONDS`、判定采样那几档阈值），这些不用重启。拿不准就重启 |
| 换了中转 / 别家之后"思考深度配了没效果" | 看第 2 节"坑二"：`reasoning_effort` 只在 `provider ∈ {"", "deepseek"}` 时才发出去 |
| 改了插件文件，重启后行为没变 | 插件不在部署脚本的白名单里，要**单独把那个插件目录拷进 `run/src/qq_roleplay_bot/plugins/`**。见 [`DEPLOY.md`](DEPLOY.md) 第 4 节 |

---

## 11. 安全清单

- `.env` / `.env.bot` **永不提交**（已在 `.gitignore`）
- 生产环境**不要**开 `QQBOT_DEBUG_MODEL_IO`
- `data/`（含记忆库、面板令牌、`operator_config.json`）、`backups/`、`.env`、
  `docs/yunru-source/`、`docs/YUNRU_FACTS.md` **都不在 git 里**——删了不可恢复
- 公开仓库里的示例一律用假号（`900000001`）；**别把真实号、真实群号写进代码或文档**
- `data/private_docs/` 里是**不进公开仓库**的私有材料（真人设、事实表、写信人格）——
  整目录已在 `.gitignore` 的 `/data/` 里，别为了"备份方便"把它挪到别处
