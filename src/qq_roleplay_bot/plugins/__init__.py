"""Stage 4 插件：**一个功能一个文件夹**。

由来（2026-10-01 用户）："每个插件一个文件夹。比如群管理功能算一个文件夹，
识图算一个文件夹。"

```
plugins/
├── __init__.py          发现与装配（只认下面的契约，不认识任何具体功能）
├── group_admin/         群管理：踢 / 禁言 / 撤回 / 头衔 / 公告 / 改名片
├── join_approval/       入群审批：白名单 → 黑名单 → 正则 → 模型兜底
├── vision/              识图：把图片消息换成一句描述
├── webui/               控制面板：本地与远程两套接入 + 前端资源
└── mail/                邮件通道：读信回信 + 每日汇报
```

## 契约

每个插件目录里必须有 `plugin.py`，导出：

    def register(registry) -> None:
        \"\"\"把这个功能接上。\"\"\"

可以导出 `ENABLED = False` 表示"装在这里但先别启用"。

`registry` 携带核心注入的**窄接缝对象**，并提供登记方法：

    registry.chat                     # ChatSeams：reserved() / allow() / run() / follow_ups()
    registry.report                   # ReportSeams：写日报要的五项
    registry.ui                       # UiSeams：控制面板要的十项
    registry.call_action / notify     # 动作与通知（后台插件用）
    registry.roles / loop             # 前置插件放上来的共享能力 / 事件循环
    registry.shared_roles()           # 取前置插件放上来的角色查询
    registry.command(plugin)          # 一条命令插件
    registry.background(plugin)       # 一条后台节拍（有 name/interval_seconds/poll_once）
    registry.provide_roles(cache)     # 前置插件：把共享能力放上来
    registry.provide_prompts(plugin)  # prompt 扩展（恋人/剧情那类）

**这里没有 `engine`**（2026-10-01 改）：引擎上有 `transport` 与三份权限名单，
给出去就等于插件能自己发消息、能读名单。要什么能力就由接缝一个一个列。

**接缝的限度要认清**（审查 2026-10-01）：进程内的插件隔离**不是安全边界，只是约定**。
闭合函数与绑定方法都能被 `__self__` / `__closure__` 绕过，所以核心侧用
`runtime._SeamBinder`（闭包只捕获不透明令牌）把"顺手拿"堵掉；但能执行 Python 的插件
总有别的办法（`import` 核心模块、`gc`）。**真正的边界是三件核心内的事**：
身份由核心盖章、渠道注入的话拒特权命令、动作执行与权限判定在核心。

## 为什么这样切

- **可单独拿走**：不想要识图，删掉 `vision/` 就行；核心对它的引用都是"没有就降级"。
- **依赖方向清楚**：插件 import 核心；核心**不 import 具体插件**，只调 `discover()`。
- **`roles/` 也是插件**（2026-10-01 用户）："一个东西搬进插件另一个插件失效不代表
  前者不能作为插件，只需要把前者作为前置插件就行。" 群管理与入群审批都要它，
  所以它是**前置插件**：`group_admin` / `join_approval` 声明 `REQUIRES = ("roles",)`。
  "B 依赖 A"从来不是 A 该进核心的理由——那样换思维链路时 A 会跟着核心一起被换掉。
"""
from __future__ import annotations

import importlib
import logging
import pathlib
import pkgutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ActionDenied(Exception):
    """这个动作**没有授权**（`capabilities` 闸门拒了）。

    为什么单独定义在接缝模块里、而不是让插件去 import 核心的 `CapabilityDenied`：
    插件不该认识 `capabilities.py`。核心侧把闸门的拒绝**翻译**成这个类型
    （`runtime._SeamBinder.call_action`），插件只认它。
    """


def _no_reserved() -> frozenset[str]:
    return frozenset()


