# 对照 MaiBot 插件生态，审我们自己的插件接口

来源：[maibot-plugin-sdk 2.9.0](https://pypi.org/project/maibot-plugin-sdk/)（README 全文我拉下来了：
`data/tmp_mai_sdk_readme.txt`）、[MaiBot 插件仓库](https://github.com/Mai-with-u/plugin-repo)、
[插件生命周期 API 文档](https://docs.mai-mai.org/develop/webui-api/plugin-lifecycle-api)。

> ⚠️ 网络说明：这台机器上 `web_fetch` 工具 DNS 解析失败（`ipv4only.arpa`），
> `raw.githubusercontent.com` 直连超时；SDK 文档是从 pypi.org 的 JSON API 取到的
> （pypi 可达）。**`plugins.json`（插件市场清单）我没拿到**，所以"市场里具体有哪些插件"
> 我只有 SDK 能力面与二手检索，**没有实测清单**。下面凡涉及结论的地方都标了依据。

---

## 一、MaiBot 的插件能声明什么（实测自 SDK README）

**组件声明 7 种**（`Action` 是兼容入口，实际是 Tool）：

| 组件 | 作用 | 我们的接口有吗 |
| --- | --- | --- |
| `@Command` | 命令匹配 | ✅ `command()` |
| `@Tool` / `@Action` | **给 LLM 一个可调用工具**（带参数 schema） | ❌ **没有** |
| `@EventHandler` | 事件订阅（入群/退群/戳一戳…） | ❌ 没有（只能轮询） |
| `@HookHandler` | 命名 Hook 点 + `mode` + `order` | ❌ 没有 |
| `@MessageGateway` | 平台接入（收发双向）、注入入站消息 | ❌ 没有 |
| `@LLMProvider` | 注册新的模型供应商 `client_type` | ❌ 没有 |
| `config_model` | 强类型配置 + WebUI Schema | ⚠️ 我们有 `operator_config`，但**插件没有自己的配置模型** |

**能力代理 17 个**（`ctx.*`）：

| 能力 | 说明 | 我们有对应吗 |
| --- | --- | --- |
| `ctx.send` | 发文本/图片/表情/转发/混合消息 | ⚠️ 只有 `ImageReply`（画卡片）+ `ActionRequest`，**不能发"文本+表情"** |
| `ctx.llm` | 让插件**自己**调模型 | ❌ 没有 |
| `ctx.api` | 插件之间互相暴露 API（含动态注册） | ❌ 没有 |
| `ctx.db` | 数据库增删改查 | ❌ 没有 |
| `ctx.config` | 插件自己的配置 | ❌ 没有 |
| `ctx.emoji` | 表情包管理 | ❌ 没有 |
| `ctx.message` | 历史消息查询 | ❌ 没有 |
| `ctx.chat` | 聊天流查询/打开/创建 | ❌ 没有 |
| `ctx.person` | 用户信息查询 | ❌ 没有 |
| `ctx.render` | **HTML → PNG** | ❌ 没有（我们只有 `help_card` 那条私路） |
| `ctx.knowledge` | 知识库搜索 | ⚠️ 有 `KnowledgeIndex`（`_host` 里，**没接线**） |
| `ctx.frequency` | 发言频率控制 | ❌ 没有 |
| `ctx.component` | 加载/重载其它插件 | ❌ 没有 |
| `ctx.statistics` | 本机统计与计费 | ⚠️ 有 `MachineProbe`/`api_usage`（**没接给插件**） |
| `ctx.tool` | 查询 LLM 工具定义 | ❌ 没有 |
| `ctx.maisaka` | **上下文追加** + 主动任务 | ⚠️ 对应我们的 `PromptPlugin`（**没接线**）+ ❌ 主动任务 |
| `ctx.paths` | 插件自己的持久化/运行时目录 | ❌ 没有 |
| `ctx.logger` | 日志 | ✅ 插件可以直接用 `logging` |

---

## 二、结论：我们的插件接口覆盖面约 1/3，而且缺的正好是"市场里最热的那几类"

把上面的 ❌ 归成六类**真缺口**（不是"没来得及加"，是**接口不存在**）：

| # | 缺口 | 典型插件（照生态推） | 为什么现在做不到 |
| --- | --- | --- | --- |
| 1 | **LLM 工具（`@Tool`）** | 查天气、算数、查资料、查快递、开灯 | 插件无法给模型一个"可调用函数"；我们只有命令（人触发）与意图（写死的动作通道） |
| 2 | **事件通道** | 欢迎新人、退群告别、被戳回应、撤回提示 | 引擎只处理"有人发消息"，其它一律得轮询 |
| 3 | **消息段 / 富内容** | 发表情、发图、发语音、合并转发、Ark | `OutgoingMessage` 只有 `text`；`ctx.send` 那种"混合消息"完全没有 |
| 4 | **插件自己的数据与配置** | 签到记录、抽签历史、点歌队列、每个群单独设置 | 插件没有 `paths`（自己的目录）也没有 `config_model` |
| 5 | **插件之间互相调用** | 一个插件复用另一个插件的查询（如"某人好感度"） | 没有 `ctx.api` 那种插件间公开接口 |
| 6 | **prompt 注入接上**（你提的恋人插件） | 恋人、剧情、关系 | **接口已存在（`PromptPlugin`）但没人能登记，判定 agent 也看不到** |

另有三个"我们其实有、但没接给插件"的：`KnowledgeIndex`、`MachineProbe`、`PromptPlugin`——
它们是 `_host` 里那些**空转的 Protocol**（审查报告 §3 末也指出了：7 个口子引擎只用了 1 个）。

---

## 三、一个必须**不照抄**的地方（MaiBot 给了插件 `ctx.send`，我们**不能**给）

MaiBot 的插件直接 `await self.ctx.send.text(...)` 自己发消息。**我们的硬约束明确禁止**：

> **命令插件不发送消息、不判断身份。** 插件只产出"内容或意图"，由核心统一发送与执行；
> 权限由核心判定。插件拿不到 `transport`。（`AGENTS.md` 2.1）

这条不是教条，是因为 **fail-closed 与审计必须在核心**：插件自己发消息，就能绕过权限判定、
绕过 `capabilities` 闸门、绕过审计。所以照搬 MaiBot 会直接破坏我们的安全边界。

**正确的对应关系**是：MaiBot 的"能力代理"里，**只读、查询类**（`message` / `person` /
`chat` / `statistics` / `knowledge`）可以给；**出站类**（`send`）与**提权类**（`component` /
`api` 的写操作）必须改成"**声明意图、核心执行**"——也就是我们已经有的
`ActionRequest` / `ImageReply` 那条路，只是要把它**扩到能表达富内容**。

---

## 四、这件事对我们"stage4 全插件化"的最终判断

之前我实测的 4 个缺口（出站表现 / 进站预处理 / 富内容 / 事件），加上你提的 prompt 注入，
对照 MaiBot 生态之后应该改写成**六条**（上面第二节）。

**其中第 1 条（LLM 工具）是最根本的一条，我之前完全没想到**：它决定了插件能不能
"给模型一个新本事"，而不是只做"人发命令 → 机器人回一句"。市场上绝大多数实用插件
（天气、查询、计算、提醒）都是这一形态。

**所以现在的答案是**：按现有接口，**stage4 做不到全插件化**，而且缺的不是细节——
缺的是"插件能做什么"的**三四个基本维度**（工具、事件、富内容、插件自有数据）。
补完之后，你那句"无缝迁移非聊天功能到更复杂的思维链路"才成立：思维链路只需要实现
**收材料（`PromptPlugin`）+ 发能力（`PluginRegistry`）+ 执行意图（`ActionRequest`）** 三件事。
