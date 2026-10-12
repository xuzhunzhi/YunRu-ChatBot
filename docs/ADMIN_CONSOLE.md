# 管理员控制台设计（多管理员 + 超管）

> **状态：设计文档，未实现。** 本次只新增这一份文档，没有改任何代码。
> 依据（都读过）：`AGENTS.md` §2.1/§2.3、`admin_control.py`、`capabilities.py`、
> `runtime_flags.py`、`operator_config.py`、`control_audit.py`，以及面板侧
> `plugins/webui/*`（**只读**，一行没动）。
>
> **标记口径**（全文只用这三个）：
> - **【实测】** = 我读过那一行代码，`文件:行` 可以直接去核对；
> - **【待核】** = 事实层面我**还没有**验证（需要真机跑一次，或需要用户确认口径）——不许当结论用；
> - **【待定】** = 设计取舍需要用户拍板，我不替他决定。

---

## 0. 路径口径（先读这一节，否则 `文件:行` 会对不上）

面板**不在 `dev/`（main）那棵树上**：`AGENTS.md:141-142` 写着它"实现在 `stage4-plugins`
分支，这条分支上没有它"。【实测】本机 git 里那条分支叫 **`plugins`**。
**面板侧的引用口径（2026-10-12 起）**：一律引
`yunru-stage4/src/qq_roleplay_bot/plugins/webui/…`——`plugins` 分支工作区里的那一份，
§4.1 / §4.3 的行号都按它核过。
**`run/` 是尚未重新部署的旧副本**：写这份文档时它跟分支逐字节相同（`git hash-object` 比对：
`webui_access.py d9a7896a38`、`webui_panel.py eb32cf112d`、`wire.py 74f79ba603`、
`webui_data.py 08637d4854`、`plugin.py f63e9cb3f3`），但"插件自报界面"那处接缝落进
`plugins` 分支之后，`wire.py` / `webui_panel.py` / `app.js` 三份就只有工作区里是新的了
（`webui_data.py` / `webui_access.py` / `app.css` / `index.html` 至今仍逐字节相同），
`run/` 要等部署那一步才追平。

| 这一侧 | 本文的写法 | 说明 |
| --- | --- | --- |
| 核心（Stage 3 本体） | `src/qq_roleplay_bot/…` | 本仓库 `dev/`，分支 `main` |
| 面板（Stage 4 插件） | `yunru-stage4/src/qq_roleplay_bot/plugins/webui/…` | `plugins` 分支的工作区；`run/` 是待部署的旧副本 |
正文里为了短，面板侧的文件名一律写成短名（`wire.py` / `webui_panel.py` / `webui_data.py` / `webui_access.py` / `app.js`），都指上表第二行那一份。

本文里凡是"面板现在拿到的接缝"，都指 `yunru-stage4/…/plugins/webui/wire.py` 造出来、
`webui_panel.py` 用的那一串闭包；"核心给的东西"指 `src/qq_roleplay_bot/runtime.py`
的 `_SeamBinder`。

---

## 1. 目标与边界

### 1.1 要做的

1. **一个超管层**：就是现在那份超管名单（§2.1），全局、所有群、所有功能。
2. **多个管理员**：就是 `/admin` 现在认的那批人（§2.2），**各有作用域**（管哪几个群）。
3. **给管理员开权限**：权限按"面板的功能块"给（§4），默认**不给危险动作**。
4. **管理员从远程进面板**：私聊拿一次性 key → 面板输入 key → 会话**继承他的作用域与权限**
   （§3），不是全权。

### 1.2 边界（这一条是硬约束，不是风格）

- **权限判定在核心，面板只显示与提交。**
  - `AGENTS.md:90`：*"**权限判定、动作执行、护栏、审计一律留在核心**——插件拿不到
    `transport`、也拿不到任何权限名单。"*
  - `AGENTS.md:45-47`：插件只产出"内容或意图"，*"权限由核心判定。插件拿不到 `transport`，
    也拿不到 `admin_user_ids`。"*
  - `AGENTS.md:146-147`：*"权限判定（本地 token / 远程口令 + CSRF）在 `webui_access.py`
    （该分支），动作执行与护栏在核心。"*
- **fail-closed**（`AGENTS.md:48-49`）：判不出来时**静默回落**，绝不能"反正没权限就交给模型"。
  在面板这一侧对应的是：**身份或作用域取不到时，按空集处理**，而不是按全集。

### 1.3 现状与这个目标的差距（为什么不是"照旧扩展一下"）

**【实测】**现在的接缝是**装配时一次性造好、没有任何调用者身份**的闭包：

- `UiSeams` 的每个字段都没有"调用者"参数（`src/qq_roleplay_bot/plugins/__init__.py:417-464`）；
- `_SeamBinder.ui_seams()` 把引擎方法直接绑上（`src/qq_roleplay_bot/runtime.py:1279-1303`）；
- `wire._panel_seams()` 把它们原样交给 `Panel`（`wire.py:84-178`）；
- `Panel.handle()` 只用 token / cookie 判**"进不进得来"**，从不判**"你是谁"**
  （`webui_panel.py:244-291`）；
- 认证结果 `AccessDecision` 只有 `ok/reason/status/cookies/source` 五个字段
  （`webui_access.py:151-167`），其中 `source` 是**IP**
  （`webui_access.py:199-207`）——不是身份；
- 唯一带"身份味道"的东西是 `self_id`，而那是**云茹自己的 QQ 号**，用来给群动作当 actor
  （`webui_panel.py:538-545`），跟操作者无关。

**结论：**现在面板里"谁能做什么"这件事**根本没人判**——能进来就是全权。所以这份设计
的实质不是"加几个权限位"，而是**把身份与作用域搬回核心**，并让每一个接缝都带上它。

---

## 2. 身份：超管、管理员、作用域

### 2.1 超管名单（【实测】）

| 事实 | 位置 |
| --- | --- |
| 名字与读取方式 | `SUPER_ADMIN_USER_IDS = _csv_ids("QQBOT_SUPER_ADMIN_USER_IDS") or frozenset({"900000001"})`　`src/qq_roleplay_bot/dev_config.py:268` |
| 真号放哪 | `.env`（`.gitignore` 忽略，换分支冲不掉）——注释见 `dev_config.py:250-267` |
| 引擎怎么收 | 构造参数默认值 `src/qq_roleplay_bot/stage3_main.py:1124-1125`；实例属性 `stage3_main.py:1202-1208`（`super_admin_user_ids`） |
| 怎么判 | `_is_super_admin_control()`　`stage3_main.py:2523-2531`——**全局**：在哪个群、私聊都算数 |

### 2.2 "管理员"这个概念现在怎么表达（【实测】：**不是一个名单，是两条**）

1. **部署级名单**（对所有群生效）：
   `ADMIN_USER_IDS = (_csv_ids("QQBOT_ADMIN_USER_IDS") or frozenset({"900000001"})) | SUPER_ADMIN_USER_IDS`
   ——`dev_config.py:269`。判定入口 `is_group_admin()`　`stage3_main.py:2483-2491`。
2. **按群授权**：`self.group_admin_ids: dict[群号, set[QQ号]]`　`stage3_main.py:1207`。
   - 谁写的：超管 `/super addadmin @某人 [群号]`　`_add_admin()` `stage3_main.py:2927-2966`；
     "哪个群"由 `_grant_group()` 决定（`stage3_main.py:2904-2926`）：**默认是发命令的那个群**，
     私聊里必须显式写群号。
   - 撤权：`_del_admin()` `stage3_main.py:2967-3014`；**配置里那份撤不掉**（会明说，见 2987、3007-3010）。
   - 落盘：`state_store.PersistedState.group_admins`（`src/qq_roleplay_bot/state_store.py:74-76`），
     写 `stage3_main.py:4307-4310`，读 `stage3_main.py:4327-4330`。
   - 旧版本那份**没有群信息的全局名单**：`state_store.py:77-79` 明写"只读出来用于提示，
     不再写"，恢复时**不生效**（fail-closed）`stage3_main.py:4328-4337`。

