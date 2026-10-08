# remind 独立审查 · 2026-10-09

对象：dev（main）的 `src/qq_roleplay_bot/remind.py`、`dialogue_judge.py` 的 `<remind>`、`registry.remind|remind_send`、
`onebot_ws.send_at`、`tests/check_module_removal.py`；yunru-stage4（plugins）的 `src/qq_roleplay_bot/plugins/remind/**`、
`plugins/mail` 新窄口（`MailSeams.send_to` + `_one_address`）、`tests/check_plugin_branch_scope.py`。
只读审查：除本文档外没写任何仓库文件；中途做过两次突变，**都已 `git restore` 还原**（见最后一节）。

## 一句话结论
接缝够窄（三个对象的字段逐个核过，没有 transport / 任意 action / 权限名单 / 回调 / 引擎），QQ 那一侧收件人
永远是请求者本人；但有 **2 条必须修**、**1 条必须人来裁**，另有 8 条较轻。可以上线，前提是先修那两条。

## 逐条核对表

| 要审的 | 结论 | 依据（文件:行） | 实测还是读码 |
| --- | --- | --- | --- |
| 1 接缝够不够窄 | **够**。`RemindDelivery(person_id,text,group_id)`、`RemindSendResult(sent,reason,detail)`、`RemindRequest(person_id,session_id,group_id,at,what,email)`；`transport/engine/call_action/action/callback/admin_user_ids/memory/...` 一个都不存在 | dev `remind.py:56-106`；实测脚本打印 `__dataclass_fields__` 与 `hasattr` 全部为空 | 实测 + 读码 |
| 2 边界 | `plugins/remind/**` 只有注释里出现 memory/engine/transport（`remind.py:52,55,202`），无一处真接口；全目录无 `call_action`/`send_msg`；插件也没有 `build_prompt`/`build_judge_hint`/`PromptPlugin`。mail 目录里确有 `BASE_PROMPT`/`memory_service`（`daily_report.py:126,225`），但那是 mail 自己的写信活，最后改动是 2026-10-05，与本夜无关 | yunru `plugins/remind/remind.py:52,55,202`；`git grep` 两个目录（`build_prompt` 等零命中）；`daily_report.py:126,225` 的 `git log -1` = `1268786` | 读码 + 实测（grep） |
| 3 "删掉插件核心照跑" | **在带插件的那棵树上没有机械覆盖**：dev 版七项、退出 0（`remind` 那行"能（已解耦）"），但 dev 没有 `plugins/remind/`，那一拦是恒等操作；插件线那份脚本还是旧版（六项、`remind` 不在 `DEFAULT_MODULES`）。我自己在插件树上把 `qq_roleplay_bot.plugins.remind` 整棵拦掉再 `build_engine`：`engine=yes`、发现器只记一行 `plugin_import_missing_module` 就跳过——判据成立 | dev `tests/check_module_removal.py:97-98,140,365-377,491-498`；yunru `tests/check_module_removal.py:88-89,126`；`plugins/__init__.py:1012-1021` | 实测（两棵树各跑一次 + 手写探针） |
| 4 六种结果 | `ok` 已发；`no_target`/缺接缝 → PENDING（`mark_pending` 不动 `tries`）；`not_delivered`/`error` → FAILED 记一次（MAX_TRIES=3）；`rejected` → 说一次人话 + 留待发；`uncertain` → GIVEN_UP 不再试。到点那条路逐条对齐；**但"问一句怎么叫他"那条路不按这套走**（见对抗 2） | yunru `plugins/remind/remind.py:445-474,369-410`；`store.py:391-413,62-70`；实测 `test_remind_plugin.py` + `test_mail_send_to.py` = `ran=31 failures=0` | 实测 + 读码 |
| 5 邮箱那条 | 两件事都真：地址**只**存在插件自己的 `data/remind/people.jsonl`（`remind.py:230-231`、`store.py:248-263`）；核心**确实**把它交给既有记忆捕获（`stage3_main.py:1643-1648`）。"私聊给的邮箱进不了记忆库"也真：`memory_inbox.offer` 第一句就是 `group is None → return False`，私聊没有群号 | `store.py:248-263`；`stage3_main.py:1643-1648`；`memory_inbox.py:33-37`；`remind.py:108-112` | 读码（+ `find_email` 实测） |
| 5b 闸门 `_one_address` | 空/空白、逗号或分号或空格分隔的多地址、`\n`、`\r\n`、`all`、`a@b` 全拒；`a@b.com\n` 收（先 `strip`，换行没进结果，不构成头注入） | yunru `plugins/__init__.py:350-370,741-760` | 实测（12 个输入逐个跑） |
| 6 对抗审查 | 有 4 处能真的做出事（下一节的 1-4），"不确定绝不重发"只在到点那条路成立 | 见下 | 实测 |
| 7 文档诚实性 | dev `AGENTS.md:187,191-208` 的"七项"与"只拦插件子树"**都真**（跑出来 7 行）；"核心真去 import 插件时 core=不能"我自己突变验过。`CONFIG_GUIDE.md` 里**一个字没提**提醒/`/提醒`/`data/remind/*.jsonl`（缺口，不是假话）；"核心查好"这个措辞不准（见修法那节） | dev `AGENTS.md:187,191-208`；`docs/CONFIG_GUIDE.md`（全文，无 remind 命中）；`stage3_main.py:1200`、`remind.py:69` | 实测 + 读码 |

