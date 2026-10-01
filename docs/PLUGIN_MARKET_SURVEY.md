# MaiBot 插件市场实测：386 个插件需要什么能力

> 数据来源与获取方式（可复核）：
> - 清单：`https://cdn.jsdelivr.net/gh/Mai-with-u/plugin-repo@main/plugins.json`（48KB，386 条，字段只有 `id` + `repositoryUrl`）
> - 落盘：`data/tmp_net_jsdelivr-plugins.txt`（备份 `data/tmp_net_ghproxy-plugins.txt`）
> - SDK 能力面：[maibot-plugin-sdk 2.9.0](https://pypi.org/project/maibot-plugin-sdk/) README（`data/tmp_mai_sdk_readme.txt`）
> - **网络备注**：`web_fetch` 工具 DNS 失败、`raw.githubusercontent.com` 直连超时；
>   上面走的是 jsdelivr / ghproxy / api.github.com / pypi.org 四个可达源。
> - **诚实边界**：清单里**没有描述字段**，所以下面的"功能族"是我按 `id` 里的关键词分的
>   （一个插件可能落多族），**不是插件自报的类别**，也没有下载量/热度数据。

---

## 一、386 个插件按功能族分布（实测）

| 数量 | 功能族 | 例子 |
| --- | --- | --- |
| **46** | 出站富内容（图 / 表情 / 语音 / TTS / 视频） | `maibot-send-image-plugin-sf`、`tts_voice_plugin`、`maibot-doubao_pic_plugin` |
| **35** | 工具与查询（搜索 / 天气 / 状态 / 解析 / 爬取） | `maibot-doubaosearch-plugin`、`bingsearch`、`internetsearchplugin` |
| **29** | 消息处理（过滤 / 守卫 / 路由 / 撤回 / 反应 / 复读 / 沉默） | `silent_mode_plugin`、`repeat_plugin`、`maiplug_message_react` |
| **28** | LLM 与上下文（摘要 / 上下文清空 / 知识 / 记忆 / prompt） | `url_summary_plugin`、`context_clear_plugin`、`chat_summary_plugin` |
| **23** | **平台适配（adapter / gateway / 协议桥）** | `maibot-discord-adapter`、`napcat-adapter`、`maibot_mcpbridgeplugin` |
| **21** | 群管与权限（禁言 / 签到 / 管理 / 统计 / 监控） | `mute-plugin`、`balance_plugin`、`maibot-screen-monitor-plugin` |
| **16** | 定时与主动（提醒 / 日程 / 定时问候 / 宵禁） | `maibot-curfew-plugin`、`timed_greeting_plugin`、`maibot-reminder` |
| **14** | 游戏与娱乐（抽签 / 轮盘 / UNO / 恋人 / 画图） | `maibot-tarots-plugin`、`partner-game`、`gemini_drawer` |

功能词频前 20（去掉 plugin/maibot 这类噪声）：
`adapter` 20、`video` 10、`search` 10、`group` 10、`message` 8、`image` 7、`tts` 7、
`summary` 7、`voice` 6、`poke` 6、`bilibili` 6、`guard` 6、`model` 6、`balance` 5、
`filter` 5、`reply` 5、`react` 4、`recall` 4、`context` 4、`proactive` 3

---

## 二、照这个市场，我们缺哪些接口（这才是要看的）

| 市场里最热的功能族 | 需要的接口 | 我们有吗 |
| --- | --- | --- |
| 出站富内容（46 个，**最大一族**） | 插件能声明"发这个图/表情/语音/视频" | ❌ `OutgoingMessage` 只有 `text`；`ImageReply` 只能画卡片 |
| 工具与查询（35 个） | **插件给 LLM 一个可调用工具**（天气、搜索、算数） | ❌ 没有（`@Tool`） |
| 消息处理（29 个） | **进站拦截/改写/吞掉**（沉默、复读、路由） | ❌ 没有进站钩子 |
| LLM 与上下文（28 个） | 往 prompt 插材料 + **清空/压缩上下文** | ⚠️ `PromptPlugin` 存在但**没人能登记** |
| **平台适配（23 个）** | **插件充当传输层**（接别的平台） | ❌ **完全没想过**——`transport` 是写死的构造参数 |
| 群管与权限（21 个） | 声明意图 + 权限由核心判 | ✅ 有（`ActionRequest` + `min_level`） |
| 定时与主动（16 个） | 定时任务 + **主动发起**（不等有人说话） | ⚠️ 后台节拍有；"主动找话题"没有 |
| 游戏与娱乐（14 个） | 插件自己的**数据存储** + 随机 | ❌ 没有 `ctx.paths` / `ctx.db` |

---

## 三、对照别家：插件模型的基本维度（检索所得，非实测代码）

| 生态 | 插件的核心抽象 | 值得偷的一点 |
| --- | --- | --- |
| **MaiBot** | 7 种组件声明（Command / Tool / EventHandler / HookHandler / MessageGateway / LLMProvider / config_model）+ 17 个能力代理 | **`@Tool`（给 LLM 用）** 与 **`@MessageGateway`（插件当传输层）** 这两个维度我们完全没有 |
| **[NoneBot2](https://pypi.org/project/nonebot2/)** | 适配器（Adapter）+ 事件（Event）+ **Matcher（匹配器）**；核心"不实现具体平台，只负责和适配器通信并处理事件" | **适配器是一等公民**：平台接入不是核心，是插件。这正是市场里那 23 个 adapter |
| **[maubot](https://pypi.org/project/maubot/)** | 事件处理器 + 命令 + Web 面板 + 定时；插件有独立清单与[插件商店](https://plugins.mau.bot/) | 插件自带 **Web 面板**（我们有，但是写死的一个面板，不是插件各自能加页） |
| **koishi** | 服务（Service）+ 中间件（Middleware）+ 指令 | **中间件**：插件能包在消息处理链上，和"消息处理 29 个"那一族对应 |

三个生态的共同点，我们一条都不完整：**（1）插件能当传输层；（2）插件能给 LLM 工具；
（3）插件能挂进消息处理链（中间件/钩子）。**

---

## 四、修正我以前给的结论

我之前（`docs/PLUGIN_GAP_VS_MAIBOT.md`）说缺口是 6 条。**照真实市场，应该改成 8 条**，
而且补上两条我原来**完全没想到**的：

1. **插件当传输层**（`@MessageGateway` / NoneBot 的 adapter）——市场第 5 大类，23 个插件。
   我们连想都没想过：`transport` 是 `runtime.serve(transport)` 写死传进去的。
2. **插件给 LLM 工具**（`@Tool`）——35 个查询类插件全靠它。

加上原来的：出站富内容、进站钩子、事件、prompt 注入、插件自有存储、
主动发起（Proactive）、上下文操作（清空/压缩）。

**这 8 条里，只有"群管与权限"我们做对了**（`ActionRequest` + `min_level` + 核心执行），
而它恰好是我们唯一写过的插件族——**说明我们当时是照着"群管理"这一个需求划的接口**，
不是照插件生态划的。这与 `AGENTS.md` 里那句自省一致：

> "接口要从第一个真实需求长出来。已经吃过这个教训：`CommandPlugin` 的边界是照着
> `/help` 和 `ping` 划的，足够用；但如果当时就按想象把'富内容、事件、权限'全塞进去，
> 现在得清理。"

**这句话现在要反过来读了**：边界照着一个需求划是**对的**（别空想），但**插件接口是
公共契约，它必须覆盖插件生态的基本维度**，否则每来一个新形态就得改核心——
而"改核心"正是用户说的"无缝迁移"要避免的事。

---

## 五、给用户的直接结论

**按现有接口，stage4 做不到全插件化。** 而且缺的不是细节，是**四个基本维度**：

| 维度 | 市场占比（实测关键词） | 我们 |
| --- | --- | --- |
| 出站富内容（图/表情/语音/视频） | 46 / 386 | ❌ |
| 给 LLM 工具 | 35 / 386 | ❌ |
| 进站拦截改写（中间件/钩子） | 29 / 386 | ❌ |
| 插件当传输层 | 23 / 386 | ❌ |
| 插件自有存储 | 多个族都依赖 | ❌ |
| 主动发起（不等有人说话） | 16 / 386 | ⚠️ |

**建议的顺序**（按"影响多少插件 × 改动大小"排）：

1. **出站富内容**（最大族，且 `OutgoingMessage` 加消息段是局部改动）
2. **进站钩子**（一次改动同时解决"识图"与"26 个消息处理插件"）
3. **给 LLM 工具**（新维度，但要先定"工具怎么暴露给模型"）
4. **prompt 注入接线**（接口已存在，只差登记 + 判定那一路）
5. **插件自有存储**（`paths`，小改动、多族受益）
6. **插件当传输层**（最大改动，但能顺手把 NapCat 适配也变成插件）
