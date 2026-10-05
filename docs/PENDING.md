# 待办与观察期（活清单）

> **为什么单开一份，而不是接在 `STAGE3_PENDING_DESIGNS.md` 后面**：那份是"三件**设计已定、等排期**"的
> 设计稿（不懂就问 / 问→解释→词条 / 情绪 + 边界待裁 + 验证手法），口径稳定、按设计读。
> 这一份混着**运维事实**（重启、掉线、生产部署状态）、**有截止期的观察期**和**等用户拍板的十几条**——
> 是会过期、会被划掉、会被推翻的东西。混进设计稿会把那篇的"设计"口径搞脏。
> `STAGE3_PENDING_DESIGNS.md` 末尾留了一行指回这里。
>
> **落盘**：2026-10-05（当天与用户的对话 + 当天实测）。
> **口径**：每条都给定位（`文件:行` / 提交 sha / 命令）。
> **"实测"= 我跑过命令或读过代码看到的；"任务书所述"= 给我的原话，我没有独立核对过。**
> 分不清的一律标出来，见 §六。

---

## 一、观察期：从 `2026-10-05 12:10` 起算，三天（→ **2026-10-08 12:10**）

### 1.1 为什么重算（这一段的账）

| 时刻 | 发生了什么 | 证据（实测） |
| --- | --- | --- |
| **04:30:46** | 机器重启 | `Get-CimInstance Win32_OperatingSystem` → `LastBootUpTime = 2026/10/5 4:30:46`。**重启原因是"系统更新"= 任务书所述，我没核** |
| **04:32:40** | 机器人**开机自启成功** | `run/data/bot.err.log:26664` 起一串启动 INFO（`:26674` 监听 8080、`:26676` `Stage 3 started`）；此前的最后一条日志是 `04:28:51` 的 mail 告警（`:26662-26663`）→ 中间是干净的一次重启 |
| **04:33:40 → 11:53:41** | **NapCat 的 OneBot 服务没起来**，她一直没连上 | `run/data/bot.err.log:26683` 起每 10 分钟一条"还没有 OneBot 客户端连上来（60 秒）：消息进不来，她不会说话。"（文案在 `dev/src/qq_roleplay_bot/runtime.py:1294`）；10–11 点这两小时里有 12 条，最后一条 `11:53:41` |
| **04:08:37 → 12:08:22** | **她收不到任何消息**（当天最大的空档） | `run/data/logs/chat.jsonl`（484 条，覆盖 10-05 00:13:24–13:15:32）按 `at` 排序求相邻间隔：最大一段 **479.8 分钟**，其间 `direction=in/out` 都是 **0 条** |
| **12:08:22 / 12:08:42** | 第一条进来的消息 / 她第一条说出去的话 | 同上；12:08:42 的 `out` 是"qwen 是不错，但你这句"对肘是好事"我没跟上——说的哪个肘？" |

**结论**：`04:32 → 12:08` = **7 小时 36 分 ≈ 7.5 小时**，那段的观察数据作废（与任务书一致）。

### 1.2 早先那 4 小时

* 任务书说：`10-04 00:13–04:32` 那 4 小时算有效样本，但**不计入**这次三天。
* **实测**：磁盘上唯一那份对话日志里，最早的记录是 **10-05 00:13:24**（正文 "wife"），
  到 04:08:37 为止——也就是**任务书写的"10-04"在日志里找不到**（"对话日志"这个功能当天
  00:02 才加上，提交 `b5761ce`，所以 10-04 那天根本没有这份日志）。
* → **待核：那 4 小时是 10-04 还是 10-05**（我只能证明 10-05 那段存在）。

### 1.3 起算点本身的 ~1.5 分钟差

任务书给的起算点是 `12:10`（"NapCat 重开、她恢复说话那一刻"）；实测是 **12:08:22 收到第一条、12:08:42 说出第一句**。
差 ~1.5 分钟。三天截止**按 12:10 算 = 10-08 12:10**（偏保守）。

---

## 二、NapCat：QQ 一掉线就只能手动重启（**未来工作项，先立项别动**）