**所以"管理员跟我之前的 admin 是同一批人"是成立的**——要读的就是引擎上这两个来源
（`super_admin_user_ids` / `admin_user_ids` / `group_admin_ids`），**不要另建一份名单**。

### 2.3 作用域从哪来（【实测】）

| 用途 | 现在怎么算 | 位置 |
| --- | --- | --- |
| 他管哪些群 | `admin_groups_of(user_id)`——**只含按群授权**，不含配置级名单 | `stage3_main.py:2493-2496` |
| 他能不能管这个群 | `_admin_may_manage_group()`——超管与**配置级名单**直接 True，否则看 `admin_groups_of` | `stage3_main.py:2506-2516` |
| 命令在哪儿才算数 | `_control_scope_ok()`——要求 `group_id is not None`，即**私聊里不生效** | `stage3_main.py:2464-2481` |
| 他能不能指挥别人群的事 | 点名了目标群的命令（转告）要再过 `_admin_may_manage_group` | `_run_admin_command()` `stage3_main.py:3528-3548` |

**面板要用的作用域函数应当写成一处的 `panel_scopes_of(user_id)`**，语义与
`_admin_may_manage_group` 对齐（超管 / 配置级名单 = 所有群；按群授权 = 那几个群）。
**不许**在面板里重算一遍——那就是第二个真相来源。

**注意一个容易忽略的差别【实测】**：配置级 `ADMIN_USER_IDS` 里的人是**事实上的全局管理员**
（对所有群生效，`stage3_main.py:2514-2515`），而不是"某个群的管理员"。控制台 v1 建议如实
呈现：配置级 = "管理员（全部群）"，按群授权 = "管理员（这些群）"。

- **【待核】T1**：本地 token（本机进面板的人）应该映射成哪个身份？
  本设计建议 **= 超管**（本机 token 等价于"机器主人"），但这需要用户确认——
  万一这台机器上有别人也能读到 `data/webui_token`，那这个映射就偏宽了。
- **【待核】T2**：`QQBOT_WEBUI_ACCESS_MODE=local`（默认）时，**管理员能不能进**？
  本设计建议 local 模式**只认超管**（管理员必须走远程 + key）。理由见 §3.3。

---

## 3. 登录流程（一次性 key）

### 3.0 先核实：私聊里 `/admin` 现在通不通（【实测】：**不通**）

这一条必须先说清楚，否则"私聊 `/admin key`"是建在沙子上的。

1. `/admin` 的解析器只认 **10 种动作**：`ENABLE / DISABLE / STATUS / CLEAR / HELP / ECHO /
   RELAY / SELECT / CONFIRM / CANCEL`（`src/qq_roleplay_bot/admin_control.py:8-18`）。
   **没有 `key` 这种动作**——`/admin key` 走 `parse_admin_command()` 返回 `None`
   （`admin_control.py:118-199`，最后 `if not match: return None`）。
2. **普通管理员（非超管）在私聊里没有管理员身份**：`_is_admin_control()`
   → `_control_scope_ok()` 里 `group_id is None` 直接 `return False`
   （`stage3_main.py:2476-2478`）。
3. 于是 `/admin key` 的落点：它**长得像** admin 命令（`PRIVILEGED_COMMAND_PATTERN`
   `src/qq_roleplay_bot/security.py:189`，`privileged_command_level()` `security.py:192-216`），
   在 `stage3_main.py:4664` 被判"没权限的 admin 命令"→ `_denied_admin_reply()`
   → 那里 `parse_admin_command(message.text) is None` → **返回 `None`，一个字都不回**
   （`stage3_main.py:3447-3448`）。
4. 就算命令能解析（例如 `/admin enable`），私聊里同样**静默**：
   `_denied_admin_reply()` 的 `if message.target.group_id is None: return None`
   （`stage3_main.py:3454-3455`），docstring 也明写"管理员命令在私聊里本来就不生效，
   回一句等于把人引到一条走不通的路上"（`stage3_main.py:3450-3453`）。
5. **超管**在私聊发 `/admin key`：走到 `stage3_main.py:4756`，回一句
   "这条 admin 命令我没认出来。发 `/admin help` 看全部可用命令。"
6. 私聊里**能**用的只有：`/admin help`（公开菜单，`stage3_main.py:4616-4623`）、
   以及超管的 `/super …`（`stage3_main.py:2523-2531`）。

**结论：私聊里 `/admin key` 现在不通——不是"没权限"，是"根本没有这条命令 + 私聊对管理员静默"。**
所以要做这件事，**第一步必须先开一条私聊通道**（§3.1）。

- **【待核】T3**：上面是**代码判定层面的实测**。我**没有**在真机上给一个管理员发过私聊验证
  "消息到底有没有到引擎"。要开工前先做一次对照实验：超管私聊 `/admin help` 有回
  （已知可用），同一条从管理员号发出去**没有任何回复**。若真机表现与代码不符，先查私聊
  白名单那一层。

### 3.1 需要先打开的私聊通道（两种形状，含取舍）

**形状 A（建议）：在 `AdminCommandKind` 里加一类 `KEY`，并在核心开一条窄例外。**
- 例外**只对 `KEY` 放开** `_control_scope_ok()` 的 `group_id is not None` 要求
  （`stage3_main.py:2476-2478`），别的一律照旧——所以它是一个**列名的窄口**，不是把
  "私聊也算管理员"这件事放开。
- 要落一条测试钉住"只有 `KEY` 能在私聊走通，`/admin enable` 在私聊仍然不生效"。
- 风险：`_control_scope_ok()` 是权限判定的核心函数，改动它必须**窄**且**有测试**。

**形状 B：另起一个完全独立的前缀（不再挂在 `/admin` 下）。**
- 不碰 admin 语义，但多一种前缀就是多一份文档漂移——`admin_control.py:32-34` 已经把
  "旧 `#bot` 前缀为什么被删"写成教训：*"两个前缀并存时，帮助文字、文档和用户记忆里
  永远会有一份是过时的。"*

**处理位置很关键（【实测】）**：会话闸门在 `stage3_main.py:4672-4677`——
普通私聊要过 `allowed_private` 白名单（默认 = 超管名单，`dev_config.py:612`），
不在里面的私聊会被**静默丢掉**。所以 `KEY` 这一类必须在**那个闸门之前**处理；
不能依赖"他发的私聊会被当成普通消息走一遍"。

- **【待核】T4**：上面这条是基于"闸门在 4677、管理员不在 `private_debug_user_ids` 里"
  的**代码推论**。真机上管理员私聊会不会在更早的传输层就被过滤（比如 OneBot 侧只收
  好友私聊），我没有验证——和 T3 一起做对照实验。

### 3.2 key 的形状与纪律（用户给的判据，落成可实现的步骤）

