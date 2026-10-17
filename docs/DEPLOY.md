# 部署与重启（Windows，本机这套跑法）

> 这份写的是**这台机器上**的三目录做法：源码在 `dev/`，插件在 `yunru-stage4/`，
> 线上跑在 `run/`。
>
> 凡是数字都标了日期和取数口；**没核到的会直说没核到**，别当承诺看。
> 配置项本身见 [`CONFIG_GUIDE.md`](CONFIG_GUIDE.md)。

---

## 0. 一句话

**部署 = 把 `dev/src` 拷到 `run/src`。插件不在里面。拷完不重启，等于没改。**

> 下文命令**默认都在那三个目录的父目录（项目根）里跑**，只有标了 `cd dev` 的那几条要在
> `dev/` 里跑。路径都写成 `dev\…` / `run\…` 这样的相对形式。

---

## 1. 三个目录是什么关系

| 目录 | 是什么 | 有 `.git` 吗 | 谁写它 |
| --- | --- | :-: | --- |
| `dev/` | 工作树根：源码、测试、文档。分支 `main` | ✅ **仓库根在这里** | 人 / AI 在这里改 |
| `yunru-stage4/` | 第二个工作树（worktree）。分支 `plugins`，装的是插件实现（`src/qq_roleplay_bot/plugins/`） | ✅ 同一个仓库的 worktree | 插件相关改动 |
| `run/` | 线上跑的那一份：`src/`、自己的 `.env`、`data/`、`backups/`、四个启动脚本 | ❌ 不是仓库 | 只由部署与运行写 |

*`run/` 底下还有 `.env.bak-*` 这类**备份过的旧配置**，那是历史文件，删不删都行——但删之前先问用户。*

三个目录**平级**放着。仓库根的 `git worktree list`（在 `dev/` 里跑）长这样：

```powershell
git worktree list
# <根>/dev          <sha> [main]      ← 仓库根
# <根>/yunru-stage4  <sha> [plugins]   ← 第二个工作树
```

### 三件容易搞混的事

1. **`.venv` 只有一个**（在顶层），`dev/.venv` 与 `run/.venv` 都是**指向它的 junction**。
   所以两边共用同一套解释器与已装的包——装一次两边都有。
2. **那个 venv 的 editable 安装把包路径钉在 `run/src` 上**（site-packages 里那条
   `__editable__.qq_roleplay_bot-*.pth`，内容就是 `run\src` 的路径）。
   于是在 `dev/` 里**裸敲** `python -m qq_roleplay_bot.stage3_main` 会 import 到
   **线上那份代码**。`run_stage3.bat` 每次都显式
   `set "PYTHONPATH=%~dp0src"`，就是为了压住它——**别删那两行**。
3. **`run/data/` 才是真数据**（记忆库、日志、面板令牌）。`dev/data/` 是开发与测试留下的
   东西，两边不是一份；顶层那个 `data/`（跟三个目录平级）又**不是** bot 的数据目录，
   它只放看护脚本与自己那份日志。

---

## 2. 首次部署（`run/` 还不存在的时候）

`deploy_to_run.py` **不会替你造 `run/`**：它一开头就检查 `../run` 在不在，不在就打印
`找不到 …run/——run/ 还没建好？` 并**退出码 2**。所以第一次是手工建：

```powershell
# 1) 建目录
New-Item -ItemType Directory -Force run

# 2) 代码与启动脚本
Copy-Item -Recurse dev\src run\src
Copy-Item dev\run_stage3.bat, dev\run_yunru_hidden.vbs, dev\watch_memory.py, dev\watch_round2.py run\

# 3) 共用 venv（junction，不复制那几千个文件）
New-Item -ItemType Junction -Path run\.venv -Target (Get-Item .venv).FullName

# 4) 配置：run/ 有自己的一份，不从 dev 拷
Copy-Item .env.example run\.env
#    然后编辑 run\.env（真实凭据只写这里）

# 5) 插件：从插件树按目录拷（见第 4 节），不要整树拷
# 6) 用部署脚本对一遍（只报告，不写任何东西）
.\.venv\Scripts\python.exe dev\deploy_to_run.py --dry-run
```

> 第 2、3、5 步是**按 `run/` 现在的形状反推**的（今天 `run/` 就是这样：`src/`、`.env`、
> `data/`、`backups/`、四个脚本、`.venv` 是 junction）。
> **我没有从零跑过一遍首次部署**——`run/` 已经在了，跑首次部署等于动线上，没做。

`run/` 建好之后，日常只走第 3 节那一套。