**用户原话**（任务书所述）：*"napcat这边不行我们回头也重写一下，现在qq掉线napcat只能重启，不然没法挂载"*

**症状**：QQ 一掉线（系统更新重启、QQ 崩、登录态失效），**NapCat 只能手动重启才能重新挂载**；
开机自启只拉起机器人，**NapCat 那侧不会自己恢复** → 她静默失联，而这件事**只有看日志才知道**。

**实测能支持到哪一步**：机器人自己活得好好的（看门狗每 10 分钟照打警告，`run/data/bot.err.log:26683` 起），
而 04:33–11:53 的 7 个多小时里**没有任何"连上来了"的迹象**（同 §1.1 的空档），直到 12:08:22 才有消息进来。
→ "它不会自己恢复"这句**实测成立**；"只能靠手动重开 NapCat"这一步是任务书所述（我从日志看不出是谁做的手动动作）。

**两条路**（任务书口径，按此记）：

1. **短期 = 看护**：现在这套是"掉线了发一封邮件告诉你"（§三）。它能**缩短发现时间**，
   但**不能**把连接恢复——恢复仍然要人动手。
2. **长期 = 换实现 / 自己写挂载层**：**大工程，先立项别动**。

---

## 三、掉线通知（已做完的那套）与它的**已知边界**

### 3.1 东西在哪（实测定位）

| 谁 | 位置 |
| --- | --- |
| **本体接缝**（只广播事件） | `dev/src/qq_roleplay_bot/plugins/__init__.py:391` `class LinkSeams`、`:431 on_disconnect(receiver)`、`:632 _add_disconnect_receiver`、`:653 disconnect_receivers` |
| **检测节奏** | `dev/src/qq_roleplay_bot/runtime.py:1241` `CONNECTION_DETECT_EVERY_SECONDS = 60.0`（那条告警日志仍是 `CONNECTION_WARN_EVERY_SECONDS` = 10 分钟，`:1228`） |
| **边沿检测**（"一次只发一次"的全部实现） | `runtime.py:1318±` `_DisconnectNotifier`：迁移表在 `:1324-1331`；广播侧 `:1414` |
| **插件** | `stage4-on-host:src/qq_roleplay_bot/plugins/outage_notice/`（`plugin.py` / `outage_notice.py` / `__init__.py`） |
| **总闸** | `dev/src/qq_roleplay_bot/dev_config.py:209` `DISCONNECT_NOTICE_ENABLED` ← `QQBOT_DISCONNECT_NOTICE`（默认开） |
| **邮件这一层** | `stage4-on-host:src/qq_roleplay_bot/dev_config.py:337` `MAIL_OUTAGE_NOTICE_ENABLED` ← `QQBOT_MAIL_OUTAGE_NOTICE`。**这个开关不在 `dev`（stage3-clean）的 `dev_config` 里**——它随 `e84dc7e` 加在插件分支上（那个提交动了核心文件 `src/qq_roleplay_bot/dev_config.py`，+8 行） |

### 3.2 依赖方向（把话说准，别读反）

`REQUIRES = ("mail",)`（`plugins/outage_notice/plugin.py:37` 附近）的意思是
**mail 是 outage_notice 的"前置提供方"，outage_notice 依赖 mail**：
mail 经 `registry.mail.operator_sender` **提供**"给操作者发一封普通邮件"，
outage_notice **取用**它；mail 那一侧不认识任何使用者的名字。
（任务书里那句"mail 是**依赖方**"照字面容易读反，这里按代码写。）

### 3.3 邮件正文：固定模板、**不调模型**

`plugins/outage_notice/outage_notice.py`：`SUBJECT = "云茹断线了"`（`:39`）、
`NOTICE_TEMPLATE`（`:42-48`）=【时间】【症状（QQ 那侧的连接没上来）】【怎么办（把 NapCat 重开一次）】
+ 结尾"—— 这封是自动发的告警，断线一次只发一封；恢复不发"。`build_notice()` 是纯函数（`:65-68`），
不碰网络、不碰注册表、不调模型。