| 要求（用户原话） | 怎么落 |
| --- | --- |
| 私聊输入 `/admin key` | §3.1 的 `KEY` 动作；回复**只发给他本人**那条私聊 |
| **有效期 5 分钟** | 内存表记 `expires_at = now + 300`。与既有的 `DENIED_ADMIN_TTL_SECONDS = 300.0`（`stage3_main.py:597`，超管放行被拒命令那个窗口）**口径一致**，别再造第二个数字 |
| **只能用一次** | 兑换成功即 `pop`（照 `/super permit` 的"一次即消"，`stage3_main.py:3495-3524` 的写法）|
| **绑定该管理员的 QQ 号** | 发 key 的那一刻就把 `user_id` 记进表里；兑换时**只凭 key 取身份**，不让面板再报 QQ 号（理由见 §8 第 1 条）|
| **限流：同一人 1 分钟只能要一次** | 每个 QQ 号一个时间戳列表，形状照 `webui_access._Fails`（`webui_access.py:122-150`）与 `write_allowed()`（`webui_access.py:315-328`）|
| **不落明文日志/审计** | 表里存 **sha256 摘要**（复用 `control_audit.token_fingerprint` 的思路，`src/qq_roleplay_bot/control_audit.py:41-46`）；审计只记"谁、何时、申请/兑换" |

补两条我建议一并定下（**【待定】T5**）：
- **key 的熵**：建议不低于现有 `secrets.token_urlsafe(32)`（`webui_access.py:78` 造本机 token
  用的就是这个）。别为了"方便念"缩短——缩短等于把 §8 第 1 条那个风险放大。
- **兑换失败也要开口**：过期 / 已用过 / 对不上，都回**同一句固定说法**（不区分原因，
  免得变成探测口），但**审计里记一条带固定原因码的拒绝**（否则"有人在猜 key"在账上看不出来）。

### 3.3 与现有 `webui_access.py` 的关系（现状 + 取舍）

**现状（全部【实测】）：**

| | `local`（默认） | `remote` |
| --- | --- | --- |
| 绑定 | `127.0.0.1`（`dev_config.py:776`） | `QQBOT_WEBUI_HOST`（可 `0.0.0.0`） |
| 认证 | `Authorization: Bearer <token>`（`webui_access.py:229-256`） | 口令 → 会话 cookie（`login()` 257-279、`check_cookie()` 281-295） |
| token 落点 | `data/webui_token`，`0600`（`webui_access.py:51-89`） | 口令哈希落 `data/webui_password`（`wire.py:55-83`），scrypt 加盐（`webui_access.py:92-121`） |
| 会话 | 无 | cookie 有效期 12h（`SESSION_TTL_SECONDS` `webui_access.py:41`），**两条** `Set-Cookie`（会话 HttpOnly + CSRF 可读，271-274） |
| CSRF | 不需要 | 写操作必须带 `X-CSRF-Token`（`csrf_ok()` 297-308）；`/api/login` 例外（`webui_panel.py:301-307`） |
| 来源校验 | 不校验 | `Origin`/`Referer` 白名单（`origin_ok()` 208-228） |
| 限速 | 认证失败限速 | 同上 + 写动作 60/分（`WRITE_LIMIT_PER_MINUTE` `webui_access.py:46`） |
| 装配 | `wire.build_web_panel()` 183-218：没凭据就**不装**（fail-closed） | 启动打 WARNING（`wire.py:247-253`） |

**取舍建议：**

1. **本地 token 保留**，映射为**超管**身份（同 T1）。
2. **远程口令保留**，仍映射为**超管**（口令 = "整面板"的等价物，`AGENTS.md:141-147`
   就是按这个写边界的）。
3. **新增 key 入口**，映射为**该管理员**，带作用域与权限位。
4. 三条入口必须**只差身份、不差代码路径**：也就是说 `AccessDecision`
   （`webui_access.py:151-167`）要能带上解析出来的身份，`Panel` 只认这个身份，
   不认"哪条路进来的"。否则会出现"同一件事两条路两种判定"。
5. **管理员不给远程口令**（那等于把超管权限给了出去），**只给 key**。
6. **local 模式建议只认超管**（T2）：面板与 bot 同进程、绑 `127.0.0.1`
   （`webui_access.py:15-19`），本机上没法证明"我是某某管理员"——
   除非他也用 key（那这条路就与 remote 合并了，等于 local 不再有独立的管理员入口）。

---

## 4. 权限模型（v1 粗粒度：按"面板的功能块"给）

### 4.1 面板现有的功能块（从**路由 + 前端**数出来，全部【实测】）

前端结构：16 个 tab（`app.js:24-47` 的 `TABS`）、6 张卡（`app.js:103-128` 的 `CARDS`）
——表里并成 15 个功能块（「金句」「金句审核」「黑话」三个 tab 算同一块）；插件卡里的 tab 是
**动态**的（`app.js:52-58` 的 `pluginTabs()`，数据来自 `/api/plugins` →
`webui_panel.py:370-379`）。
后端：读路由在 `webui_panel.py:346-471`，写路由在 `webui_panel.py:476-520`。

| # | 功能块（前端 tab） | 读接口（`webui_panel.py`） | 写接口 | 现在靠什么 | 有群维度吗 |
| --- | --- | --- | --- | --- | --- |
| 1 | 总览 / 本体状态（`overview`） | `/api/overview` 347-354、`/api/state` 357-358 | — | `state_reader` 接缝（`wire.py:140`） | 部分（`enabled_group_ids`、`sessions`） |
| 2 | 会话明细（`group`/`private`） | `/api/session/messages` 389-394 | `/api/session/clear` 501 → `_clear_session()` 704-713 | 读 = **面板直接读 `runtime_state.json`**（`webui_data.py:167-215`）；写 = `session_clear`（`runtime.py:1227`） | **有**（`session_id` 形如 `group:<号>`） |
| 3 | 用量（`account`） | `/api/usage` 387-388 | — | **面板直接读 `data/api_usage.json`**（`webui_data.py:216-262`） | 无（按 agent 汇总） |
| 4 | 记忆（`memory`/`memory_profile`） | `/api/memory/overview` 395-396、`/api/memory/problem` 397-403、`/api/memory/<表>` 404-428 | `/api/memory/purge` 505 → `_memory_purge()` 715-732、`/api/memory/affinity` 507 → `_memory_affinity()` 734-751 | 读 = **面板自己开只读 sqlite**（`webui_data.py:263-274`、`_LIST_SQL` 359-382、`read_memory_table()` 383-477）；写 = `memory_ops`（`memory_ops.py:40-107`） | **部分**（见 §5.3） |
| 5 | 知识库（`knowledge`） | `/api/knowledge/overview` 442-443、`/chunks` 444-445、`/operator` 446-447 | `/api/knowledge/operator` 513 → `_knowledge_write()` 790-823、`/rebuild` 519 → `_knowledge_rebuild()` 825-839 | `knowledge`（`wire.py:158-160`；核心给的是 `_knowledge_for(engine)`，`runtime.py:1294`，定义在 `runtime.py:1680`） | **无**（全局） |
| 6 | 金句 / 黑话（`knowledge_quotes`/`knowledge_slang`） | `/api/knowledge/quotes` 451-453、`/slang` 462-464 | `/quotes` 515 → `_quotes_write()` 1168-1219、`/slang` 517 → `_slang_write()` 1221-1271 | `learned`（`wire.py:169-171`；契约是 `LearnedSeams`，`plugins/__init__.py:465-539`） | **部分**（见 §5.3） |
| 7 | 插件（`plugins` + 动态 tab） | `/api/plugins` 370-379、**`/api/plugin/<名字>`**（`webui_panel.py:380-386` → `_plugin_panel()` 596） | — | `plugins`（只读清单，`wire.py:98`）+ **`panel_spec` / `panel_names`**（`wire.py:235-236` 递的两个绑定方法；插件自己在 `register()` 里用 `registry.provide_panel(name, spec)` 报它那一页） | 无（每页归那个插件自己） |
| 8 | 日志（`logs`） | `/api/logs` 429-435 | — | **面板直接读 `data/logs/*`**（`webui_data.py:557-573`） | 无（按 feature，不是按群） |
| 9 | 调参（`settings`） | `/api/settings` 436-437 → `_settings_view()` 847-895 | `/api/settings` 509 → `_settings()` 753-762 | `apply_overrides`（`runtime.py:225-307`；白名单 `operator_config.SETTINGS` `operator_config.py:40-97`） | **无**（全局配置） |
| 10 | 六套 prompt（`settings` 内） | `/api/prompts` 438-439 → `_prompts_view()` 897-914、`/versions` 440-441 → `_prompt_versions()` 916-927 | `/api/prompts` 511 → `_prompt_write()` 764-788 | `prompt_library`（六套：`PROMPTS = ("persona","reply","judge","memory","review","vision")` `prompt_library.py:63`） | **无** |
| 11 | 动作 / 群管理（`actions`） | **—**（动作清单不再是面板的读接口：它由 `plugins/group_admin/plugin.py` 的 `PANEL_ACTIONS` 自报，面板只按封闭 kind 词表画出来） | `/api/action` 495 → `_action()` 536-568 | `execute_action`（`wire.py:120` → `runtime.py:1221` → `stage3_main.py:2754+`）；菜单 `PANEL_ACTIONS`（`plugins/group_admin/plugin.py:35-56`） | **有**（显式 `group_id`） |
| 12 | 入群审批（插件 tab） | `/api/plugin/join_approval`（`webui_panel.py:380-386` → 596；取数闭包是 `plugins/join_approval/plugin.py:108-142` 的 `_pending_reader`） | `/api/approval` 497 → `_approval()` 570-594 | `call_action`（`get_group_system_msg` / `set_group_add_request`） | **有**（`group_id`） |
| 13 | 操作记录（`history`） | `/api/history` 465-468 | — | `control_audit.tail()`（`control_audit.py:118-135`） | 无 |
| 14 | 群开关（`group` 内） | — | `/api/groups` 499 → `_groups()` 636-648 | `group_switch`（`wire.py:145` → `runtime.py:1230`） | **有** |
| 15 | 重启（`settings` 内） | — | `/api/control/restart` 493 → `_restart()` 525-534 | `restart`（`wire.py:143` → `runtime.py:1224`）+ 冷却闸（`wire.py:21-33`） | 无 |

