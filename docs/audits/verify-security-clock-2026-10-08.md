# 核查报告 · 安全面与时间口径

被核查对象：`docs/audits/security-clock-2026-10-08.md`（提交 `a52ae55`，8 条：A-01…A-06、B-01、B-02）。
核查方式：只读源码 + 只读复现（`dev/src/qq_roleplay_bot/`、`dev/tests/`）。未改源码、未碰 `run\`、未碰 `.env`、未碰 `data/memory/`、未跑离线测试套件、未 push。
核查时的树：`main` @ `a52ae55`，`git status --short` 只有一行未跟踪的 `yunru-source/`（工作区是干净的），所以下面所有行号都是**当下这一份源码的真实行号**，不是抄审计里的。
标注规则：**实测** = 我读到的文件与行号，或我离线跑出来的渲染结果；**推断** = 由代码结构推出、未在运行中复现。

## 一句话结论（这份有几成可信 / 有没有必须更正的）

**结论方向我核过了：成立，没有一条需要推翻。** 8 条的现象描述全部对得上源码，引用的代码片段**逐字**与我读到的一致（我抽查了 14 处引文的每一行），行号绝大多数准确。

但有 **1 条必须更正、3 处要收紧**，而且被更正的那条正好是它自己最重的一条（A-02）：

1. **必须更正（A-02）**：它说"QQ 号进 prompt"只有三条路，并且把"用名册编号做身份、不用 QQ 号"当成既有设计。**都不准确。** 名册那一行（`stage3_runtime.py:196-199`，进**稳定段**，每轮每人都印）就是 `1=小K（QQ10001）`，QQ 号是**常规**写进 prompt 的；而且 `tests/test_roster_roles.py`、`tests/test_dialogue_judge.py:192-193` 把"名册里有 QQ 号"**逐字钉住**了——所以"名册编号代替 QQ 号"不是设计、更不是被破坏的设计，这个前提本身错了。它对 A-02 的严重度（中）因此偏低。
2. **要收紧（"未发现"第 1 条）**：按它说的五类来源扫，**确实没有**用户/模型/资料/插件内容进 system 段（我另做了 AST 全扫 + 逐个消费点追，见"两处重点"）。这句话作为**请求路径**的结论成立。但同一个模块里，**完整 system 正文与全部 user 段会被原样写进 `data/logs/*.jsonl`**（`stage3_main.py:1441-1442` → `feature_log.py:237-239`，单字段 2 万字，默认开），"清账"只清了进出模型的请求，没提落盘。这是它完全没写的一类。
3. **要收紧（A-06）**："代码注释自己承认了与 fail-closed 的张力"——注释确实写了（`stage3_main.py:504-505`，我核过），这句话不算编；但 `QQBOT_ADMIN_DENY_NOTICE=0` 被它说成"一键退回静默"，**只管被拒 `/admin` 的默认回话**，管不了 `/super permit` 那份带成员 QQ 的列表（`_permit_usage` 不看这个开关）。把两者绑成一个裁决点是误导。
4. **标明（A-04）**："`persona` 这一套实际上没有消费者"过强：`builtin("persona")` 被 `available()` / `backfill()` 用到（`prompt_library.py:210-213`、`:404`、`:261`），测试还要求它出现在名单里；准确说法是"没有任何**运行时**消费 `text("persona")`"。

**A-05 它自己标了"推断"，我确认它真的只是推断**：代码路径实测无误，但"那 500 字里有什么"没有样本，不能当事实引用。

## 逐条核对（8 条）

### A-01　超管命令的回复发回原会话 + 本机绝对路径（中）——**对**

- **文档原话**：`/super processes|lan|fan|status` 的结果发到敲命令的那个会话；`/super quote|slang` 结尾附本机绝对路径；证据 `stage3_main.py:3482-3484`。
- **我的核对**：
  - 顺着 `message.target` 走了一遍：`handle()` 的 `/super` 分支（`stage3_main.py:3469-3484`）拿到 `reply` 后，**`str` 类**回复一律 `return OutgoingMessage(message.target, reply, paced=False)`（`:3484`）；`OutgoingMessage` 类回复（只有 `/super permit` 会走，`:3482-3483`）直接返回它自己，而它的 target 也是发起命令那个会话。`message.target` 来自入站事件，群里发的 `group_id` 就是那个群——**"群里的超管命令 → 回复进群"成立**。
  - **还有一条它没写的同源路径**：`/super processes cpu` 走 `_spawn_diagnostics(message.target, mode)`（`:1776-1781`），采完 5 秒由 `runtime.py:592-597` 的 `_send_diagnostics(target, text)` 发出去，target 还是那个 `message.target`。同步那条被它引了，异步这条**同样把本机进程表发回群里**，文档里没有。
  - 三处路径行**逐处核过**，三处都是 `lines.append(f"（文件：{store.path}）")`：`:2375`、`:2443`、`:2493`（`grep` 全 `src/` 只有这三处这个形状）。`store.path` 是绝对路径：`quote_learning.py:227-234`（`Path(__file__).resolve().parents[2] / value`，默认值 `data/quote/profile.json` 见 `dev_config.py:581`），`slang` 那份同形状（`slang_learning.py:295` + `dev_config.py:598`）。
  - `runtime_diagnostics.py:137-139` 确实产出 `超管诊断：<title>` + `ProcessName | PID <id> | 内存 <MB>MB`，`:124-140` 起 PowerShell 取数：**对**。
  - 适用范围：`_is_super_admin_control`（`:1735-1743`）只看 `message.user_id`；`memory_view.py:182` 明写"超管命令是全局的：在哪个群、私聊都能用"——**都对**。
  - 反面参照 `memory_view.py:1`（"渲染成有界的、可在群里发送的文本"）与 `security.py:16-24`（`SENSITIVE_REQUEST_PATTERNS` 覆盖"本机+进程/文件/端口"）：**都对**。
- **判定：对。** 严重度"中"的理由（发起者是超管本人、泄漏物是本机信息 + 成员原文、无提示无开关不可撤回）站得住。
- **最小改法是否最小**：`message.target.group_id is None` 才发、群里回一句"请在私聊用"，是核心侧一处判断，**是最小**。但 `（文件：…）` 那三行改相对路径要注意：`store.path` 是唯一的路径来源，删掉那三行就没人知道文件在哪了——"只留相对仓库根的形式"需要新增一处"根目录在哪"的算法，不算纯删除。
- **要补一句**：群里 `/super quote note <id> off|on` 这类**会改数据**的命令（`:2372-2374` 列出的用法）本身也仍在群里生效，文档只谈了"看"，没谈"改"。

### A-02　群成员 QQ 号进 prompt（中）——**现象对、行号对，但前提与"三条路"的框定都错，严重度偏低**

- **文档原话**：三条路（`@` 段、`at=`/`main_partner=` 回落、长期记忆 `subject=`/`about=`）；"与名册编号代替 QQ 号的设计直接矛盾"。
- **我的核对**：三条路我都追到了**最终渲染成什么文本**（不是停在调用点）：
  1. `conversation_context.py:285-287`：`elif seg_type == "at": … parts.append(f"@{qq}" if qq else "@")`——`segments_to_text()` 的最后一行在 `:295`（审计引的 `:203` 是它的调用点，`:203` 处 `text = segments_to_text(raw.get("message"))`，**准**）。这一步把 `@` 段固定成字面量 `@QQ号`，再经 `stage3_main.py:1615-1621`（`state.add(item, …)`，补历史）进 `_format_history` → `<history>` 块。我离线跑了一遍：`segments_to_text([{at,qq=10001},{text:" 你看这个"}])` → **`'@10001 你看这个'`**，它落在 `<text>@10001 你看这个</text>` 里。
  2. `stage3_runtime.py:1213`：`targets.append(str(alias) if alias is not None else f"QQ{user_id}")`（审计把这条引在 `:1199` 上面，`:1199` 是 `_partner_label` 的裸号回落——**两处都对**，只是顺序与它标的行号反了）。渲染结果实测：`<message who="1" at="QQ10002" index="1">`；`_partner_label("10001", None)` → `'10001'`，进易变段 `main_partner=10001`（`:1580`）。**"不认识的回落裸号/QQ号"成立。**
  3. `memory_model.py:292-294`（`_about`）与 `:307-309`（`subject="{owner}"`）。实测：`names` 里有这个人 → `<memory … subject="10001" about="乐乐">`；`names` 里没有 → `about="10001"`。`subject=` 的来源我追到了写库那一步：`memory_store.py:736-737` 的 `subject_user_id = batch.user_id`（**QQ 号**），所以"`subject=` 恒为 QQ 号"**成立**。
  - 对照说实时路径刻意剥掉 `@` 段：`onebot_ws.py:40-57` 只取 `type == "text"`（`:53`），**对**。
- **必须更正的两处**：
  - **它说"实时路径刻意剥掉 `@` 段，所以 `@QQ号` 只从补上下文这一条路进来"——这句话把最大的那条路漏掉了。** `stage3_runtime.py:162-200` 的 `roster_lines()`（`<people>` 名册，进**稳定段** `stable_content`，`:1532`）逐人输出 `f"{alias}={name}{suffix}（QQ{…}）"`（`:196-199`）。实测输出：`1=小K（QQ10001）`、`2=阿甲（QQ10002）`。**这条不依赖 `@`、不依赖回落、每个出现在窗口里的人都有一行**——是"QQ 号进 prompt"里最常规、每轮都在的那条。文档的三条路一条都没算它。
  - **它把"名册编号代替 QQ 号"当成设计要求，并据此说实现"与设计直接矛盾"——这个前提是错的。** `tests/test_roster_roles.py:96/123/136/150/158/160/173` 与 `tests/test_dialogue_judge.py:192-193` 逐字断言名册行**必须**含 `（QQ…）`；`stage3_runtime.py:112-114` 的注释还写明这是**故意的**（"名字→QQ号→编号→说话人"要多跳一次，所以名册带上 QQ 号让旧名能一跳对上）。所以"用名册编号**而不是** QQ 号"（它引的 `:1579` 讲的是 `main_partner` 这个**字段**）不是全局设计原则，把 QQ 号从名册里拿掉会直接碰红那两组测试。
- **判定：现象与行号对，框定与前提错。** 严重度"中"**偏低**：泄漏不是"回落时偶尔发生"，而是**每一轮对每一个在场的人都发生**；模型复述的风险面比它写的大。
- **最小改法是否最小**：它给的 A-02 改法只覆盖它自己那三条（`@` 段、`subject`/`about`），**改完名册那一行照样印 QQ 号**，等于没解决它声称的问题。真要按它的前提动手，得连 `roster_lines` 一起改，并同时改那两组测试——那就不是"最小改法"了。

### A-03　插件档位 fail-open（中）——**对**

- **文档原话**：`level_of` 把拼错/没声明一律当 `public`；`session_allowed` 没实现当"任何会话都允许"；配套 `_plugin_level_allowed` 非 admin/super 一律 `return True`。
- **我的核对**：引文 `command_plugins.py:108-115`、`:210-219` **逐字一致**；`stage3_main.py:1865-1869` 是 `if level == "super" … if level == "admin" … return True`，**准**。我离线构造了三个假插件跑真函数，实测：
  - `min_level = "Supre"` → `level_of` 返回 `'public'`；
  - 完全不声明 `min_level` → `getattr(..., 'min_level', '<missing>')` 是 `<missing>`（`CommandPlugin` 协议里 `min_level: str` 只有注解、**没有值**，`hasattr(CommandPlugin, 'min_level')` 为 `False`）→ 也返回 `'public'`；
  - 没实现 `session_allowed` → `_session_allowed(...)` 返回 `True`；实现里抛异常 → 返回 `False`（这一支是 fail-closed）；`match` 抛异常 → `_matches` 返回 `False`。
- **一处要收紧**：`level_of` 的 docstring 写着"**不认识的档位不放宽**"（`:109`）。文档说"实际行为正好相反"——**方向对，但不精确**：认不出的档位落成 `public` 走正常放行路径，从这个意义上确实是放宽了；不过 `session_allowed` 那一支的行为是**收窄**（抛异常→False），不是放宽。真正被文档点中的是"**该收窄的（写错的特权档）被当成 public 放行**"。
- **判定：对。** 影响面确实在带插件的树（本体 `src/qq_roleplay_bot/plugins/` 我列过，只有 `__init__.py`），严重度"中"合理。
- **最小改法是否最小**：把"属性缺失"与"值不合法"分开处理，是一处函数改动，**最小**。

### A-04　机制词护栏只在保存时和测试里（中低）——**对，一处措辞要收**

- **文档原话**：`scan_text` 只在 `PromptLibrary.save()` 里跑；读覆盖文件不复验；人格文件加载不扫；幂等判据是子串存在；`persona` 没有消费者。
- **我的核对**：
  - `prompt_library.py:309-320`（审计引 `:309-320`，引文是 `:315-320`）**逐字一致**：`text()` 只做 `self._load()` + `self._cache.get(key)`，**不扫**。`load()`/`_read_file()`（`:275-307`）只检查 `version` 字段与 `text` 是不是非空字符串。
  - `grep` 全 `src/`：`scan_text` 只出现在 `prompt_guard.py:57`（定义）与 `prompt_library.py:354`（`validate()` 内），`validate(` 的唯一调用点是 `:372`（`save()` 内）。**"只有保存这条路扫"成立。**
  - `base_prompt.py:242-244` 的正则抽取与文档引文**逐字一致**；`:256` 的 `BASE_PROMPT = _load_base_prompt()` 是 import 期一次，**对**。
  - 测试侧 `tests/test_persona_shape.py:75` 确实是 `scan_persona_text` 的断言，**对**。
  - 幂等那段 `stage3_runtime.py:888-890`（`for rule in (...) if rule not in body: body += rule`）**逐字一致，对**。
- **一处要收**："`persona` 这一套实际上没有消费者"过强——`builtin("persona")`（`:210-213`）被 `available()`（`:261`）与 `backfill()`（`:404`）用到，`tests/test_optional_capabilities.py:156/181` 还要求 `persona` 出现在名单与版本里。准确说法：**没有任何运行时消费 `text("persona")`**（它只服务于面板列表与版本留档）。
- **判定：对（措辞收一下）。**
- **最小改法是否最小**：把 `scan_text` 挂到 `load()`/`text()` 与 `_load_base_prompt()` 上是**局部**改动，最小。"幂等判据改成必须出现在末尾"比它写的"子串存在"更严，但也更会误伤面板上手工粘过、顺序不同的覆盖版（会变成**每轮重复追加**）——真要改得同时给出"末尾在哪"的判据，不是一句话。
- **补一处它没提的守卫缺口**：`tests/test_two_clocks.py:164-165` 那类断言只扫**常量**（`TIME_IS_GIVEN_RULE`），不扫面板覆盖进来的 `reply` 正文；而 `prompt_guard.scan_text` 是按 `PERSONA_KEYS = {"persona", "reply"}`（`prompt_guard.py:54`）选判据的——**保存路径**会扫 `reply`（`:354`），**加载路径**不会。这与 A-04 同源，但文档把它笼统写成"人格文件"，没点出 `reply` 这条被覆盖的 system 也在同一张网里。

### A-05　`LLMError.detail` 带进模型端错误体（低，**推断**）——**代码路径对，后半段确实只是推断**

- **文档原话**：`_classify` 把 `f"status={exc.status} body={exc.body}"` 塞进 `detail`，进进程日志与功能日志；与类 docstring"绝不包含"矛盾。
- **我的核对**：`llm_client.py:258-263` 引文**逐字一致**；`_safe_error_body` 在 `:348-359`（`limit: int = 500`，`:359` 截断），**对**；`logger.warning("model_retry … detail=%s" …)` 实际在 `:243-250`（审计写 `:243-250`，**准**）；`stage3_main.py:3973-3979` 的 ERROR 与 `:3971-3972` 的 `_log_model_io(... error=summary)`，`summary = exc.safe_summary()`（`:3970`）= `f"{kind}: {detail}"`（`llm_client.py:63-64`）——**链路成立**；docstring 在 `llm_client.py:51-56`，引文"绝不包含 API Key、请求头或聊天正文"**逐字一致**。
- **判定：代码路径实测对；"body 里有没有敏感内容"我没有样本，**确认它只是推断**，引用时不可当事实。** 严重度"低"与它的自评一致。`QQBOT_DEBUG_MODEL_IO` 默认关（`model_trace.py:35-38`）与这条日志无关，**对**。

### A-06　未授权 `/admin` 默认回话 + `/super permit` 用法回显发起人 QQ（低，**有意为之**）——**行为对，"有意为之"成立，但一处把两件事绑在一起了**

- **文档原话**：默认回话可被 `QQBOT_ADMIN_DENY_NOTICE=0` 关掉；`/super permit` 用法里列出"本群最近的被拒命令"，每条带 QQ 与命令原文前 40 字；证据 `:2583-2585`、`:508`、`:2607-2615`；"这一条是有意为之……代码注释自己也承认了与 fail-closed 的张力"。
- **我的核对**（这是"是不是真有意为之"的关键，我按注释/文档行号逐处查了）：
  - 默认开：`stage3_main.py:500-508`，`:508` 就是 `os.environ.get("QQBOT_ADMIN_DENY_NOTICE", "1").lower() not in {"0","false","off"}`，**行号与内容都对**。
  - 默认回话：`_denied_admin_reply`（`:2560-2585`），只有 `:2585` 真的 `return OutgoingMessage(message.target, _admin_deny_notice(), paced=False)`；`:2579-2584` 三种静默（私聊 / 已是管理员 / 开关关掉）。审计引的 `:2583-2585` 落在这一段里，**准**。
  - **"有意为之"确实有出处，不是审计替它开脱**：`:503` 写"默认回（用户 2026-09-28 要求："回复无权限用户的 admin 命令"）"，`:504-505` 写"但 AGENTS.md 里"未授权时静默回落、不暴露层级存在"是安全侧的默认，所以留 `QQBOT_ADMIN_DENY_NOTICE=0` 一键退回静默"。`memory_view.py:205-207` 也记着用户要求。**这条是代码里明写的用户要求，审计不是替它开脱。**
  - `/super permit` 的列表：`_permit_usage`（`:2601-2618`），`:2607` 取 `_recent_denied_admins(group_id)`，`:2610-2614` 输出 `· {item.user_id}：{text[:40]…}`。审计引文用 `...` 省略了 `:2612-2613` 的截断，**内容与行号都对**。
- **要收紧（这条最实质）**：文档把两件事捆成一个裁决点——**"`QQBOT_ADMIN_DENY_NOTICE`（`:508`，`0` = 完全静默）——现成的裁决点"**。事实上：
  - `=0` 只让"未授权 `/admin` 的默认回话"变成不回（`:2583-2584`）；
  - `/super permit` 的那份列表**不看这个开关**，超管不带引用发一次就会在群里列出成员 QQ 与命令原文（`:2630-2633` 只在没有 `reply_to_message_id` 时落到 `_permit_usage`）。
  所以"要不要恢复静默默认"与"要不要把列表限定私聊"是**两个独立裁决点**，后者没有现成开关。它自己的"最小改法（要改的话）"里其实分开了，但"可调项"那一行没分开。
- **一处漏**：`_denied_admin_reply` 只记**能解析出来**的 `/admin` 命令（`:2572-2573` 解析不出就 `return None`，既不回也不记）；`_recent_denied_admins` 只列**本群**、且 5 分钟内（`DENIED_ADMIN_TTL_SECONDS = 300.0`，`:497`）。文档写"本群最近的被拒命令"没错，但没说"私聊永远不记不回"（`:2579-2580`）——这条其实是对它有利的证据，漏了。
- **判定：行为与"有意为之"都对；把两个开关当一个是实质错误。**

### B-01　时刻显示依赖服务器本地时区、代码把"本地=北京"写死（中）——**对**

- **文档原话**：全部由 `datetime.fromtimestamp` / `time.localtime` 渲染；代码明确假定"服务器本地就是北京"；UTC 机器上"群里这边"差 8 小时且不报错。证据 `stage3_runtime.py:1426-1429`、`dev_config.py:288-290`、`stage3_main.py:337-340`。**并要求搜 `tz` / `utcoffset` / `timezone` / `astimezone`。**
- **我的核对（命中清单，全 `src/`）**——搜 `\btz\b|utcoffset|timezone|astimezone|pytz|zoneinfo|localtime|gmtime|fromtimestamp|UTC`，共 **5 处命中，无一处是显式时区**：
  1. `verify_log.py:212`：`datetime.now().astimezone().isoformat(...)`——**留痕日志**用的，与给她看的时间无关，无偏移参数（用机器本地）；
  2. `stage3_runtime.py:817`：docstring 里写着"不出现"时区"/"UTC"这类词——**注释**，不是代码；
  3. `stage3_runtime.py:1427`：`here = datetime.fromtimestamp(moment)`——**就是被点的那行**；
  4. `stage3_main.py:340`：`time.strftime("%m-%d %H:%M:%S", time.localtime(timestamp))`；
  5. `stage3_main.py:2528`：`time.localtime(updated)`（画像更新时间那一行）。
  此外 `pytz` / `zoneinfo` / `timezone(` / `utcoffset(` / `gmtime` **零命中**。**"代码里确实没有显式时区"成立。**
- 引文核对：`stage3_runtime.py:1426-1429` **逐字一致**（`:1426` `moment = …`、`:1427` `here = …`、`:1428` `there = here + timedelta(hours=dev_config.persona_offset_hours())`、`:1429` 返回两句话）；`:1404-1424` 的 docstring 里 `:1421-1423` 明写"服务器本地时间（部署在北京）"，`dev_config.py:288-290` 的注释**逐字一致**（"服务器本地就是北京，所以这只是"她那边"相对这边的固定小时差"）；`stage3_main.py:337-340` 的 `_format_clock` **逐字一致**。
- **"本机零影响、上云差 8 小时"的推理链我自己走了一遍**：
  - 这台机器 `Get-TimeZone` = `China Standard Time`（UTC+08:00）→ 本机现在与"北京"一致，**零影响**；
  - 渲染只经 `datetime.fromtimestamp` / `time.localtime`，**没有** `tzinfo` 参数、没有 `astimezone`、没有 offset 配置；
  - 实测（同一份代码、同一个 epoch、只换进程本地时区）：`Asia/Shanghai` → `群里这边是 … 16:40`；`UTC` → `群里这边是 … 08:40`——**正好 8 小时**；"她那边"两处都是 +3 小时，跟着一起平移。
  - 所以 `QQBOT_PERSONA_OFFSET_HOURS` 调不了"这边"（它只加在 `here` 上），文档"可调项"那句**对**。
- **一处它没做的验证（我做了）**：它说"本机测不出来"，我想验"测试能不能替我们拦住"。结果：`tests/test_two_clocks.py` 里那 6 个纯函数在 `TZ=UTC` 下**照样全过**——因为 `BEIJING = datetime(2026,10,8,22,12).timestamp()` 与 `fromtimestamp()` 是**同一台机器上的一对逆运算**，两边一起平移，断言不变（`:34-35` 的注释说的就是这件事）。**这坐实了它的判断：没有任何测试能在换机器时报警。**（另：Windows 上 `time.tzset` 不存在，POSIX 的 `TZ` 只能部分影响 CPython 的本地时间；我上面的 8 小时是用"显式构造带偏移的时刻"对照出来的，不是靠 `TZ` 骗进程。）
- **判定：对。** 严重度"中"合理。最小改法（显式 offset + `timezone(timedelta(...))`）是最小形状，但它没提**代价**：`_format_clock` 有 3 个调用点（`:2375`/`:2443`/`:2493` 之外还有 `:2491`、`:2528` 一带）都要跟着换成带时区的渲染，不然同一个回复里会出现两种时间口径。

### B-02　时长用"墙上时间点之差"（低）——**对**

- **文档原话**：运行时长、出站补发存活期、账本落盘节流三处用了墙上时刻之差。
- **我的核对**：
  - `stage3_main.py:1121` `self.started_at = time.time()`——`time.time` 是墙上时钟（`:1124-1128` 另存 `self.clock` 单调、`self.wall_clock` 墙上，注释在 `:970-973`），**对**；`:2742` `uptime = max(0.0, time.time() - snapshot.started_at) if snapshot.started_at else 0.0`，**逐字一致**。
  - `outbox.py:105-107` `_expired` 的 `now - item.queued_at > self.max_age_seconds` **逐字一致**；`queued_at = self.clock()`（`:91`），而 `Outbox` 的 `clock` 默认是 `time.time`（`:56`），装配点 `runtime.py:2023` 用 `Outbox()` 不传 clock → **确实是墙上时钟**，"对"。
  - `api_usage.py:110-118` `if not force and now - self._last_save < SAVE_INTERVAL_SECONDS`（`:113-115`）**逐字一致**，`clock` 是构造参数（`:61`），**对**。
  - **还有一处同类**它没列：`stage3_main.py:3063` `age_hours = max(0.0, (time.time() - float(newest.get("at") or 0.0)) / 3600.0)`——"最近一封信多久以前"，同样是墙上时刻之差。
- **判定：对。** 严重度"低"合理（只在系统时钟被调整那一刻出错）。
- **最小改法是否最小**：把"过了多少秒"统一改用 `self.clock()` 是**局部**改动；但注意 `snapshot.started_at` 是快照的一部分（`:1631-1639` 一带），换口径要同时看 `started_at` 有没有被序列化/被其它读取点用——审计只指了 `:1121` + `:2742` 两行，实际要动的比两行多。

## 两处重点

### 一、"没有找到聊天/记忆/资料/插件内容进 system 段"——**我试着推翻了，没推翻；但它的"清账"只清了请求，没清落盘**

**我扫的方法（不是抽看，是全扫 + 逐个消费点追）：**

1. **AST 全扫**：对 `src/qq_roleplay_bot/` 下每个 `.py` 解析出所有 `role="system"` 的消息构造（关键字形式与字典字面量两种都试）。命中的 system 段构造点共 6 处（`feature_log.py:232`、`quote_learning.py:979` 那两处是**日志/留痕**字段名，不是给模型的 messages，已排除）：
   - `stage3_runtime.py:1669-1672`（回复段）→ `compose_system_prompt(resolve_system_prompt() | system_text)`；
   - `dialogue_judge.py:292-294`（判定段）→ `resolve_judge_prompt()`；
   - `memory_maintenance_agent.py:169`（记忆维护）→ `_resolve_prompt("memory", …)`，`:167` 这个别名就是 `prompt_library.resolve`；
   - `style_reviewer.py:302`（风格审核）→ `_resolve_prompt("review", …)`，`:300` 同一个别名；
   - `dialogue_compaction.py:99` → **硬编码常量** `COMPACTION_SYSTEM_PROMPT`（`:36`），**连 prompt_library 都不过**；
   - `quote_learning.py:715` → `learning_system_prompt()`（`:721-724`）**直取模块常量**，`:722` 的注释自己写着"面板将来要能改的话，走 prompt_library 那一套；这里先直取常量"。
   （识图那套在本体树不存在：`src/qq_roleplay_bot/plugins/` 只有 `__init__.py`，`_provided_prompt("vision")` 返回 `None`。）
2. **五类来源逐个追**，并额外找"第五类"：
   - 模块常量：`SYSTEM_PROMPT = BASE_PROMPT + """…"""`（`stage3_runtime.py:682`），三条追加规则是常量（`:888-890`），逐轮逐字相同——**对**；
   - `prompt_library` 覆盖：`text()`（`:309-320`）只读 `data/prompts/<name>.json`，`load()` 只查 `version`（`:304-307`）——**对**；
   - 插件登记：`_provided_prompt`（`:161-188`）只被 `builtin("vision")` 用（`:241`），登记 `reply`/`judge`/`memory`/`persona` **无处读取**，静默忽略——**对**（fail-closed，不是漏洞）；
   - 人格文件：`_load_base_prompt`（`base_prompt.py:197-252`），正则抽 `BASE_PROMPT = """…"""`，import 期一次（`:256`）——**对**；
   - **第五类候选我逐条排掉了**：① 环境变量注入——`stage3_runtime.py` 里 `os.environ`/`getenv` **零命中**，能进 system 的环境变量只有 `QQBOT_BASE_PROMPT_FILE`（人格文件路径，见第 4 类）；② 面板/配置覆盖——`operator_config.SETTINGS` 全表我列了（`:42-96`，32 个键，模型/凭据/审批/开关），**没有** `QQBOT_BASE_PROMPT_FILE`，也没有任何键指向 prompt 正文（`grep BASE_PROMPT` 在 `operator_config.py` 里 **0 命中**）；③ 知识库/资料——`_extension_data`（`stage3_runtime.py:1348-1366`）产出的 `<extra_prompt>`/`<plugin>`/`<knowledge>` 三块经 `:1590-1593` 包在**易变段**；④ 邮件/信——`_letter_block`（`:1369-1379`）经 `:1641` 进**易变段**；⑤ 摘要——`<earlier_summary>` 在**稳定段**（`:1533-1535`），但它是 user 段，而且 `parse_compaction_output`（`dialogue_compaction.py:104-124`）会剥标签、限长、按 `memory_filters` 剔"关于系统自身"的内容。
   - **结论：system 段在请求里确实只有这四类来源，没有第五类。它这句清账，核过，成立。**
3. **但"清账"的清法有一处它没覆盖**：同一次调用，**system 全文 + 全部 user 段会被原样写进功能日志**：`stage3_main.py:1441-1442` 的 `_log_model_io` 把 `request_parts(request)` 拆成 `system`/`user_content` 交给 `self.logs.record(...)`，`feature_log.py:237-239` 对三个字段**只做 2 万字截断**（`MAX_FIELD_CHARS = 20000`，`:49`），默认开（`logs_enabled()`，`:57-62`），落在 `data/logs/<feature>.jsonl`，每功能最近 1000 次（`:47`）。`feature_log.py:30-32` 的模块说明**自己写着**"内容含完整 system prompt 与聊天正文"——也就是说这是**有意设计**，不是 bug（`data/` 被 gitignore）。但它意味着："没有内容**进** system 段"为真，"没有内容**被记下来**"为假，而且是默认记账。这条正好落在 AGENTS §2.1"不记录敏感内容"那句话的边上，而这份审计为了 A-01 的"成员原文进群"给了"中"，却没提默认落盘的量级大得多。**这是我认为它最该补的一条。**

### 二、A-06"有意为之"——**是真的有意为之，不是审计替它开脱；但它把两个开关当成了一个**

- 判定依据不是审计的转述，而是代码自己的字：`stage3_main.py:503`"默认回（用户 2026-09-28 要求："回复无权限用户的 admin 命令"）"、`:504-505`"但 AGENTS.md 里"未授权时静默回落、不暴露层级存在"是安全侧的默认，所以留 `QQBOT_ADMIN_DENY_NOTICE=0` 一键退回静默"；`memory_view.py:205-207`"用户 2026-09-28 要求：'普通成员可以呼叫 /admin help 调起 help 菜单'"。**"用户明确要求回一句"与"代码自己承认与 fail-closed 有张力"这两句，在源码里都能逐字找到。**
- 所以把它与真缺陷分开列、标"低·裁决点"，是**正确的处理**，不是开脱。
- 但它那条"可调项"把两件事绑成了一个开关（理由见 A-06 正文）：`QQBOT_ADMIN_DENY_NOTICE=0` 管不了 `/super permit` 那份带成员 QQ 的列表。"要不要静默"和"被拒命令列表给不给群里看"是**两个**裁决点，后者当前没有开关。
- 顺带：这一条与 A-02 其实是**同一个信息面**——`_permit_usage` 把 `item.user_id`（QQ 号）与命令原文发到群里，正好是 A-02 关心的那类身份信息，只是走的是"回复"而不是"prompt"。文档把它们分在两条里，没有互相引用。

## 它漏掉的

按"同一区里它没提、我顺手发现的"排，第一条是重点：

1. **名册那一行把 QQ 号常规写进 prompt（A-02 区，且推翻它的前提）**：`stage3_runtime.py:196-199` 的 `1=小K（QQ10001）`，进**稳定段**（`:1532`），每轮、每个在场的人都有一行；`tests/test_roster_roles.py:96` 等逐字钉住它。它把 A-02 写成"三条回落路"，漏掉了这条**主干**。
2. **完整 system + user 段默认落盘**：`stage3_main.py:1441-1442` → `feature_log.py:237-239`（2 万字/字段、默认开、`data/logs/*.jsonl`）。这是"内容隔离"审计该报的量级，文档一个字没提。
3. **`/super processes cpu` 的延迟回发同样把诊断发进群**：`stage3_main.py:1776-1781` → `runtime.py:592-597`。A-01 只引了同步那条。
4. **同一回复里两种时间口径**：`_format_clock`（墙上/本地）与台账、画像更新时间（`:2491`、`:2528`）混用；B-01 的最小改法没提这几处也要一起换。
5. **B-02 还有第四处**：`stage3_main.py:3063` 的 `age_hours = (time.time() - at)/3600`。
6. **`reply` 覆盖版也在 A-04 那张网里**：`prompt_guard.PERSONA_KEYS = {"persona", "reply"}`（`prompt_guard.py:54`），保存路径扫 `reply`、加载路径不扫——文档笼统写成"人格文件"。
7. **`dialogue_compaction` 与 `quote_learning` 的 system 完全绕过 `prompt_library`**：前者硬编码常量（`dialogue_compaction.py:99`），后者自认"先直取常量"（`quote_learning.py:722`）。所以"面板能改 system 的只有 `data/prompts/*.json`"这句在**范围上**要限定成"回复/判定/记忆/审核 四套 + 识图（插件在时）"，压缩与金句学习这两套面板**改不了**。文档的"未发现"第 2 条把它说成全称了。
8. **A-06 里对自己有利的一条被漏掉**：`_denied_admin_reply` 在私聊、在"已经是管理员的人"身上都静默（`:2579-2582`），不是无差别回话。

## 我复现了哪些"实测"

都是**离线、只读**，没有启动 bot、没有连 QQ、没有跑离线测试套件（`tests/run_offline.py`）：

1. **时区依赖（B-01）**：同一份源码、同一个 epoch，只换进程本地时区 → `Asia/Shanghai` 印 `16:40`、`UTC` 印 `08:40`，差 8 小时。另外把 `tests/test_two_clocks.py` 的 6 个纯函数在 `TZ=Asia/Shanghai` 与 `TZ=UTC` 下各跑一遍 → **两边全过**，坐实"换机器没有测试会报警"。（Windows 无 `time.tzset`，所以这不是"骗进程"，是显式构造对照。）
2. **QQ 号进 prompt 的四条渲染（A-02）**：直接调真函数。① `segments_to_text` → `'@10001 你看这个'`；② `_format_message` → `<message who="1" at="QQ10002" index="1">`、`_partner_label` → `'10001'`；③ `MemoryMaterial.as_data` 两种情形 → `subject="10001" about="10001"` 与 `subject="10001" about="乐乐"`；④ `ConversationState.roster_lines()` → `1=小K（QQ10001）` / `2=阿甲（QQ10002）`（带角色时 `1=小K（QQ10001；…）`，普通成员不追加词）。
3. **插件档位 fail-open（A-03）**：假插件跑真函数——`min_level="Supre"` → `public`；不声明 → `public`；无 `session_allowed` → `True`；`session_allowed` 抛异常 → `False`；`match` 抛异常 → `False`。
4. **本机时区（B-01 前提）**：`Get-TimeZone` = `China Standard Time` / UTC+08:00 → 本机与"北京"一致，零影响。
5. **行号复核**：上面每条引用的行号我都直接读过那几行（不是 grep 计数），8 条共核对约 40 处行号 / 14 段引文。

## 我没做到的 / 读不到的

1. **没跑任何测试套件**（`tests/run_offline.py`、`pyflakes`、`check_module_removal.py` 都没跑）。为了让"两个时区下测试全过"这个说法有依据，我只**单独 import 并调了** `test_two_clocks.py` 的 6 个纯函数，不是跑套件——所以"这套测试拦不住时区变化"这句话是**实测**，但"整套测试是否全绿"我**没有验证**。
2. **没有真机复现任何一条**：没在群里敲 `/super processes`（A-01）、没换一台 UTC 机器跑 bot（B-01 的端到端）、没有真实 provider 错误体（A-05 那 500 字里有什么）。A-05 的后半段我同样标**推断**。
3. **插件树与面板不在本树**：`src/qq_roleplay_bot/plugins/` 只有 `__init__.py`，`stage4-plugins` 分支不在这次的工作区里。所以 A-03 的"影响面在带插件的树上"我只能核到核心侧判据（已核），**没能验证插件作者实际会怎么写 `min_level`**；面板自己的写盘路径（`data/prompts/*.json` 是不是只能经 `PromptLibrary.save`）也只能从核心侧接口推——我核到的是"核心侧读路径不扫"，**没核到面板侧有没有第二条写路径**（`workflow`/`webui` 不在本树）。
4. **`data/` 的真实内容没碰**（按规矩不碰 `data/memory/`，也没读 `.env`）。所以"本机生效的那份人格文件到底长什么样""`data/prompts/*.json` 现在有没有覆盖版"我**都不知道**——"system 只有四类来源"是**代码层**结论，不含"这台机器上现在真的有哪几套"。`base_prompt.py` 在我 import 时打了"人格正文回落到**模板**"的 WARNING，正好说明这台机器的真人格文件我没读、也不该读。
5. **A-02 的严重度重估是判断，不是测量**：我说"它把 A-02 定低了"，依据是"名册每轮每人都印 QQ"这个事实；但"模型到底会不会复述 QQ 号"我没有样本，**不能当实测**。
6. **没有第二个独立视角复核我这些行号**（与被我核查的那份文档同一处境）。