@dataclass(frozen=True, slots=True)
class DeliveredMessage:
    """插件交给对话流程的**原话**。**插件指定不了身份，只能指定"渠道 + 发件人 + 正文"。**

    为什么不让插件交一整条 `IncomingMessage`（2026-10-01 审查抓到的一条真实越权）：

    `IncomingMessage` 里有 `user_id` 与 `sender_role`。插件自己造一条、把 `user_id`
    填成超管的号，核心就会**按超管处理这条消息**——审查者实测用它执行了
    `/super addadmin @X`，真的给一个普通号授了群管理员。
    复用现成的 `IncomingMessage` 就等于把"以任意身份说话"这个原语发给每个插件。

    所以：插件只说它知道的事（**这是哪个渠道来的、发件人是谁、他说了什么**），
    `message_id` / `session_id` / `sender_role` / `target` 由核心按**渠道**统一决定。
    """

    #: 逻辑渠道名（`"mail"`）。核心用它决定会话前缀与"能不能带主人权限"。
    channel: str
    #: 发件人在**这个渠道上**的标识。邮件是地址、别的通道是自己那套 id。
    sender: str
    #: 正文原话。
    text: str
    #: **这条消息在渠道内的唯一标识**（邮件是它的 message_id）。
    #:
    #: 为什么必须有这个字段（2026-10-01 审查抓到的功能回归）：核心原来用
    #: `f"{channel}:{namespace}:{sender}"` 当 `message_id`，那是**每个发件人一个常量**。
    #: 而引擎对**普通对话**按 `message_id` 去重（`stage3_main` 的去重器）——
    #: 于是同一个地址的**第一封之后，后续来信全被静默丢掉**，而通道把
    #: `reply is None` 读成"她自己决定不回"，把那封标记成已处理、不再重试。
    #: 所以渠道**必须**给出每封自己的 id；核心把它拼进最终的 message_id。
    message_id: str = ""
    #: 会话的独立命名空间（邮件用 `"mail"`）：渠道之间的上下文不串台。
    session_namespace: str = ""
    #: 给模型看的显示名（"主人" / 发件地址）。**只是显示，不影响权限判定。**
    sender_name: str = ""
    #: 渠道**自己声明**"这条来自主人"。核心**可以忽略**它——
    #: 主人权限的判定条件写在核心那一侧的 `_chat_seams_for` 里（见 `allow_privileged`）。
    claims_owner: bool = False