（另有两条不对应任何 tab、但存在：`/api/login` 479-485、`/api/logout` 486-488；
以及 `/health` 320（`_health()`）、469-470 与静态外壳——`PUBLIC_PATHS` `webui_panel.py:242`。）

**插件自报界面（2026-10-11 的那处接缝）**：每个插件可以在 `register()` 里用
`registry.provide_panel(name, spec)` 报**它自己那一页**，面板用一条通用读路由
`/api/plugin/<名字>`（`webui_panel.py:380-386` → `_plugin_panel()` 596）取回来画。
描述符是**纯数据**：顶层只有 `title` / `note` / `blocks`，每块只有 `kind` / `title` /
`rows` / `error`（键白名单在 `_panel_view()` 1354 与 `_panel_block()` 1315，有测试钉住；
`route` / `url` / `method` / `html` 这类键一律丢弃）。`kind` 是**封闭词表**：
`action_form`（群管理那张表单，来自 `plugins/group_admin/plugin.py`）、
`join_queue`（入群申请队列，来自 `plugins/join_approval/plugin.py`）；不认识的 kind
面板**如实说画不出来**，不静默丢。块里可以带**一个只读取数闭包**（`read`），面板经既有的
`_call()` 跑它，取数失败时回 200 + `error`（**永不 500**）。
**写动作一条都没变**：仍走 `/api/action` 与 `/api/approval` 那两条已鉴权、已审计的写路由。
于是"面板认识哪个插件"这件事没有了——`webui_panel.py` 不再 import 任何兄弟插件，
也不再替谁解析数据。

### 4.2 v1 权限位（按块给，粗粒度两位）

建议权限位的**名字就叫块名**，读/写分开（一个块两个位）：

```
panel.overview.read      panel.sessions.read     panel.sessions.clear
panel.usage.read         panel.memory.read       panel.memory.purge
panel.memory.affinity    panel.knowledge.read    panel.knowledge.write
panel.learned.read       panel.learned.write     panel.plugins.read
panel.logs.read          panel.settings.read     panel.settings.write
panel.prompts.read       panel.prompts.write     panel.actions.read
panel.actions.execute    panel.approval.read     panel.approval.decide
panel.audit.read         panel.groups.switch     panel.restart
```

**超管 = 全给**（与现状一致：`AGENTS.md:119`、`AGENTS.md:141`——
*"面板权限除了不能动代码以外权限跟你是一致的"*）。**管理员 = 默认只给"读 + 自己群内的两个开关"**。
- **【待裁】`panel.actions.read`**：这一位与 §4.1 第 11 行的现状**不一致**——动作清单已改由插件自报（§4.1 末段），面板不再有“读动作清单”这条读路由。这一位是改成`panel.actions.form`（“能看见那张表单”）还是整个删掉，待裁。

### 4.3 危险动作默认不给（逐个列出，v1 一律只给超管）

| 危险动作 | 面板入口 | 为什么危险 |
| --- | --- | --- |
| **删记忆** | `/api/memory/purge`（`webui_panel.py:505` → `_memory_purge()` 715-732）→ `memory_ops.purge()`（`memory_ops.py:51-68`） | 动了她的记忆。**内容不销毁**（先归档，`AGENTS.md:51-53`；面板回复里也写着"原文已归档，保留 30 天" `webui_panel.py:731`），但"删了什么"只有审计能还原 |
| **调 / 重置好感** | `/api/memory/affinity`（`webui_panel.py:507` → `_memory_affinity()` 734-751）→ `memory_ops.affinity()/reset_affinity()`（`memory_ops.py:70-107`） | 直接改关系档；而且**这张表没有群维度**（§5.3） |
| **踢人 / 禁言 / 撤回 / 全员禁言** | `/api/action`（`webui_panel.py:495` → `_action()` 536-568）+ `PANEL_ACTIONS` 里 `group_admin` 那 6 行（`plugins/group_admin/plugin.py:36-43`）；允许集合 `capabilities.GROUP_MANAGE_ACTIONS`（`capabilities.py:100-102`） | 动的是群里的人；**踢人不可逆**（`set_group_kick` 在名单里，但要求明确指定：`stage3_main.py:2766-2767`） |
| **设 / 撤 QQ 群管理员、改群名片 / 群名 / 头衔 / 公告** | `/api/action` + 同一份目录里 `group_owner` 那 6 行（`plugins/group_admin/plugin.py:44-55`）；`capabilities.GROUP_OWNER_ACTIONS`（`capabilities.py:116-122`） | 等于发权限 / 改群；`capabilities.py:97-99` 明写这几个是"要么不可逆、要么等于发权限" |
| **入群审批通过与拒绝** | `/api/approval`（`webui_panel.py:497` → `_approval()` 570-594）；`JOIN_APPROVAL_ACTIONS`（`capabilities.py:126`） | 让陌生人进群 |
| **重启** | `/api/control/restart`（`webui_panel.py:493` → `_restart()` 525-534） | 会中断线上（`AGENTS.md:322`：动运行中的进程要先问） |
| **改配置（含凭据）** | `/api/settings`（`webui_panel.py:509` → `_settings()` 753-762）→ `apply_overrides`（`runtime.py:216-295`）；`secret` 类见 `operator_config.py:83-89` | 凭据类只回显掩码（`webui_data.mask_settings()` `webui_data.py:71-88`），但**写**是真的改运行配置 |
| **改六套 prompt** | `/api/prompts`（`webui_panel.py:511` → `_prompt_write()` 764-788）→ `PromptLibrary.save/reset/read_version`（`prompt_library.py:378/428/544`） | 直接改她的人格与判定材料（`AGENTS.md` §2.2） |
| **改 / 删知识库、重建索引** | `/api/knowledge/operator`（`webui_panel.py:513` → `_knowledge_write()` 790-823）、`/rebuild`（`webui_panel.py:519` → `_knowledge_rebuild()` 825-839） | Stage 3 的数据（`AGENTS.md:96`） |
| **群开关 / 清会话** | `/api/groups`（`webui_panel.py:499` → `_groups()` 636-648）、`/api/session/clear`（`webui_panel.py:501` → `_clear_session()` 704-713） | 这两个是**管理员本职**，但**必须过作用域**（§5.3）；清会话**不可逆**（`leave_session` `stage3_main.py:4082-4089`） |