## 对抗审查发现（按严重度）

1. **给任意单个地址寄信，没有任何授权或频次闸**（需人裁）
   能做什么：任何能触发提醒的人，按"先给一个邮箱 → `/提醒 换 3`"三步选定邮件那一路，就能让**操作者的邮箱**
   往**他指定的任意一个地址**寄信；条数不限（每条约会一封）。
   怎么做：核心把地址当"他的邮箱"收下（`stage3_main.py:1637-1640` 只做 `find_email`），插件 `_deliver_mail`
   照地址发（`remind.py:428-443`），接缝只挡"不是一个地址的形状"。
   影响：操作者邮箱成了可被外部人调用的发信口（主题/正文模板固定，`{what}` 可控；无白名单、无每日上限、
   无二次确认）。证据：上述读码 + 实测 `_one_address` 收下任意合法单地址 + `mail_client.py:152-155` 直接用它做 `--to`。
2. **`uncertain` 在"问一句怎么叫他"那条路上每拍重发**（必须修）
   能做什么：问话可能被重复送到他面前，且**永不封顶**。怎么做：`_ask_pending` 只区分 `SENT_OUT`/`PENDING`，
   其余（`NO_SESSION`/`GIVEN_UP`/`RETRY`）一律只记一行日志、状态不动 → 下一拍照发。
   影响：与模块头那张表"`uncertain` 记成不再试"自相矛盾；30 秒一拍，可无限重复（`rejected`/`not_delivered`/`error` 同样）。
   证据：实测 4 拍、四种失败各送出去 4 次，`asked=False`、`ask_session` 原样（`remind.py:348-367`）。
3. **`rejected` 的"只对他说一次"是按条记的，不是按人**（建议修）
   能做什么：同一个人三条提醒同时到点且都被拒 → 他在群里收到**三遍**"私聊得你先来私聊我一次…"。
   证据：实测 `NO_PRIVATE 说了次数 = 3`；`told_no_session` 存在 `Reminder` 上（`store.py:136-138,415-421`）。
4. **插件那份待发/提醒列表无上限**（建议修）
   能做什么：一拍把 300 条全交出去（300 条消息突发），发过的条目仍留在文件里（已发的 300 条一条不清）。
   证据：实测 `add 300 → due 300 → 一拍送出 300，reminders.jsonl 600 行，文件里仍有 300 条`；
   核心那两份有上限（`MAX_REMIND_ASKS=64`、`MAX_REMIND_EMAILS=256`，实测塞 65/257 条后最旧的被淘汰）。
5. **邮件那一路会被"完全正常的内容"确定性卡死，而且不告诉他**（必须修）
   `MAIL_SUBJECT = "到点提醒：{what}"`，`what` 里带 `&`、`"`、`%` 之一 → `clean_subject` 拒发（argv 白名单），
   `_deliver_mail` 一律当"可重试"→ 3 次后永远卡在 `failed`，用户只看到它一直挂在"待发"里。
   另有地址口径差：接缝收 `lele@example.c0m`、mail 的 `clean_email` 拒 → 白烧 3 次。
   证据：实测三句 `what` 全被 `bad_subject` 拒；`mail_client.py:51,86-96`。
6. **@ 的注入面**：`send_at` 把 `person_id` 原样塞进 at 段，`_id_value` 只试 `int()`，失败就回原串——
   实测拼出 `{'type':'at','data':{'qq':'all'}}`（NapCat 语义 = @全体）。但 `person_id` 来自事件的 `user_id`
   （`onebot_ws.py:164` 只做 `str()`、不校验数字），真实适配器给的是数字，所以要打得中得先能伪造事件。
   定级：低危（信任适配器），但一行 `isdigit` 就能关掉。
