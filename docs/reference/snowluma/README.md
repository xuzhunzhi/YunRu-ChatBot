# SnowLuma 接口文档 · 本地存档

这是**第三方文档的本地存档**，不是本仓库的规范。上游是 SnowLuma 项目，不是我们维护的。
存档的目的是以后不再联网慢慢查。

- 抓取日期：**2026-10-08**
- 上游文档站：<https://snowluma.github.io/zh/docs/api>（简体中文）
- 文档站源码：<https://github.com/SnowLuma/SnowLumaDocs>，抓取时 `main` = `1a88e07be42c`
- 运行时源码：<https://github.com/SnowLuma/SnowLuma>，抓取时 `main` = `87527cb7641a`
  （该提交即 `chore(release): v1.14.22`）
- 本机安装：`C:\ProgramData\NapCatQQ Desktop\components\SnowLuma\`，
  `package.json` 与 `.installed_tag` 都是 **v1.14.22**（与上面那个上游提交同版本号）

## 抓到的是什么形式（重要）

上游文档站是 Fumadocs 站，**每个动作的页面不是独立的 Markdown 文件**，而是由
`public/openapi/snowluma.json` + `public/api/catalog.json` 在构建时渲染出来的。
该仓库里真正以 Markdown（`.mdx`）存在的文档只有站点自己的导语页。

所以这一份存档是**混合**的，每个文件属于哪一种都写在下面：

| 文件 | 形式 | 说明 |
| --- | --- | --- |
| `api-index.mdx` | **Markdown 原文**（逐字节原样） | 上游 `SnowLumaDocs@1a88e07b` 的 `content/docs/api/index.mdx`，1202 字节，与上游 blob 大小一致。带 frontmatter，末行是 `<ApiCatalog />` 组件占位 |
| `api/*.md` | 生成（非上游原文） | 由上一步抓到的 `catalog.json` 生成的表格，字段直接取自该文件，没有改写 |
| `api-index.md` | 生成 | 分类到文件的索引 |
| `local-actions.md` | 生成（来自本机） | 从本机分包静态抽取，见下 |

一句话：**导语页给了 Markdown 原文，动作表给的是上游机器可读目录的忠实渲染**。
没有把渲染后的网页正文当成"原文"存进来。

## 目录结构与索引表

12 个分类，每个分类一个文件；正文都是该分类的动作表（动作名、摘要、参数名与类型、
是否只读、官方页面链接）。

| 分类 | 上游动作数 | 本机动作数 | 文件 |
| --- | --- | --- | --- |
| 信息 | 5 | 5 | [api/信息.md](api/信息.md) |
| 消息 | 5 | 6 | [api/消息.md](api/消息.md) |
| 好友 | 3 | 3 | [api/好友.md](api/好友.md) |
| 群信息 | 6 | 6 | [api/群信息.md](api/群信息.md) |
| 群管理 | 18 | 21 | [api/群管理.md](api/群管理.md) |
| 群文件 | 12 | 12 | [api/群文件.md](api/群文件.md) |
| 请求 | 2 | 2 | [api/请求.md](api/请求.md) |
| 扩展 | 115 | 115 | [api/扩展.md](api/扩展.md) |
| 群相册 | 8 | 9 | [api/群相册.md](api/群相册.md) |
| 空间 | 9 | 9 | [api/空间.md](api/空间.md) |
| 系统表情 | 4 | 4 | [api/系统表情.md](api/系统表情.md) |
| 流式接口 | 6 | 6 | [api/流式接口.md](api/流式接口.md) |
| **合计** | **193** | **198** | 索引页：[api-index.md](api-index.md) |

其他文件：

| 文件 | 作用 |
| --- | --- |
| [api-index.mdx](api-index.mdx) | 上游导语页 Markdown 原文 |
| [api-index.md](api-index.md) | 分类索引（生成的） |
| [local-actions.md](local-actions.md) | 本机 198 个动作的总表：动作名、分类、别名、参数 |
| [extract-actions.py](extract-actions.py) | 从本机分包抽动作的脚本（只读） |
| [build-reference.py](build-reference.py) | 由抽取结果 + `catalog.json` 生成上面那些 Markdown |

## 数字对不上：189 / 193 / 194 / 198

前一轮调研写的是 **189**（`defineAction` 134 + `groupAction` 55），随包那份
`src/qq_roleplay_bot/data/snowluma_actions.json` 是 **194**，上游文档站现在是 **193**，
而本次从本机分包实际抽出来是 **198**。四个数都真实存在，来源不同：

| 数 | 是什么 | 怎么来的 |
| --- | --- | --- |
| 189 | 只数了两种声明函数 | 上一轮用正则数 `defineAction(` 与 `groupAction(`。本机还有 5 个动作是用 `defineStreamAction` 写的，**没被数进去**（`defineAction` 那 134 里还含 3 条包装函数定义） |
| 194 | 仓库里那份随包目录（快照） | `dev/src/qq_roleplay_bot/data/snowluma_actions.json`，是从更早一次上游目录同步来的 |
| 193 | 上游文档站当前目录 | `https://snowluma.github.io/api/catalog.json`（本次实测下载，332420 字节） |
| **198** | **本机分包里真实写出来的动作** | `extract-actions.py` 逐条抽出来的，见 `local-actions.md` |

三份清单的具体差集（实测，用脚本对比出来的）：

- 本机分包有、仓库快照没有的 4 个：`add_group_tag`、`delete_group_tag`、
  `get_group_tags`、`send_custom_face` —— 说明仓库那份快照是更早的版本。
- 仓库快照有、本机分包没有的：**没有**。
- 仓库快照有、上游文档站没有的 1 个：`upload_video_to_qun_album`。
- 本机分包比上游文档站多 5 个，就是上面这 4 个加上 `upload_video_to_qun_album`
  （分类分别是 群管理 x3、消息 x1、群相册 x1）。
- 上游文档站有、本机分包没有的：**没有**。

也就是说：**本机这份分包比上游 v1.14.22 的公开目录多 5 个动作**
（群资料标签 3 个、`send_custom_face`、`upload_video_to_qun_album`）。
两边 `package.json` 都写 v1.14.22、上游目录的同步提交也是 `SnowLuma@87527cb`，
所以差异不出在版本号上，更像是两种发行包内容不同（本机是 NapCatQQ Desktop
里的组件包，上游目录来自 npm/Docker 那条线）。**这一条是推断**：我没有跑通
上游源码的构建来比对，`raw.githubusercontent.com` 在本次抓取后半段开始超时。

本仓库 `dev/AGENTS.md` 写的是 189（2026-10-08 晚已按这份实测改成 198）。按本次实测，本机真实动作数是 **198**。

## 怎么刷新这一份

### 1. 官方文档（两条路都可用）

上游仓库里的 Markdown（最干净，推荐）：

```powershell
# 站点源码与导语页
curl.exe -sSL -o api-index.mdx `
  https://raw.githubusercontent.com/SnowLuma/SnowLumaDocs/main/content/docs/api/index.mdx

# 动作目录（分类 + 参数，生成 api/*.md 的输入）
curl.exe -sSL -o catalog.json https://snowluma.github.io/api/catalog.json
```

- 页面浏览入口：<https://snowluma.github.io/zh/docs/api>
- 站点源码仓库：<https://github.com/SnowLuma/SnowLumaDocs>
  （中文页面在 `content/docs/**`，动作目录在 `public/api/catalog.json`）
- 注意：文档站为**每个动作**渲染的页面不是 Markdown 文件，别去仓库里找
  `content/docs/api/message/send_private_msg.mdx`，它不存在。
- 分类在 URL 里用的是英文段（`system` / `message` / `friend` / `group-info` /
  `group-admin` / `group-file` / `request` / `extended` / `group-album` / `qzone` /
  `sys-face` / `stream`），但站点正文里的分类名是中文；`build-reference.py`
  里的 `CATEGORY_SLUG` 就是这张对照表。

礼节提醒：同一个域名别连着轰。抓一轮之间隔几秒，页面之间也留间隔。

### 2. 本机清单（从分包里抽）

只读抽取，不会写那份安装里的任何东西。仓库根是 `QQRoleplayBot\dev`，
虚拟环境在它上一层的 `QQRoleplayBot\.venv`：

```powershell
# 在仓库根（QQRoleplayBot\dev）执行
..\.venv\Scripts\python.exe docs\reference\snowluma\extract-actions.py --json local-extract.json
```

不带参数时会自动挑 `components\SnowLuma\` 下 mtime 最新的 `config-*.js`；
也可以直接把分包路径作为第一个位置参数传进去。

### 3. 重新生成 Markdown

```powershell
..\.venv\Scripts\python.exe docs\reference\snowluma\build-reference.py `
    --extract local-extract.json --catalog catalog.json `
    --out-dir docs\reference\snowluma --generated-on 2026-10-08
```

## 抽取方法（复跑过，不是纸上命令）

本机安装里 `index.mjs` 是打包产物，动作注册表被拆到 `config-<hash>.js` 里；
本机当前生效的是 `config-ClrCrmAk.js`（目录里另有 `config-CEQwQxHY.js`、
`config-CLf1PhZ6.js` 两个更旧的，`defineAction` 条数分别是 131 与 123）。

要抽的三样东西都在同一个文件里：

- 四个声明辅助函数 `defineAction` / `defineStreamAction` / `groupAction` /
  `groupUserAction`（后三个都走 `defineAction`），动作名在各自的 `name:` 属性里：
  要么是字符串，要么是数组（`[0]` 是动作名，其余是别名）；
- 每个动作的 `params: {...}` 字面量，取它的顶层键就是参数名；
- `ACTION_GROUPS` 字面量，把分类名映射到 `actions$N` 数组，按"最内层包含"定分类。

下面这条是**实际跑过的输出**（2026-10-08，在仓库根 `QQRoleplayBot\dev` 执行）：

```text
PS> ..\.venv\Scripts\python.exe docs\reference\snowluma\extract-actions.py
bundle: C:\ProgramData\NapCatQQ Desktop\components\SnowLuma\config-ClrCrmAk.js
declarations: 201
actions (name-bearing, non-hidden): 198
duplicate names: none
non-literal declarations skipped: 3
per category:
  信息: 5
  好友: 3
  扩展: 115
  流式接口: 6
  消息: 6
  空间: 9
  系统表情: 4
  群信息: 6
  群文件: 12
  群相册: 9
  群管理: 21
  请求: 2
```

`--tsv` 会打印一条动作一行（`动作名<TAB>分类<TAB>别名<TAB>参数`），实测 198 行：

```text
PS> ..\.venv\Scripts\python.exe docs\reference\snowluma\extract-actions.py --tsv
can_send_image	信息
can_send_record	信息
get_login_info	信息
get_status	信息
get_version_info	信息
...
```

几条容易踩的坑（都是这次实际踩到的，写下来省得下次重踩）：

- 数 `defineAction(` 出现次数**不等于**动作数：函数定义、`groupAction` 内部的
  调用、以及没有字面量名的包装调用都会算进去。本机 201 条声明里有 3 条是后者。
- `name` 可能是**数组**。本机有 5 个动作是这样写的：
  `fetch_ptt_text`（别名 `get_ptt_text`、`get_record_text`）、`get_rkey`（`nc_get_rkey`）、
  `ocr_image`（`.ocr_image`）、`send_packet`（`.send_packet`）、
  `set_group_sign`（`send_group_sign`）。按字符串匹配会整条漏掉。
- 带**前导点**的名字不一定是内部东西：`.get_word_slices`、`.ocr_image`、`.send_packet`
  都是真实动作或别名。真正要排除的只有 `ACTION_REGISTRY` 里的
  `.handle_quick_operation` 与 `.handle_quick_operation_async`（它们不在 `ACTION_GROUPS` 里）。
- `ACTION_GROUPS` 最后一个分类写成简写 `{ category: "系统表情", actions }`，
  没有冒号后面那个表达式；只认 `actions:` 的正则会整条漏掉这个分类的 4 个动作。
- 本机分包里**没有**完整动作目录的 JSON（`data\` 下只有 `sys-face-catalog.json`），
  所以只能从分包抽，没有更省事的路。

## 已知不足

- `api/*.md` 里的参数类型对少数动作是 `?`（`catalog.json` 的 schema 没给 `type`），
  照抄上游，没有补全。
- 本机 `local-actions.md` 的参数列**只有参数名**，没有类型、必填、默认值：
  三样在分包里都是链式调用构造的（`f.string({ allowEmpty: false })`），
  静态抽取不能保证忠实，宁缺勿编。
- 本机分包多出来的那 5 个动作，上游文档站没有页面，因此没有官方链接。