- **【待定】T6**：管理员要不要能用**踢人 / 禁言 / 撤回**？他本来就是 QQ 群管理员，
  面板做这件事只是"换个客户端"。本设计 v1 **不给**（危险动作默认不给），
  要开就单独提需求、单独列名单——和 `capabilities.py:89-99` 当初处理群管理那条路同一形状。

---

## 5. 作用域过滤（最实质的一块）

### 5.1 现状：接缝是**全权、无身份**的（【实测】）

- `UiSeams` 的每个字段都没有调用者参数（`src/qq_roleplay_bot/plugins/__init__.py:417-464`）；
- `_SeamBinder.ui_seams()` 把引擎方法直接绑上（`runtime.py:1279-1303`）；
- `wire._panel_seams()` 把闭包原样交给 `Panel`（`wire.py:84-178`）；
- `Panel.handle()/authorize()` 只判"进不进得来"（`webui_panel.py:244-291`）；
- `AccessDecision` 里 `source` 是 IP（`webui_access.py:151-167, 199-207`）。

**更要紧的一件事：有一整批数据根本不走接缝。**
面板从 `data_root`（`wire.py:177`，值就是 `dev_config.data_dir()`）拿到数据根目录，
然后**自己开文件 / 开 sqlite**：

| 面板自己读的东西 | 位置 |
| --- | --- |
| 会话历史（`runtime_state.json`） | `webui_data.py:167-187` |
| 用量（`api_usage.json`） | `webui_data.py:216-262` |
| 记忆库（**只读 sqlite**） | `webui_data.py:263-274`（`open_memory_readonly`、`_memory_path`）、`_LIST_SQL` 359-382、`read_memory_overview()` 333-382、`read_memory_table()` 383-477、`read_problem_records()` 487-524 |
| 日志（`data/logs/*`） | `webui_data.py:557-573` |

`self.root`（属性 `webui_panel.py:229-232`，值来自 `data_root` 接缝）在 `webui_panel.py` 里的全部调用点（342、1039，以及把它转交给 `webui_data` 的 350、351、358、369、388、394、396、401、424、432）
**每一处都是数据读取**——我没找到别的用途（这一点我逐处看过，见 **【待核】T7**）。

**结论：光在接缝上加参数不够。** 只要读路径还在面板侧直接开库，
"管理员只能看他管的群"这句话就不可能成立——**这是本设计里最大的改动面**。

### 5.2 目标形状（三条）

1. **身份由核心盖章。** 兑换 key / 口令 → 核心返回
   `PanelIdentity { user_id, scopes: frozenset[群号] | ALL, perms: frozenset[str] }`；
   面板把这个身份放进**它自己的会话表**（现在的会话表只有 `session → 过期时间`：
   `webui_access.py:180`、`login()` 266、`check_cookie()` 289-295）。
2. **每次调接缝都带上身份**，或由核心侧的绑定闭包带上；**过滤在核心那一段做**，
   面板里不出现任何 `if 他有这个群` 之类的判断。
3. **直接读文件的那几块改掉**：要么走核心接缝，要么由核心先算出"允许的群集合"
   再交给面板查询（`allowed_groups`），并且**查询与集合都在核心拼**——
   面板不许自带 `group_id` 去直查。建议走前者（加接缝），因为这正是本仓库既定的纪律：
   *"面板能改的东西必须有'接入面'，不许临时加后门"*（`AGENTS.md:149-153`）。

**fail-closed 的具体做法**：身份缺失 / 作用域取不到 → **按空集**，不是全集。
并且要把"没有名单"和"没接上"分开——`ChatSeams.knows_reserved()` 就是为这件事存在的
（`plugins/__init__.py:267-307` 把两种"空集"的成因讲得很清楚），面板这一侧应当照抄这个形状。

### 5.3 逐个接缝怎么收窄

