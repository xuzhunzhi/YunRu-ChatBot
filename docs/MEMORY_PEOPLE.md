# 记忆 ↔ 人：多个人对应（schema v4，现为 v6）

状态：**已实现**（2026-09-27；v6 加"一个人多个号"见第十节，2026-10-01）。这份文档说明
"一条记忆怎么跟一个人/几个人对应"，以及为什么有些对应故意不做。

## 一、要解决的问题（都是实测出来的）

体检真库（44 条活跃记忆）发现三件事：

1. **归属字段早就有，但用不起来。** 每条记录都带 `scope_type` / `scope_key` /
   `subject_user_id`，写入时绑定当时说话的人，隔离有测试；可是回复 prompt 里渲染出来的是
   `<memory scope="user_global" subject="900000001">`——**一个裸 QQ 号**，
   而 `<people>` 名册只列当前这段对话里出现的人。人不在场，这个号就没有意义，
   人格里那句"旧事只属于它标注的那个人"基本落不了地。
2. **提到别人时想不起他。** 检索候选池是 `_namespaces(group_id, speaker_id)`
   （说话人的 `user_group` + `user_global` + 本群 `group`），**别人的记录从不进候选**。实测：
   - 邦邦自己问"我喜欢喝什么来着" → 命中他自己的 2 条 ✓
   - 别人问"邦邦是不是喜欢喝奶茶" → **0 条**关于邦邦的 ✗
   - "尿哥是谁" / "你还记得乐乐吗" → 同样 0 条 ✗
3. **"几个人"没有位置。** 跨人事实只能硬塞进一个人名下，真库里就有：
   `9608a7fd`（"想和'尿哥'（rikka）一起喝酒"挂在 900000004 名下，rikka 只是正文文本）、
   `f1b8db1a`（780078268 的 `name` 记录里混着"朱蝶"）。更糟的是 `ce8116c7`
   （挂在 900000001 名下"被群友称作杜若汀"）与 `fd82a708`（900000005"自称杜若汀"）
   互相矛盾——而 `name` 是**每轮常驻注入**的，错的那条会一直影响她怎么叫人。

## 二、数据模型（v3 → v4，只加表、不改旧数据）

```sql
CREATE TABLE record_subjects (           -- 一条记忆关联到哪些人（含归属人本人）
    record_id TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
    PRIMARY KEY (record_id, user_id));
CREATE INDEX record_subjects_lookup ON record_subjects(group_id, user_id);

CREATE TABLE people (                    -- 本群见过的人：QQ → 最近一次显示名
    group_id TEXT NOT NULL, user_id TEXT NOT NULL,
    name TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (group_id, user_id));
```

分工必须说清楚，否则就是两个真相来源：

- **`records.subject_user_id` 管隔离**：它是这条记录的归属人（由 scope 决定），
  检索时的命名空间过滤只看它。
- **`record_subjects` 管归属与召回**：它是"这条记忆跟谁有关"的索引，
  用来（a）渲染成名字、（b）被提到的人也能取到与他有关的记录。

迁移时按 `subject_user_id` 给已有记录补一行 subject（群记录没有归属人，补 0 行）。

## 三、写入规则

维护 agent 可以在 ADD/UPDATE/MERGE 里多给一个可选字段 `subjects`：**本群名册里的 QQ 号**
（`people` 名单随批次一起给它，它只能照抄，不许编）。规则：

- 不填 = 只有归属人一个人（默认，绝大多数情况）；
- 只在**这条事实确实涉及多个人**时才填（"约好一起…""两人都在场"），最多 4 个；
- 校验在存储层做，不信模型：不认识的 id 直接丢掉（记 audit），只留归属人也要能落库；
- 个人偏好、边界**永远单人挂**——把别人的私事挂成多人条目是最坏的一种错。

**与"不许记这个系统自己"的关系**（2026-09-30 加固）：写入路径上还有一道规则闸
（`memory_filters.py`），讲实现/模型/命中率/注入测试/"谁做的她"的内容**逐条跳过、进 audit，
不让整批失败**；对话压缩摘要也按句清洗。见 `docs/MEMORY_SELF_REFERENCE.md`。

## 四、隐私规则（跨人回忆只借"本群学到的"）

被提到的人 M 的记录要能取到，但**不是所有记录都能借**。关联人行里记的是
**这条事实是在哪个群学到的**，所以规则一句话：**只借本群学到的事**。

- 允许：`record_subjects.group_id = 本群` 的记录（不管它的 scope 是 `user_group` 还是
  `user_global`）——这些是 M 在**本群**公开说过、或本群发生的公共事实；
- 不允许：在**别的群**学到的记录（同一句 `user_global` 偏好，在 A 群知道就在 A 群借，
  在 B 群不借）。理由：跨群是不同的社交场合，把他在 A 群说的话搬到 B 群，
  等于替他做了一个他没做过的公开；