@dataclass(frozen=True, slots=True)
class ChatSeams:
    """**窄接缝**：插件要用到"对话那一侧"的全部东西，就这四个函数。

    为什么不是把引擎给插件（2026-10-01 修掉的一个真实越界）：

    `PluginRegistry` 原来持有 `engine`，而引擎上有 `transport`（活的传输层）、
    `super_admin_user_ids`、`admin_user_ids`、`capabilities`——**插件顺着注册表
    就能拿到权限名单并自己发消息**。`AGENTS.md` 与本文档都写着"插件拿不到
    transport、拿不到权限名单"，那两句话当时已经是假的，而 `mail` 插件**正在用**：
    它直读超管/管理员名单，并直接调 `engine.handle()`。

    现在改成：**接缝是函数，不是对象**。面板那边早就是这么做的
    （`webui/wire.py` 的 `_panel_seams` 全是闭包），这里推广到所有插件。

    四项各对应什么：

    - `reserved_user_ids()`：读"不该被冒充的号"（超管 + 配置里的管理员）。
      **只读**，且只返回集合本身——插件拿不到引擎，也就改不了名单。
    - `allow_private(user_id)`：把某个号放进私聊白名单。邮件通道收信后要放行发件人，
      这是**通道自己的策略**，但名单在引擎手里，所以走这个口子。
    - `deliver(parcel)`：把一条**进来的话**交给对话流程，返回它的回复（可能是 None）。
      收的是 `DeliveredMessage`（**不是 `IncomingMessage`**）：身份由核心盖章，
      插件伪造不了发送者，也就走不到特权那条路上。见 `run()`。
    - `take_follow_ups(session_id)`：取走某个会话的续发段（长回复被拆成几条时）。

    四项**都可缺省**：缺省时读名单返回空集、放行与投递是空操作、续发段是空的。
    于是"没有对话能力"的插件既不会崩，也拿不到任何权限信息。

    ## 一条必须说清的限度（审查 2026-10-01）

    接缝把"引擎可达性"降下来了（绑定方法与闭包都会被 `__self__` / `__closure__` 绕过，
    所以真正的防线不在这里）。**进程内的插件隔离不是安全边界，只是约定**：
    同一个进程里，恶意插件总有办法（`gc.get_objects()`、导入核心模块、读文件）。
    能真正挡住的只有三件事，这三件都在核心：

    1. **身份由核心盖章**（`DeliveredMessage` → `IncomingMessage`）；
    2. **特权命令在核心被拒**（渠道注入的话永远拿不到 `/super` / `/admin`）；
    3. **动作执行与权限判定在核心**（插件只产出意图）。
    """

    reserved_user_ids: object = _no_reserved
    allow_private: object = None
    deliver: object = None
    take_follow_ups: object = None
    #: **插件 → 核心**的两个回填口（`provide_roles` / `register_reporter` 用）。
    #: 它们不是"给插件的能力"，而是"插件把东西放回核心"的通道；
    #: 同样只给一个函数，不给引擎。
    roles_sink: object = None
    reporter_sink: object = None
    #: 哪些渠道**允许**声明"这条来自主人"。白名单，不是黑名单——
    #: 没列进来的渠道一律按**普通发件人**处理。这是核心的判定，插件改不了它。
    owner_channels: tuple[str, ...] = ()

    def reserved(self) -> frozenset[str]:
        """当前"不该被冒充"的号；取不到就返回空集。"""

        getter = self.reserved_user_ids
        if not callable(getter):
            return frozenset()
        try:
            return frozenset(str(item) for item in getter())
        except Exception:  # noqa: BLE001 - 读不到名单不该让插件崩，按空处理
            logger.warning("plugin_reserved_ids_failed", exc_info=True)
            return frozenset()

    def knows_reserved(self) -> bool:
        """**接缝接上了吗**——用来区分"没有名单"与"没接上"这两件事。

        为什么需要它（2026-10-01 审查点的 fail-open）：`reserved()` 的缺省是空集，
        而"空集"有两种完全不同的成因：

        - **接缝没接上**（有人在测试或别处手造 `ChatSeams()`）：这时读不到名单，
          任何依赖它的护栏都会**静默失效**——必须让调用方看得出来；
        - **这台机器真的没配管理员**（`super_admin_user_ids` 为空）：
          那是合法的部署状态，护栏本来就没什么要挡的，功能该照常。

        只看 `reserved()` 是不是空集**分不出这两者**，于是要么 fail-open（护栏消失），
        要么误杀（没配管理员的机器上邮件绑定直接不工作）。
        这个方法给的是"来源在不在"：缺省那个 `_no_reserved` 占位符 = 没接上。
        """

        return self.reserved_user_ids is not _no_reserved

    async def allow(self, user_id: str) -> None:
        """放行一个私聊号（缺省是空操作）。"""

        if callable(self.allow_private):
            await _maybe_await(self.allow_private(user_id))

    async def run(self, parcel: DeliveredMessage) -> object:
        """把一条进来的话交给对话流程，返回回复（缺省返回 None = 不回）。

        `parcel` 必须是 `DeliveredMessage`——**传 `IncomingMessage` 一律拒绝**。
        这条检查刻意做在**接缝里**而不是靠文档：插件绕过它就得先改核心代码。
        """

        if not callable(self.deliver):
            return None
        if not isinstance(parcel, DeliveredMessage):
            # 有人把整条 IncomingMessage 塞进来了（旧写法）。**拒绝投递**：
            # 放它过去就等于让插件自己指定 user_id 与 sender_role。
            logger.error("plugin_deliver_refused reason=not-a-parcel type=%s",
                         type(parcel).__name__)
            return None
        return await _maybe_await(self.deliver(parcel))

    def follow_ups(self, session_id: str) -> list[object]:
        """取走续发段（缺省是空列表）。"""

        if not callable(self.take_follow_ups):
            return []
        try:
            return list(self.take_follow_ups(session_id) or [])
        except Exception:  # noqa: BLE001
            logger.warning("plugin_follow_ups_failed", exc_info=True)
            return []


async def _maybe_await(value: object) -> object:
    """接缝可能是同步函数也可能是协程函数，这里统一。"""

    if hasattr(value, "__await__"):
        return await value  # type: ignore[misc]
    return value