| 接缝（面板侧） | 现在给的是什么 | 在"管理员只能看他管的群"下要收窄成什么 |
| --- | --- | --- |
| `memory_ops`（`wire.py:130-131`，`_Lazy` 延后取；核心侧 `runtime.py:1259-1261`） | `MemoryOps` 实例（`memory_ops.py:40-107`：`available` / `purge` / `affinity` / `reset_affinity`） | `purge(record_ids)`：**先按 id 反查这些记录的群归属再筛**——`records` 用 `scope_type`/`scope_key`（`webui_data.py:359-360`），而 `scope_key` 对 `user_group` 就是 `群号:人`（`memory_store.py:498-510` 的 `_namespaces`），`record_subjects` 也有 `group_id`（`memory_store.py:127-129`）。**越界一律剔除、不做部分成功**；一个都没剩下就拒。`affinity` / `reset_affinity`：**待定（T8）**——`relationships` 表**只有 `user_id`，没有群维度**（`memory_store.py:117-120`），所以它天然跨群。三个选项：(a) 只给超管（v1 建议）；(b) 按"这个人在我管的群里说过话"做门（弱，不构成隔离）；(c) 给关系表加群维度（改数据模型，代价大，且会动 Stage 3 的记忆语义） |
| `knowledge`（`wire.py:158-160`） | `OperatorKnowledge` 那一块（`runtime.py:1680` 给的是 `prompt_sources.knowledge_base`） | **无群维度**（知识库是全局的）→ **只给超管**；管理员面板里这一块**不显示**（不是"显示了再禁用"——"面板只显示与提交"意味着不给的就不显示） |
| `learned`（`wire.py:169-171`；契约 `LearnedSeams` `plugins/__init__.py:465-539`） | 七个函数：`quote_view` / `quote_correct` / `quote_note_enabled` / `slang_list` / `slang_update` / `slang_delete` / `slang_mark_wrong` | **按群收窄（部分可行）**：`quote_view(group_id)`、`quote_correct(group_id, emoji_id, …)`（`runtime.py:1348-1524`）、`slang_list(group_id=None)`（`runtime.py:1535-1542`——**必须由核心把 `None` 换成"调用者的 scopes"**，透传 `None` 等于"所有群"）、`slang_update` / `slang_delete` / `slang_mark_wrong`（`runtime.py:1543-1574`）都带 `group_id`，逐个校验。<br>**两个例外【实测】**：(1) `quote_note_enabled(note_id, enabled)` **没有群参数**（`runtime.py:1525-1534`）——它操作的是 `QuoteStore.disabled_notes`，而 `notes` / `disabled_notes` 在存储里是**全局的**（`quote_learning.py:494-499, 501-511`；`note_id` 是**内容指纹**，`quote_learning.py:387-401`），只有 `meanings` / `overrides`（表情含义表与人工覆盖）按群（`quote_learning.py:480-493, 614-632`）→ 这条**只给超管**；(2) 同一条理由，`_quotes_view` 里与笔记有关的那半（`webui_panel.py:1057-1096`）也不能按群收窄 |
| `execute_action`（`wire.py:120` → `runtime.py:1221` → `stage3_main.py:2754+`） | 面板必须显式给 `group_id`（`webui_panel.py:564`）、actor 固定是云茹（`webui_panel.py:538-545`） | 核心在执行前加一道"**调用者能不能管这个群**"，语义对齐 `_admin_may_manage_group()`（`stage3_main.py:2506-2516`）与 `_out_of_scope_reply()` 的固定说法（`stage3_main.py:3562-3585`）。**注意**：这里判的是**面板身份**，不是 QQ 消息里的身份，所以不能直接复用 `_control_scope_ok(message)`——要一个新入口（例如 `execute_action(..., panel_identity=…)`），否则就是把消息路径与面板路径混在一处 |
| `apply_overrides`（`wire.py:122-123` → `runtime.py:1274-1278` → `runtime.py:225-307`） | 改**全局**运行配置（白名单 `operator_config.SETTINGS` `operator_config.py:40-97`） | **无群维度** → **只给超管**（凭据类尤其）。管理员调用直接拒，不进 `apply_overrides` |
| `prompt_library`（`wire.py:95`） | 六套 prompt 的读写（`prompt_library.py:63, 378, 428, 544`） | **只给超管** |
| `control_audit`（`wire.py:105`） | **写**：`record(action, detail, result, source, token)`（`control_audit.py:80-105`）；**读**：`tail(count)`（118-135）→ `/api/history`（`webui_panel.py:465-468`） | 写：每条要能带**操作者 QQ**（§6）。<br>读：管理员只看**自己**的条目——现在这条做不到，因为记录里没有"谁"（§6）。<br>附带一处【实测】的缺口：面板 `_audit()` 读 `self._seam("token")`（`webui_panel.py:1273-1282`，另见 760），但 `wire._panel_seams()` 造的 seams **没有 `"token"` 这一项**（`wire.py:93-178`）→ `str(None or "")` → 审计里 `token` 字段**恒为空串**，也就是现在**分不出是哪把 token 干的** |
| `state_reader`（`wire.py:140` → `runtime.py:1262-1265`） | `{"snapshot": engine.snapshot(), "usage_store": …}`——**整份快照**（含 `enabled_group_ids`、`sessions`） | 收窄成"只含调用者范围内的群与会话"：`enabled_group_ids ∩ scopes`、`sessions` 按 `session_id` 前缀筛。<br>**【待定】T9**：如果 `snapshot` 的结构不适合在核心侧就地过滤，就给它加一个参数化的读取口（`state_reader(scope)`），但**不要再让面板自己去读 `runtime_state.json`**（现在它在 `webui_data.py:167-187` 这么干） |
| `group_switch`（`wire.py:145` → `runtime.py:1230-1234`） | `(group_id, enabled) -> 是否真的变了` | 入参就是 `group_id` → 校验在 scopes 内；`ALL` 不限。**这是管理员 v1 的正当能力之一** |
| `session_clear`（`wire.py:147` → `runtime.py:1227-1229`） | `(session_id) -> bool` | 先解析出群号（`group:<号>`；`leave_session()` `stage3_main.py:4082-4089`）再校验。**【待定】T10**：非 `group:` 前缀的会话（例如 `mail:*`）管理员能不能清？本设计建议**不能**（"所在群聊"不含它） |
| `self_id`（`wire.py:149-156`） | 云茹自己的 QQ 号 | 不是数据，是"她是谁" → **不需要收窄** |
| `plugins`（`wire.py:98`；`runtime.py:1304`） | **只读**插件清单 | 无敏感内容 → 可保持不变（`plugins/__init__.py:450-451` 那段说明它为什么是只读） |
| `flags`（`wire.py:135`） | `RuntimeFlags.snapshot`（`runtime_flags.py:26-60, 90-92`） | **读**（展示开关状态）可以给；**写**走的是 `apply_overrides`（`operator_config.py:42-51` 那些 `applies="live"` 的键）→ **只给超管** |
| `restart`（`wire.py:143`） | 请求重启 | 全局且危险 → **只给超管**（冷却闸 `wire.py:21-33` 保留） |
| `data_root`（`wire.py:177`） | 数据根目录字符串 | **它就是"绕过接缝直接开库"的入口**。收窄的实质是**取消它**（§5.1 那四处读改走接缝）——前提是 T7 的核查结果确认它没有别的用途 |

- **【待核】T7**：`data_root` 还有没有"非数据读取"的用途（例如静态资源、列表页）？
  我读了 `webui_panel.py` 里 `self.root` 的全部调用点与 `webui_data.py` 的相关函数，
  **没找到**别的东西；但我**没有逐行读完** `webui_data.py` 全 727 行与前端 `app.js`，
  所以标待核。取消一个接缝之前要把这件事查干净。
- **【待定】T8**：好感（`relationships`）没有群维度 → 只给超管，还是改数据模型？（见上表）
- **【待定】T9**：`state_reader` 是就地过滤快照，还是加参数化读取口？
- **【待定】T10**：非 `group:` 会话能不能被管理员清？

### 5.4 端到端判据

见 §7 最后那条"必做验收"。

---

## 6. 审计

### 6.1 现状（【实测】）

| 事实 | 位置 |
| --- | --- |
| 落点与形状 | `data/control_audit.jsonl`，一行一条 JSON：`seq/at/action/result/source/token/detail`（`control_audit.py:80-105`；模块头 1-12 说明为什么单独一份） |
| 凭据保护 | `SECRET_KEY_HINTS = ("key","token","password","secret","passwd")`，命中这些**名字**的键，值换成 `"[已改]"`（`control_audit.py:31-32, 54-68`）；`token` 字段只存 sha256 前 8 位（41-46） |
| 不会无限长 | 超 4MB 裁到最近 2000 行（27-29, 106-116） |
| 面板每条写动作都记账 | `_audit()` `webui_panel.py:1273-1282`；调用点：restart 533、action 567、approval 590/593、group_switch 647、session_clear 712、memory_purge 728、affinity 743/750、prompt 783/785、knowledge 810/814/819/821、quote/slang 1191/1216/1240/1250/1266、settings 由核心 `apply_overrides` 记（`runtime.py:301-302`） |
| 读 | `tail(count)`（118-135）→ `/api/history`（`webui_panel.py:465-468`） |

**缺的是什么【实测】：没有"谁"。**
- `source` 是 **IP**（`webui_access.py:199-207` 的 `source_of`）；
- `token` 字段**恒为空串**（原因见 §5.3 那条：seams 里没有 `"token"`）；
- 唯一像"人"的 `actor_id` 记的是**云茹自己的 QQ 号**（`webui_panel.py:538-545` 把 `self_id`
  当 actor 传进 `_audit(actor=…)`）——不是操作者。

所以现在这份流水能回答"什么时候、改了什么"，**答不了"是谁干的"**。多管理员一上来，
这一点必须先补。

### 6.2 要做成什么样

1. **每次管理员动作一条**：`who`（操作者 QQ 号）/ `when`（`at` 已有）/ `what`（`action`）/
   `target`（群号、记录 id、目标 QQ——已有的 `detail` 里确实带，但**字段名不统一**，
   要定成稳定字段，别每处自己起名）。
   建议给 `ControlAudit.record()` 加一个明确的 `actor` 参数（而不是让每个调用点往
   `detail` 里塞 `actor_id`），这样"谁"就是一个**结构字段**，不靠约定。
2. **key 的申请与兑换各一条**：`panel_key_issued` / `panel_key_redeemed`，
   **不含 key 本身**（连摘要都不进 `detail`；要关联就记同一批的序号）。
3. **兑换失败也记一条**（`panel_key_rejected` + 固定原因码）——原因见 §3.2 末尾。
4. **不做**：审计内容不得进记忆库（`AGENTS.md:63-65`：记忆库不得记录"关于这个系统自身"的信息）。

