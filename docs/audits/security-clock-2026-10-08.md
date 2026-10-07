# 安全面与时间口径审计 · 2026-10-08

范围：`dev/src/qq_roleplay_bot/` 与 `dev/tests/`，分支 main。只看这两处，不看 `run\`、不看插件树。
方法：只读。未改源码、未跑测试、未碰任何进程。
标注规则：**实测** = 读到的文件与行号，代码路径逐条核对过；**推断** = 由代码结构推出、未在运行中复现。

已排除（本次不重复报，属于已知项）：时间用了 `monotonic`（正在修，另有 `tests/test_two_clocks.py`）· `/health` 未认证回本机路径 · 面板审计无身份（token 恒空）· 令牌边界只覆盖回复段 · 链接被正则误判 · 面板直接开库。

## 一句话结论

本区**最重的那条答案是好消息**：**没有找到任何"聊天/记忆/资料/插件内容进 system 段"的路径**（逐处追过，见下面"未发现"一节）。真正要修的是另一类——出站面把**本机诊断结果与本机绝对路径**发回原会话（群里可见），以及**群成员 QQ 号**经三条路进了给模型的 prompt；时间口径上除已知那处 `monotonic` 之外，还有一条独立的时区假设：**代码把"服务器本地就是北京"写死了**，换到 UTC 机器上"群里这边"整整差 8 小时，而她被明确要求"以告诉你的为准"。

## A 区最该修的两件

### A-01　超管命令的回复发回**原会话**，里面是本机进程与本机绝对路径（中）

**现象**：`/super processes|lan|fan|status` 会把这台机器在跑什么、网卡流量、内存占用发到**敲命令的那个会话**；`/super quote|slang` 的结尾还会附一行本机绝对路径。超管是全局身份，在哪个群都能用——也就是说在群里敲一次，全群都看到。

**证据（实测）**：`stage3_main.py:3482-3484`

```
            if isinstance(reply, OutgoingMessage):
                return reply
            return OutgoingMessage(message.target, reply, paced=False)
```

配套：
- `stage3_main.py:2375`、`:2443`、`:2493` 三处 `lines.append(f"（文件：{store.path}）")`；`store.path` 是绝对路径（`quote_learning.py:227-234` → `Path(__file__).resolve().parents[2] / "data/quote/profile.json"`）。
- `runtime_diagnostics.py:138-139` 的输出形状是 `ProcessName | PID | 内存 MB`（`:124-140` 起 PowerShell 取数）。
- 超管命令的适用范围是"全局"：`_is_super_admin_control`（`stage3_main.py:1735-1743`）只看 `user_id`，`SUPER_HELP`（`memory_view.py:182`）明写"超管命令是全局的：在哪个群、私聊都能用"。
- 反面参照：**记忆查看那一条是有意的**——`memory_view.py:1` 的模块说明直接写"渲染成有界的、**可在群里发送**的文本"。进程、网卡、绝对路径没有任何一处有这层说明。

**影响**：一次误发（在别人的群、或不熟的群里随手试一下）就把"这台机器上跑着什么/装在哪/群里谁说过哪句原文"摊给全群，且不可撤回。同一类信息，普通用户主动问是**会被拦的**（`security.py:17-24` 的 `SENSITIVE_REQUEST_PATTERNS` 正是"本机+进程/文件/端口"），bot 自己却会印。

**最小改法**：把"诊断类输出"与"带路径的收尾行"限定为**只在私聊**回（`message.target.group_id is None` 才发，群里回一句"这条请在私聊用"）；`（文件：…）` 去掉，或只留 `data/quote/profile.json` 这种相对仓库根的形式。

**可调项**：如果希望保留"在群里也能看诊断"，把 3 处路径行单独做成开关；但进程/网卡那两类建议不开口子。

**严重度：中　理由**：发起者确实是超管本人（不是越权），但泄漏物是**本机信息 + 成员原文**，且默认无提示、无开关、不可撤回；危险不来自攻击者，来自一次手滑。

### A-02　群成员 QQ 号进 prompt（三处，与"名册编号代替 QQ 号"的设计直接矛盾）（中）

**现象**：补进来的群历史里，`@` 段被渲染成 `@真实QQ号` 进了正文；名册认不出的人，`at=` 与 `main_partner=` 会回落成 `QQ<号码>`／裸号码；长期记忆渲染时 `subject=` **恒为** QQ 号，`about=` 查不到名字也回落裸号。

**证据（实测）**：三条路各一段

```
conversation_context.py:285-287
        elif seg_type == "at":
            qq = data.get("qq")
            parts.append(f"@{qq}" if qq else "@")