@dataclass(frozen=True, slots=True)
class ReportSeams:
    """写日报 / 写信要的东西。**实测出来的五项**，一项不多给。

    `plugins/mail/daily_report.py` 用引擎的地方只有这些（量出来的，不是估的）：

    | 它要的 | 用途 |
    | --- | --- |
    | `snapshot()` | 数一数今天处理了多少消息、回了多少条 |
    | `client` | 生成那封信的模型调用 |
    | `log_model_io(...)` | 把这次模型 I/O 记进按功能分的日志（`data/logs/`） |
    | `note_letter(letter)` | 发完记一笔"她写过这封信" |
    | `memory_service` | 信里要提到最近的记忆 |

    **五项里没有一样是权限名单**。所以这里给的是五个具体东西，而不是引擎——
    插件拿不到 `transport`，也就发不出绕过闸门的消息。

    全部可缺省：缺省时快照为空、没有 client（写信判为不可用）、其余是空操作。
    """

    snapshot: object = None
    client: object = None
    log_model_io: object = None
    note_letter: object = None
    memory_service: object = None
    #: `() -> client`：**写信 agent 自己那把模型通道**。
    #:
    #: 为什么要单独有一个（2026-09-30 用户要求"写信 agent 用独立的 client 与 user_id"）：
    #: 以前写信借用回复 agent 的 client，信与群聊共用一份缓存隔离空间。
    #: 它是**工厂**不是 client 本身——装配发生在写第一封信之前很远的地方，
    #: 而 client 要现造（每个 agent 有自己的 `user_id`）。
    #:
    #: 以前 `mail/wire.py` 是 `from ...runtime import _build_letter_client`——
    #: 那是核心的私有函数，插件 import 它等于把"核心必须叫这个名字"写进插件，
    #: 换一套思维链路时那个名字未必还在（2026-10-01 改成接缝）。
    letter_client: object = None

    def snap(self) -> object | None:
        """取一次运行快照；没有就返回 None。

        **只接受 `snapshot` 这个字段是可调用的**。这里原来写了一段"兼容引擎形状"
        的兜底（`snapshot` 不是函数、但有个 `.snapshot()` 方法时就去点它）——
        审查 2026-10-01 指出那正好说明"接缝在结构上不构成闸门"：它会让
        `ReportSeams(snapshot=engine)` 也能工作。既然接缝的形状定死了，
        就不再猜别的形状——传错了就取不到快照（`None`），而不是悄悄接受一个引擎。
        """

        getter = self.snapshot
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 - 快照失败不该让日报炸掉
            logger.warning("plugin_report_snapshot_failed", exc_info=True)
            return None

    def log_io(self, feature: str, request: object, raw: object,
               *, session_id: str = "", trigger: str = "") -> None:
        """记一次模型 I/O；没有接缝就是空操作。

        `log_model_io` 由装配点填成"调核心的 `_log_model_io`"，
        所以这里只管调，不管实现叫什么名字。
        """

        if not callable(self.log_model_io):
            return
        try:
            self.log_model_io(feature, request, raw, session_id=session_id, trigger=trigger)
        except TypeError:
            # 更窄的实现可能只吃位置参数；退一步再试一次。
            try:
                self.log_model_io(feature, request, raw)
            except Exception:  # noqa: BLE001
                logger.warning("plugin_report_log_failed", exc_info=True)
        except Exception:  # noqa: BLE001 - 记日志失败不影响写信
            logger.warning("plugin_report_log_failed", exc_info=True)

    def remember_letter(self, letter: object) -> None:
        """发完记一笔；没有接缝就是空操作。"""

        if not callable(self.note_letter):
            return
        try:
            self.note_letter(letter)
        except Exception:  # noqa: BLE001
            logger.warning("plugin_report_note_failed", exc_info=True)


@dataclass(frozen=True, slots=True)
class UiSeams:
    """控制面板要的东西。**列在这里的每一样都是函数或只读取值闭包**，没有引擎。

    面板是这个项目里**权限最大**的插件（"权限除了不能动代码以外跟你是一致的"），
    所以它更不该拿到引擎对象：拿到就能顺着 `engine.transport` 发消息、
    顺着 `engine.super_admin_user_ids` 读名单——那等于绕开它自己那套 token/CSRF 认证。

    字段含义见 `plugins/webui/wire.py::_panel_seams`（那边逐项取用，缺哪项就"没启用"）。
    全部可缺省。
    """

    control_audit: object = None
    execute_action: object = None
    #: `(body) -> applied`：面板改配置的入口，引擎由核心侧闭包提前绑好。
    apply_overrides: object = None
    #: `() -> memory_ops | None`：**取值闭包**（记忆服务起得比装配晚）。
    memory_ops: object = None
    #: `() -> {"snapshot":…, "usage_store":…}`：活快照，取不到就退状态文件。
    state_reader: object = None
    restart: object = None
    group_switch: object = None
    session_clear: object = None
    #: `() -> self_id`：她自己的 QQ 号（面板的群动作要以她为 actor）。
    self_id: object = None
    knowledge: object = None
    #: `() -> tuple[dict, ...]`：**插件清单**（见 `inventory()`）。只读——面板拿它
    #: 列卡片里的 tab；"装/卸"不是面板点一下的事（预装的不给卸、webui 自己也不给卸）。
    plugins: object = None