### 3.4 已知边界（**必须写清，别粉饰**；下面是 `runtime.py:1336-1345` 的原文口径，我核过）

1. **通知是采样出来的，不是事件回调**：看门狗每 `CONNECTION_DETECT_EVERY_SECONDS`（**≤60 秒**）
   看一次 `transport.connected` → 所以"断了"最多**晚一个检测周期**（最坏 60 秒）才被广播。
2. **"断与恢复都挤在同一分钟内"的瞬时抖动看不见、不通知**（两次采样之间断了又连上）。
3. **开机后 60 秒还没连上，按一段掉线广播一次**——**这是有意的**
   （"机器重启后 NapCat 没起来、机器人活着但谁也看不见"正是这条通知最大的价值）。
4. **恢复不发**（"断线一次只发一封"）。

> **要零延迟 / 不漏**：得让传输层在连接状态变化处发事件、由核心订阅
> （原文：在 `_connection_ready.clear()` 那里发事件）。**这次没做，记为"以后可做"**。

### 3.5 ⚠️ 部署状态：**生产那份拷贝里还没有它**（别当成已经在跑）

实测 `run/src`（生产运行目录；`run/run_stage3.bat` 用 `PYTHONPATH=%~dp0src` 跑的就是它）：

* `run/src/qq_roleplay_bot/runtime.py` 里搜不到 `_DisconnectNotifier` / `DISCONNECT_NOTICE` / `on_disconnect`；
* `run/src/qq_roleplay_bot/dev_config.py` 里搜不到那两个开关；
* `run/src/qq_roleplay_bot/plugins/` 下只有 `group_admin` / `join_approval` / `mail` / `roles` / `webui`
  （**没有 `outage_notice`**，也**没有 `vision`**）——与 `run/data/bot.err.log:26673` 那条
  "后台插件已装配：roles，group_admin，join_approval，mail，webui" 对得上；
* `run/src` 那份的最新改动时间：`stage3_main.py` `2026-10-05 00:01`、`runtime.py` `2026-10-04 21:12`。

→ **所以 04:32–12:0x 那次掉线没有发出任何邮件。** "已上线的那套"要分开理解：
代码在 `stage4-on-host` 上做完了（`e84dc7e` / `f9b10a2`），**但没有部署到 `run/`**；
要生效得先走 `deploy_to_run.py` 那一步。

---

## 四、待用户拍板 / 待处理（一条一句，各带定位）

1. **那条 flaky 测试要不要修** —— `tests/test_memory.py:623`
   `MemoryStoreTests::test_real_protected_directory_initializes_and_reopens`
   （worktree 那份在 `:626`）。失败形状：`memory_store.py:318-320` 调 `protect_directory()` →
   `dev/src/qq_roleplay_bot/memory_config.py:56` `next(csv.reader(identity.stdout.splitlines()))[1]`
   → `AttributeError: 'NoneType' object has no attribute 'splitlines'`（`identity.stdout` 是 `None`）。
   **实测到了一次完整 traceback**：`yunru-stage4/data/logs/verify_last.txt`（mtime `2026/10/5 12:29:44`）末尾
   `ERROR: test_real_protected_directory_initializes_and_reopens ... FAILED (errors=1, skipped=4)`，
   与 `yunru-stage4/data/logs/verify_runs.log` 的
   `2026-10-05T12:29:44+08:00 label=offline sha=69ea9c7 branch=stage4-on-host dirty=yes ran=943 failed=0 errors=1 skipped=4 result=FAIL` 一次运行对应。
   **同一份代码在我这边单独跑 10 次全 PASS**（`dev`，`MemoryStoreTests('test_real_protected_directory_initializes_and_reopens')` 跑 10 遍，
   每次 `errors=0 failures=0`）→ 同一提交不同次跑结果不同，坐实。
   `identity.stdout` 为什么会是 `None` **我没有查出来**（`dev/tests` 里没有任何用例全局 patch `subprocess`）。
   **→ "全绿"当门槛之前必须先修这条**（任务书口径）。

