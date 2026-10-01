# 加一个命令（插件）

**Stage 4 的内容一律做成插件**——对外功能扩展（群管理、识图、邮件、面板、戳一戳…）。
**禁止往 `stage3_main.py` 里加 `if is_xxx_command(...)`**。

> ⚠️ 别把这条读成"所有命令都得做成插件"。**用户没这么说过**（2026-10-01 纠正：
> "什么命令用插件实现，我他妈什么时候这么说过"）。原话是
> "**stage4 的内容**都用插件实现，你好像直接加到 super 的底层里去了"。
> Stage 3 自己的管理命令（`/admin` 开关群与清会话、`/super memory*` 管记忆、
> `/ping`、`/help`）**不走插件机制也不违背这条**——它们本来就是 Stage 3 的事。
> 这个文件讲的是"**当你要为 stage4 加一条命令时**该怎么做"。

这条规矩的由来：早期版本把命令判定堆在主循环里，加一条命令要动三处，改错一次就互相打架。
插件的边界是照着 `/help` 与 `ping` 划出来的，但**不要按想象预先扩大它**——
需要什么再加什么。

> 后一句在 2026-10-01 被实测修正过一次：边界照着**一个**需求划是对的（别空想），
> 但**插件接口是公共契约**，它必须覆盖插件生态的基本维度，否则每来一个新形态就得改核心。
> 实测数据见 `docs/PLUGIN_MARKET_SURVEY.md`（MaiBot 市场 386 个插件：
> 出站富内容 46 / 工具与查询 35 / 消息处理 29 / LLM 与上下文 28 / 平台适配 23 …，
> 八族里我们只对上了"群管与权限"一族）。

## 一、插件长什么样

`command_plugins.py` 里的协议。最小实现：

```python
from .command_plugins import CommandPlugin


class WeatherCommand(CommandPlugin):
    """查天气。公开命令，任何人可用。"""

    name = "weather"
    #: 档位。核心按它判定权限，插件自己**不判**。
    min_level = "public"

    def match(self, message) -> bool:
        return message.text.strip().startswith("/weather")

    async def handle(self, message) -> str | None:
        return "今天晴。"
```

三个名字是契约：`name`、`min_level`、`match()` / `handle()`。

**`min_level` 只有三档**：`"public"`（谁都能用）、`"admin"`（群里管理员）、
`"super"`（超管）。

## 二、插件能返回什么

`handle()` 的返回值就是"意图"，由**核心**决定怎么发：

| 返回 | 意思 | 谁负责发送 |
| --- | --- | --- |
| `str` | 一段纯文本 | 核心 |
| `None` | 不回复（静默） | — |
| `ImageReply(...)` | "这份内容适合画成卡片" | 核心（画与发都在底层） |
| `ActionRequest(...)` | "我想做一个动作"（踢人、改名片…） | 核心（过护栏后执行） |

## 三、插件**拿不到**什么

这是硬边界，不是风格问题：

- **拿不到 `transport`**。插件不发送任何东西，只产出内容或意图。
- **拿不到权限名单**（`admin_user_ids` / `super_admin_user_ids`）。
- **不判断身份**。`min_level` 只是声明，判定在核心。

理由：插件是外部可替换的部分。给它 `transport` 就等于给它整个出站能力；
给它名单就等于把权限判定的真相源复制一份。**权限判定、动作执行、护栏、审计一律留在核心。**

## 四、注册

在 `builtin_commands.py` 的 `build_command_registry()` 里加进去：

```python
plugins = (ping_plugin, help_plugin, balance_plugin, WeatherCommand()) + tuple(extra)
```

**顺序有意义**：越具体的排越前。`ping` 在 `help` 之前是有意的——它更具体、
行为最简单，先匹配掉可以少走一层。

## 五、后台能力（不是命令，是节拍）

有些能力不是"一条消息进来"，而是"隔一会儿干一件事"（入群审批、定时汇报）。
那是**后台插件**，形状不同：

```python
class MyPoller:
    name = "my-poller"
    interval_seconds = 60.0
    enabled = True

    async def poll_once(self) -> object:      # 不许抛出
        ...

    async def close(self) -> None:            # 可选：节拍取消时核心会调一次
        ...
```

装配点只有一个：`background_plugins.build_background_plugins()`。
节拍循环也只有一份：`run_background_plugin()`。
**加一个后台能力 = 加一个插件 + 在装配点登记**，不要往 `runtime.serve` 里再加一个
`create_task`。

后台插件要动外部世界时，走核心注入的两个窄接缝（同样拿不到 `transport`）：

- `call_action(action, params)` —— 核心先过 `capabilities` 闸门（按用途）再调对面；
- `notify(target, text)` —— 核心统一发送（走补发队列）。

## 六、加完之后

```powershell
.\.venv\Scripts\python.exe tests\run_offline.py   # 全绿
.\.venv\Scripts\python.exe -m pyflakes src tests   # 无输出
```

再补一个测试。测试怎么写见 `tests/test_command_plugins.py`——它**只实现命令用到的那几个
方法**，不共享真实现的代码。
