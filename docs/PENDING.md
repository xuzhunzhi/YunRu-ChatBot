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

## 零、2026-10-26 收尾小结（最新在最前）

> 这一天动了三件事，都已上线；证据在 `run\data\logs\`、`verify_runs.log` 与提交里。

### 0.1 私聊：按"群临时会话"回，不再按好友私聊发

- **现象**：群友从群里点开她私聊，她答了但 QQ 拒收——`result=16 发送失败，请先添加对方为好友`
  （`run/data/bot.err.log` 10:25:42 实测）。
- **根因**：QQ 私聊有两条路——好友私聊（`sub_type=friend`）与群临时会话（`sub_type=group`，
  发起群号在 `sender.group_id`）。`onebot_ws` 解析时把群号丢了、发送时也不带，NapCat 于是按
  好友私聊发，非好友必被拒。
- **修法**：`MessageTarget.temp_group_id`；`private + sub_type=group` 时留住 `sender.group_id`；
  发送带上；会话失效（NapCat 重启/超过 7 天）只退回好友私聊重试**一次**。
- 提交 `24df7a9`；部署 12:16。

### 0.2 好友申请：与私聊白名单同一份，名单外通知主人

- **用户口径**：「加好友的白名单跟 private 进行同步，这两个作为一份白名单」
  「申请被拒建议给我发，通过邮件或者私聊」「不能主动读吗」「那就加一个启动时补读吧」。
- **现在**：推送事件当场处理（名单内自动通过，名单外不批并通知）；启动连上后**补读一次**
  待处理列表（捡掉线期间漏掉的）；通知先私聊、发不出去再走邮件。
- **第一次真实触发**：14:15:18，`475842590` 申请加好友 → 自动通过（`Stage 3 friend approved`）。
- 提交 `24df7a9` / `79feb4a` / `d610fc8`；部署 12:16 / 14:18 / 15:16。

### 0.3 Stage 5 整体迁出，本仓库不再维护

- **用户口径**：「stage5整个切割出去，丢到v2去」「记忆这些能迁移就迁移，尽量兼容」
  「living bot得先测试好才能拿来接入现有记忆」。
- **新仓库**：`LivingBot`（私有）。4 个实验提交以 `git format-patch` 归档，附
  `docs/MIGRATION.md`（记忆/伪群/名单/日志的兼容契约，第 0 节是"先测好再碰真库"）。
- **本仓库**：`yunru-stage5` 工作树与 `stage5/*` 两条本地分支、远端
  `origin/stage5/letter-tools-plugins` 已删除；`AGENTS.md` / `STAGE_BOUNDARY.md` 改成实话。
- 提交 `8a98c48`。

### 0.4 还没验证的（别当成已完成）

- `3421255296`（桓珩）的私聊：白名单内、尚未加好友，临时会话修复**对他是否有效还没实测**。
- "名单外 → 通知主人"这条**还没被真实触发过**（唯一一次真实申请是名单内，直接批了）。
- 记忆迁移：LivingBot 未开始，`run\data\memory\` **一个字节没碰**。

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
| **邮件这一层** | `dev/src/qq_roleplay_bot/dev_config.py` `MAIL_OUTAGE_NOTICE_ENABLED` ← `QQBOT_MAIL_OUTAGE_NOTICE`。**这个开关原来只在插件分支上**——它随 `e84dc7e` 加在插件线的**核心文件** `src/qq_roleplay_bot/dev_config.py`（+8 行）。现已作为一条独立的 Stage 3 提交**移回本体**：它与 `DISCONNECT_NOTICE_ENABLED` / `AUTO_APPROVE_JOIN` / `VISION_*` / `WEBUI_*` 同属"本体声明开关、插件读它"那一类，本体 `dev_config.py` 里本来就有这些 |

### 3.2 依赖方向（把话说准，别读反）

`REQUIRES = ("mail",)`（`plugins/outage_notice/plugin.py:37` 附近）的意思是
**mail 是 outage_notice 的"前置提供方"，outage_notice 依赖 mail**：
mail 经 `registry.mail.operator_sender` **提供**"给操作者发一封普通邮件"，
outage_notice **取用**它；mail 那一侧不认识任何使用者的名字。
（任务书里那句"mail 是**依赖方**"照字面容易读反，这里按代码写。）

### 3.3 邮件正文：固定模板、**不调模型**、**用她那口气**

`plugins/outage_notice/outage_notice.py`：`SUBJECT = "云茹断线了"`（`:45` 附近）、
`NOTICE_TEMPLATE`（`:69` 附近）=第一行"我这边断了。"+症状（QQ 那头连不上，群里的消息我收不到，我说的话也发不出去）
+**问句形式的求助**（"能不能把 NapCat 重开一次？"）+ 时刻（"断的时候是 2026-10-06 21:37:12（+0800）"，夹在句子里）。
最后一句就是那个时刻——**正文到那里就完了**。`build_notice()` 是纯函数（`:96` 附近），
不碰网络、不碰注册表、不调模型。

**2026-10-11 用户改了口三次**：先是 *"这里，我认为可以用yunru语气"*（早先那版是第三人称运维腔"她收不到群里的消息"），
接着 *"就不能把方括号丢掉吗，从yunru角度，这是求助邮件不是通知"*——`【时间】【怎么回事】【怎么办】`
那套小标题全去掉：**通知腔才需要小标题，求助信不需要**，现在整封正文里一个 `【` 都没有；
最后 *"这封是自动发的不留，，不用交代机制"*——结尾那句"—— 这封是自动发的：一段断线只发一封，恢复了不另发"
也删了：**一个人求人帮忙，不会在自己消息末尾附"本条为系统自动发出"**。
"一段断线只发一封、恢复不另发"仍是核心的**行为**（§3.4 第 4 条），只是不再由正文交代。
现在仍是**手写定稿**——用她的语气**不等于**让插件去拿她的人格：不读人格文件、
不拼她的系统提示、不碰对话那条路（守 `AGENTS.md` 的边界）。判据见
`tests/test_outage_notice.py` 的 `test_the_mail_speaks_in_her_voice`（第一人称、
第三人称不许回来、不撒娇不卖惨、**不出现"自动/系统/只发一封"这类机制交代**）、
`test_one_outage_sends_exactly_one_mail_with_the_fixed_template`
（四个锚：第一行/症状/问句求助/时刻，方括号不许回来，正文以那个时刻收尾）与 `test_the_mail_body_carries_no_persona_mechanism_words`。

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

8. **记忆只认群** —— 实测：`memory_service.py:27`（构造）与五处
   `group not in self.allowed_groups()`（`:53` `retrieve`、`:63` 检索回读、`:97` `identity_for`、
   `:116` `profile_for`、`:140` `care_note`）、`memory_inbox.py:36/66`、
   `memory_store.py:580 claim(...)`；
   `allowed_groups` 的来源是 `runtime.py:1949` `lambda: set(engine.memory_group_ids) if engine.enabled else set()`；
   而邮件/私聊的 `target.group_id` 是 `None`（`runtime.py:783-791` 造的是 `MessageTarget(user_id=sender)`）。
   → **邮件一个字进不了记忆库、检索不到**。这是"她记不住邮件"的**第二层原因**
   （第一层是那条被禁的"插件推对话"接缝）。**已写进 `docs/MAIL_AS_CORE_CHANNEL.md:184-212`（§二.8）**，动手时一并解决。
   **2026-10-08 补充（提交 038e67c）**：`allowed_groups` 现在**不再是"已启用的群"**——
   记忆有自己的按群名单（`engine.memory_group_ids`，`/super enable|disable memory`
   在哪个群说就动哪个群），与 `/admin enable|disable` 那张对话名单互不相干；
   那处 `discard_disallowed`（`:631`）也已整个删掉，关掉记忆不再删待处理行。
   "记忆只认群"这条本身没变。

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
   **2026-10-11 已收拾**：`make_corpus()` 现在拒绝写到系统临时目录之外
   （`tempfile.gettempdir()` 退化成 cwd 时当场 `AssertionError`），四条
   `make_corpus(Path(tmp) / "..")` 改成临时目录里的一棵子目录（`Path(tmp) / "corpus"`）。
   两棵树里那两份残留已删。实测三种越界写法逐一被拦（往 `dev/`、往 `yunru-stage4/`、
   把 cwd 当临时目录），拦下之后两边仓库根都没被写脏；`test_knowledge_base.py` 的 34 条
   在 dev 与 worktree 两边都过。

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
* **出戏 + 胡话两类故障的修**（当天晚些，在 `dev` 这条线上）：
  ① 常驻 system 末尾加**禁口令** `stay_in_character.py`（经
  `stage3_runtime.compose_system_prompt` 拼进去，规矩里机制词零命中）；
  ② 风格审核判据**四类 → 六类**（出戏自指 / 不成句 / 空回复写实）、去掉宽松偏置、
  输出认不出**重试一次**、拦截率进 `EngineSnapshot` 与 `/super status`、
  审核请求带上"谁在说 + 对方原话 + 当前话题"。
  起因是两条生产故障：群 `908696203` 里她接了"改架构、加插件"的话
  （"再加一个插件，那条线还得挪。"），以及 `22:06:54` 那条
  "哦，看啊，云茹，今天的大大的哈基米。"（第三人称叫自己 + 词序乱）。
  **未部署**（`run/` 要另做一步，见 `docs/STAGE3_PENDING_DESIGNS.md` §③"部署注意"）。

* **"不懂就问"改口径：拆掉"以问回应"，换成"要么不出声，要么别断言"**（10-05 晚，在 `dev` 这条线）：
  用户否掉了上一版（*"别做不懂就问"* · *"先改掉不懂装懂硬插话"*）。现在三层：
  ① 判定那两个信号（`understood` / `specialist`）照旧，**不多花一次调用**；
  ② **路由**（确定性）：没被叫到 + 没懂 + 具体话题 → **不出声**（`stage3_main.py` 那道
  `verdict.unsure and verdict.specialist`，以及 `decide_grounding` 里的 `speak=False`）；
  ③ **兜底**（确定性）：被叫到 + 没懂/没根据 + 她那一版话里有**具体断言**（数字/型号/流程，
  `ask_when_unsure.has_specific_claim`）→ **打回，同一轮重写一次**（复用 `_rewrite_after_review`，
  `stage3_main.py` 那道 `grounding.rewrite ... has_specific_claim`）。
  **两个教训都没重犯**：常驻 system 一字不增（`UNSURE_ASK_RULE` 与新的 `NO_ASSERT_NOTE`
  都不在 `compose_system_prompt` 路上）；拦与放行是纯函数 + 一处引擎判断，不靠措辞自觉。
  旧的限量（`QQBOT_ASK_MAX_*` / `AskBudget` 接线）**整套删掉**——没有消费者了。
  **未部署**（`run/` 仍未同步）。

（上面每条的主题与 sha 是我实测查的；"拆成两条线 / 风格审核放宽 / 不懂就问 / 对话日志"这些**说法**来自任务书。）
（10-05 晚那一条是我这次改的；sha 见 `docs/STAGE3_PENDING_DESIGNS.md` §① 末尾那一节与 git log。）

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

---

## 七、2026-10-21 待办落盘（compact 前留档）

> 这一段是为了"上下文压缩后不丢细节"。每条都带落点与状态。

### 已完成（今天）
- 关系刻度 -100..100 / -30..30 + 22/7 档文字（`relationship_tiers.py`），生产库已迁移（15 行）。
- 防备：单次 = 一档（10），日净上限 10；判定路径不再写死 8。
- 判定加"这句在说谁"：在说别人一律 HOLD（prompt 2729 字）。
- 预设型编造闸：经历门槛词表 + 同句锚点（真实日志命中 13 → 3，全真）。
- 邮件：日报档名改吃新刻度；收信改水位+翻页（不再 `--is-unread`）；正文缓存；只有成功才标记；`+watch` 来信立刻唤起。
- 掉线通知：只对"先连上过再掉"发信（重启不再发）。
- 画像：碎片化（`profile` 总览 + `profile/<方面>`，每条 ≤120 字、最多 12 条）；不按年龄裁剪；**人手写的总览（origin=operator_seed）模型不许覆盖**。
- 记忆维护十小时停摆：根因是模型多写 `scope_key` 被严格校验整批作废，已放行该键。
- 文档：`MAIL_AS_CORE_CHANNEL.md` 增搬家进度+三 bug；`AGENTS.md` 增"插件源码在 plugins 分支"与语音边界。

### 待做（按优先级）
1. **语音插件**（用户 2026-10-21 明确"先不做"，边界已写进 `AGENTS.md §2.3`：出站变换窄接缝、失败回落文字）。
   **2026-10-22 用户改为"排队"**：「语音插件排队」——即"有优先级、但排在记忆/画像/称呼之后"，仍不是现在做。
2. **称呼：从"不叫"到"自然叫昵称"**（**不是**兜底替换）。实测：7 天 1731 条回复里**全名** 0 次（0/1731，95% 上限 0.17%）；但审计口径只收全名/别称，**删减形式（深海翻滚少女→翻滚）根本统计不到**，所以"她从不叫人"这个结论不成立（2026-10-21 自我纠正）。方向（用户 2026-10-21 定）：先看记忆里记着的称呼（如 `少女哥`）→ 名册「别称」→ 主名去装饰；名字跟着话走、不喊"名字+逗号"；形近/同名（实测 28 对，`桓衡≈桓珩≈桓街`）不叫。**等用户对定稿点头后开工**。
3. **日志保留半个月**（用户 2026-10-21：「日志 ttl 长一点吧，最起码半个月」「全部都要留」）。实测坑：五种模型日志连一天都留不住（`judge` 只留 10 小时、`security` 2.6 小时），因为轮转是**按条数**（feature 1000 条 / chat 20000 条）不是按天。方案 B（用户 2026-10-21 选 b）：活文件大小不变，**裁掉的行归档**到 `logs\archive\<label>-YYYYMMDD.jsonl`，`QQBOT_LOG_ARCHIVE_DAYS` 默认 30，清理**按文件名日期**严格保留最近 N 个自然日（用户 2026-10-21：「日志归档存活时间严格设定30天」）。改的是共用实现 `feature_log.RollingJsonlFile.trim()`（所有按条滚动的日志一起生效）。**状态：已改完并提交（`9235391` + `223c5c9`）、三门槛 `ran=1933` 全绿；2026-10-22 14:12 已随 0.4.0b1 部署到 `run`（见 §九.1）**。
4. **私聊记忆**：2026-10-22 已实现（伪群方案，`dm:<uid>`）：**不过群名单、不做渠道标、
   不做群侧 visibility 过滤**（用户当天口径：「记所有人」「记忆进群，私聊内容泄漏风险自负」）。
   群侧检索看不到 `dm:` 的 `user_group` / `group` 记录，靠命名空间天然不相交；
   `user_global` 照旧跨群共享。邮件仍不进记忆（它有自己的命名空间，0.5 才做）。
5. **画像碎片观察**：看 `profile/*` 是否持续增长；`PROFILE_MAX_PIECES=12` 只写在 prompt，没有机械执行。
6. **/v2/ 面板**：页 2–23 仍是占位；`/api/model` 的 `available:false`（装配点未注入 model_providers）。
7. **记忆分层 A2**：`wip/cache-recall` 分支上有一版撞坏 6 条既有召回语义，未合。
8. `bot.err.log` 缺行（计数器 30.8% 打回率 vs 日志 551 行）、1,194 条"群未启用却在叫她"待核。
9. `webui-compose` 已进 git（`plugins/webui/compose-src`），但**未实测构建**。
10. **跨群原话上下文（搁置 → 2026-10-22 改为"等 V2.0"）**：用户 2026-10-22 要的是"有人问『你记得我在隔壁说了什么吗』，她能说记得"。现状**答不出**——inbox 按 `(群, 人)` 分区成批（`claim()` 一次只取一对），召回 `retrieve()` 只查本群 + `user_global` 概括，跨群的只有概括不是原话。评估过三种：A 概括型（维护批附带"他在别的群最近 N 条"，产出 `user_global` 概括，≤10 分钟延迟，给不出原话）/ B 原话·短期（回复段注入"他本人在别群最近 N 条"，来自 inbox，只管 1–3 小时）/ C 原话·长期（按 `user_id` 查 `chat.jsonl`，8 天，但要先把"日志当召回源"讨论清楚）。建议 B+A。**用户 2026-10-22：「这个现阶段似乎有点太难做了，先放下来吧」→ 搁置**；README 已如实写成"跨群的是人物记忆，不是对话原文"。三条硬约束记在这里，将来做时照办：只取他本人发言、不进 system 只在 DATA、默认不在群里主动提别群内容。