2. **新插件分支的核心 = `main`**（`da5e345`），**不搬旧 `stage4-plugins` 线的核心改动**
   （那条线带被禁的 `judge_hints` / `plugin_hints`）——已按此执行，**待用户确认**。
   实测：`git merge-base da5e345 stage4-on-host` = `da5e345`；`git merge-base --is-ancestor stage4-plugins stage4-on-host` **退出码 1**（不是祖先）；
   `git grep -c "judge_hints\|plugin_hints" stage4-plugins -- src` 命中
   `dialogue_judge.py` 2 处、`extensions.py` 1 处、`stage3_main.py` 5 处；
   同样的搜索在 `stage4-on-host` 与 `stage3-clean` 的 `src` 里命中 **0 处**。

3. **`/super ban @某人 99999` 的解析怪癖** —— 定位 `stage4-on-host:src/qq_roleplay_bot/plugins/group_admin/builtin_group_commands.py:90-101`
   （`parse_group_action`）+ `:152`（有 @ 时目标取 `mentioned`）+ `group_admin.py:31-34`（`MIN/MAX/DEFAULT_BAN_MINUTES`）、`:64-74`（`clamp_minutes`）。
   读代码推演（**我没有真机发这条命令**）：`tokens=["99999"]` → `:93-95` 把 **5 位纯数字先当 QQ 号**；
   `:98` 又要求 `token != target`，于是同一个 `99999` **不再当年限** → `duration=""` →
   `clamp_minutes("")` 回默认 **10 分钟 = 600 秒** → **30 天那道上限（43200）永远碰不到**。

4. **`execute_action` 不过档位闸门** —— 实测一半、**另一半核不上**：
   `dev/src/qq_roleplay_bot/stage3_main.py:1543` 里确实**没有任何档位判定**；档位在 `:1504 _plugin_level_allowed`，
   只在消息路径 `:2783` 调。
   **但"面板路径传普通 `actor_id`"这句我核不上**：面板传的 actor 是**云茹自己**
   （`stage4-on-host:.../plugins/webui/webui_panel.py:477` `actor = _call(self_id)`，`:494-496` 调
   `execute(..., group_id=…, actor_id=actor, mentioned=True)`），而注入进去的函数是
   `runtime.py:843-844` `ui_execute_action(self, request)` → `engine.execute_action(request)`（装配在 `:876-878`）——
   **它只收一个位置参数**，面板带 `group_id=` / `actor_id=` 关键字去调会直接 `TypeError`。
   → 要么我读错了，要么这条的真实形状与任务书写的不一样，**待写这条的人确认**。
   可做的仍是那两样：给执行口补调用方身份、或把 docstring 写实。

5. **`memory_maintenance_failed category=MemoryValidationError`** —— 发点在
   `dev/src/qq_roleplay_bot/memory_maintenance_agent.py:274`；生产日志 `run/data/bot.err.log` 里
   **375 次**，最早一次 `2026-09-27 17:05:35`（**部署前就有**）。
   **未解释**：我没查它为什么常驻（不在本次范围）。

6. **`stage3_main.py:2379` 把引擎当 `report` 递**（被禁的"猜形状"）—— 实测
   `dev/src/qq_roleplay_bot/stage3_main.py:2379` `draft = await reporter.run_once(self, force=True)`；
   而形状声明在 `stage4-on-host:src/qq_roleplay_bot/plugins/mail/daily_report.py:327`
   （docstring 明写"`report` 是 `plugins.ReportSeams`……**不是引擎**"）；
   同一个类的另一处调用 `plugins/mail/background.py:50` 传的是 `self.report`（对的）。
   → 同一份代码里两条调用口径不一致，必然有一条错。（`run/src` 那份是同一句，行号 `:2377`。）