class PluginRegistry:
    """插件能用的**全部**东西。核心造一个，交给 `discover()`。

    **这里没有 `engine`**——那是刻意的。插件要用的能力都由窄接缝给：
    `call_action` / `notify` / `roles` / `loop` / `chat` / `report`。

    为什么连"只读的引擎"也不给：引擎上有 `transport`（活的传输层）与三份权限名单
    （超管、管理员、补发白名单）。给出去就等于**插件能自己发消息、能读权限名单**，
    而那正是 `AGENTS.md` 2.1 禁止的事。要什么就给什么，一个一个列。
    """

    __slots__ = ("call_action", "notify", "roles", "loop", "chat", "report", "ui",
                 "action_caller", "commands", "backgrounds", "_shared_roles", "_commands",
                 "_prompts", "loaded")

    def __init__(self, *, call_action=None, notify=None, roles=None,
                 loop=None, chat: ChatSeams | None = None,
                 report: "ReportSeams | None" = None,
                 ui: "UiSeams | None" = None,
                 action_caller=None) -> None:
        self.call_action = call_action
        self.notify = notify
        self.roles = roles
        self.loop = loop
        #: 对话那一侧的全部能力（四个函数）。缺省是"什么都没有"。
        self.chat: ChatSeams = chat if chat is not None else ChatSeams()
        #: 写日报/写信要的东西（快照、模型 client、记账、写信给人）。
        self.report: ReportSeams = report if report is not None else ReportSeams()
        #: 控制面板要的东西（见 `UiSeams`）。面板权限最大，所以更不该拿到引擎。
        self.ui: UiSeams = ui if ui is not None else UiSeams()
        #: `(purpose) -> async (action, params) -> response`：造一个**已过闸门**的调用函数。
        #:
        #: **这是给核心自己用的**，不是给插件的：核心执行"插件声明的动作"时
        #: （`/super kick` 那类）需要调对面，而闸门必须由核心来判。
        #: 2026-10-01 之前那两处是直接把**活的 transport** 交给
        #: `plugins/group_admin/` 里的函数（`stage3_main` 的 docstring 还写着
        #: "插件从头到尾没拿到 transport"——那句话当时是假的）。现在改成给一个函数。
        self.action_caller = action_caller
        self.commands: list[object] = []
        self.backgrounds: list[object] = []
        self._shared_roles: object | None = None
        #: 命令注册表。`discover()` 之前由 `attach_plugins` 装上来。
        self._commands: object | None = None
        #: **prompt 扩展**（`extensions.PromptPlugin`）。恋人/剧情这类插件靠它
        #: 往两处 prompt 里放**不可信材料**——见 `provide_prompts` 的说明。
        self._prompts: list[object] = []
        #: 装上了哪些插件（`discover()` 的返回值）。**发现只跑一次**：
        #: 跑两次会让同一个插件被登记两遍（同一条命令认两次、两份角色缓存）。
        self.loaded: tuple[str, ...] = ()

    def command(self, plugin: object) -> None:
        """登记一条命令插件（`name` / `min_level` / `match()` / `handle()`）。

        命令插件**不在这里攒着**：本注册表持有一个核心命令注册表（`install()` 装上来），
        直接加进去——这样"哪条命令在链上"只有一份，不会出现
        "插件装上了、命令列表里却没有"的中间态。
        """

        if self._commands is not None and plugin is not None:
            self._commands.add(plugin)
        else:  # 还没有命令注册表（纯后台装配）：先攒着，`install()` 时补上
            self.commands.append(plugin)

    def install(self, commands: object) -> None:
        """装上核心的命令注册表，并把此前攒下的命令插件补进去（`build_engine` 调）。"""

        self._commands = commands
        for plugin in self.commands:
            commands.add(plugin)
        self.commands.clear()

    def background(self, plugin: object) -> None:
        """登记一条后台节拍。插件自己带 `name` / `interval_seconds` / `poll_once()`。"""

        if plugin is not None:
            self.backgrounds.append(plugin)

    def provide_roles(self, cache: object) -> None:
        """**前置插件**把共享能力放上来：`roles` 插件调它。

        两个去处：

        - 记在注册表里（`shared_roles()`），供**其它插件**在 `register()` 里取用——
          这是插件之间的共享方式，不经过引擎；
        - 同时交给核心的 `chat.roles_sink`，因为动作的**执行端在核心**
          （`stage3_main`），它执行群主动作时需要一个角色来源。

        为什么不让插件直接 `engine.self_roles = cache`：那是逆流——换一套思维链路时，
        新引擎未必有这个名字。走这里，核心与插件只约定"有这么个查询"，不约定字段名。
        """

        self._shared_roles = cache
        if callable(getattr(self.chat, "roles_sink", None)):
            self.chat.roles_sink(cache)  # type: ignore[attr-defined]

    def shared_roles(self) -> object | None:
        """取前置插件放上来的角色查询；没有就返回 None（依赖它的插件该跳过）。"""

        return self._shared_roles

    def provide_prompts(self, plugin: object) -> None:
        """登记一个 **prompt 扩展**（`extensions.PromptPlugin`）：恋人 / 剧情 / 关系都走它。

        这类插件**不产出回复**，它往两处 prompt 里放材料：

        - `build_prompt(context) -> str | None`：给**回复** agent 的背景材料；
        - `build_judge_hint(context) -> str | None`：给**判定** agent 的极短提示
          （可选，不实现就等于没有）。判定 agent 是刻意的"便宜那次调用"，
          所以它只吃短提示；
        - `after_decision(context, decision)`：**只观察**，不得改变决定。

        ## 硬边界（这是"人格稳定"的地基，别绕过）

        插件写的东西**永远是不可信 DATA**，只进 user 段，**不能碰 system 前缀**：

        - 核心用 `PromptSources.collect()` 收材料，它会过 `sanitize_chat_text`、
          按 `MAX_PLUGIN_PROMPT_LENGTH` 截断、标出来源、异常降级为空材料；
        - `PromptPlugin` 协议里**根本没有**"写 system prompt"这条通道；
        - `build_judge_hint` 另有一条**更短**的上限（判定那边窗口小）。

        也就是说：**插件能让她"知道得更多"，不能让她"变成另一个人"。**
        """

        if plugin is not None:
            self._prompts.append(plugin)

    def shared_prompts(self) -> tuple[object, ...]:
        """取所有已登记的 prompt 扩展（`runtime.build_engine` 交给 `PromptSources`）。"""

        return tuple(self._prompts)

    def register_reporter(self, reporter: object) -> None:
        """登记"每日汇报器"（面板要读它判断今天发没发）。同 `provide_roles` 的道理。"""

        if callable(getattr(self.chat, "reporter_sink", None)):
            self.chat.reporter_sink(reporter)  # type: ignore[attr-defined]

    def set_loop(self, loop: object) -> None:
        """补上事件循环。

        为什么是"补"而不是构造时给：`build_engine` 是**同步**函数，那时候还没有运行中的
        循环（`asyncio.get_running_loop()` 会抛）。要跨线程把协程丢回主循环的插件
        （面板的 HTTP 线程就是）需要它，所以在 `serve` 里跑起来之后补一刀。
        """

        self.loop = loop