---

## 八、2026-10-22 夜：优先级改口径（用户口述，照此排队）

> 这一节是**用户当晚亲口定的顺序**，与 §七 的编号不冲突：§七 是"做过/待做"的账，
> 这里是"接下来先做什么"。原文用「」照抄；我没做过的标"未开工"。

### 8.1 P0（最高，先做）

1. **画像恢复**：用户原话「我的画像丢了还没改回来」「记忆属于 p0」。
   实测（只读查生产库）：人手写的 `profile` 总览（`origin=operator_seed`，191 字）**还在**，
   10-18 的备份里也是同一份；丢的是别的（碎片化之前 agent 写的长画像？），已派 opencode 取证。
   **动记忆库要用户点头**，计划见 `dev/data/plans/05-画像修复.md`。
2. **私聊对话记忆**：用户「私聊对话记忆开始做」。**2026-10-22 当天口径改为伪群方案**
   （`dm:<uid>`）：不过群名单、不做渠道标、不做群侧隔离；原计划
   `dev/data/plans/03-私聊记忆.md` 的"渠道标 + 群侧渲染过滤"作废。邮件仍不进记忆（0.5 才做）。

### 8.2 排在后面（做，但不占 P0 位置）

3. **称呼**：用户 2026-10-22「改。」——计划 `dev/data/plans/01-称呼.md`，等对"叫法"点头后开工。
4. **邮件重构**：用户「邮件重构」「两个都改完上线传 release」。计划 `dev/data/plans/02-邮件重构.md`，
   M0（`mail_state` 的 `bodies`/`poll_watermark` 未落盘，实测）→M1→M2→M3→M4。