- 已存在的老记录（迁移前写的）关联人 `group_id` 是空的，**一律不外借**：
  迁移时无法还原"当时是在哪个群说的"，宁可少借；
- 说话人自己的记录照旧（含跨群 `user_global`），因为那本来就是对他自己说话。

顺带说清楚：**记忆只从群聊里来**。私聊消息根本进不了 inbox
（`MemoryInbox.offer` 要求 `target.group_id` 在允许列表里），所以不存在"私聊说的事被
借到群里"这条路。

## 五、检索与排序

候选 = 说话人命名空间 ∪ 本群 group ∪ **本条消息提到的人**（@ / 引用 / 正文点名，≤2 人）
在本群范围内的相关记录。排序：

1. 命中的查询词数（`WEAK_TERMS` 里的关系词不算命中）；
2. 说话人自己的记录优先，其次被提到的人，最后群记录；
3. `confidence`、`updated_at`。

**本人的 `name`/`boundary` 常驻**（不靠词命中，≤2 条）这条不变；别人的边界不进常驻，
只在被提到且词命中时才可能出现——避免"把别人的雷区当成眼前这个人的"。

## 六、渲染

`<memory>` 增加 `about` 属性，写**名字**（查不到才退回 QQ 号）：

```
<memory scope="user_group" subject="900000004" about="小张, 尿哥">想和"尿哥"（rikka）一起喝酒…</memory>
```

名字来源优先级：当前这段对话的名册 → `people` 表（本群见过的人）→ QQ 号。
判定 agent 的最小记忆（`as_judge_note`）同样只给当前说话人的称呼与边界，保持原样。

## 七、明确不做的事

- **不做人名模糊匹配**：正文里出现"乐乐"不一定是在说那个 QQ 是 475842590 的人，
  宁可不召回，也不要认错人（认错人比想不起来更伤）。
  **唯一的例外是人自己认定的**——操作者可以手工登记"这两个号是同一个人"，
  见第十节。代码里仍然没有任何"猜"的路径。
- **不做 embedding / 语义检索**：现在数据量（44 条）和预算不支持，先把归属与召回修对。
- **不做跨群借阅**：理由见第四节。
- **不让人名参与权限判断**：昵称可以改、可以重复，权限只认 QQ 号。

## 八、人物画像（kind=profile，2026-09-29）

上面几节解决的是"**一条事实**属于谁"。画像解决的是另一层：**她对这个人的整体印象**。
用户原话是"要构建人物画像了"。

- **一条人一条**：`kind=profile`、`scope_type=user_global`、`normalized_key` 固定 `profile`、
  `durable=true`、`ttl_days=null`、`subjects=[]`。校验在 `memory_model.py`，
  写成群内画像、自造键、带关联人、或 TTL 短于 30 天，一律拒。
- **谁写**：记忆维护 agent（`memory_maintenance_agent.py` 的 prompt 里有专门一段）。
  一批里最多动 1 条，大多数批次不该动——画像要的是"印象变了才更新"。
- **怎么渲染**：说话人一开口就取一次（`MemoryStore.profile` → `MemoryService.profile_for`
  → `build_dialogue_messages(profile_note=...)`），落在易变段的
  `--- 你对这个人的印象 ---`。**跟着人走，不跟着话题走**，所以不进按词命中的检索。
- **边界**：`_candidate_records` 里显式排除 `kind='profile'`，避免画像既出现在画像块里、
  又混进"你记得的旧事"里重复占位；内容照样走 DATA 转义与写入期的安全过滤。
- **跨群**：画像与关系档位一样是**按人跨群**的（同一个人在哪个群都是同一份）。

## 九、维护请求的字段顺序也是成本（2026-09-29）

记忆维护的请求是「不变的 system 长规则 + 每次都变的 DATA」。服务商的提示词缓存只认
**从头逐字节相同**的前缀，所以 DATA 里第一个字段是 `now` 时，整段 DATA 都作废——
实测命中率 55%~63%，而"只有 system 能命中"的上限正好 ~65%。

按 981 条真实请求算过前缀匹配（`data/memory_cache_ab.py`），把顺序改成**稳定优先**：
`people`（同群 88% 的相邻调用完全相同）→ `batch` → `existing_memories` → `events` → `now`。
相邻前缀匹配中位 66% → 74%；再用真实请求做 A/B（同群相邻两次调用，只看第二次的命中），
命中率 **70.7% → 81.7%**。每次调用的缓存命中情况现在也会写进 `data/logs/memory.jsonl`
（`cache_hit` / `cache_miss`），以后可以直接看数，不用再猜。

## 十、一个人有多个号（schema v6，2026-10-01）