# 经 :203 text = segments_to_text(raw.get("message")) → stage3_main.py:1615-1621 进历史 → 渲染进 user 段正文

stage3_runtime.py:1213
            targets.append(str(alias) if alias is not None else f"QQ{user_id}")
stage3_runtime.py:1199
    return str(alias) if alias is not None else sanitize_chat_text(str(active_user_id), max_length=128)

memory_model.py:294 / :307-309
        return ", ".join(self.names.get(uid, uid) for uid in people)
        owner = escape(r.subject_user_id or "group", quote=True)
        line = f'<memory scope="{r.scope_type}" subject="{owner}"{detail}>{content}</memory>'
```

对照：实时路径**刻意剥掉** `@` 段——`onebot_ws.extract_text`（`:40-57`）只取 `type == "text"`，所以 `@QQ号` 只从"补上下文"这一条路进来。而 `memory_service.py:66` 的注释写的是"渲染成 `about="乐乐"` 而**不是裸 QQ 号**"，实现里那个回落正好相反。

**影响**：模型拿到真实 QQ 号，完全可能在回复里复述出来（群里就看到了号）；同时破坏"用名册编号做身份"这套设计（`stage3_runtime.py:1579` 的注释明写"值写名册编号而不是 QQ 号"）。

**最小改法**：补历史走与实时同一套渲染（`@` 段只留 `mentioned_user_ids`，别写进 `text`）；`MemoryMaterial` 的 `subject` 写 alias 或干脆省略该属性，`about` 查不到名字时留空而不是回落号码。

**可调项**：`QQBOT_GROUP_LISTEN` 只影响是否每句都进模型，不影响这三处。

**严重度：中　理由**：不是"被注入"，而是内部标识外泄 + 与既有设计冲突；泄漏面是"她可能自己念出来"，不可控、也不可事后追回。

## B 区最该修的一件

### B-01　时刻显示完全依赖服务器本地时区，而代码把"本地=北京"写死了（中）

**现象**：给她的"现在是什么时候"和 `/super status` 里那些人类可读时刻，全部由 `datetime.fromtimestamp` / `time.localtime` 渲染；代码里明确假定"服务器本地就是北京"。机器时区不是 UTC+8 时（云主机默认 UTC），"群里这边"整整差 8 小时，而且**不报错**。

**证据（实测）**：`stage3_runtime.py:1426-1429`

```
    moment = time.time() if now is None else float(now)
    here = datetime.fromtimestamp(moment)
    there = here + timedelta(hours=dev_config.persona_offset_hours())
    return f"你那边现在是 {_clock_text(there)}；群里这边是 {_clock_text(here)}"
```

配套：
- `dev_config.py:288-290`：`# 用户口径（2026-10-08）："她那边 = 北京 + 3 小时"。服务器本地就是北京，所以这只是 "她那边" 相对这边的固定小时差`——也就是把时区当成前提，不是配置。
- `stage3_main.py:337-340` `_format_clock` 同样走 `time.localtime(timestamp)`，`/super status`、`/super quote`、`/super slang` 的时刻全用它。

**影响**：时间会不会错——会，而且是**她主动说错**：`TIME_IS_GIVEN_RULE`（`stage3_runtime.py:825-829`）明确写着"眼下是几号、几点钟，以告诉你的为准……两者对不上时，信告诉你的那个"。所以错的时刻会被她当成事实讲给群里，并且和聊天里的真实时间线冲突时她也只会照错的答。