5. **语音插件（排队）**：用户「语音插件排队」——见 §七 第 1 条，仍不是现在做。
6. **小红书插件（排队）**：用户「小红书插件进待办文档排队」。
   选型报告已完成：`dev/data/xhs-research.md`、`xhs-research-v2.md`（主选 `xpzouying/xiaohongshu-mcp`）。
   **未开工**，等前四项之后。
7. **webui 打磨**：用户「webui 多开几个 opencode 给你提意见」「webui 做好再传」——
   已派三个 opencode（可用性 / 安全边界 / 工程质量）出意见，报告在 `dev/data/opencode/out/`。

### 8.3 搁置 / 不管

8. **跨群原话上下文**：用户「跨群上下文等更宏大的 V2.0 版本」→ 从"搁置"升级为"**排给 V2.0**"，
   三条硬约束仍见 §七 第 10 条。
9. **NapCat 掉线问题**：用户「napcat 不管」→ §二 那条"短期看护 / 长期换实现"**不再推进**，
   掉线只能手动重启这件事已知、接受。

### 8.4 待用户点头的两件（本轮不做）

10. **部署（上线）**：**这条已被 §九.1 / §十推翻**——2026-10-22 白天那批（版本 `0.4.0b1`、日志归档
    30 天、画像读侧、私聊记忆）**已于 14:12 部署并重启**，M1 + 写信改造于 15:46 部署并重启。
    仍然没部署的只有之后新增的改动（Stage 5 实验已在 2026-10-26 连分支一起迁出到
    独立仓库 LivingBot，本仓库不再维护）。
