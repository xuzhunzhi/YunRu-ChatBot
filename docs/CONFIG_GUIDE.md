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
| `QQBOT_JUDGE_API_KEY` | 可选，判定用；不配回落主 key |
| `QQBOT_MEMORY_API_KEY` | 可选，记忆维护用；不配回落主 key |

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
| `QQBOT_MEMORY_MODEL` | 空 | 不写就复用 `QQBOT_API_MODEL` |

记忆库在 `data/memory/memory.sqlite3`。**删记忆等于删她的经历**，动手前先备份。

---

## 5. 行为开关

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_GROUP_LISTEN` | `1` | `1` = 群里每条消息都送进模型判断，她能看见别人之间的对话；`0` = 只跟对话对象说话 |
| `QQBOT_TYPING_SIM` | `1` | `1` = 先亮"正在输入"、按字数停顿、长回复拆成几条；`0` = 立刻一次性发出 |
| `QQBOT_VISION` | `1` | 识图：有图的消息先看一眼再判定 |
| `QQBOT_STYLE_REVIEW` | 跟随 | 回复出口前的窄职责校对；配了审核 key 时默认开 |
| `QQBOT_DUAL_AGENT` | `1` | 判定与回复分离；判定不接就不发生回复调用 |

---

## 6. 运行数据与状态

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `QQBOT_DATA_DIR` | 项目下 `data/` | 所有运行数据、状态、日志、记忆的根目录 |
| `QQBOT_STATE_PERSIST` | 开启 | `0` 关闭运行状态持久化 |
| `QQBOT_STATE_FILE` | 由上面推导 | 指定 `runtime_state.json` 的完整路径 |
| `QQBOT_CHAT_LOG` | 开启 | **对话日志**（实际收发，`data/logs/chat.jsonl`）。`0` 关掉整份 |
| `QQBOT_CHAT_LOG_MAX` | `20000` | 对话日志保留多少条（按条滚动，不是按天）。范围兜在 10..1000000，**不会无限增长** |
| `QQBOT_CHAT_LOG_DIR` | 空 | 空 = 与模型日志同一个 `data/logs/`；要单独搬走就写路径 |

**调试用（生产建议保持注释掉）**：`QQBOT_DEBUG_MODEL_IO=1` 会把模型的完整
输入输出（含 system prompt 与聊天正文）落盘到 `data/traces/`。

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

---

## 11. 安全清单

- `.env` / `.env.bot` **永不提交**（已在 `.gitignore`）
- 生产环境**不要**开 `QQBOT_DEBUG_MODEL_IO`
- `data/`（含记忆库）、`backups/`、`.env` **都不在 git 里**——删了不可恢复
- 公开仓库里的示例一律用假号（`900000001`）；**别把真实号、真实群号写进代码或文档**