**最小改法**：给"这边"一个显式时区偏移（例如 `QQBOT_LOCAL_TZ_OFFSET_HOURS`，默认 8），用 `timezone(timedelta(...))` 参与计算，别用依赖机器 TZ 的 `fromtimestamp`／`localtime`；`persona_offset_hours()` 继续相对它算即可。

**可调项**：现有 `QQBOT_PERSONA_OFFSET_HOURS`（默认 3）只调"她那边"，调不了"这边"——这正是本次要补的那一项。

**严重度：中　理由**：本机（北京）影响为零，所以本机测不出来；一旦上云/换机，错的是"她答时间"这条**高频且用户敏感**的能力，且完全没有可见信号。

## 其余 5 条

### A-03　插件档位 fail-open：档位拼错或没声明一律当 public（中）

**现象**：插件的权限档位 `min_level` 一旦写了但写错（大小写以外的任何拼写错误），或者根本没声明，核心会把它当 `public` 放行；`session_allowed` 没实现也当"任何会话都允许"。

**证据（实测）**：`command_plugins.py:108-115`

```
def level_of(plugin: object) -> str:
    value = str(getattr(plugin, "min_level", "") or "").strip().casefold()
    return value if value in LEVELS else "public"
```

`command_plugins.py:210-219`

```
    checker = getattr(plugin, "session_allowed", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(target))
    except Exception:  # noqa: BLE001
        return False
```

再配套 `stage3_main.py:1865-1869`：`_plugin_level_allowed` 对非 `admin`/`super` 的档位一律 `return True`。

**影响**：越权——`min_level = "Supre"` 这种一个字的手误，让"踢人/禁言/改群名"这类特权命令变成**谁都能用**，而且过程静默：没有日志、没有拒绝、没有告警（`level_of` 落成 `public` 后走的是正常放行路径）。注意这条的判据自己说的是"不认识的档位**不放宽**"，实际行为正好相反。影响面在**带插件的树**上（本体 `plugins/` 只有 `__init__.py`，没有特权插件）。

**最小改法**：把"没声明"和"声明了但不认识"分开处理——属性缺失才回落 `public`，值不合法就拒绝装载该插件并记 WARNING；`session_allowed` 缺失时保守返回 `False`（或要求插件显式声明 `session_allowed=None` 表示公开）。

**可调项**：`LEVELS`（`command_plugins.py:70`）是唯一的档位闭集，判据可挂在这里。

**严重度：中　理由**：一旦发生就是完整的越权（不只是一条信息泄漏），且没有任何可见信号；但需要插件作者先写错一个字，本体树当前不触发。

### A-04　机制词护栏只在"保存时"和"测试里"，加载路径一个都不扫（中低）

**现象**：`prompt_guard` 的机制词扫描只在 `PromptLibrary.save()` 里跑；**读**覆盖文件时不复验，人格文件加载时也不扫。所以手改文件、从备份恢复、或换 `QQBOT_BASE_PROMPT_FILE` 指一份新人格，都能把机制词送进 system，运行时零告警。附带两条同源问题：拼装幂等判据是**子串存在**；`persona` 这一套实际上没有消费者。

**证据（实测）**：`prompt_library.py:309-320`（读路径）

```
        if not self._loaded:
            self.load()
        override = self._cache.get(key)
        if override:
            return override
        return self.builtin(key)
```

`:337-359` 的 `validate()`（里面才调 `scan_text`）只有 `save()` 会调；`base_prompt.py:237-244` 读真人格文件时只做正则抽取：

```
        found = re.search(r'BASE_PROMPT\s*=\s*"""(.*?)"""', text, re.S)
        if found and found.group(1).strip():
            return found.group(1)
```

扫描只存在于测试里（`tests/test_persona_shape.py:75`）。幂等那段是 `stage3_runtime.py:888-890`：