---

## 3. 日常更新：改了什么 → 拷哪一步

| 你改的是 | 拷哪一步 | 要重启吗 |
| --- | --- | :-: |
| `dev/src/**`（核心、prompt、核心侧的提醒 / 金句 / 黑话） | `python dev\deploy_to_run.py` | 要 |
| `dev/` 的那四个启动脚本 | 同上（脚本会一起拷） | 要（下一次启动才用上） |
| 插件：`yunru-stage4/src/qq_roleplay_bot/plugins/<名字>/` | **脚本不管**，手工拷**那一个插件目录** | 要 |
| `run/.env` | 直接改 `run/.env`（脚本**绝不**碰它） | 多数要 —— 哪些键是"每次调用现读"的那几个，见 `CONFIG_GUIDE.md` §10 最后几行 |
| 只改配置、没改代码 | 不用拷 | 视键而定 |

### `deploy_to_run.py` 到底做什么（2026-10-18 把源码读了一遍）

- **白名单**：只拷 `src/`（整目录，逐文件比）与四个文件：`run_stage3.bat`、
  `run_yunru_hidden.vbs`、`watch_memory.py`、`watch_round2.py`。
  刻意是白名单而不是"整个 `dev/`"——黑名单式的同步早晚会把 `tests/`、`docs/`
  甚至 `.env` 一起搬过去。
- **只覆盖变化的文件，一个都不删**：逐字节比（`filecmp`），跳过 `__pycache__`；
  `run/` 里多出来的东西它一律不动。
- **绝不碰** `data`、`backups`、`.env`、`.tmp_test_run`（源码里那张 `PROTECTED` 表）。
- 复制前记下这几样的**文件数**，复制后核对"**只增不减**"。
  **故意用文件数而不是哈希**：bot 正在跑、正在往 `data/logs/*.jsonl` 追加，哈希必变
  ——2026-10-03 实测踩到过：报"生产数据被动过"、退出码 1，而其实一个字节都不是部署改的。
- **打印什么**：每个目录会变几个文件（最多列 12 条，多的写"…… 还有 N 个"）、
  `=== 核对：部署没删掉生产数据里的任何东西`、每个受保护项一行 `[OK] data: N 个文件`，
  最后是 `部署完成；共 N 个文件变化。` 与 `提示：bot 要**重启**才会用上新代码。`
- **退出码**：`0` 成功；`2` 找不到 `run/`；`3` 生产数据的文件数**变少了**
  （此时打印 `!! 有文件消失——立刻停下查原因`，**这是唯一该停手的信号**）。
- **工作区脏不挡部署**：它只是先把 `git status --short` 打出来提醒你"下面这些改动也会
  一起部署过去"，然后**照拷**。`--force` 只是**把这段提醒去掉**，不是"允许部署"的开关。
  这一点最容易看反。

---

## 4. 插件的文件，部署脚本一个都不管

2026-10-18 实测（逐文件比对）：

```
dev/src    83 个文件
run/src   129 个文件
只在 run 里的：46 个文件，全部在 plugins/ 下
只在 dev 里的：0 个
```

也就是 `run/src` 是 `dev/src` 的**超集**：核心那 83 个文件**逐字节相同**，多出来的 46 个
是插件。**插件不在 `dev/src` 里**，所以部署脚本既不会更新它们，也不会删它们。

要更新插件，只能**手工拷那一个插件的目录**：

```powershell
Copy-Item -Recurse -Force `
  yunru-stage4\src\qq_roleplay_bot\plugins\mail `
  run\src\qq_roleplay_bot\plugins\mail
```

**别整棵树拷**：`yunru-stage4` 的核心可能落后于 `main`（两条分支各自往前跑，插件分支只在
合并时同步一次）。2026-10-18 实测 `plugins` 分支**缺 `main` 上的两个核心提交**：

```
git log --oneline plugins..main
# cfae575 写信素材：核心侧补上结构（时间 / 会话 / 引用链 / 带图）
# 6ebed64 模型：reply/judge 从 xkiro 的 Gemini Flash 切回 deepseek
```

也就是说插件树里的 `model_config.py` **还把 reply / judge 指向中继的 Gemini Flash**
（`{"key": "reply", "model": "google/gemini-2.5-flash"}`）。整树拷过去会：
**① 把模型路由退回切之前那一版；② 把写信素材那笔核心改动抹掉。**
拷之前先看一眼：

```powershell
cd dev
git log --oneline main..plugins     # 插件分支多出来的
git log --oneline plugins..main     # 插件分支还缺的（缺的就别整树拷）
```