---

## 7. 分几步做、每步判据

顺序（用户给的建议顺序，我同意）：**先核心权限模型 → 再接缝过滤 → 再面板登录 →
最后权限管理界面**。

**每步都要"能独立验证"**，也就是不改 UI 也能看出对错。

### Step 1 — 核心权限模型（不动面板、不动 UI）

- **内容**：定义 `PanelIdentity {user_id, scopes, perms}`（perms 见 §4.2）；
  实现 `panel_scopes_of(user_id)`，数据源只用引擎上已有的三样
  （`super_admin_user_ids` / `admin_user_ids` / `group_admin_ids`，
  `stage3_main.py:1202-1208`），语义对齐 `_admin_may_manage_group()`
  （`stage3_main.py:2506-2516`）。
- **判据（单测，可独立跑）**：
  1. 超管 → `scopes = ALL`、`perms = 全集`；
  2. 配置级名单里的人 → `ALL`（与 `stage3_main.py:2514-2515` 一致）；
  3. 只在 A 群被授权的人 → `scopes = {A}`；
  4. **名单外的人 → `scopes = 空集 + perms = 空集`**（fail-closed）；
  5. 旧状态文件里那份全局名单**不产生任何身份**（对照 `stage3_main.py:4328-4337`）。

### Step 2 — 接缝过滤（核心侧；面板这一版先不动）

- **内容**：按 §5.3 逐条把接缝改成"带调用者"的形状；把面板**直接开库/开文件**的四处读
  （§5.1）改成走接缝或走核心给的允许集合。
- **判据（每条接缝一个"越界必须失败"的测试）**：
  1. 用"只有 A 群"的身份问 B 群的数据 → **空 / 拒**（每个接缝一条，逐个列名）；
  2. **身份缺失 / 作用域取不到 → 空集**（不是全集）；
  3. `slang_list(None)` 在核心侧被替换成 scopes，**不会**返回所有群；
  4. `execute_action(group_id=B)` 在核心被拒，且回复是固定说法（对齐
     `stage3_main.py:3562-3585` 的口径）。

### Step 3 — 私聊 `/admin key` + 兑换（核心侧）

- **内容**：§3.1 的窄例外 + §3.2 的 key 库（TTL 300s、单次、绑 QQ、限流 60s、不落明文）。
- **判据**：
  1. **真机**：管理员私聊 `/admin key` 拿到 key；对照实验见 【待核】T3；
  2. 同一个 key 兑第二次 → 失败；
  3. 6 分钟后再兑 → 失败；
  4. 1 分钟内要第二次 → **拿不到新 key**（限流生效），且**不重置**第一次那条的计时；
  5. `Select-String` 在 `data/control_audit.jsonl` 里**搜不到** key 明文；
  6. `/admin enable` 在私聊里**仍然**不生效（窄例外没有放宽别的）。

### Step 4 — 面板登录 + 会话身份

- **内容**：`AccessDecision` 带上身份（`webui_access.py:151-167`）；
  `/api/login` 支持 key；本地 token / 远程口令解析成超管（T1）；
  面板会话表存身份（现在只存过期时间：`webui_access.py:180, 266, 289-295`）。
- **判据**：
  1. 只用 key 登进来的会话，`/api/state` 里**看不到** B 群；
  2. 超管用口令/token 登进来，**看得到**（否则"全都没数据"也能骗过测试）；
  3. key 会话过期 / 登出后，接口回到 401；
  4. 远程模式下 CSRF 与来源校验**没有**因为加了 key 而被绕过
     （`webui_panel.py:301-307`、`webui_access.py:208-228, 297-308` 的既有测试仍绿）。

### Step 5 — 权限管理界面（最后）

- **内容**：超管在面板里给管理员**开关权限位**、改作用域。改的是**核心的名单与权限表**
  （经核心接缝），不是面板自己存一份。
- **判据**：
  1. 给某管理员加一个权限位 → **他的下一个请求**就生效（不需要重启）；
  2. 撤掉 → 立刻失效；
  3. 改完之后 `data/` 里**没有**出现"第二份权限表"（权限只在核心一份）。

### 必做的一条端到端验收（最终必须绿；每个 Step 之后都要能跑）

**判据：一个只被授权 A 群的管理员，任何接口都拿不到 B 群的数据。**

怎么测：

1. 准备两个群 A、B（现有测试群是 `717151356`，见 `capabilities.py:105` 的注释；
   B 用另一个号），并造一个**只被授权 A 群**的管理员 X
   （用 `/super addadmin` 在 A 群里授权，`stage3_main.py:2927-2966`）。
2. 用 X 的 key 登面板，**逐个**打这些接口，断言**拿不到 B 群的任何数据**：
   - `GET /api/state` —— B 不出现在 `enabled_group_ids` / `sessions` 里；
   - `GET /api/session/messages?session_id=group:B` —— 空，或干脆 403（口径见 §5.3）；
   - `GET /api/memory/<表>` —— B 的行不出现（含 `inbox` / `people` / `records`）；
   - `GET /api/knowledge/quotes?group_id=B`、`?group_id=A` 对照；
   - `GET /api/knowledge/slang?group_id=B`、以及**不带** `group_id` 的那次（不得返回全部）；
   - `POST /api/groups{group_id:B}`、`POST /api/session/clear{session_id:"group:B"}`、
     `POST /api/action{group_id:B,…}`、`POST /api/memory/purge{B 的记录 id}`、
     `POST /api/approval{group_id:B}` —— 全部失败。
3. **反例也要断言**：X 打 A 群的同一批接口**能用**——
   否则"全部 403"也能骗过这条测试。
4. 测试落点：`tests/`。面板的既有测试是**直接构造 `Panel`** 的
   （`wire.py:199-204` 的注释就指着那批），所以这条能放进离线套件；
   第 1 步那两条**只有真机**（T3/T4）。

---

## 8. 风险与取舍（诚实）

### 8.1 一次性 key 被盗用 / 被暴力猜

- **防护叠了几层**：5 分钟 TTL + 单次 + 绑定申请人的 QQ 号 + 申请限流（1 次/分/人）+ 兑换
  失败进限速。猜中的窗口由 key 的熵决定（建议不低于 `secrets.token_urlsafe(32)`，
  与 `webui_access.py:78` 造本机 token 同量级）。
- **"绑定了 QQ 号所以更安全"这句话要谨慎说。** 关键在**谁在什么时候声明身份**：
  本设计把绑定放在**申请侧**——是核心在"某人私聊要 key"那一刻把 key 记在他名下，
  兑换侧**只凭 key 取身份**，面板**不需要**（也不该）让操作者填 QQ 号。
  **这是安全的**（面板报的号不可信，所以根本别让它报）；但如果有人把它实现成
  "面板上填 key + 填 QQ 号，两个都对就放行"，那个绑定就**退化成填一个数字**，
  等于没有。**实现时这一条要写死在核心，别给面板留"申报身份"的参数。**
- **我的判断：这套够用来防"误入"和"猜"，不足以防"这个人的 QQ 记录被看"**——
  key 是明文发在 QQ 私聊里的，聊天记录里就有。所以真正的兜底是
  **短 TTL + 单次 + 低权（管理员 v1 没有危险动作）**：即使被看到，能造成的后果也小。
  反过来说，**如果 v1 就给管理员删记忆/踢人，那 5 分钟 + 单次就不够了**——
  这两件事要一起决定（见 T6）。
- **本设计里我最想让用户裁决的就是这一条**：v1 的管理员权限范围，直接决定 key 泄露的后果。

### 8.2 面板远程暴露面的变化