11. **GitHub release**：用户「两个都改完上线传 release」+「webui 做好再传」→
    前置是称呼 + 邮件 M1 完成、webui 必修项收掉、预装归零做完（见
    `dev/data/plans/04-预装归零与发布.md`）；**不打 tag、不发 release**，等上述齐了再报告。

### 8.5 本节的边界

- 用户原话「别他妈提公告听到就烦」：**这批计划不含任何群公告/对外发言步骤**，以后也不主动提。
- 用户原话「该核实的先派 opencode 核实再说」：四条待核发现 + 评审 P1/P2 共 20 条，
  已派三个 opencode 核实（`dev/data/opencode/out/verify-*.md`），**核实结论没回来之前不当事实用**。

---

## 九、2026-10-22 白天：已上线 0.4.0b1 + 邮件三件拍板

### 9.1 已做的（都已提交并部署到 `run`，bot 已于 14:12 重启）

| 项 | 落点 | 说明 |
| --- | --- | --- |
| 画像读侧修复 | `stage3_main.py` `_profile_block` 改走 `store.profile()` | `/super profile` 与面板显示全部碎片（总览在前）；取证见 `data/opencode/out/profile-forensics.md`（库里一条没少） |
| 人手写记忆不被覆盖 | `memory_store.py` 守卫扩到任意 kind | 判据从"看 kind/键"改成"看 origin=operator_seed"；audit 记 `operator_seed_readonly` |
| 私聊进记忆（记所有人） | `memory_model.py` 伪群 `dm:<uid>` + inbox/service/maintenance | 不升 schema、不做群侧隔离（用户口径「记忆进群，私聊内容泄漏风险自负」）；白名单外私聊只入记忆、不回复 |
| 日志归档 30 天 | `feature_log.py` | 本次随核心一起上线 |
| 0.4.0b1 release | tag `v0.4.0b1` | **只含核心与 Stage 3**；插件（含 `webui/`）不随 release 上传 |