```
    for rule in (STAY_IN_CHARACTER_RULE, DATA_BOUNDARY_RULE, TIME_IS_GIVEN_RULE):
        if rule not in body:
            body += rule
```

`persona` 无消费者：`prompt_library.py:210-213` 的 `builtin("persona")` 只被 `available()` / `backfill()` 用；全 `src/` 搜索没有 `text("persona")` / `resolve("persona", …)` 的调用（**实测**：grep 全仓只有 `PROMPTS` 名单、`builtin` 分支与 `prompt_guard.PERSONA_KEYS` 三处提到 `persona`）。

**影响**：这是"面板/文件改了 system 之后还有没有护栏"的答案——**保存这条路有，别的路没有**。生产上没人跑测试，所以"测试扫过"不等于运行期拦得住。幂等那条更细：只要覆盖文本里**粘上那三段原文**，`compose_system_prompt` 就不再追加，后面可以接一段方向相反的话（护栏是"加"，不是"核"）。另外 `persona` 这套可存不可用，容易被误读成"护栏把它拦住了"。

**最小改法**：`load()` / `text()` 读进来时也跑一次 `scan_text`，命中就记 WARNING 并回落内置默认；`base_prompt._load_base_prompt` 同样在返回前扫一次（命中走警告 + 模板）；幂等判据改成"必须出现在末尾"。

**可调项**：`QQBOT_PROMPT_DIR`（覆盖目录）、`QQBOT_BASE_PROMPT_FILE`（人格文件）——两个入口都值得在加载时复验。

**严重度：中低　理由**：需要有人能写 `data/`（面板/本人/备份恢复），不是外部输入；但一旦发生，进的是**可信 system 前缀**，正是 AGENTS §2.1／§2.2 最在意的那一段，而当前防线全在测试里。

### A-05　`LLMError.detail` 把模型端原始错误体带进日志（低，**推断**）

**现象**：HTTP 错误时，provider 返回的原始响应体（截 500 字）被塞进 `LLMError.detail`，随后进进程日志与功能日志；而同一个类的 docstring 断言它"绝不包含"这类东西。

**证据（实测的是代码路径，"body 里到底有什么"是推断）**：`llm_client.py:258-263`

```
    def _classify(self, exc: ModelRequestError) -> LLMError:
        if exc.kind == "http_error":
            detail = f"status={exc.status} body={exc.body}"
        else:
            detail = exc.detail
        return LLMError("模型请求失败", kind=exc.kind, detail=detail)
```

`llm_client.py:348-359`（`_safe_error_body`，读原始 body，截 500 字）、`llm_client.py:243-250`（`logger.warning("model_retry … detail=%s", error.detail)`）、`stage3_main.py:3973-3979`（`detail=%s` 再进 ERROR 与 `_log_model_io`）。与 `llm_client.py:54-56` 的"绝不包含 API Key、请求头或聊天正文"对照。

**影响**：外部输入（provider/网关/代理的错误页）落进本机日志与 `data/logs/`。会不会"别人看到不该看的"——取决于那 500 字里有什么；典型 OpenAI 兼容错误体只有一条 message，但网关 400/413 常回显请求片段。

**最小改法**：只记 `status` 与 body 的类别（如"JSON 错误体 / HTML / 其它"），或按白名单字段取（`error.message`／`error.type`）后截断。

**可调项**：`QQBOT_DEBUG_MODEL_IO`（`model_trace.py:35-38`，默认关）只影响原始 I/O 追踪，不影响这条日志——它**始终**写。

**严重度：低　理由**：**推断**成分大——我没有真实的错误体样本，无法证明里面有敏感内容；但"绝不包含"目前没有任何机制保证，属于该收紧的假设。

### B-02　时长一律用"墙上时间点之差"算——与刚修好的 monotonic 是反方向的同一个错（低）

**现象**：已经定下"单调时钟只算时长、墙上时钟只印时刻"的纪律，但仍有几处把**墙上时刻之差**当时长：运行时长、出站补发的存活期、账本落盘节流。