def attach_plugins(registry: PluginRegistry, commands: object | None = None) -> object:
    """**唯一的装配入口**：发现一次，把命令插件接进核心的命令注册表。

    以前这一步是漏的：`discover()` 只在 `build_background_plugins()` 里跑过一次，
    插件 `registry.command(...)` 登记的东西进了一个没人读的列表——群管理命令于是
    "装上了但认不出"（测试里表现为一批命令测试全红）。

    现在命令与后台走**同一次发现**：命令进核心注册表，后台留在 `registry.backgrounds`
    给节拍用。发现只跑一次，重复调用是空操作（`loaded` 非空）。

    `commands` 是核心的命令注册表（`builtin_commands.build_command_registry()` 的产物）；
    不传时自己造一个。
    """

    if not registry.loaded:
        registry.loaded = discover(registry)
    if commands is None:
        from ..builtin_commands import build_command_registry

        commands = build_command_registry()
    registry.install(commands)
    return commands


def discover(registry: PluginRegistry, *, only: tuple[str, ...] = ()) -> tuple[str, ...]:
    """扫 `plugins/` 下每个目录，按**依赖顺序**把它们的 `register(registry)` 调一遍。

    每个插件的 `plugin.py` 可以导出：

    - `register(registry)`（必须）
    - `ENABLED = False`（可选，装在这里但先别启用）
    - **`REQUIRES = ("roles",)`**（可选，**前置插件**）

    ### 关于 `REQUIRES`（2026-10-01 用户纠正）

    用户原话："一个东西搬进插件另一个插件失效不代表前者不能作为插件，
    只需要把前者作为前置插件就行。"

    也就是说：**"B 依赖 A"从来不是"A 该进核心"的理由。** 正确做法是 A 也是插件，
    B 声明 `REQUIRES = ("A",)`，由这里保证 A 先装上。把 A 塞进核心才是错的——
    那样换一套思维链路时，A 会跟着核心一起被换掉，而它其实只是个功能。

    本函数据此做三件事：

    1. **按依赖顺序装**：`_load(name)` 先递归装它的 `REQUIRES`，再装自己；
    2. **前置装不上就跳过自己**（并记一行日志）：`roles` 没装成，
       `group_admin` 就不该半死不活地挂在那里；
    3. **环依赖不会转不出来**：正在装的集合里出现重复就报错跳过。
    """

    available = {m.name for m in pkgutil.iter_modules(__path__)
                 if m.ispkg and not m.name.startswith("_")}
    loaded: list[str] = []
    loading: set[str] = set()
    failed: set[str] = set()

    def _load(name: str, *, required_by: str = "") -> bool:
        if name in loaded:
            return True
        if name in failed:
            return False
        if name in loading:
            logger.error("plugin_dependency_cycle name=%s required_by=%s", name, required_by)
            failed.add(name)
            return False
        if name not in available:
            logger.warning("plugin_missing name=%s required_by=%s", name, required_by)
            failed.add(name)
            return False

        loading.add(name)
        try:
            plugin_module = importlib.import_module(f"{__name__}.{name}.plugin")
        except ModuleNotFoundError as exc:
            # 纯资源目录：不算失败，静默跳过。
            # 但**有 `plugin.py` 却缺依赖模块**要留一行日志（2026-10-04 审查 F7）：
            # 那种情况下插件是"装过、坏了"，不是"这里本来没插件"，静默会让它隐身。
            if _plugin_file(name).is_file():
                logger.warning("plugin_import_missing_module name=%s missing=%s",
                               name, getattr(exc, "name", "?"))
            loading.discard(name)
            return False
        except Exception:  # noqa: BLE001 - 坏插件不许带走别的
            logger.exception("plugin_import_failed name=%s", name)
            loading.discard(name)
            failed.add(name)
            return False

        for need in tuple(getattr(plugin_module, "REQUIRES", ()) or ()):
            if not _load(str(need), required_by=name):
                logger.warning("plugin_skipped_missing_dependency name=%s need=%s", name, need)
                loading.discard(name)
                failed.add(name)
                return False

        if not getattr(plugin_module, "ENABLED", True):
            logger.info("plugin_disabled name=%s", name)
            loading.discard(name)
            failed.add(name)
            return False

        register = getattr(plugin_module, "register", None)
        if not callable(register):
            logger.warning("plugin_has_no_register name=%s", name)
            loading.discard(name)
            failed.add(name)
            return False
        try:
            register(registry)
        except Exception:  # noqa: BLE001
            logger.exception("plugin_register_failed name=%s", name)
            loading.discard(name)
            failed.add(name)
            return False
        loading.discard(name)
        loaded.append(name)
        return True

    for name in sorted(available):
        if only and name not in only:
            continue
        _load(name)
    # **按实际装填顺序**返回（前置插件在前）。以前这里又排了一次序，于是"谁先装的"
    # 在返回值里看不出来——那正是 `REQUIRES` 要保证的东西，埋在日志里等于没法验证。
    # 顺手留一份给面板的清单接缝（`inventory()`）：接缝是在 `registry` 造好**之前**
    # 塞进 `UiSeams` 的，它拿不到那个 registry，所以这个事实得由这里自己记下来。
    global _LAST_LOADED
    _LAST_LOADED = tuple(loaded)
    return tuple(loaded)