### 9.2 邮件三件（用户 10-22 拍板）

1. **第二个插件名**：`daily_report`（用户「第一个同意」）。
2. **陌生人 vs 熟人**：用户要求「要想办法识别这个人到底是陌生人还是熟人，剩下的照你说的做」
   ——陌生人只进中短期（7–14 天），长期只留"这人是谁/关系"；识别方式待设计
   （用 `people` / `relationships` / 关系刻度的既有数据，不新造名单）。
3. **信的 scope**：「共用，但是标注好是邮件的。而且进长期记忆，可以通过记忆调起收到的邮件」
   ——`user_global` 共用 + 渠道标注 `mail` + 长期保留 + 召回时能带出邮件内容（0.5 的 M3）。

### 9.3 没做的（明确记账）

> ⚠️ **2026-10-22 晚补：下面第一条已被 §十推翻**（M1 当天下午就做了并上线）。原文保留，
> 别拿它当现状；其余三条仍成立。

- ~~**邮件 M1（发信核心化）没做**：要新建核心 `mail/`、`registry.mail` 由核心 provide、
  `REQUIRES` 改能力判定，并跨 `dev` 与插件树改测试；当天选择先上线已完成四项，M1 顺延。~~
  → **已做（§十.1，15:46 部署）**：核心 `mail/` 已建、`provide_senders()` 在 `attach_plugins()`
  **之前**、`REQUIRES` 改按 `has_capability("mail")` 判定。
- **邮件 M2/M3/M4**：按计划放 0.5。
- **webui 必修清单**（`data/plans/06-webui必修清单.md`）：`/api/action` 全挂、重启按钮、
  模型页空白等**未动**——用户已明确"webui 没做好，插件先不上传"。
- **预装归零的代码改动**：本次 release 直接只发 main（天然不含插件），没改 `discover()`。

---

## 十、2026-10-22 下午：M1 上线 + 写信改造（都已部署并重启，15:46）

### 10.1 邮件 M1（发信进核心）

- 核心新增 `qq_roleplay_bot/mail/`（`client.py` 发信最小实现 + `provide_senders()`），
  `runtime.build_engine()` 在 `provide_roles()` 之后、`attach_plugins()` **之前** provide
  （`runtime.py:996`）。