**证据（实测）**：`stage3_main.py:1121` + `:2742`

```
        self.started_at = time.time()
        ...
        uptime = max(0.0, time.time() - snapshot.started_at) if snapshot.started_at else 0.0
```

`outbox.py:105-107`

```
    def _expired(self, item: OutboundItem, now: float) -> str:
        if now - item.queued_at > self.max_age_seconds:
            return "too_old"
```

`api_usage.py:113-118` 的落盘节流同理（`now - self._last_save < SAVE_INTERVAL_SECONDS`）。

**影响**：NTP 校时或手动改表（Windows 开机 RTC 错一次很常见）时，"已运行"会跳变，最坏被 `max(0.0, …)` 夹成 0；出站队列的存活期与落盘节流也会一次性失真。与已知的 monotonic 那处正好相反：**那边是把时长当时刻印，这边是把时刻当时长用。**

**最小改法**：凡是"过了多少秒"的语义统一改用 `self.clock()`（单调）；只在需要印给人看的地方把墙上时刻单独取一份。

**可调项**：`clock` / `wall_clock` 都是构造参数（`stage3_main.py:968-973`），注入点已经现成。

**严重度：低　理由**：只在系统时钟被调整的那一刻出错，且不致命（数量级为"数字难看"）；但它属于同一套线索里的第二半，一起改比分成两次改便宜。

### A-06　未授权 `/admin` 默认回话，`/super permit` 用法里回显被拒命令的发起人 QQ 与原文（低，**有意为之**）

**现象**：没有管理权限的人发 `/admin …`，bot 默认会回一句（可被 `QQBOT_ADMIN_DENY_NOTICE=0` 关掉）；超管发 `/super permit` 且没带引用时，回复里会列出"本群最近的被拒命令"，每条带**发起人 QQ 号**与命令原文前 40 字。

**证据（实测）**：`stage3_main.py:2583-2585`（默认开，见 `:508` `os.environ.get("QQBOT_ADMIN_DENY_NOTICE", "1")`）、`:2607-2615`

```
        recent = self._recent_denied_admins(group_id)
        if recent:
            lines.append("本群最近的被拒命令：")
            for item in recent[:3]:
                text = " ".join(item.text.split())
                ...
                lines.append(f"· {item.user_id}：{text}")
```

**影响**：与 AGENTS §2.1 的 "fail-closed：未授权时静默回落……不暴露层级存在" 有张力——回一句就等于确认了"管理员"这一层存在；`permit` 的用法回显又把成员 QQ 与命令原文摊在群里。

**最小改法**（要改的话）：把默认值改回静默；"最近被拒命令"那份列表只在私聊里给。

**可调项**：`QQBOT_ADMIN_DENY_NOTICE`（`stage3_main.py:508`，`0` = 完全静默）——现成的裁决点。

**严重度：低　理由**：**这一条是有意为之，不是 bug**：`stage3_main.py:3501-3507` 与 `memory_view.ADMIN_HELP_GUEST_NOTE` 都写明"用户明确要求回一句"，代码注释自己也承认了与 fail-closed 的张力。列在这里是给用户一个**裁决点**（要不要恢复静默默认、要不要把列表限定私聊），不是当成缺陷计分。

## 未发现（如实报告）

**1. 没有找到任何"聊天/记忆/资料/插件内容进 system 段"的路径。**（这是本次最重的一条，也是清账）

system 只有四类来源，逐处追过：

| 来源 | 位置 | 是否含用户/模型内容 |
| --- | --- | --- |
| 模块常量（含代码层规矩） | `stage3_runtime.SYSTEM_PROMPT` / `compose_system_prompt` 追加的三条常量（`stage3_runtime.py:848-891`） | 否，逐轮逐字相同 |
| `prompt_library` 覆盖 | `data/prompts/<name>.json` → `PromptLibrary.text()` | 是**管理员**写的，不是用户；保存时过机制词扫描 |
| 插件登记 | `registry.provide_prompt(name, …)`，仅 `vision` 一套有消费者（`prompt_library.py:241-245`） | 由插件提供；登记 `reply`/`judge`/`memory`/`persona` 会被**静默忽略**（`builtin()` 只对 `vision` 分支查插件登记）——fail-closed，不是漏洞 |
| 人格文件 | `QQBOT_BASE_PROMPT_FILE` → `data/private_docs/base_prompt.REAL.py` → 模板（`base_prompt.py:197-252`） | 管理员/部署者写的文件，import 期定一次 |