- 现在是"**一个口令 = 全权**"（`webui_access.py:257-279`）；加 key 之后多了一条**低权**入口，
  但**同源、同端口**——可被尝试的路径多了一条。
- 缓解：key 的失败复用同一套限速（`_Fails` `webui_access.py:122-150`）；
  兑换失败也进审计（§6.2）；日志里绝不出现 key。
- **没有变**的部分：仍然**不做 TLS**（`webui_access.py:18-19` 明说不做，交给反向代理/隧道），
  仍然建议前面挂反代或 Tailscale；远程模式启动时那条 WARNING（`wire.py:247-253`）
  **一个字都不用改**，因为它说的还是实话。

### 8.3 管理员误操作的可逆性

| 动作 | 可逆吗 |
| --- | --- |
| 群开关（`/api/groups`） | **可逆**：再点一次就回来（`runtime.py:1230-1234`） |
| 清会话（`/api/session/clear`） | **不可逆**（`leave_session` `stage3_main.py:4082-4089`；清的是短期对话状态，不是记忆） |
| 删记忆 | **内容不销毁**：先归档保留 30 天（`AGENTS.md:51-53`；面板回复 `webui_panel.py:731` 也这么说）。但 v1 只给超管 |
| 踢人 / 禁言 / 撤回 | 禁言与撤回可以在群里纠正；**踢人不可逆**（`stage3_main.py:2766-2767` 那条"必须明确指定"的护栏是**有意的**）。v1 不给管理员 |
| 改配置 / 凭据 | 可逆性取决于人记不记得旧值——覆盖层在 `data/operator_config.json`（`operator_config.py:106-109`），凭据只回显掩码（`webui_data.py:71-88`），**旧值不会留在面板上**。v1 只给超管 |

### 8.4 与 `AGENTS.md` 边界的一致性

| 边界 | 本设计 | 一致吗 |
| --- | --- | --- |
| §2.1"插件不判断身份、权限由核心判定"（`AGENTS.md:45-47`） | 身份与作用域**搬回核心**，面板只提交 key 与显示结果 | **一致**（这正是本设计的目的） |
| §2.1 fail-closed（`AGENTS.md:48-49`） | 身份/作用域取不到 → **空集** | **一致** |
| §2.3"权限判定、动作执行、护栏、审计一律留在核心"（`AGENTS.md:90`） | 过滤与审计都在核心 | **一致** |
| §2.3"面板要动的东西先有接入面，不许临时加后门"（`AGENTS.md:149-153`） | 本设计正是"先加 identity-aware 接缝，再由面板调它" | **一致** |
| §2.3 面板"只写 `data/`，**不改 `src/`**"（`AGENTS.md:147-148`） | 本设计的改动**全部在核心侧**，不是让面板去改 `src/`；面板自己那一侧只加"提交 key / 显示" | **一致**，但要注意：这意味着这条工作**不能塞进 `plugins/webui/` 就完事** |
| §2.3 那句"权限判定（本地 token / 远程口令 + CSRF）在 `webui_access.py`"（`AGENTS.md:146-147`） | 多管理员一上来，**"认证（你是谁）"与"授权（你能做什么）"必须分开**：认证可以留在 `webui_access.py`，**授权必须回核心** | **需要修订那一句**（见 T11） |

- **【待定】T11**：`AGENTS.md:146-147` 那句要不要改？我倾向改成
  "认证（token / 口令 / key 的校验与会话）在 `webui_access.py`，**授权与作用域在核心**"。
  这是**上位文档**的改动，**不在这份设计文档里顺手改**——要单独提给维护者确认。

---

## 附录 A：待核 / 待定清单（方便逐条裁决）

| 编号 | 类型 | 内容 | 影响哪一节 |
| --- | --- | --- | --- |
| T1 | 待核 | 本地 token 的操作者是否映射为超管？ | §2.3、§3.3 |
| T2 | 待核 | `local` 模式下管理员能不能进（建议只超管）？ | §2.3、§3.3 |
| T3 | 待核 | 真机验证：管理员私聊现在到底有没有到引擎（对照超管 `/admin help`）？ | §3.0 |
| T4 | 待核 | 真机验证：管理员私聊会不会在传输层/白名单更早处被丢？ | §3.1 |
| T5 | 待定 | key 的熵与字母表（建议 `token_urlsafe(32)` 同量级）？ | §3.2 |
| T6 | 待定 | 管理员 v1 要不要给踢人/禁言/撤回？ | §4.3、§8.1 |
| T7 | 待核 | `data_root` 有没有非数据读取的用途（取消它之前必须查干净）？ | §5.1、§5.3 |
| T8 | 待定 | 好感（`relationships` 无群维度）：只给超管，还是改数据模型？ | §5.3 |
| T9 | 待定 | `state_reader` 就地过滤快照，还是加参数化读取口？ | §5.3 |
| T10 | 待定 | 非 `group:` 会话（如 `mail:*`）能不能被管理员清？ | §5.3 |
| T11 | 待定 | `AGENTS.md:146-147` 那句要不要修订为"认证在此、授权在核心"？ | §8.4 |

## 附录 B：本文【实测】的关键事实一览（便于复核）

| 事实 | 位置 |
| --- | --- |
| 超管名单从 `QQBOT_SUPER_ADMIN_USER_IDS` 读，兜底 `900000001` | `src/qq_roleplay_bot/dev_config.py:268` |
| 管理员 = 部署级 `QQBOT_ADMIN_USER_IDS` ∪ 超管 ∪ 按群授权 | `dev_config.py:269`、`stage3_main.py:1207, 2483-2491` |
| 按群授权由 `/super addadmin` 写入，默认落在"发命令的那个群" | `stage3_main.py:2904-2926, 2927-2966` |
| 作用域函数 | `admin_groups_of()` `stage3_main.py:2493-2496`；`_admin_may_manage_group()` 2506-2516 |
| 私聊里管理员没有管理员身份 | `_control_scope_ok()` `stage3_main.py:2476-2478` |
| `/admin` 只有 10 种动作、没有 `key` | `admin_control.py:8-18, 118-199` |
| 私聊里的管理员命令**静默**（一个字都不回） | `stage3_main.py:3447-3448, 3454-3455` |
| 特权命令的"形状"判定（未授权也挡住、不进模型） | `security.py:189, 192-216`；收尾 `stage3_main.py:3435-3475` |
| 面板两套模式与认证细节 | `webui_access.py:34-36, 51-89, 92-121, 122-150, 208-228, 229-256, 257-279, 281-296, 297-308, 315-328` |
| 面板装配与 fail-closed | `wire.py:84-178, 183-218, 229-256` |
| 面板路由（读/写） | `webui_panel.py:346-471, 476-520` |
| 面板自己开库/开文件读数据（绕过接缝） | `webui_data.py:167-187, 216-262, 263-274, 333-477, 557-573` |
| 审计流水形状与凭据保护 | `control_audit.py:31-32, 41-46, 54-68, 80-117` |
| 审计里缺"谁"（token 字段恒空） | `webui_panel.py:1273-1282`（读 `seam("token")`）对 `wire.py:93-178`（没有这一项） |
| 接缝清单（面板侧） | `wire.py:93-178`；核心侧 `runtime.py:1221-1303` |
| 接缝契约（核心侧） | `plugins/__init__.py:417-464`（`UiSeams`）、`465-539`（`LearnedSeams`） |
| 动作闸门与档位 | `capabilities.py:27-47, 100-102, 116-122, 126, 210-246` |
| 运行期开关 | `runtime_flags.py:26-60, 63-140` |
| 配置覆盖层与白名单 | `operator_config.py:40-97, 112-138, 149-225` |