- `REQUIRES = ("mail",)` **声明没改，判定改了**：`discover()._load` 先问
  `has_capability("mail")`；为真跳过装前置插件（删掉 `plugins/mail/` 也放行），
  为假才退回老路，装不上跳过自己（fail-closed）。
- 启动日志实测：`模型配置 letter: provider=mimo model=mimo-v2.6-pro key=QQBOT_LETTER_API_KEY 有值`、
  `发信能力已经有人提供过，本次不覆盖`、`寄给某个地址的能力已经有人提供过，本次不覆盖`、
  9 个插件全部装配、OneBot 已连接。
- 门槛：dev `ran=1954` PASS；插件树 `ran=2510` PASS（skipped=1 为缺 `data/relationship-tiers.md`）；
  两树 pyflakes 静默；解耦七项全过；插件范围门槛 exit 0。
- **门槛增了一档要知道**：`check_plugin_branch_scope.py` 新增 `MIRRORED_CORE_FILES`
  （4 个精确路径：`mail/__init__.py`、`mail/client.py`、`runtime.py`、`model_config.py`），
  因为插件树测试要 import 核心 mail；注释写明"逐字复制自 dev、M4 后删除、不许扩成通配"。

### 10.2 写信（质量 + 模型）

- **模型**：`model_config` 的 letter 行改为 `{"key": "letter", "model": "mimo-v2.6-pro"}`
  （用户 2026-10-22：「写邮件模型我说过是用 mimo v2.6pro 吧」；代码里原有一行是 2026-10-12
  记的"借 judge / deepseek-v4-pro"，与本轮口径不符，已改并同步两处过时注释）。
- **不再写成汇报**：`daily_report.py` 的写作 prompt 删掉「必须写这段时间的事」「至少接上
  一句他说的话」「没有新事就说没有新事」，改成「今晚想写什么就写什么，不写这段时间的事也可以」。
- **标题**：新增纯函数 `letter_writer.subject_problem(subject, body)`（>20 字 / 禁开头 /
  复述词 / 与正文第一句最长公共子串 ≥6 → 不合格），走校对阶段已有的 `<subject>改过的主题>`
  通道重给一次，仍不合格保留原标题；**不加模型调用**。
- 实测（真实调用、素材取自 10-19/10-20/10-21 三封信）：两轮 6 封，标题 6/6 不再像首句、
  "我这边没什么新事"类模板 6/6 消失；**残余**：仍有 2/6 以"今晚在群里看到你…"起头，
  下一步要再动"开头"那一条。

### 10.3 待办（明确）

- 插件分支**没有 push**（用户 10-22「所有插件先不上传」）；本地已合并 main 并全绿。
- `0.4.0b1` release 之后又有了 M1 + 写信改造两个提交（`8fcd615`、`6d9b009`）——
  **要不要补一个 `v0.4.0b2` release，等用户说**。
- webui 必修清单仍未动（`data/plans/06-webui必修清单.md`）。
- **文档盘点（2026-10-22 晚，只改文档）**：产出 `data/docs-audit-20261022.md`（盘点表 / 改动 /
  未核实项）与 `data/docs-index.md`（`data/` 下散落报告的总索引，**没删任何文件**）；
  修正了 `ARCHITECTURE.md`、`docs/CONFIG_GUIDE.md`、`docs/FILE_MAP.md`、`README.md`、
  `docs/STAGE_BOUNDARY.md`、`AGENTS.md` 里已过期的条目（逐条见盘点报告）。

---

## 十一、2026-10-23 ～ 10-26：架构定调 + 判定交出写权限 + 私聊申请制/好感上限上线

> 这一节是**补记**：§十 停在 10-22 20:04，之后没人回写。  
> 来源分两层并逐条标注：**[会话]** = 那几天的主会话报告（我没重跑门槛）；**[实测]** = 2026-10-26 10:40 当场跑命令看到的。  
> ⚠️ 先说最要紧的一条：**这几天上线的东西大部分没有提交**，`main` 的 HEAD 还停在 `c152184`（10-22 23:26）。见 §11.5。

### 11.1 架构定调：语言中枢 vs 海马体（10-24 夜，用户拍板）

用户用比喻定下的分工，**这是边界不是建议**：

| 部分 | 包含 | 职责 | 写权限 |
| --- | --- | --- | --- |
| 语言中枢 | 判定 agent + 回复 agent | 只回答"现在说什么 / 接不接这句" | **没有任何写权限** |
| 海马体 | 记忆 / 画像 / 关系 | 独立运行、按批处理 | 读写都在它内部；语言中枢只**读**它的输出 |