用户报的实例："在不在不在"（475842590）和"兔子狗可爱喵"（430523284）是同一个人。
实际情况比"合并一下"麻烦：**那个她很少用的号（475842590）反而挂着 4 条记忆**
（奶味曲奇、不认路、游戏态度、`preferred_name`），常用号（430523284）只有 1 条画像——
她换回常用的号说话，记忆就调不起来了。真库里还留着一条被删掉的
`rarely_uses_this_account`（"说这个号她很少用"），正是这条线索。

### 表

```sql
CREATE TABLE person_aliases (            -- 操作者认定的"这个号也是他"
    alias_user_id TEXT PRIMARY KEY, canonical_user_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
    CHECK (alias_user_id <> canonical_user_id));
```

### 规则

- **只有人能写**。面板 / `data/merge_person_accounts.py` 是唯一入口，代码里没有任何
  "昵称一样就合并"的路径——第七节那条底线不动。
- **读和写都按规范号**：`MemoryStore.canonical_user()` 是唯一入口，
  `_namespaces()` 把三个命名空间全按规范号算，`claim()` 也用规范号建批次
  （否则维护 agent 会把一个人当两个人整理两遍）。
  `relationship*` / `apply_affinity` / `profile_record` 同样按规范号。
- **合并整体搬运，不动正文**：记录、墓碑、`record_subjects`、`relationships`、
  `relationship_log`、`archive`、`people`、`leases`、`participants` 一起搬。
  两个号对同一件事说法矛盾时（真例：一个说"别叫乐乐了"、另一个的画像写着"自称乐乐"），
  那是**内容问题**，脚本不替她编，只把两边都摆给操作者看。
- **撞键拒绝合并**：同一把键（scope + normalized_key）两边都活着的话直接拒绝、整批回滚，
  不猜谁对谁错。
- **墓碑必须跟着搬**：不然"删掉的键"在规范号下就不算删过，旧证据能把它写回来
  （`test_tombstone_follows_the_merge` 锁这件事）。
- **名册仍按真实登录号记**：`note_person` 存的是**发消息那个号**的显示名
  （口径见下一节：QQ 昵称优先），不换成规范号——"这个号叫什么"是号的事实。
  合并时会删掉旧号那一行，此后新消息按规范号记
  （她的规范号本来就在名册里，不会因此少一个人）。
- 管理视图按 `subject_user_id` 筛，合并后拿旧号查不到东西是**对的**（记录确实都搬走了），
  核对该用规范号。`MemoryStore.retrieve()` 两边都取得到同一份——那才是"记忆调得起来"。

### 显示哪个名字：一律用 QQ 昵称（2026-10-01）

用户原话："群昵称和qq昵称你总的选一个来显示吧，一些用qq昵称一些用群昵称"，
追问后定的口径是"**应当用qq昵称，多群一个人多个群名片怎么用群昵称啊**"。

改之前是**两套混着用**，同一个人的名字会跳：

| 路径 | 改前取的字段 |
| --- | --- |
| 实时群消息（`onebot_ws.parse_message_event`） | `card`（群名片）→ 没有才 `nickname` |
| 成员名单索引（`conversation_context._index_members`） | `card` → `nickname`（同上） |
| 历史消息（`conversation_context._history_message`） | **只读 `nickname`** |

真机实测（群 800000001，2026-10-01）：**59 人里 42 人的名片与昵称不同**，而且名片
随时会变——同一个人隔几条消息就从"桓珩"变成"洹桁"。不同群的名片也不一样
（475842590 在群里叫"在不在不在"，QQ 昵称其实是"宋秋湙"）。
拿名片当身份，跨群就没有稳定答案。

现在只有一处口径：`transport.display_name_from_sender(sender)`
—— **`nickname` 优先，缺失才退回 `card`**。显示、`people.name`、
喂给模型的说话人名全走它。

**群名片没有被丢掉**，以下场景照旧用它（那是它的正当用途）：

- `set_group_card` / 改群名片命令（`group_owner.py`、builtin 命令表）；
- 按名字找人的匹配：`stage3_main` 的 `/admin relay` 同时拿
  `nickname` / `remark` / `card` 去比，**任何名字能对上就找到人**。

名册里老代码写进去的群名片用 `data/fix_people_names.py` 一次性刷成昵称
（实测 89 行里刷了 46 行；刷完与线上口径 0 不一致）。

### 画像

画像（`kind=profile`）是**每轮常驻注入**的，所以它跟"称呼"打架时最伤人。
`merge_alias(..., drop_profile=True)` 会走归档（`delete_reason='operator_merge'`）清掉
规范号名下那条画像，让维护 agent 按新证据重写；`purge_records` 在 `memories_short`
和 `memory_long` 两张表上按 `scope+key` 一起清，不会漏。