> `run/src/qq_roleplay_bot/plugins/__init__.py`（发现与装配那一层）**在** `dev/src` 里，
> 所以它**会被部署覆盖**。这是对的——那是两条分支共用的契约，不是某个插件的实现。
> 但它有个前提：插件树与核心要用**同一版契约**。拷完记得重启，并按第 5 节验证
> `后台插件已装配：…` 那一行列出来的插件名对不对。

---

## 5. 停、起、验证（这一段最有用）

### 停

启动脚本与看护脚本用的是**同一条判据**：命令行里含 `qq_roleplay_bot.stage3_main` 的
`python.exe`。

```powershell
# 只是看一眼（只读，不用问谁）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*qq_roleplay_bot.stage3_main*" } |
  Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine
```

**一次启动通常看到两个进程，不是一个**（2026-10-18 实测）：

| 进程 | 是什么 | 端口 8080 在谁身上 |
| --- | --- | --- |
| `…\.venv\Scripts\python.exe` | venv 的**转发壳**（`OriginalFilename=py.exe`，几乎不占 CPU） | ✗ |
| 真正的解释器（`pyvenv.cfg` 里 `home =` 指向的那一个，是上面那个的子进程） | 干活的 | ✅ |

所以：只杀壳，真进程还在；只杀子进程，壳会成孤儿。**按命令行一起停**：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*qq_roleplay_bot.stage3_main*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

**这一步要先问用户**（见第 7 节）。停完立刻确认一下真的没了（同一条查询，应该 0 条）。

### 起

```powershell
schtasks /Run /TN "YunRu-bot-launcher"
```

这个任务的动作是 `wscript.exe "<run 目录>\run_yunru_hidden.vbs"`，起始目录是 `run\`。
那个 VBS 只干三件事：

1. **先查有没有实例在跑**（命令行含 `qq_roleplay_bot.stage3_main`），有就**安静退出**。
   这一步不能省：正在跑的实例占着 `bot.err.log`，第二个实例连日志都打不开，于是
   既不启动也不报错，看起来像"自启动没生效"。
2. 用**隐藏窗口**跑 `cmd /c`：`cd` 到脚本所在目录，显式
   `set PYTHONPATH=<该目录>\src`、`set PYTHONIOENCODING=utf-8`。
3. 把 stdout / stderr **追加**到 `run\data\bot.out.log` / `run\data\bot.err.log`。

所以它不一定要走任务计划：`wscript.exe run\run_yunru_hidden.vbs` 等价。
走任务计划的好处是**父进程是任务计划服务**，不会被某个终端的进程树清理带走。

### 起来了看什么

日志是 `run\data\bot.err.log`（它在**追加**，用 `-Tail` 看尾巴）。按这个顺序核：

| 看到这行 | 意思 |
| --- | --- |
| `模型配置 reply: provider=deepseek model=deepseek-flash key=… 有值`（六个用途各一行） | 模型与凭据解析对了。写 `**缺**` 的那一路没有可用 key |
| `运行状态已恢复：groups=… sessions=… admins=… enabled=…` | 状态文件读回来了 |
| `后台插件已装配：group_admin、join_approval、mail、outage_notice、remind、timeout_alert、vision、webui` | **插件都装上了**——核实"插件拷对了没有"最直接的就是这一行 |
| `server listening on 127.0.0.1:8080` + `OneBot 反向 WebSocket 监听于 ws://127.0.0.1:8080` | 端口起来了 |
| **`OneBot 已连接`** | **NapCat 连上来了**。没有这一行 = 她收不到任何消息，一个字都不会回 |
| `每日汇报已就绪：收件人=… 发送时间=23:00（窗口＝距上次汇报到现在）` | 邮件那一路接上了 |

端口侧面再核一遍（只读）：

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen |
  Select-Object LocalAddress, LocalPort, OwningProcess