落实（[会话] 10-25 00:00 那轮）：
- 删掉判定 → `apply_affinity` 的写关系路径（`_apply_guard_signal` 里那段 `guardedness=+10, source=judge`）。
- 判定输出里的 `guard>UP` / `guard_reason` / `OFFENSE_CODES` / `_verify_guard_quote` 整节失去消费者，变成死代码。
- 判定不再读画像口径（`compose_identity_note` 撤回）；**保留** `PROBE`（当面打探机制）当轮不接。
- 判定只往**内存**挂 30 分钟"冷感"（`GUARD_FLASH_SECONDS = 1800`），不落库；关系/画像/记忆的写入全部归维护 agent。
- 门槛 `ran=1972 failed=0`（[会话] 10-25 00:24）。

### 11.2 已上线（10-25 三次部署，每次都重启过）

| 时间 | 内容 | 落点 |
| --- | --- | --- |
| 00:36:53 | 判定交出写权限那批（5 个文件）+ 重启，PID 56736/13864；备份 `run\backups\src-20261025-003545` | `stage3_main.py`、`dialogue_judge.py`、`memory_maintenance_agent.py`、`memory_model.py`、`affinity_caliber.py` |
| 15:13:00 | **私聊申请制**：群内 `/yunru private`（别名 `/yunru 私聊` / `/yunru 私信`）登记 → 主人在群里 `/super enable` 放行 | 新 `run\data\private_allow.json`（只存号/时间/谁批的，重启仍有效）；按群只留**最近一条**、30 分钟有效；`QQBOT_PRIVATE_REPLY_ALL` 保留但**没开** |
| 16:35:49 | **好感三道上限** + **命令语法改"功能在前"** | `U≥85` 需主人到 95；`80≤U<85` 需落后主人 5 点以上；其余 `U−O>20` 冻结到主人追平（`O≥U`）。只拦"别人"的正增量，主人自己/`manual`/负增量不受限。新写法 `/super memory enable`、`/super private enable`，旧写法保留为别名 |

同批还上线了（[会话] 10-25 14:40 前后）：画像出身闸 `ORIGIN_CLAIM`、维护 prompt「画像只写印象、句句有出处」、`data\logs\profile.jsonl` 画像流水（`ran=1992`）；写信的 7 条有出处写法 + 一道机械网（事实只能来自事件原文、看不懂不许编场景、数字照抄不换算）。  
数据侧修正（先备份）：删「山东临沂人」那条；fa_555 的籍贯改成「山东人；自称『比较老广』是玩笑」，`origin=operator_seed`（模型以后改不动）。

### 11.3 判过但不做 / 已纠正的口径

- **SAG 检索架构**（10-24 21:38）：试"查询时实体展开"后全量 Recall@5 **25.0% → 20.0%**（real 20.8→16.7、synthetic 27.8→22.2），没到用户定的 +10 门槛 → **不改 core**。
- **判定 prompt 尺寸上限**：`JUDGE_PROMPT_MAX_CHARS = 2942.8` 是 2026-10-20 某个 agent 自己量出来钉死的水位，**不是用户要求**，10-24 已撤回该理由；要不要继续受它约束由用户定。
- **webui 安全五条**（10-23 00:08 用户逐条拍板）：令牌文件权限**不做**；`api_base_url` 校验**做**（协议/主机白名单 + 私网段拒绝 + 改地址二次确认）；remote 模式 DoS **不做**，以后走 SSH 隧道（写进部署文档，不改代码）；注销不生效 + 认证失败不记日志**做**。
- **`#sl` 不是云茹发的**（10-25 14:50）：NapCat Desktop 的 `SnowLuma` 组件与云茹**共用同一个 QQ 号**，`#sl` 由它处理；我们只处理入站，`reply.jsonl` 里查不到它。SnowLuma 只做动作/命令、不聊天，不冲突。

### 11.4 10-24 夜两笔数据事故（已回正，教训要留）

1. 维护 agent 在**画像就摆在输入里**的情况下，把「他说话直、在带你、不是嫌你」读成"语气不好" → 好感 44→40（同一份输入里 `在带你`/`那不是瞧不起你`/`让你别插话` 三个片段都在）。
2. 判定 agent 把「他看不起她」直接写成防备 0→10。

修法与结果：两条路各加硬闸门（判定那条已按 §11.1 整个删掉）；数据 40/10 → **44/0**，每笔留 `source=manual_correction` 流水、原记录不删；备份 `run\data\memory\memory.sqlite3.bak-20261024-221810`（6,828,032 B，backup API 带 WAL）。  
另一笔根因：「她记不住」不是模型不听话——`memory.jsonl` seq 1780 那次维护**只写了好感（AFFINITY −4）、没写任何记忆**，当时 inbox 积压 388 条（19:04–22:10）。

### 11.5 未提交状态（⚠️ 2026-10-26 10:40 [实测]，全场最要紧）