#: 上一次 `discover()` 装上了哪些（`inventory()` 读它）。
_LAST_LOADED: tuple[str, ...] = ()


def _plugin_file(name: str) -> pathlib.Path:
    """`<某个插件根>/<name>/plugin.py` 的路径（判断"这目录到底算不算插件"用）。

    扫**整个 `__path__`**（不只第一个根）：这样测试可以把一个临时目录插进
    `plugins.__path__` 来验证"缺依赖的插件会被列出来而不是隐身"。
    """
    for root in __path__:
        candidate = pathlib.Path(root) / name / "plugin.py"
        if candidate.is_file():
            return candidate
    return pathlib.Path(__path__[0]) / name / "plugin.py"


def inventory() -> tuple[dict, ...]:
    """插件清单：给控制面板的"插件"卡用。**不装、不卸、不改装配状态。**

    ⚠️ **口径（2026-10-04 审查 F6 纠正）**：它**不调用任何 `register()`**、不改
    `_LAST_LOADED`，但它**会 `import` 每个插件的 `plugin.py`**——也就是**执行那些
    文件的顶层代码**。所以"只读、不改任何东西"是**半真话**：在"它自己造成的装配
    状态"这个口径上为真，在"有没有执行代码"这个口径上为假。要写就先写清楚。

    每一行：名字、显示名（`TITLE` 或模块 docstring 首句）、是不是预装、有没有声明
    `ENABLED=False`、**这次真装上没有**（`loaded`）、依赖谁（`REQUIRES`）、
    以及**能不能卸**。**成功行与失败行是同一组 9 个键**（面板可以无脑读）。

    ## "预装"与"能不能卸"的口径（2026-10-03 用户）

    > "有几个插件我建议是做成预装的，但是记住一定得按插件包装"
    > "插件是每个插件占卡片里面一个 tab，默认显示预装插件这些，
    >  webui 不给在 webui 里卸载这样"

    * **在 `plugins/` 目录里 = 预装**（随代码发布）。所以这里每一行都是预装。
    * **预装的不给卸**；**`webui` 自己也不给卸**——它卸掉就等于把面板自己删了。
      所以目前 `removable` 一律 `False`。真要"装/卸"，那是**加插件**的事
      （放一个文件夹进来），不是面板点一下的事；面板最多只该管"启用/停用"，
      而那要写 `data/`（**不许动 `src/`**）。
    """

    rows: list[dict] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        name = info.name
        if not info.ispkg or name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{name}.plugin")
        except ModuleNotFoundError as exc:
            # ⚠️ **两种"没有 plugin.py"必须分开**（2026-10-04 审查 F7 抓到的真 bug）：
            #   1. **纯资源目录**（真的没有 `plugin.py`）→ 正常跳过，不是插件；
            #   2. **有 `plugin.py`，但它 import 了一个装不上的模块** → **必须列出来**。
            # 这条原来一律 `continue`，于是第 2 种（新插件最常见的坏法）**在面板上
            # 彻底隐身**——而面板存在的意义正是"让插件看得见"。`discover()` 那边
            # 同样静默（口径一致，但一致地漏），所以那边补了一行 warning 日志。
            if _plugin_file(name).is_file():
                logger.warning("plugin_inventory_import_missing name=%s missing=%s",
                               name, getattr(exc, "name", "?"))
                rows.append({"name": name, "title": name,
                             "note": f"导入失败：找不到模块 {getattr(exc, 'name', '?')}",
                             "builtin": True, "enabled": False, "loaded": False,
                             "requires": [], "removable": False, "panel": False})
            continue
        except Exception:                 # noqa: BLE001 - 坏插件照样要能列出来
            rows.append({"name": name, "title": name, "note": "导入失败（看日志）",
                         "builtin": True, "enabled": False, "loaded": False,
                         "requires": [], "removable": False, "panel": False})
            continue
        doc = (getattr(module, "__doc__", "") or "").strip().splitlines()
        rows.append({
            "name": name,
            "title": str(getattr(module, "TITLE", "") or name),
            "note": doc[0][:80] if doc else "",
            "builtin": True,              # 在 plugins/ 里就是预装
            "enabled": bool(getattr(module, "ENABLED", True)),
            "loaded": name in _LAST_LOADED,
            "requires": [str(item) for item in (getattr(module, "REQUIRES", ()) or ())],
            "removable": False,           # 预装的不给卸；webui 自己更不给卸
            "panel": name == "webui",     # 面板自己（它的 tab 就是面板本体）
        })
    return tuple(rows)
