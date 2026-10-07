# 核查报告 · 可调项清点

被核查对象：`dev/docs/audits/configurability-inventory-2026-10-08.md`（364 行，20 条可调项）。
核查时间：2026-10-08。核查方式：只读源码 + 只读运行（未改任何源码、未碰 `run\` / `.env` / `data\memory\`）。
仓库：`C:\Users\XuZhunzhi\QQRoleplayBot\dev`，分支 `main`，核查起点 `HEAD = cb72f55`。
我开工时 `git status --short` 只有 `?? yunru-source/`（未跟踪），**没有源码被修改**——它文末说的"同树有别的改动在进行"到此刻已经不成立。

术语约定：**实测** = 我读过文件、或在本机跑过命令并看到输出；**推断** = 我的判断，没有直接读数支撑。

---

## 一句话结论（这份文档有几成可信 / 有没有必须更正的地方）

**约八成可信，但有两处必须更正的硬伤，另有一处必须补的关键数目。**

- 它的**两条结构性事实站得住**：面板白名单里一个数值类键都没有（`kind` 只有 `bool` / `str` / `csv` / `secret`，实测 `normalize()` 无 number 分支）；`dev_config.X` 是 import 期求值、面板可改的值走的是"调用时现读"（`operator_config.inject()` 的 docstring 自己就把这条纪律写出来了，`runtime.py:322` 还留了一次真实事故的记录）。
- 它最核心的一条论断（第 5 条）**判错了**：`BATCH_SIZE` / `COOLDOWN_SECONDS` 根本不是环境变量，是 `dev_config.py:269-270` 的硬编码常量；它引用的两个环境变量名**全仓零命中**（实测检索）。这一条的"现状"表格把"写死"写成了"环境变量 ✓（需重启）"——方向正好搞反。
- 关键数目**数错了**：`operator_config.SETTINGS` 是 **32 个键**，不是 30 个（实测枚举；其中 `bool` 10 / `str` 13 / `secret` 7 / `csv` 2）。
- 还有一处**整类漏项**：`dev_config.FOCUS_*` 那四条数值环境变量（多群焦点的静默/当值/连回上限/"稍等"冷却）在装配点真的被传进去了，清单里一个字都没有。
- 其余抽查的 8 条现值、行号、常量名**全部对得上**（差一个数的情况没有出现），它自报的"行号可能漂移、照名字找"也算诚实。

---

## 我抽了哪几条、逐条核对结果

抽样口径按任务要求偏向"最容易被写错的"：现值是数字的、现状写着"环境变量/面板可改"的、"建议默认值"与"谁该能改"的。共抽 **10 条**（原清单第 1、2、5、6、7、8、9、14、18、20 条）。

### 抽 1 · 第 1 条「记忆注入条数上限 + 置信度门槛 + 候选池」——**正确**

| 它写的 | 我读到的 |
| --- | --- |
| 门槛 0.5，`memory_store.py:1547` | `1547: " AND visibility='group_safe' AND confidence>=0.5 AND ..."` 一致 |
| 门槛 0.5，`memory_store.py:1563` | `1563: "WHERE r.status='active' AND r.visibility='group_safe' AND r.confidence>=0.5 "` 一致 |
| `scored[:max(0, 5 - len(profile))]`，`memory_store.py:1626` | `1626: result = tuple(record for _, record in scored[:max(0, 5 - len(profile))]) + tuple(profile)` 一致 |
| 每个 scope 候选 `LIMIT 100` | `1549: " ORDER BY updated_at DESC LIMIT 100"` 一致（它没给行号，但值对） |

现值 5 / 0.5 / 100 全部正确，行号零漂移。"写死"判断也成立（这三处都是 SQL 字面量与切片字面量，无环境变量读法）。

### 抽 2 · 第 2 条「压缩三条阈值」——**正确（但漏了两条邻居常量）**

- `stage3_main.py:114` `HISTORY_LIMIT = 2000`、`:116` `LIVE_TARGET = 500`、`:118` `COMPACT_KEEP = 50`——**实测一致**，行号也一致。
- `:894` `ConversationState(history_limit=HISTORY_LIMIT)`，确实是直接进构造函数、无开关读法，它的"写死"判断成立。
- **但同一片常量区里还有两条它没列**：`:121 MAX_COMPACT_BATCHES_PER_TURN = 6`、`:122 ACTIVE_TIMEOUT_SECONDS = 180.0`。见下文「它漏掉的」。

### 抽 3 · 第 5 条「防抖 / 批量 / 冷却 / 触发时间窗」——**错（本报告最重的一处）**

它写：「批量 20 条、冷却 60 秒（这两项走环境变量 `QQBOT_BATCH_SIZE` / `QQBOT_COOLDOWN_SECONDS`，`dev_config.py:269-270`，**但不在面板白名单**）」。

我读到的实际：

- `dev_config.py:269` `BATCH_SIZE = 20`、`:270` `COOLDOWN_SECONDS = 60.0`——**是纯字面量，没有任何 `get()` / `_int()` / `_float()` 包裹**。它甚至引对了行号，却读错了这两行的性质。
- `stage3_main.py:16-17` 是 `from .dev_config import (BATCH_SIZE, COOLDOWN_SECONDS, ...)`——import 期把常量名拿进来，与"环境变量"无关。
- 实测检索（`src/` 全树 + `tests/`）：`QQBOT_BATCH_SIZE` 命中 **0**、`QQBOT_COOLDOWN_SECONDS` 命中 **0**。这两个环境变量**在本仓库里根本不存在**（连注释都没有）。
- 同一行的第三个参数它写的"60.0 写死"是**对的**：`stage3_main.py:899` `IdleTrigger(BATCH_SIZE, 60.0, COOLDOWN_SECONDS)`，第二个位置参数就是 `trigger.py:39 interval_seconds: float = 60.0`。

结论：这一条**不是"混合"**，是**三项全部写死**。它的"现状"列与"建议做成"列都建立在一个不存在的环境变量上，必须改。附带影响：它的"一句话结论"把环境变量的存在当成了论据（"两个环境变量 ✓（需重启）"），那句话也要跟着改。

### 抽 4 · 第 6 条「分段发送的节奏」——**正确（调用点的解释可以更准）**

- `typing_sim.py:19 / 22 / 24 / 26 / 28 / 30` 分别是 `MAX_SEGMENTS = 3`、`FIRST_BASE_SECONDS = 1.4`、`PER_CHAR_SECONDS = 0.075`、`MIN_GAP_SECONDS = 0.7`、`MAX_PAUSE_SECONDS = 6.0`、`JITTER_RATIO = 0.22`——**六个值、六个行号全部一致**。
- 总开关 `stage3_main.py:158-161` `_typing_sim_enabled()` 读 `QQBOT_TYPING_SIM`——一致。
- `stage3_main.py:4672` `pauses = engine.host.styler.delay_plan([...])`——确实是**不传 kwargs** 的写法，它说的没错。更准的说法是：不传 kwargs 之后落到 `_host_adapters.py:49-52` `TypingStyler.delay_plan` → `typing_sim.delay_plan(segments)`，用模块默认值。这与结论（参数写死）同向。

### 抽 5 · 第 7 条「风格审核的超时与最多重写几次」——**正确，但内部有个自相矛盾要指出**

- `style_reviewer.py:91` `REVIEW_TIMEOUT_SECONDS = 8.0`——**实测一致**。
- 重写控制流 `stage3_main.py:3999` `style_rewrote = False`、`:4036` `if (grounding is not None and grounding.rewrite and not style_rewrote ...)`——它给的 `:3993-3994` / `:4029-4030` / `:4036` 是注释与判断所在的位置，**对得上**；"最多一次、审核与『没把握』共用"这条读法我核对注释后确认成立。
- **自相矛盾**：这一条"建议做成"写的是「超时走面板可改」，而文末「不建议做成可调的 5 条」第 4 条又把「审核的 fail-open 方向（超时…）」整块划为不该给面板。同一份文档对同一个旋钮给了两个相反结论，需要择一。（我的看法见下文「它说『不建议可调』里我认为可以可调的」。）

### 抽 6 · 第 8 条「记忆维护的节拍」——**部分正确，但两处注释与行号不准**

- `memory_config.py:30-33`：`interval_seconds: float = 600.0` / `retention_days: int = 7` / `model_timeout: float = 45.0` / `max_batches: int = 8`——**四个值、四个行号全对**。
- `memory_maintenance_agent.py:225` `lease_seconds=self.timeout + 30`——**实测一致**，"租约 = 超时 + 30"对。
- `runtime.py:1939-1940` `timeout=settings.model_timeout - 1, max_tokens=4096`——值对，但**它写的是"维护 client `max_tokens = 4096`"，而真正送进去的超时是 `model_timeout - 1`（默认 44.0），不是 45**。这一处它没说错，但读者容易把 45 当成实际超时。
- 两个环境变量的夹取（`memory_config.py:41-42`，间隔 [60, 86400]、保留期 [1, 30]）我**跑了一遍**，确认属实（见「我复现了哪些实测」）。
- **它没说的**：`model_timeout` 与 `max_batches` 也走同一个 `MemorySettings.from_environment()`，但那个 classmethod **只读两个变量**，这两个字段没有任何环境变量入口（实测检索 `QQBOT_MEMORY_TIMEOUT` / `QQBOT_MEMORY_MAX_BATCHES` 均命中 0）。它说"批数 8 / 超时 45 / 4096 写死"，结论对，理由没给全。

### 抽 7 · 第 9 条「模型调用超时 / 重试次数 / 退避」——**正确**

- `llm_client.py:19-21` `DEFAULT_MAX_ATTEMPTS = 3`、`DEFAULT_BACKOFF_SECONDS = 0.5`、`MAX_BACKOFF_SECONDS = 8.0`——**实测一致**。
- 它写"`llm_client.py:168`（`timeout: float = 60.0`）"：**`timeout: float = 60.0` 在 `:168`，这是对的**（`def __init__` 在 `:163`），我核对过。
- "构造点都不显式传"：实测 `OpenAICompatibleClient(` 在 `src/` 下共 **7** 处，其中 `runtime.py:551 / 1595 / 1607 / 1629 / 1672 / 1697 / 1935`（它列了 551 / 1595 / 1607 / 1629 / 1697，**漏了 1672 与 1935**）；全树只有两处带 `timeout=`（`runtime.py:95` 是 SnowLuma 上下文客户端的，`:1939` 是记忆维护客户端），**回复/判定/审核/写信那几路都没有传**。所以"默认 60.0 秒"对，但"构造点"那张清单不全。

### 抽 8 · 第 14 条「模型日志容量」——**正确**

- `feature_log.py:47` `DEFAULT_CAPACITY = 1000`、`:49` `MAX_FIELD_CHARS = 20000`——**实测一致**。
- `feature_log.py:81` 读 `QQBOT_FEATURE_LOG_CAPACITY`，`:84` `return max(10, min(100000, value))`——**实测一致**，"环境变量 ✓、不在面板白名单、需重启"的判读成立（它没写夹取上限，我补上：**上界是 100000，不是注释里那个"10 分钟"**）。

### 抽 9 · 第 18 条「去重相似度阈值 / 每日新增上限 / 重放次数」——**正确，且"构造函数开了口子但没人传值"这句是它最有价值的一处**

- `memory_store.py:215` `NEAR_DUPLICATE_RATIO = 0.4`、`:218` `DEFAULT_DAILY_ADD_LIMIT = 8`、`:199` `MAX_REPLAY_ATTEMPTS = 2`、`:197` `MAX_INBOX_EVENTS = 10000`、`:198` `MAX_INBOX_EVENTS_PER_GROUP = 2000`——**五个值、五个行号全对**。
- 构造参数 `:302 daily_add_limit: int = DEFAULT_DAILY_ADD_LIMIT`、`:303 near_duplicate_ratio: float = NEAR_DUPLICATE_RATIO`——**实测一致**。
- **"没有任何装配点传值"我复核了**：全仓 `MemoryStore(` 只有一处构造点 `memory_service.py:21`，写的是 `MemoryStore(self.settings.data_dir / "memory.sqlite3", retention_seconds=self.settings.retention_days * 86400, protect=True)`——**确实没传 `daily_add_limit` / `near_duplicate_ratio`**。它这条判读是对的。
- 可选修正：`retention_days` 与 `retention_seconds` 是**同一件事的两个名字**（`memory_config.py:31` 与 `memory_store.py:301`），说"写死"时它们其实是一条环境变量链上的（`QQBOT_MEMORY_RETENTION_DAYS`）——它把这两者放在了第 8 条与第 17/18 条两处，读起来像两件事。

### 抽 10 · 第 20 条「运维节奏与容量」——**正确**

- `runtime.py:2129` `CONNECTION_WARN_EVERY_SECONDS = 600.0`、`:2142` `CONNECTION_DETECT_EVERY_SECONDS = 60.0`——**两个值、两个行号全对**。
- `dev_config.py:546` `RAW_EVENTS_MAX = _int("QQBOT_RAW_EVENTS_MAX", 5000)`、`:527` `CHAT_LOG_MAX = _int("QQBOT_CHAT_LOG_MAX", 20000)`、`:492` `WEBUI_PORT = _int("QQBOT_WEBUI_PORT", 8790)`——**三个值、三个行号全对**；"环境变量 ✓、不在面板白名单"成立（`SETTINGS` 里确实没有这三个键）。

### 「谁该能改」这一列：**没有论证，是随口给的**

这是它 20 条里唯一**每一条都缺依据**的一列。我查过可能的依据来源：

- `operator_config.SETTINGS` 里**没有任何权限字段**——它只有 `kind` / `applies` / `env` 三种（`operator_config.py:40-97`）。也就是说"这个值该超管还是该管理员"这件事，在配置层根本没有对应的概念可引用。
- 仓库里确实有超管 / 管理员的区分（`dev_config.py:267-268` 的两份名单、`stage3_main.py:1743 is_super_admin`），也有 `docs/ADMIN_CONSOLE.md` 描述这两层的**命令**权限；但那篇文档没有给"某个配置值的改动权归谁"划任何线。
- 它自己在文末引用的那条口径恰好**否掉了这个区分**：用户 2026-10-01 说过"面板权限除了不能动代码以外权限跟你是一致的"（`operator_config.py:3`）。按这条，面板上根本不存在"管理员能改但不能改这个数"的档位。

**结论**：这一列不是"错"，是**无论证**——20 条里"超管"与"管理员"的分配看不出任何来源，读者无法据它落地。建议要么删掉这一列，要么补一条现成的权限口径作为依据。

---

## 三条关键论断的核查结果

### 论断 1「面板白名单 `operator_config.SETTINGS` 没有数值类键」——**成立，但键数它数错了**

实测（脚本枚举，`sys.path` 指向 `src`）：

```text
TOTAL 32
bool   10  judge_enabled review_enabled vision_enabled memory_enabled compaction_enabled
           letter_enabled group_manage_enabled group_owner_enabled ask_when_unsure auto_approve_join
csv     2  approve_whitelist approve_blacklist
secret  7  api_key reply_api_key letter_api_key judge_api_key memory_api_key review_api_key vision_api_key
str    13  provider api_base_url api_provider api_model memory_model reply_api_base_url reply_api_model
           letter_api_base_url letter_api_model vision_api_base_url vision_api_model
           approve_pattern approve_reject_reason
```

- **键数 = 32**，它写 **30**。**这是必须更正的数目。**（`applies` 分布实测为 `live` 27 / `restart` 5，它文末提到的"十个布尔开关"对得上——bool 正好 10 个。）
- **kind 种类 = 4**（`bool` / `str` / `csv` / `secret`），**没有 `number`**——它这条论断成立，`len({...})` 也只有这四种，脚本里 `any(kind == "number")` 为 False。
- 它另一个相关引用也对："`normalize()` 在 `:112-136`，无 number 分支"——`normalize` 实测定义在 `operator_config.py:112`，函数体到 `:136`，分支只有 `bool` / `csv` / 兜底字符串三路。
- 口径提醒（推断）：键数是**会漂的**（这份文档成稿时是 30 还是 32，我无法回溯确认——我只能说我读到的这一版是 32）。所以这一条建议改成"键数另行枚举，不写死数字"，并把"零数值键"这个**结构性结论**留下。

### 论断 2「`operator_config` 只有 bool/str/csv/secret 四种 kind、没有 number」——**成立，定义处如下**

kind 的**定义处**是 `operator_config.py:40` 起的 `SETTINGS` 字典字面量本身（每个键的 `"kind"` 字段），**消费处**是 `normalize()`。贴代码（实测，原文）：

```python
# operator_config.py:40-51（节选，展示 kind 的取值形状）
SETTINGS: dict[str, dict[str, object]] = {
    # --- agent 开关（全部 live：调用点每次现读） ---
    "judge_enabled": {"kind": "bool", "applies": "live", "env": "QQBOT_JUDGE_ENABLED"},
    ...
    "ask_when_unsure": {"kind": "bool", "applies": "live", "env": "QQBOT_ASK_WHEN_UNSURE"},
```

```python
# operator_config.py:112-136（normalize 的类型分支，全文）
def normalize(key: str, value: object) -> str:
    """把面板传来的值归一化成字符串（落盘与环境变量都只存字符串）。"""

    spec = SETTINGS.get(str(key))
    if spec is None:
        raise ConfigRejected(f"不认识的配置项：{key}")
    kind = str(spec["kind"])
    if kind == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        text = str(value).strip().casefold()
        if text in BOOL_TRUE:
            return "1"
        if text in {"0", "false", "no", "off", ""}:
            return "0"
        raise ConfigRejected(f"{key} 只接受 true/false")
    if kind == "csv":
        items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        return ",".join(cleaned)
    text = str(value or "").strip()
    if kind == "secret" and not text:
        # 空值 = "清掉这一项，回落到主 key / .env"。这是有意义的操作，不是错误。
        return ""
    return text
```

`kind` 的取值只有 `bool` / `csv` / 其余归入字符串（`secret` 只是"空值有意义"的特例）。**没有任何数值校验、范围钳制或 `float()` / `int()` 转换**——所以它的论断成立，并且它第 1 条改造建议（"加一种 `kind: "number"`"）方向正确。

顺带一个它没提到的细节（实测 + 推断）：因为兜底分支是 `str(value)`，面板即使提交数字，也会被存成字符串；落盘与 `env` 注入都只认字符串。所以"加 number"不只是加一个分支，还要决定"范围谁校验"。

### 论断 3「`dev_config.X` 是 import 期求值，面板可改的数值照抄会『显示已生效其实没生效』」——**成立，而且仓库里有现成的正面/反面两类证据**

这条是全篇最重要的，我按"找反例"的方式查：

1. **它自己的正面证据是对的。** `operator_config.py:205-210` 的 `inject()` docstring 原文写着：
   > 必须在 `dev_config` 被 import 之前调用一次——`dev_config` 的所有常量都在 import 时求值。

   这是**仓库自己在源码里承认的**一条结构性事实，不是它的推断。

2. **"现成的、面板可改的值走 import 期常量"的反例：我没有找到。** 我逐个抽了 `SETTINGS` 里最像会踩坑的三类：
   - `judge_enabled`（`live`）：调用点 `stage3_main.py:1478 if not self._flags().judge_enabled` 走 `runtime_flags` 对象；`runtime_flags.py:1-7` 明确写「`dev_config.X` 在 import 时求值一次，改环境变量不会回头改它——那是"需重启"的默认形态；这里这份每处调用点**现读**」。成立。
   - `api_base_url` / `api_model` / `reply_api_model` 等（`live`）：`model_config._read()`（`model_config.py:242-245`）就是 `os.environ.get(name, "").strip()`，`resolve()`（`:488`）每次调用现算；`runtime.py:357-359` 的注释还专门交代"先把值写进环境，再解析那一对"。成立。
   - 三个 `restart` 项（`auto_approve_join` / `approve_*`）：策略对象在装配时构造，标 `restart` 是**如实**的。
   → **所以这一条不是"已经发生了"，而是"照抄会踩"的预防性论断。** 它写的是"照抄它的常量读法会让面板上的改动不生效"（条件句），措辞没错；但它开篇把这条列为"两条结构性事实（都是实测）"之一，读起来像"现在就有这个 bug"，这两者不是一回事。

3. **同类事故的现成记录我找到了（这反而加强它）。** `runtime.py:320-333` 那段注释记着一次真实事故，根因正是 import 期常量：
   > 这里原来写的是 `os.environ[env] = ""`，而且紧跟着读 `_cfg.API_KEY` 当回落值——但 `_cfg.API_KEY` 是**这个进程启动时**从环境里读的常量，一旦环境里已经被写成空串，它自己就是空的。

   而 `dev_config.API_KEY` 确实是 import 期常量（`dev_config.py:188 API_KEY = _ENV_API_KEY`），今天仍有两处按 import 期语义读它：`runtime.py:341` `fallback = os.environ.get("QQBOT_API_KEY", "") or _cfg.API_KEY`、`runtime.py:349`。
   **这是我能找到的最接近"面板上那个 key 与进程里那个常量不是同一个东西"的实据**——但它的设计后果被处理成了"删除环境变量 + 回落"，所以**不是**"显示已生效其实没生效"的活 bug（它的 `applies` 标 `live` 是对面板那条链路而言的）。

**结论**：论断 3 **方向与依据都成立**，"正确形状可照抄"的清单（`persona_offset_hours()` 在 `dev_config.py:294-297`、`reply_api_*` 在 `:212-233`、`memory_config._float_env` / `_int_env` 在 `:10-23`）我逐处核对**全部属实**。唯一要收紧的是措辞：**它证明的是"这个坑已经写进纪律、且踩过一次"，不是"今天有一个面板键正在骗人"。**

---

## 它漏掉的（我另找到的候选，给现值与位置）

我把 `dev_config.py` / `memory_config.py` / `stage3_main.py` 顶部常量区又扫了一遍，清单里没有的数值常量不少。**最该进清单的第一条是"整类漏项"，其余按价值排序。**

### 漏项 1（整类）· 多群焦点的四条数值——**已经是环境变量、已经接进装配点、清单里一个字没有**

实测：

- 定义 `dev_config.py:508-511`：
  ```python
  FOCUS_QUIET_SECONDS = _float("QQBOT_FOCUS_QUIET_SECONDS", 45.0)
  FOCUS_DUTY_SECONDS = _float("QQBOT_FOCUS_DUTY_SECONDS", 300.0)
  FOCUS_REPLY_LIMIT = _int("QQBOT_FOCUS_REPLY_LIMIT", 50)
  FOCUS_ACK_COOLDOWN_SECONDS = _float("QQBOT_FOCUS_ACK_COOLDOWN_SECONDS", 120.0)
  ```
- 装配 `stage3_main.py:1102-1106`：
  ```python
  self.focus = FocusController(
      quiet_seconds=dev_config.FOCUS_QUIET_SECONDS,
      duty_limit_seconds=dev_config.FOCUS_DUTY_SECONDS,
      reply_limit=dev_config.FOCUS_REPLY_LIMIT,
      ack_cooldown_seconds=dev_config.FOCUS_ACK_COOLDOWN_SECONDS,
  )
  ```
- 模块默认值在 `focus.py:22-25`（45.0 / 300.0 / 50 / 120.0）。

现值：**45 秒（话题静默多久算结束）/ 300 秒（当值上限）/ 50 条（连回上限）/ 120 秒（"稍等"冷却）**。
为什么该进清单：它是**唯一一组"数值 + 环境变量 + 真装配点"三项俱全**的例子，比第 5 条那个不存在的环境变量更有资格代表"现状"那一档；而且这四个数直接决定"她在一个群里连说几句才走"，属于第 5 条同类价值。

### 漏项 2 · 压缩一轮最多几批

- `stage3_main.py:121` `MAX_COMPACT_BATCHES_PER_TURN = 6`。
- 用途：一轮里最多发几批压缩调用（原注释：一次触发通常 4 批能压完，这里防积压时一轮发出太多调用）。
- 为什么该进清单：它和第 2 条那三条阈值是**同一件事的成本闸**——第 2 条调大了压缩频率，这一条不跟着调，就会出现"每次都压不完、越积越多"。它比第 3 条那四个 `dialogue_compaction` 常量更贴近"一轮要花多少钱"。

### 漏项 3 · 会话活跃超时

- `stage3_main.py:122` `ACTIVE_TIMEOUT_SECONDS = 180.0`。
- 用途：`stage3_main.py:905 if state.last_activity_at is not None and now - state.last_activity_at > ACTIVE_TIMEOUT_SECONDS: state.exit()`——群多久没人说话就退出活跃对话。
- 为什么该进清单：这与第 5 条（触发节奏）、第 11 条（触发时间窗）是**同一族节奏参数**，冷群/热群同样差一个量级；它现在连"环境变量"都没有。

### 漏项 4 · 状态持久化的两个数

- `stage3_main.py:149` `MAX_PERSISTED_SESSIONS = 200`（用途见 `:3172 self.sessions.export(max_sessions=MAX_PERSISTED_SESSIONS)`；`state_store.py:21` 还有一份同值默认，**两处各写一遍**——这本身就是"该收口成一个可调项"的信号）。
- `stage3_main.py:151` `PERSIST_MIN_INTERVAL_SECONDS = 10.0`（用途见 `:4334`，节流写盘）。
- 为什么该进清单：持久化相关文档里它出现过（`docs/ADMIN_CONSOLE.md` 提到 `stage3_main.py:3196` 的状态恢复），但数值没人管；重启后"记得多少会话"直接取决于 200。

### 漏项 5（可选）· 转告与扩展超时

- `stage3_main.py:146` `EXTENSION_TIMEOUT_SECONDS = 2.0`（调用点 `:3860`、`:4148`）。
- `stage3_main.py:147` `RELAY_CONFIRMATION_TIMEOUT_SECONDS = 120.0`（调用点 `:3225`、`:3233`、`:3272`、`:3315`）。
- `stage3_main.py:148` `RELAY_CONTENT_MAX_LENGTH = 1000`（调用点 `:3278`）。

这三条价值低一档（它们是工具性流程的常数），列出来是为了说明**它的"20 条"口径不是"全部数值常量"**，而它开篇那句话（"现在写死、但明显应该可调的值至少 20 处"）读起来容易让人以为已经穷尽。

### 顺带：它自己的清单内部也漏了同类项

- 第 19 条"金句学习"列了 5 条（`MAX_NOTES_PER_REPLY` / `INJECT_CHAR_BUDGET` / `DEFAULT_INTERVAL_SECONDS` / `MAX_SAMPLES_PER_RUN` / `MAX_BATCHES_PER_RUN` / `MAX_NOTES`），但 `quote_learning.py` 顶部还有 `NOTE_BIGRAM_MIN = 0.2`（`:89`）、`NOTE_CHAR_MIN = 0.6`（`:90`）、`NOTE_CHAR_FALLBACK_MAX = 2`（`:93`）、`DEFAULT_TIMEOUT_SECONDS = 60.0`（`:104`）、`MAX_SEEN = 2000`（`:100`）。**这几个才是"注多注少"的判据**，比它列的条数项更影响结果。

---

## 它说"不建议可调"里我认为可以可调的（给理由）

它那 5 条我逐条读过后，**第 1、4 两条我认为可以（其中第 4 条它自己就已经给了相反结论），其余三条我同意它**。

### 可以可调 1 · 「数据边界短码的长度与字符集」（它的第 1 条）

- 它反对的理由是"形状判据（含入站中和）与它绑在同一份正则上，改了就等于同时改两件事"。
- 但**这两件事在源码里本来就是同一个常量驱动的**：`data_boundary.py:71 MARKER_LENGTH = 7`、`:73 MARKER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"`，而正则 `:80-81` 是 `r"\u2039([A-Z2-9]{%d})\u203a" % MARKER_LENGTH`——**注意 `[A-Z2-9]` 是写死的字符类，并没有引用 `MARKER_ALPHABET`**。也就是说：改 `MARKER_LENGTH` 会同步改"生成"与"判形状"（这正是它担心的耦合），但改 `MARKER_ALPHABET`**不会**动正则——两边今天就可能悄悄不一致。
- 我的理由：**把 `MARKER_LENGTH` 做成可调（带范围钳制，例如 [4, 16]）反而是把这份耦合唯一化的机会**——让正则从常量拼出来（它现在已经是 `% MARKER_LENGTH` 拼的），并让字符集也参与拼装，比"两个常量各写一半"更安全。真正不该开放的是"关掉整套记号"，而那件事**已经有开关**（`QQBOT_DATA_MARKERS`，`dev_config.py:615` / `runtime_flags.py:154`）——它把它放在了第 3 条，也承认了。
- 保留意见：这东西一旦调短会**降低伪造成本**，所以如果开放，必须带"只能在不缩小熵的前提下调"的钳制，或者干脆只开放"加长"。这是唯一的真实风险，但它不等于"不可调"。

### 可以可调 2 · 「重写次数上限（1 次）与审核的 fail-open 方向」——**它自己已经矛盾了**

- 它的第 4 条把"审核的 fail-open 方向（超时、认不出按通过）"和"重写次数上限"绑成一件"不该给面板"的控制流。
- 但它的 20 条清单第 7 条写的是："**建议做成：超时走面板可改**；重写次数保持 1 次，只给超管一个'再宽一次'的开关"。
- 同一份文档里，同一个旋钮一次"建议走面板"、一次"给面板只会多出一条把成本翻倍的旋钮"。**至少有一处要改。**
- 我的理由（倾向第 7 条那一侧）：**"审核超时"与"重写次数"是两件事，不该捆在一起。** `style_reviewer.py:91 REVIEW_TIMEOUT_SECONDS = 8.0` 是"慢"的唯一来源，而 `stage3_main.py:3994` 的"最多一次"是**循环上界**——把超时做成可调不会让重写次数增加（次数由 `style_rewrote` 这个布尔量管着，与超时无关）。它把两者并称，等于用一个真实的成本风险（放宽重写次数）去否掉一个无关的延迟旋钮。
- 真正该保留的边界是：**重写次数不开放**（这条我同意它），**超时可以开放且必须带范围钳制**（例如 [2, 15] 秒，上界要保证"就算审核一直超时也不会把一条回复拖过 15 秒"）。

### 我同意它、并补一句理由的三条

- **第 2 条（超管/管理员兜底号）**：它已经是环境变量（`dev_config.py:267-268`），**不再是硬编码**；把它做成面板可改确实等于"给在线入口一条自提权路径"。它给的事故理由（checkout 还原导致权限丢失）在 `dev_config.py:251-257` 有原文，属实。
- **第 3 条（关掉边界记号升级成一排参数）**：总开关确实已存在（`dev_config.py:615`），再开放字符集/长度的子参数就是把"机器可验证的边界"逐步调没。与上面"可以可调 1"不冲突的一点是：我主张开放的是**带钳制的长度**，不是"关掉整套"。
- **第 5 条（记忆自我提及闸门词表）**：`memory_filters.py:60-99` 那组词表是 AGENTS §2.2 的执行体，放宽它是**安全边界**问题而不是可调性问题，词表本身在 `memory_filters.py:75-99` 的 `SYSTEM_SELF_RULES` 里逐条带"实测漏进来的原文"注释，动它需要一次体检而不是一个配置项。

---

## 我复现了哪些"实测"、结果如何

它说"实测"的地方我挑了三处**可独立复现**的，都在只读环境里跑过（没有写任何文件、没有碰 `data/`）。

### 复现 1 · 环境变量的上下限钳制（对应它第 8 条）

命令（`sys.path` 指向 `src`，直接调 `MemorySettings.from_environment()`，逐个改环境变量）：

```text
default    interval= 600.0 retention= 7 timeout= 45.0 max_batches= 8
low        interval= 60.0 retention= 1 timeout= 45.0 max_batches= 8     (QQBOT_MEMORY_INTERVAL_SECONDS=5, QQBOT_MEMORY_RETENTION_DAYS=0)
high       interval= 86400.0 retention= 30 timeout= 45.0 max_batches= 8 (999999 / 99)
mid        interval= 120.0 retention= 3 timeout= 45.0 max_batches= 8
garbage    interval= 600.0 retention= 7 timeout= 45.0 max_batches= 8    ('abc' / 'xyz')
```

**结果：它写的那两个钳制区间完全属实**（[60, 86400] 与 [1, 30]），并且**额外确认**：写坏的值回落到默认，不抛错；`model_timeout` 与 `max_batches` 全程恒为 45.0 / 8——**再次证明那两个字段没有任何环境变量入口**（它说"写死"，对）。

### 复现 2 · 容量钳制（它文末"带上下限钳制"那段点名的）与文档口径

```text
feature_log DEFAULT_CAPACITY 1000  MAX_FIELD_CHARS 20000
  QQBOT_FEATURE_LOG_CAPACITY='1'       -> 10
  QQBOT_FEATURE_LOG_CAPACITY='5'       -> 10
  QQBOT_FEATURE_LOG_CAPACITY='10'      -> 10
  QQBOT_FEATURE_LOG_CAPACITY='5000'    -> 5000
  QQBOT_FEATURE_LOG_CAPACITY='9999999' -> 100000
chat_log    QQBOT_CHAT_LOG_MAX='1'         -> 10 ; '999999999' -> 1000000
raw_events  QQBOT_RAW_EVENTS_MAX='1'       -> 10 ; '999999999' -> 1000000
```

**结果：它说的"容量夹取（[10, 1000000]，不允许无限增长）"成立。** 补充两条它没写的：`feature_log` 的上界是 **100000**（不是 1000000），而 `chat_log.py` / `raw_events.py` 这两份**确实是 1000000**——也就是"不能无限增长"这个结论对三份都成立，但**上界数字三份并不一致**，它把三份并成一句话说，读起来像同一个范围。

### 复现 3 · 面板白名单的键与 kind（对应它论断 1 与论断 2）

见上文「论断 1」的枚举输出。**结果：零数值键成立；键数 32（它写 30）；kind 四种、无 number 成立。**

### 未复现但它引用的历史数据（我只做"溯源"，没有独立重跑）

- 第 13 条"实测 60% 闲聊会召回平均 1300 字"、第 1 条相关的"实测 470 条里丢了 152 条"：我在源码里检索到的是**注释里的记录**（例如 `knowledge_base.py:73-74` 记着语义召回的实测相似度 0.84 / 0.45 / 0.25），**没有**找到 60% / 1300 字 / 470 条 / 152 条这组数字的原始出处。它自己在文末也承认了"来自源码注释里的记录，我没有独立复现"——**这一处它标得诚实，我不追加指控**，但也确认不了。

---

## 我没做到的 / 读不到的

1. **我读不到面板侧（WebUI 插件）的代码，所以"面板上显示成什么样"我只能从契约推断。** 实测：本树 `src/qq_roleplay_bot/plugins/` 只有 `__init__.py`；`git ls-files | Select-String "webui|operator"` **无命中**；`main` 上不存在面板实现（AGENTS §2.3 也写明面板在 `stage4-plugins` 分支）。所以：
   - 它文末说"没有读完面板把所有键渲染成表单的那段代码（那条路径在插件侧）"——**这个限制是真的，且在这棵树上无法解除**；
   - 我核查"`applies` 标的 live/restart 是否如实"时，只能核到**核心侧的读法**（`runtime_flags` / `model_config` / `operator_config`），核不到面板的标注文案是否与之一致。这一截我**没做到**。
2. **我没有跑离线测试套件。** 原因是 §三 的测试会读写真实 `data/`（记忆库、日志），而我的任务边界是只读。我跑了 `pyflakes src tests`（实测：无输出、退出码 0），这是我能做的、零副作用的静态检查。注意：**这条核查本身没有产生 `data/logs/verify_runs.log` 凭证**（我没跑 `run_offline.py`），所以本报告里任何"测试全绿"的说法都不存在。
3. **"键数 30 vs 32"我无法回溯它成稿那一刻的树。** 它说当时同树有别的改动在进行（`git status --short` 显示 `dev_config.py`、`stage3_runtime.py` 被改）；我开工时工作树是干净的。所以我能确定的是"**在 `cb72f55` 这一版上键数 = 32**"，不能确定它当时数错的原因（是数漏了两项，还是那一版确实是 30）。**推断：更像数漏**（32 与 30 的差在 `api_provider` 与 `memory_model` 这类"夹在中间"的字符串键上，肉眼过一遍容易漏）。
4. **我没有验证"面板能不能改某个具体的数值"这类端到端行为**（面板不在这棵树上，见第 1 点）。所有关于"面板可改/不可改"的结论，都来自"该键是否在 `SETTINGS` 白名单里"这一层契约。
5. **`QQBOT_PERSONA_OFFSET_HOURS` 我只核到定义与消费点**（`dev_config.py:294-297` 定义、`stage3_runtime.py:1428` 消费），**没有实际改环境变量验证渲染结果**——那需要跑引擎，超出只读范围。所以"它是调用时现读、所以不重启也能改"这句话：**定义读法为实测，运行效果为推断**。
6. **它"找不到 / 不存在的"那两条我没有复核**（`backups/` 轮转策略、概率触发倍率）。前者按 AGENTS §五 我不该碰 `backups/`，后者需要一次全仓 AST 级别检索——我只做了文本检索（`random.random()` 在 `llm_client.py:274`，与它说的一致），**没有到"证明不存在"的强度**，所以我不对它那两条表态。

---

## 建议它怎么改（给原作者的最小修订清单）

1. **第 5 条重写**：`BATCH_SIZE` / `COOLDOWN_SECONDS` 是 `dev_config.py:269-270` 的**硬编码**，删掉 `QQBOT_BATCH_SIZE` / `QQBOT_COOLDOWN_SECONDS` 这两个不存在的变量名；该条"现状"改成"三项全部写死 ✗"；夹带地修正"一句话结论"里那句"两个环境变量 ✓（需重启）"。**这是唯一一条会让读者照着改错代码的错。**
2. **键数 30 → 32**（或改成"另行枚举，此处不写死数字"）。论断本身（零数值键）不动。
3. **补一整类：`dev_config.py:508-511` 的四条 `FOCUS_*`**（含 `stage3_main.py:1102-1106` 的装配点），它们比第 5 条更有资格当"现状"的样本。
4. **补第 2 条那两个邻居**：`stage3_main.py:121 MAX_COMPACT_BATCHES_PER_TURN = 6`、`:122 ACTIVE_TIMEOUT_SECONDS = 180.0`。
5. **修第 7 条与"不建议"第 4 条的自相矛盾**：审核超时与重写次数分开表态。
6. **"谁该能改"那一列**：要么删，要么补上现成口径（`operator_config.py:3` 那句"面板权限除了不能动代码以外权限跟你是一致的"或 `docs/ADMIN_CONSOLE.md` 的身份定义），否则 20 条各写一个"超管/管理员"都是随口给的。
7. **论断 3 的措辞收紧**：它是"照抄会踩、纪律已写、且踩过一次"（证据：`operator_config.py:205-210`、`runtime.py:320-333`），**不是"今天有一个面板键在骗人"**——它在开篇把它并列为"两条实测的结构性事实"，容易被读成后者。