```

最后在群里或私聊发一句 **`/super status`**（超管）：能看到"这次重启之后的运行情况 +
累计"。**回得出话才算真活了**——进程在、端口在，但 OneBot 没连上，她一样不出声。

### 必须知道的坑：看护任务会把你停掉的实例拉回来

`YunRu-bot-watchdog` 每 **10 分钟**跑一次（2026-10-14 起）。判据两条，**任一为真就算
"她不在"**：① 没有 `stage3_main` 进程；② 8080 没在听。然后它
`schtasks /Run /TN "YunRu-bot-launcher"`。

它**在跑的时候不写日志**（写的话每 10 分钟一条噪音），只在"拉起"与"异常"时写一行，
位置是 `data\logs\bot_watchdog.log`（**顶层 `data\`**，不是 `run\data\`）。

所以：**停掉之后 10 分钟内必须起来**，否则会被自动拉起。想让她保持停着，得先把
`YunRu-bot-watchdog` 停用（`schtasks /Change /TN "YunRu-bot-watchdog" /DISABLE`）——
这同样是要先问用户的动作。

---

## 6. 出问题怎么回滚

| 层 | 回滚怎么做 | 代价 / 边界 |
| --- | --- | --- |
| 代码（`dev/src` → `run/src`） | 在 `dev/` 里回到旧提交（`git switch <旧 sha>` 或 `git revert`），**再部署一次** | `deploy_to_run.py` **不备份 `run/src`**，它只是覆盖。所以"线上那份之前长什么样"没有本地副本——证据只有 git |
| 插件 | 插件树也是 git（`plugins` 分支），回到旧提交后**重新拷那一个插件目录** | 手工拷过去的文件没有副本，回滚要重拷一次 |
| 数据（`run/data/`、`run/backups/`） | **没有回滚**。这两样都不在 git 里，`backups/` 也只兜住一部分 | 任何"为了排查"去删 / 改 `run/data/` 的动作，一律先问用户 |
| 配置（`run/.env`） | 同目录下有一堆 `.env.bak-*`（历史备份），拷回去再重启 | 备份不是自动的，最新的那份可能不包含你昨天改的东西 |

**现场最快的止血不是删，是停**：先 `Stop-Process`，别再动数据，看 `run\data\bot.err.log`
的尾巴（和 `data\logs\bot_watchdog.log`）再决定。

> **我没有演练过回滚**（会动线上）。上面是照着"代码在 git、数据不在 git"这两条事实推出来的
> 流程，**没有实测**。

---

## 7. 哪些动作必须先问用户

`AGENTS.md` §四 / §五 与 §7.2 定的"两次点头"，这里只列清单：

| 动作 | 为什么必须问 |
| --- | --- |
| 跑 `deploy_to_run.py`（`--dry-run` 除外） | 会改线上代码 |
| 重启：`schtasks /Run /TN "YunRu-bot-launcher"`、`/super restart`、面板上那个重启按钮 | 会中断线上 |
| 杀进程（`Stop-Process`） | 同上；而且看护任务 10 分钟后会再拉起来，你以为停了其实没停 |
| 改 `run/.env` | 真实凭据在那儿；改错就是全线 401 |
| 动 `run/data/`（删日志、删记忆、清 token） | **不可逆**，没有第二份 |
| 停用 / 启用两个计划任务 | 会改变"她挂了会不会被拉起来" |

**不用问的**：`deploy_to_run.py --dry-run`、`schtasks /Query`、`git log` / `git status`、
读日志、`Get-CimInstance` 那条查询。这些全是只读。

---

## 8. 常见坑

1. **插件文件不在部署脚本里。** 脚本只拷 `dev/src`；插件在
   `yunru-stage4/src/.../plugins/`，要单独拷。今天实测 `run/src` 比 `dev/src` 多 46 个文件，
   全在 `plugins/` 下——这 46 个文件**永远不会**被部署脚本更新。
2. **别整树拷插件分支。** 它的核心可能落后于 `main`，整树拷会把核心一起退回去。
3. **`.env` 是四层，不是三层。** 优先级（2026-10-18 读实现核出来的，**不是**照抄注释）：
   `真实环境变量 > data/operator_config.json（面板写的） > .env > 代码默认`。
   覆盖层与 `.env` 都遵守"不覆盖已经存在的键"（`operator_config.py:216`、`dev_config.py:91`），
   而真实环境变量在进程启动时就已在 `os.environ` 里 ⇒ **谁先进 `os.environ` 谁赢**，
   真实环境变量永远最先。覆盖层在 `dev_config.py:113` 注入、`.env` 在 `:114` 读，所以覆盖层压住 `.env`。
   想把某一项退回去：**清掉覆盖层那一项**，别指望在 `.env` 里写一句更大的。
   （`dev_config.py:99-101` 与 `operator_config.py:9` 两处注释把顺序写反了，**以代码为准**。）
4. **`QQBOT_ENV_FILE` 是整体替换，不是合并。** 只写一个 key 会让别的配置全退回默认值。
5. **`run/.env` 与 `dev/.env` 是两份。** 部署脚本**绝不碰** `run/.env`；改线上配置只能改
   `run/.env`，改开发配置改 `dev/.env`，两边不会同步。
6. **任务不是 Enabled 就什么都不会发生。** 两个任务现在都是
   `Scheduled Task State: Enabled`、`Status: Ready`，但都登记成
   **`Logon Mode: Interactive only`**（`LogonType=InteractiveToken`）——
   **只在那个用户登录的会话里跑**，不是"开机就跑"。
   把 `YunRu-bot-watchdog` 改成 `Disabled` = **看护关掉**（她挂了没人拉）。
   把 `YunRu-bot-launcher` 禁用之后 `schtasks /Run` 还起不起得来，**我没试过**；
   要绕开就直跑脚本：`wscript.exe run\run_yunru_hidden.vbs`（等价）。
   ```powershell
   schtasks /Query /TN "YunRu-bot-launcher" /V /FO LIST
   schtasks /Query /TN "YunRu-bot-watchdog" /V /FO LIST
   ```
   重点看 `Scheduled Task State`、`Status`、`Last Run Time`、`Last Result`（**0 才是成功**）、
   `Schedule Type`、`Repeat: Every`。
   ⚠️ `/TN "YunRu*"` 这种**通配不生效**（会报 `ERROR: The system cannot find the file
   specified.`），要一条一条查。
7. **部署完不重启等于没改。** 脚本最后一行也会提醒你。
8. **在 `dev/` 里裸跑 `python -m qq_roleplay_bot.stage3_main` 会用线上那份代码**
   （venv 的 `.pth` 指向 `run/src`）。要跑 dev 的代码就设 `PYTHONPATH=<dev>\src`，
   或者用 `run_stage3.bat`。
9. **`run/` 的 `__pycache__` 不会被清。** 删了源码但留着 `.pyc` 一般没事（按 mtime 判失效），
   但"这个模块到底还在不在"以 `.py` 为准。
10. **`run_yunru_hidden.vbs` 必须保持 CRLF 行尾。** 它是非 Unicode 脚本、`wscript` 按 GBK 读；
    改成 LF 后中文多字节会把换行也吃掉，整个脚本编译不过（报 `Expected statement`）。
    用只写 LF 的编辑器改完，记得转回来。
11. **日志编码别乱动。** `run_stage3.bat` 与 VBS 都显式设了 `PYTHONIOENCODING=utf-8`；
    不设的话 `bot.err.log` 会半截 GBK 半截 UTF-8（2026-10-12 实测）。
12. **部署脚本自己也有历史坑**：它以前对 `data/` 用哈希，bot 一跑就误报（见第 3 节）。
    所以它现在只比**文件数**——"少了文件"才是它唯一会喊停的事。

---

## 9. 两个计划任务各自干什么

以下都是 `schtasks /Query /V /FO LIST` 与 `/XML` 读出来的（2026-10-18）。

| | `YunRu-bot-launcher` | `YunRu-bot-watchdog` |
| --- | --- | --- |
| 动作 | `wscript.exe "<run 目录>\run_yunru_hidden.vbs"` | `wscript.exe "<项目根>\data\bot_watchdog_hidden.vbs"` |
| 起始目录 | `run\` | 项目根 |
| 触发器 | **On demand only**（没有时间触发器，只在 `schtasks /Run` 时跑） | 一次性起点 `2026-10-14 15:00:39`，之后**每 10 分钟**一次 |
| 最终落到哪 | `cmd /c` → `run\.venv\Scripts\python.exe -m qq_roleplay_bot.stage3_main`；日志追加到 `run\data\bot.err.log` | `data\bot_watchdog.cmd` → `data\bot_watchdog.ps1`（有 pwsh 用 pwsh，没有用 powershell 5.1） |
| 登录方式 | `Interactive only`（只在用户登录的会话里跑） | 同 |
| 超时 | 无限制（`Stop Task If Runs X Hours: Disabled`） | `00:05:00` |
| 多实例 | 不适用 | `MultipleInstancesPolicy=IgnoreNew`（上一跑没完不并发） |

`bot_watchdog.ps1` 干的事（读源码核过）：查 `stage3_main` 进程 + 查 8080 是否在听；
**都在就直接退出、不写日志**；不在就 `schtasks /Run /TN "YunRu-bot-launcher"`，等 25 秒
再查一次有没有进程，成功 / 失败各写一行到 `data\logs\bot_watchdog.log`。

> 历史证据（那份日志里）：2026-10-14 有两次"她不在 → 拉起 → `拉起成功：pid=…, …`"，
> 两次都是**两个 pid**——与第 5 节"一次启动两个进程"对得上。