反向确认（都进 **user 段**，不是 system）：插件材料与知识库 → `_extension_data` → `volatile_content`（`stage3_runtime.py:1590-1593`）；打回重写的指令 → `_revision_request` 追加一条 user 消息（`stage3_main.py:1256-1264`）；记忆/画像/信/金句材料 → 各 `data_boundary.render(...)`（`stage3_runtime.py:1601-1641`）。

**2. `base_prompt` 面板改不了。** `QQBOT_BASE_PROMPT_FILE` **不在** `operator_config.SETTINGS`（`operator_config.py:57-97` 全表核对过，里面只有模型/凭据/审批类键），面板的覆盖层只能写表里的键；而且 `BASE_PROMPT = _load_base_prompt()` 是 import 期常量（`base_prompt.py:256`），运行期改环境变量也不会生效。唯一能在运行期改 system 的是 `data/prompts/*.json` 覆盖（保存路径有护栏，加载路径没有——见 A-04）。

此外逐条看过、结果正常的（供核对）：`/super` 与插件命令的未授权分支都是"不回复、不进历史、不交模型"（`stage3_main.py:3419-3426`、`:3489-3508`）；越权 relay 目标被 `_admin_may_manage_group` 挡住（`:3254-3259`）；旧版状态文件里的"全局管理员名单"只提示不生效（`:3196-3203`，fail-closed）；`read_version` 的版本号按白名单形状校验、不拼路径（`prompt_library.py:527-537`）。

## 证据与量法

- **A-01 / A-02 / B-01 / A-03 / A-04 / B-02 / A-06：实测。** 依据是读到的文件与行号，代码路径逐条追到调用点（例如 A-02 的 `@{qq}` 追到 `conversation_context.py:203` → `stage3_main.py:1615-1621` → `stage3_runtime._format_history`）。
- **A-05：代码路径实测，"有没有敏感内容"是推断。** 我没有 provider 的真实错误体样本，无法证明那 500 字里有什么；标"推断"是因为命题的后半段（"里面有不该记的东西"）没有复现过。
- **A-06：行为实测，"要不要改"是人的裁决。** 代码注释与帮助文本都明写用户要求过，所以我把它与真缺陷分开列。
- 行号取自 2026-10-08 的 main 工作区；若同一棵树上另有 agent 正在改源码，行号可能已经漂移，复核时以**函数名 + 片段**为准（每条都贴了原文）。

## 我没做到的

1. **没跑任何测试**（本次是只读审计，未执行 `tests/run_offline.py`、未跑 pyflakes），所以"护栏在测试里"那句话的依据是**读**到 `tests/test_persona_shape.py:75`，不是"跑过并通过"。
2. **没有在运行中复现任何一条**，包括 A-01（没有真去群里敲 `/super processes`）与 B-01（没有换时区跑一遍）。全部结论来自静态代码路径。
3. **面板与插件树不在本树**（main 的 `plugins/` 只有 `__init__.py`），所以"面板能不能改 system 段"只回答了核心侧的三口接缝（`PromptLibrary` / `operator_config` / 人格文件），没有验证 `stage4-plugins` 分支上面板自己的写盘路径；A-03 的影响面同样落在插件树上。
4. **没看 `run\`、没看 `data/` 的真实内容**（只按路径推断 `data/prompts/*.json` 与 `data/private_docs/base_prompt.REAL.py` 存在），也没有核对本机实际生效的那份人格文件。
5. 未做交叉验证（没有第二个独立视角复核这些行号）。