7. **插件线那份门槛脚本会假红**：默认中文控制台（GBK）下打印 `↔` 触发 `UnicodeEncodeError`，**退出码 1**
   （实测；加 `PYTHONIOENCODING=utf-8` 才是 `通过：检查了 6 项`、退出 0）。点 `remind` 还会拦到**核心**模块
   `qq_roleplay_bot.remind` 并报"还被拴着"——正是 dev 新版修掉的那个"红错对象"。
8. `REQUIRES = ("mail",)` 的实际后果是"没有 mail 插件就整个提醒功能消失"，而 `plugin.py:15-23` 的注释说的是
   "取不到就不提供邮箱那一路"（读码，未做删 mail 的实测）。
9. `channel=group` 但 `group_id=""` 时，核心按私聊发（实测目标 `user_id`），与插件回的"好，到时候我在群里 @ 你"不符。
10. `_remind_emails` 是**内存 dict**，只由当轮消息写入，重启即空；文档里"核心**查好**递过来的值"
    （`remind.py:69`、`stage3_main.py:1200`）没有"从记忆回查"这一步——他上次给过的地址在插件 `people.jsonl` 里，
    核心这一侧却会重新问一遍。证据：`git grep _remind_emails` 只有那 6 处。

## 我**没能证实/证伪**的
- 没跑整套离线测试（`AGENTS.md` 里"1199 个"那句我没核）。只跑了 remind 相关的：dev `ran=37 failures=0`、
  插件线 `ran=31 failures=0`，加两道门槛脚本。
- 没有真的发出一封邮件（要本机 `agently-cli` 在位）；只到 `MailClient` 的参数校验层。
- NapCat 收到 `qq:"all"` 到底是不是 @全体、真实事件里 `user_id` 有没有可能不是数字——本机模拟，没上真机。
- 插件树上那份**新版** `check_module_removal.py` 我是用手写探针复刻的（没把脚本搬过去，避免写仓库文件）。

## 建议的修法
- **必须人来裁**：邮件那一路要不要对外可用（白名单 / 每人每日上限 / 只允许"已验证是他的地址"）。
- **必须修**（两条）：
  1. `_ask_pending` 的失败分类对齐 `_send_due`——`uncertain` 不再试、`not_delivered/error` 计次并封顶、
     `rejected` 说一次并留待发（否则就是自相矛盾的无限重发）；
  2. 邮件那一路：`bad_subject`/`bad_recipient` 这类确定性失败**不许记成"可重试"**，而且要给他说一句人话
     （或主题改成固定文案、`what` 只进正文），否则"寄到邮箱"这个选项会安静地永远不生效。
- **建议**：`rejected` 的"只说一次"改成按人记（挪到 `Preference`）；待发列表与一拍的送出条数加上限、
  已发/取消的条目加过期清理；`send_at` 校验 `person_id` 是数字（拒 `all`）；
  插件线那份门槛更新到 dev 新版并把 `↔` 换掉（或在插件树上跑新版）；
  `CONFIG_GUIDE` 补 `data/remind/*.jsonl` 与 `/提醒`（或明说不含插件线）；
  把"核心查好"改成实话（内存里按人存、重启丢，没有从记忆回查这一步）。

## 我做的突变与还原（如实交代）
1. dev `runtime.py:81` 后临时加 `from .plugins.remind import RemindCapability`，跑 `tests/check_module_removal.py` →
   7 项全红、退出 1，`remind` 那行的原因是 `blocked for the probe: qq_roleplay_bot.plugins.remind`；随后
   `git restore -- src/qq_roleplay_bot/runtime.py`。
2. yunru-stage4 `runtime.py:81` 后加同一句，用 meta-path 拦 `qq_roleplay_bot.plugins.remind` 再 `build_engine`：
   改前 `core=yes engine=yes`（且发现器只记 `plugin_import_missing_module name=remind` 就跳过），改后
   `core=no ModuleNotFoundError: blocked for the probe`；随后 `git restore -- src/qq_roleplay_bot/runtime.py`。
两棵树跑完 `git status --short` 都回到原样（dev 只剩本来就有的未跟踪 `docs/LOGGING_PLAN.md`、`yunru-source/`；
插件线只剩 `yunru-source/`）。本次审查除本文档外没有提交或改动任何文件。