7. **`TLABEL`（`stage3_runtime.py`）缺 `mail` 档** —— `dev/src/qq_roleplay_bot/stage3_runtime.py:765-780`
   的键只有 mention / name / reply_to_bot / active_message / media / threshold / private_debug；
   兜底在 `:1246` `.get(trigger, '群里的普通发言')`。已在 `docs/MAIL_AS_CORE_CHANNEL.md:208-218`（§二.9）记过。
   **未核的一环（而且这一环决定它现在会不会真的发生）**：现有代码里 `trigger` 的取值只有
   `active_message` / `private_debug` / `address_reason(...)` / `threshold`
   （`stage3_main.py:3077`、`:3095`、`:3097`、`:3099`），**没有 `mail`**（`dev/src` 里搜不到 `trigger="mail"`）。
   所以那条兜底文案要等 `trigger` 真的变成 `mail` 才走得到——那是"mail 进核心"那次搬家要做的事；
   而今天一封被投进来的信（`group_id=None`，按私聊处理）拿到的是 `private_debug`
   （"有人私下找你说话"）或 `threshold`（"群里聊了一阵，你一直在旁边听着"，`:778`）——
   后者对一封信**同样是错的措辞**。→ 结论：这档确实缺，但"会被说成群里的普通发言"是**结构上的后果**，
   不是我实测到的输出。

8. **记忆只认群** —— 实测：`memory_service.py:17/20/56`（前两处是构造与 `capture` 的
   `group not in self.allowed_groups()`）、`memory_inbox.py:20/30`、
   `memory_store.py:580 claim(...)` / `:631 discard_disallowed`；
   `allowed_groups` 的来源是 `runtime.py:1112` `lambda: set(engine.enabled_group_ids) if engine.enabled else set()`；
   而邮件/私聊的 `target.group_id` 是 `None`（`runtime.py:783-791` 造的是 `MessageTarget(user_id=sender)`）。
   → **邮件一个字进不了记忆库、检索不到**。这是"她记不住邮件"的**第二层原因**
   （第一层是那条被禁的"插件推对话"接缝）。**已写进 `docs/MAIL_AS_CORE_CHANNEL.md:184-206`（§二.8）**，动手时一并解决。

9. **`tests/run_offline.py` 会在仓库根生成未跟踪的 `yunru-source/`** —— 实测：
   `git -C dev status --short` 与 `git -C yunru-stage4 status --short` **两边都是** `?? yunru-source/`；
   目录内容与 `dev/tests/test_knowledge_base.py:26-58` `make_corpus()` 造的**合成语料逐项一致**
   （8 个文件、79–650 字节，含 `01-Bot人设/云茹Bot系统提示词.md`）。
   **机制我复现了（换了入口）**：`make_corpus(tmp)` 写的是 `Path(tmp)/"yunru-source"`（`:29`），
   而 Python 在**取不到系统临时目录**时 `tempfile.gettempdir()` 会退化成 **cwd**——
   实测（`TEMP`/`TMP`/`TMPDIR` 清掉，在仓库外一个空目录里直接调
   `test_knowledge_base.test_vectors_are_built_and_stored()`）→ `gettempdir=` 就是那个 cwd，
   跑完 cwd 里长出 `yunru-source/`（探测目录我已经删掉）。
   → "仓库根会多出 `yunru-source/`"成立，**但触发条件是临时目录取不到**；
   我**没有**在正常 TEMP 下跑全量套件重现它落在仓库根这一件事。是测试的副作用，迟早要收拾。

10. **`dev/data/logs/verify_runs.log` 的 `branch=`** —— 实测最后 8 行全是 `branch=stage4-on-host`
    （`sha=8b73969/e84dc7e/f9b10a2`），而那些运行是在 worktree `yunru-stage4` 里跑的，
    日志却落在 **dev** 的 `data` 下（`dev/.env:17` 与 `yunru-stage4/.env:17` **都**写
    `QQBOT_DATA_DIR=…\QQRoleplayBot\dev\data`，两个 `.env` 指同一处）。
    `verify_log.py:141-163 git_head()` 读的是**传进来的那棵树**的 HEAD/ref（`commondir` 已在 `75f341c` 修好，见 `:74-101`），
    所以 `branch=` 本身没错；错的是"一份日志里混着多棵树的分支、只按 `branch=` 认会认错"这个口径。
    → **没改**（任务书口径）。