| 树 | 状态 |
| --- | --- |
| `dev`（`main`） | HEAD `c152184`（10-22 23:26）；**24 项未提交**（19 改 + 5 新，`+1122/−174`）；新文件 `src\qq_roleplay_bot\affinity_caliber.py` + 4 个新测试。⚠️ 10:52 再测已是 **29 项**：另一路在 **10:39** 又改了 `onebot_ws.py` / `onebot_client.py` / `transport.py` / `test_onebot_ws.py`（群临时会话私聊），加上本节补记的 `PENDING.md` 本身 |
| `yunru-stage4`（`plugins`） | `04e45f4`；**6 项未提交**（`webui_access.py`/`webui_panel.py` 的 #4/#5、`mail/daily_report.py`/`letter_writer.py`、2 个测试）；**ahead origin 48**（用户「所有插件先不上传」）；相对 `main` **157 ahead / 11 behind** |
| `run`（生产） | 无 git；`src` 最新文件时间 **10-25 16:22** ⇒ 线上跑的是**那一刻的 dev 工作区**（既不是 HEAD，也不是现在的工作区——10-26 08:01 又改过 `stage3_main.py`/`runtime.py`） |
| 门槛凭证 | 可用的绿是 `2026-10-26T10:43:33 ran=2015 failed=0 errors=0 skipped=0 result=PASS`（`dirty=yes`）；`08:07:39` 那条是 `ran=2009`。日志末尾另有 3 条 FAIL，成因见本节末 ⚠️ |

**下一任第一件事**：把这 24 项按主题分组提交（好感上限 / 自指画像与纠偏 / 判定冷感 / 命令语法 / 画像流水 / 文档），再跑一遍三门槛，让 `verify_runs.log` 出现一条 **`dirty=no`** 的 PASS。

**门槛实跑（2026-10-26，[实测]）**：离线套件 `10:43:33 ran=2015 failed=0 errors=0 skipped=0 result=PASS`（`dirty=yes`，本次只改本文档）；`pyflakes src tests` 无输出（exit 0）；`check_module_removal.py` 七项全"能（已解耦）"（exit 0）。

> ⚠️ 同一份日志后面还有 **3 条 `ran=2015 failed=6 errors=606` 的 FAIL**（10:45 / 10:48 / 10:50），别按字面读：
> - **10:45、10:48 是我造成的环境失败**：我把 `TEMP` 指进工作区后重跑，sqlite 在沙箱下建不出库（`unable to open database file`），606 条是连锁。
> - **10:50:49 那次不是我跑的**：同一天 **10:39** 另有一路改动写进了 `onebot_ws.py` / `onebot_client.py` / `transport.py` / `test_onebot_ws.py`（群临时会话私聊），那一路也在同一棵树上跑套件。
> - 判据用 **10:43:33 那条 PASS**，或自己重跑一次再记账。**跑门槛前先确认没有第二路在同树动手**——两套 `run_offline` 会互踩 `.tmp_test_run/`。

### 11.6 仍然挂着

- **webui**：前端退出要补 `POST /api/logout`（`app.js:2756` 现在只清 `localStorage`，改完要重取 `test_webui_v2_static.py` 的字节基线）；上传前清理 `compose-src\evidence\` 175 个文件（19 张带真实 QQ）、`NOTES.md:223` 的 self_id、`composeApp.js` 的本机绝对路径，`build\` 不提交；然后 `merge main` 进 `plugins` + 四门槛。**详单在 `data\webui-handoff-20261025.md`（7.4KB，路径/行号/验收都在里面，别重写一份）**。
- **面板布局**（10-23 00:21 的计划，未落地）：1920 宽下内容列 1320px 左对齐、右侧空 480px → ≥1500 宽改 `max-width: 760px` 居中。
- 顺序仍按 §八：称呼 → 邮件 M2–M4 → 语音（排队）→ 小红书（排队）→ 跨群（V2.0）；napcat 掉线不管。
- 沿用 §七 没结的账：`memory_maintenance_failed category=MemoryValidationError`、`bot.err.log` 缺行（30.8% 打回率 vs 551 行）、1,194 条「群未启用却在叫她」。

### 11.7 会话侧的意外（与 bot 无关，别算在 bot 头上）

上一个主会话（DSH，标题「阅读工作区内容」，`session-642c6f37…`）从 **10-25 23:04** 起发不出消息：历史里引用的两个附件对象被删（`sha256:19fd3b5a…` 一张 computer-use 截图、`sha256:0598…` 上传的 `miuix-0.9.4.zip`），DSH 每次请求都在本地准备阶段读不到 → `ATTACHMENT_NOT_FOUND` → 被适配器兜底 catch 包成 `TRANSPORT`（文案 `DeepSeek Messages transport failed`），每次尝试 20–60ms 就失败、**零 HTTP 请求**、退避重试 5 次全败。  
2026-10-26 10:39 已按方案 C 修复：往 `session.v4.jsonl.zstd` 末尾追加一条 `image/offload` 事件（`seq=111483`，target `seq=107542`、`imageIndexes=[0]`），备份 `session.v4.jsonl.zstd.bak-20261026023954.`，**重启 DSH 后生效**。