11. **待处理的三处边界**（`AGENTS.md` §2.3 已标）—— 实测定位：
    * `runtime.py:477` `prompt_sources.add_plugins(prompt_plugins)`（AGENTS 里写"约 L460"，实测 **477**）；
    * 面板的 `memory_ops` / `knowledge`（插件分支在 `plugins/webui/wire.py`；`dev` 这侧对应
      `runtime.py:857-858 ui_memory_ops()` 与 `runtime.py:886 knowledge=_knowledge_for(engine)`）；
    * `registry.chat`（`runtime.py:736-792` `ChatSeams.deliver`；接缝类 `plugins/__init__.py:145 class ChatSeams`）。
      **`registry.chat` 会随"mail 进核心"一起撤掉**（设计稿 `docs/MAIL_AS_CORE_CHANNEL.md`）。

12. **代码注释里的日期写成了 2026-10-06** —— 实测本机日期是 **2026-10-05**
    （`Get-Date` = `2026-10-05 13:22 +08:00`；最新提交 `1ba1fc0` 也是 `2026-10-05 13:20`），
    但 `dev_config.py:207`、`runtime.py:1344`、`plugins/__init__.py`（`LinkSeams` 段）、
    `plugins/outage_notice/*` 里都把用户今天说的话记成"用户 2026-10-06"。
    只是注释日期、不影响行为，但"日期能对上"是核对的前提，记一笔。

---

## 五、2026-10-05 做完的事（简短，便于以后回看）

* **插件集成进本体，拆成两条线**：本体侧只留接口 `plugins/__init__.py`（`69ea9c7`，12:21；
  配套把要装插件的用例搬到插件侧：`ecacf32` / `5cdc8b5` / `07a4743` / `dfbef1a`）；
  插件在 **`stage4-on-host` + worktree `yunru-stage4`**（`8b73969`，12:32）。
* **风格审核放宽**（只上审核那一半，人格正文撤回）+ **不懂就问**：`44048b1`（10-04 23:37）。
* **对话日志**（实际收发，与模型日志分开）：`b5761ce`（10-05 00:02）。
* **mail 写信 bug 修复**（工厂当 client）：`37868d3`（00:10）。
* **识图搬成插件**（`plugins/vision/`）：`888bfd3`（00:46）。
* **群管理执行端核实已通**（走真装配逐条钉住发出的 action）：`808a3c1`（00:31）。
* **掉线通知**：接缝 `477aeb4`（12:41）、检测降到 ≤60 秒 `da5e345`（12:46）、
  插件 `e84dc7e`（13:10）+ `f9b10a2`（13:17）。

（上面每条的主题与 sha 是我实测查的；"拆成两条线 / 风格审核放宽 / 不懂就问 / 对话日志"这些**说法**来自任务书。）

---

## 六、这份清单自己的核对备注（**实测 vs 照抄**）

* **实测（我跑过命令或读过代码）**：§一的全部时刻与日志、§二里"不会自己恢复"这半句、
  §三的全部定位与边界四条、§三.5 的部署状态、§四第 1/2/3/5/6/7/8/9/10/11/12 条的定位、
  §五的 commit 主题与 sha。
* **照抄任务书（我没有独立核对）**：重启原因是"系统更新"；"早先 04:32 之前那 4 小时算有效样本，不计入三天"；
  NapCat 用户原话与"只能手动重启才能重新挂载"；"长期 = 换实现 / 自己写挂载层，先立项别动"；
  §四第 1 条"全绿当门槛前必须先修"的**取舍**；第 2 条"待用户确认"；第 8 条"第二层原因"的口径；
  第 11 条"`registry.chat` 随 mail 进核心一起撤掉"。
* **没核到 / 核不上（要人拍）**：§1.2 那 4 小时的**日期**（10-04 还是 10-05）；
  §四第 4 条"面板传普通 `actor_id`"（实测是云茹自己 + 签名对不上）；
  §四第 7 条邮件 trigger 是否叫 `mail`；§四第 1 条 flaky 的**根因**。
