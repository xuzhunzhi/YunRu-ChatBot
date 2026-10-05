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
├── mail/                邮件通道：读信回信 + 每日汇报
└── outage_notice/       掉线通知：断一次给操作者发一封固定模板的邮件（依赖 mail）
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
    registry.link                     # LinkSeams：掉线事件（on_disconnect(接收者)）
    registry.mail                     # MailSeams：发一封给操作者的普通邮件（provide/notify）
    registry.call_action / notify     # 动作与通知（后台插件用）
    registry.roles / loop             # 预留（**未接线**） / 事件循环
    registry.shared_roles()           # 共享位：角色事实（**核心**放上来的那一份）
    registry.command(plugin)          # 一条命令插件
    registry.background(plugin)       # 一条后台节拍（有 name/interval_seconds/poll_once）
    registry.provide_roles(cache)     # 前置插件：把共享能力放上来
    registry.provide_prompts(plugin)  # prompt 扩展（恋人/剧情那类）
    registry.provide_prompt(name, t)  # 某套 prompt 的**内置原稿**（识图那套走这里）
    registry.provide_group_action(g, f)  # 群管理动作的**执行函数**（见下）
    registry.provide_owner_channel(c)  # 渠道声明"可以声明这条来自主人"（见下）
    registry.vision = factory         # 一个"看一眼图"的工厂（`(usage_store) -> 识图器`）

**这里没有 `engine`**（2026-10-01 改）：引擎上有 `transport` 与三份权限名单，
给出去就等于插件能自己发消息、能读名单。要什么能力就由接缝一个一个列。

**接缝的限度要认清**（审查 2026-10-01）：进程内的插件隔离**不是安全边界，只是约定**。
闭合函数与绑定方法都能被 `__self__` / `__closure__` 绕过，所以核心侧用
`runtime._SeamBinder`（闭包只捕获不透明令牌）把"顺手拿"堵掉；但能执行 Python 的插件
总有别的办法（`import` 核心模块、`gc`）。**真正的边界是三件核心内的事**：
身份由核心盖章、渠道注入的话拒特权命令、动作执行与权限判定在核心。

## 为什么这样切

- **可单独拿走**：不想要识图，删掉 `vision/` 就行；核心对它的引用都是"没有就降级"。
  2026-10-05 起这句在**运行路径上**成立：核心**不再 import 那个模块**——识图器由
  `vision` 插件经 `registry.vision` 给一个工厂、它的 prompt 由
  `registry.provide_prompt("vision", …)` 登记，删掉那个文件夹就是"这次部署没有识图"
  （实测：`tests/check_module_removal.py` 拦掉整棵子树后 `build_engine()` 照起）。
  **一处例外要如实说**（2026-10-05 核对时发现）：`prompt_library._vision_plugin_present()`
  用 `importlib.util.find_spec("qq_roleplay_bot.plugins.vision.vision")` 问"那个模块在不在"
  （面板的 `available()` 不许在插件被藏起来时撒谎）。**它提了模块名，但不 import**，
  所以"删掉照跑"仍然成立；只是"核心一个字都不提那个模块名"这句**不成立**，别那么写。
- **群管理动作也走这条口**（2026-10-06 补）：动作的**判定与执行仍在核心**
  （`stage3_main.execute_action` 判权限、过闸门、审计），核心只是不再自己去
  `import` 插件里的执行函数——`group_admin` 插件在 `register()` 里用
  `registry.provide_group_action("group_admin", factory)` /
  `("group_owner", factory)` 把**执行函数**放上来，核心用
  `getattr(registry, "group_action", None)` 取（工厂形状见下表）。
  在改这条之前，核心那两句 `from .plugins.group_admin.group_admin import execute`
  是**按模块名**找函数的：把那个插件文件夹改个名字，功能会**静默消失**
  （`except ModuleNotFoundError` 吞掉，回一句"这条部署没有群管理能力"），
  而 `discover()` 那边一切正常——所以这是"核心认识具体插件"，不是"插件提供能力"。
  登记进注册表的是一个**工厂**（形状同 `registry.vision`）：核心先把用量账本递进去、
  拿到执行函数，之后每次动作只调那个执行函数：

  | 名字 | 登记进注册表的工厂 | 它交出来的 `execute` |
  | --- | --- | --- |
  | `"group_admin"` | `(usage_store) -> execute` | `execute(kind, *, call, group_id, actor_id, target_id, minutes, message_id, mentioned, protected_ids, enabled)` |
  | `"group_owner"` | `(usage_store) -> execute` | `execute(kind, *, call, roles, group_id, actor_id, target_id, text, mentioned, enabled)` |

  取不到时**保持 fail-closed**：不执行、记一行日志、回一句"这条部署没有群管理能力"。
- **渠道自己声明"我说的话可以算主人"**（2026-10-06 补）：核心原来在 `runtime.py`
  里写死 `_OWNER_CHANNELS = ("mail",)`——**核心知道有一个叫 mail 的渠道**。
  外部审查判定它不是安全洞（`claims_owner` 只影响给模型看的 `sender_role` 标签，
  **不授予命令权限**），是**扩展性耦合**。现在渠道在 `register()` 里说一句
  `registry.provide_owner_channel("<渠道名>")`，核心只问"这个渠道声明过吗"
  （`_SeamBinder._owner_channel_ok`）。核心那一份只剩 `_OWNER_CHANNELS_BUILTIN`
  那个**过渡引导项**（`mail`），`plugins/` 侧即使一个字不改，行为也与改之前一致；
  **新渠道一律走声明**。这是进程内受信任的插件声明，换来的不是安全，而是
  "核心不认识任何具体渠道名"。
- **依赖方向清楚**：插件 import 核心；核心**不 import 具体插件**，只调 `discover()`。
- **"掉线通知"也是插件**（2026-10-06 用户："记住这个也是插件"）：本体侧只做两件事——
  把看门狗本来就有的状态变成**确定的边沿**（`在线 → 掉线` 算一段），再在
  `registry.link` 上广播**"断了"这个事实**。谁来接、拿它做什么（发信 / 记日志 /
  面板弹一条）都是插件的事；核心不知道有邮件这回事，也不会去发。见 `LinkSeams`。
  接它的那一个（`outage_notice/`）**依赖 `mail`**：`REQUIRES = ("mail",)`，方向不许反，
  见 `MailSeams`。
- **`roles/` 曾经也是插件**（2026-10-01 用户）："一个东西搬进插件另一个插件失效不代表
  前者不能作为插件，只需要把前者作为前置插件就行。" 当时群管理与入群审批都要它，
  所以它是**前置插件**：那两个声明 `REQUIRES = ("roles",)`。那句话本身仍然成立
  （"B 依赖 A"不是"A 该进核心"的理由，现在活着的正例是 `outage_notice → mail`）。
  **但 2026-10-05 变了**：用户把**身份/权限事实**判给核心，角色就是这种东西，
  于是那两个插件的 `REQUIRES` 撤掉了，角色改由**核心**放进共享位
  （本体 `1c5fcf3`：`runtime.build_engine` 在 `discover()` 之前
  `registry.provide_roles(engine.group_roles)`；插件问 `registry.shared_roles()`）。
  `group_admin` 更是连角色查询都不碰：执行端拿的是核心**递进来**的那份
  （`stage3_main.execute_action(roles=engine.group_roles, …)`）。
  `plugins/roles/` 那个文件夹**已经删掉**了——它和核心那一份都走 `provide_roles`
  这条"后到者覆盖先到者"的路，插件一装上就把核心那份替换掉（实测：核心
  `tests/test_group_roles.py` 当场四条报 `SelfRoleCache` 没有 `self_role` / `ready`）。
  删掉它之后共享位上只剩核心一份，**单一来源**才真的成立。
  这条与 `AGENTS.md` §2.3 是同一个道理：**能当插件不等于什么都该当插件**——
  身份/权限事实属于核心（它是权限判定的一部分）。
"""
from __future__ import annotations

import importlib
import logging
import pathlib
import pkgutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 进程里那一份注册表（`build_engine` 装完插件后放上来）。
#:
#: 为什么要它：核心有一处必须**按名字**问"某套 prompt 的原稿在不在"
#: （`prompt_library.builtin("vision")`），而那次调用拿不到注册表对象
#: （它可能在 `build_engine` 之前就被调用）。所以留一个进程级的读口，
#: 与 `prompt_library.shared()` / `runtime_flags.shared()` 同一套路。
#: 测试里反复 `build_engine` 只会把它换成最新那一份——它只是活引用，没有副本。
_REGISTRY: "PluginRegistry | None" = None


def registry() -> "PluginRegistry | None":
    """取进程里那份注册表；还没有就是 `None`（调用方按"没有插件"降级）。"""

    return _REGISTRY


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
    #:
    #: 2026-10-06 改：清单不再由核心写死，而是**渠道自己声明**
    #: （`registry.provide_owner_channel("<渠道名>")`，在插件的 `register()` 里，
    #: 一说一句"我这个渠道比过发件地址，可以声明主人"）。核心那份
    #: `runtime._OWNER_CHANNELS_BUILTIN` 只留一条**过渡用**的引导项，
    #: 不新增——新渠道一律走声明。这里这个字段是**只读的当前快照**，给插件看，
    #: 不是判定用的权威来源（判定在 `runtime._SeamBinder.deliver`）。
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
    #: 她**学来的东西**（金句 / 黑话）的读写接缝（见 `LearnedSeams`）。**只碰这两份数据**，
    #: 没有记忆入口、也没有对话入口——那份函数清单由测试逐个钉住。
    learned: object = None


@dataclass(frozen=True, slots=True)
class LearnedSeams:
    """她**学来的东西**（金句 / 黑话）的读写接缝。**这里是函数，不是引擎、不是记忆。**

    由来（2026-10-06 用户口径）：*"金句我看应该划到**知识库**里"*、*"还有**黑话**"*。
    这两样都是 Stage 3 的数据（`AGENTS.md` §2.3：对话、记忆、知识库是 Stage 3 的地盘），
    面板要能看、能改——但**绝不能**顺手把记忆入口或对话入口也放进来。§2.3 那张表里
    被禁的正是"任何让插件读/写**长期记忆**、**知识库**的接缝"，而这一份是"她学来的
    材料"这条独立的窄口：面板能改的只有这两份 JSON，**碰不到记忆库、也发不出话**。

    判据是**函数集合本身**：`tests/test_learned_seams.py` 把下面这些字段名逐个钉住
    （多一个、少一个都红），并按名字扫"有没有夹带记忆/对话入口"。
    所以这份清单是**契约**，不是"顺便能用的东西"。

    | 字段 | 形状 | 干什么 |
    | --- | --- | --- |
    | `quote_view` | `(group_id) -> dict` | 读：按群的表情含义表 + 风格/语境笔记 |
    | `quote_correct` | `(group_id, emoji_id, sense, note="") -> dict` | 改：纠正某表情方向（`auto` 撤销） |
    | `quote_note_enabled` | `(note_id, enabled) -> dict` | 改：停用 / 恢复某条笔记 |
    | `slang_list` | `(group_id=None) -> list` | 读：黑话词条（`None` = 所有群） |
    | `slang_update` | `(group_id, word, definition) -> dict` | 改：改释义（记一次修订） |
    | `slang_delete` | `(group_id, word) -> bool` | 改：删词条 |
    | `slang_mark_wrong` | `(group_id, word, wrong=True) -> dict` | 改：标错 / 取消标错 |

    返回的都是**结构化数据**（dict / list / bool），不是给她看的话——面板要显示什么自己排版。
    取不到数据时一律返回空结构（`{}` / `[]` / `False`），**不抛**：
    面板不该因为"这份东西还没学出来"崩掉，核心也不该因为面板没接上而变样。
    真正的读写口在核心（`quote_learning.QuoteStore` / `slang_learning.SlangStore`），
    这一层**不自己存一份**——两份数据迟早会分叉。
    """

    quote_view: object = None
    quote_correct: object = None
    quote_note_enabled: object = None
    slang_list: object = None
    slang_update: object = None
    slang_delete: object = None
    slang_mark_wrong: object = None


@dataclass(frozen=True, slots=True)
class LinkSeams:
    """**连接事件**（现在是掉线）的接缝：插件登记一个"被叫一次"的接收者。

    由来（2026-10-06 用户）：*"掉线可以调用 mail 插件给我发消息通知我"*、
    *"断一次只发一次，不要反复调用"*、*"记住这个也是插件"*。

    ## 这一侧只有**事件**，没有"通知给谁"

    | 谁 | 负责什么 |
    | --- | --- |
    | 核心（`runtime._watch_connection`） | 看传输层的 `connected`，做**边沿检测**：`在线 → 掉线` 算一段，广播一次 |
    | 这个接缝 | 把"谁在听"收上来（`on_disconnect`），别的不做 |
    | 插件（掉线通知那一个） | 决定"断了之后做什么"——发信、记一条、面板弹窗 |

    这一侧**不认识任何具体接收者**：它只广播"断了这个事实"，谁接、发到哪、
    发得出去发不出去，都是插件自己的事（核心这条路径上一个插件名都没有）。

    ## "一次掉线只叫一次"由谁保证（别在这里再记一遍状态）

    由**核心那一侧的边沿检测**保证（`runtime._DisconnectNotifier`）：掉线期间
    反复检查**不算新事件**，重连后重新武装，再次断开才是新的一段。这里刻意
    **不重复记状态**——两处各记一份，迟早会分叉，而用户最强调的就是这一条。

    ## 接收者的形状（写插件时照这个来）

    * **不收参数**：核心只广播"断了"这个事实。要时间戳自己取（`time.time()`）。
    * **同步函数或协程函数都行**（广播侧统一 await）。
    * **抛异常不会影响机器人**：广播侧逐个兜住并记一行 `disconnect_receiver_failed`，
      剩下的接收者照常被叫。发信失败不该把看门狗（以及进程）带走。
    * `on_disconnect` 可以被叫多次（登记多个接收者），按登记顺序广播。

    字段同样**只放函数**（理由见 `ChatSeams`）：这里给的是核心注入的登记口，
    不是引擎、不是传输层——插件拿不到 `transport`，也就不能自己造一条连接事件。
    """

    #: `(receiver) -> None`：登记口，由 `PluginRegistry` 注入（它自己持有名单）。
    #: 缺省（手造 `LinkSeams()`）时 `on_disconnect()` 是空操作——没有注册表的
    #: 单元测试里也不会崩。
    disconnect_sink: object = None

    def on_disconnect(self, receiver: object) -> None:
        """登记一个接收者：**掉线那一次**被叫一次（同一段掉线不会叫第二次）。

        返回 `None`：这里不告诉调用方"登记成没成"。**接缝没接上时静默不登记**
        与 `ChatSeams.reserved()` 的缺省语义一致（没接上 = 什么都没有），
        但要区分"没接上"与"接上了没人听"请用 `PluginRegistry.knows_link()`
        （同 `ChatSeams.knows_reserved()` 的道理）。
        """

        if not callable(self.disconnect_sink):
            return
        try:
            self.disconnect_sink(receiver)
        except Exception:  # noqa: BLE001 - 登记失败不该让插件装配炸掉
            logger.warning("plugin_link_register_failed", exc_info=True)


class MailSeams:
    """**"给操作者发一封普通邮件"**的能力面：谁来发由 `mail` 插件登记，别的插件只管用它。

    由来（2026-10-06 用户）：*"掉线可以调用 mail 插件给我发消息通知我"* ·
    *"这个作为 mail 插件的后置插件，也就是说 mail 插件是这个插件的依赖，不要搞错了"*。

    ## 为什么要有它

    mail 插件的发信能力原来只服务它自己（每日汇报从 `wire.build_daily_report` 里
    直接 `MailClient(...)`）。别的插件要发一封"给自己人看的"邮件**没有口子**——
    没有这个口子，掉线通知只有两条路：把逻辑写进 mail 插件（用户明确否了），
    或者自己再造一个 `MailClient`（配置就有了第二份）。两条都不是"插件之间的依赖"。

    ## 方向（这一条绝不能反）

    | 谁 | 做什么 |
    | --- | --- |
    | `mail`（前置） | 在 `register()` 里 `registry.mail.provide(fn)` —— **提供**发信能力 |
    | `outage_notice`（后置） | `REQUIRES = ("mail",)`，在 `register()` 里 `registry.mail.notify(...)` —— **使用** |

    反过来（mail 认识掉线通知、或核心认识邮件）都是错的：`discover()` 保证前置先装，
    装不上就跳过后置（见那个函数的 `REQUIRES` 一节）。

    ## 它给的是"发一封信"，不是"邮件通道"

    接收者拿到的是 `(subject, body) -> await`：**收件人与发信方式都由 mail 插件定**
    （它读自己的配置 `MAIL_REPORT_TO`，与日报同一个收件人）。使用者指定不了发件人、
    指定不了收件人、也拿不到 `MailClient`——所以"给任意地址发信"这个原语没有被发出去。

    字段同样是**函数**（同 `ChatSeams` 的理由）：这里给的是插件登记的一个函数，
    不是引擎、不是传输层、也不是邮箱凭据。

    ## 一件刻意不做的事：这里不写"失败怎么办"

    `notify()` 把异常**照原样抛**——是使用者（发告警的那个插件）自己决定
    "记一笔还是吞掉"，还是核心的广播侧兜住。在这里吞掉等于让一切都变成静默成功，
    而"发信失败要记一笔"正是使用者的责任（见 `plugins/outage_notice/`）。

    ## 为什么是普通类，不是 `@dataclass(frozen=True, slots=True)`

    与 `LinkSeams` 的唯一区别：那一个的字段（`disconnect_sink`）**由注册表在构造时绑好、
    之后只读**；这一个的字段是**别的插件登记进来的**，所以它必须能被写一次。
    写成 frozen dataclass 就得用 `object.__setattr__` 绕过——那是为了形状统一而绕过
    自己定的规矩，不值得。字段清单仍然只有下面一个，语义仍然是"只放函数"。
    """

    #: `(subject, body) -> await`：登记上来的发信函数；缺省 `None` = 这次部署没有 mail。
    #: 与 `LinkSeams.disconnect_sink` 同一形状（注册表自己持有名单，不需要引擎注入）。
    __slots__ = ("operator_sender",)

    def __init__(self, operator_sender: object = None) -> None:
        self.operator_sender = operator_sender

    def provide(self, sender: object) -> None:
        """**前置插件**（`mail`）把"发一封给操作者的邮件"放上来。

        非可调用的东西一律不收（`on_disconnect` 同一条纪律：错在这里发现，
        比在掉线那一刻发现便宜得多）。后到者覆盖先到者，与 `provide_prompt` 一致。
        """

        if not callable(sender):
            logger.warning("plugin_mail_sender_refused type=%s", type(sender).__name__)
            return
        self.operator_sender = sender

    def knows(self) -> bool:
        """**这个能力在不在**（区分"没人提供"与"有人提供了但发不出去"）。

        同 `ChatSeams.knows_reserved()` 的道理：使用者据此**决定要不要登记掉线接收者**——
        没有发信能力时登记一个注定失败的接收者，只会在每次掉线时往日志里灌一行错。
        """

        return callable(self.operator_sender)

    async def notify(self, subject: str, body: str) -> object:
        """发一封给操作者的邮件；**没有提供者就抛 `RuntimeError`**（不许静默丢弃）。

        为什么不静默返回 `None`：调用方（告警）**必须**能分清"发出去了"与"根本没人接"。
        在这条路上"以为发了其实没发"的代价是"断了没人知道"，比一行异常贵得多。
        """

        sender = self.operator_sender
        if not callable(sender):
            raise RuntimeError("没有插件提供发信能力（registry.mail 是空的）")
        return await _maybe_await(sender(str(subject), str(body)))


class PluginRegistry:
    """插件能用的**全部**东西。核心造一个，交给 `discover()`。

    **这里没有 `engine`**——那是刻意的。插件要用的能力都由窄接缝给：
    `call_action` / `notify` / `roles` / `loop` / `chat` / `report` / `link` / `mail`。

    为什么连"只读的引擎"也不给：引擎上有 `transport`（活的传输层）与三份权限名单
    （超管、管理员、补发白名单）。给出去就等于**插件能自己发消息、能读权限名单**，
    而那正是 `AGENTS.md` 2.1 禁止的事。要什么就给什么，一个一个列。
    """

    __slots__ = ("call_action", "notify", "roles", "loop", "chat", "report", "ui",
                 "link", "mail", "vision", "action_caller", "commands", "backgrounds",
                 "_shared_roles", "_commands", "_prompts", "_prompt_defaults",
                 "_group_actions", "_owner_channels", "_disconnect_receivers", "loaded")

    def __init__(self, *, call_action=None, notify=None, roles=None,
                 loop=None, chat: ChatSeams | None = None,
                 report: "ReportSeams | None" = None,
                 ui: "UiSeams | None" = None,
                 vision=None,
                 action_caller=None) -> None:
        self.call_action = call_action
        self.notify = notify
        #: **预留、当前未接线**：读它的地方一处都没有（`runtime.build_engine` 传的是
        #: `None`）。身份/权限事实进核心那件事**没有**走这个槽位——本体 `1c5fcf3`
        #: 走的是**既有接缝** `provide_roles()` / `shared_roles()`：
        #: 核心在 `discover()` **之前** `provide_roles(engine.group_roles)`，
        #: 插件问 `registry.shared_roles()` 拿到的就是核心那一份
        #: （见 `plugins/join_approval/plugin._RoleSource` 与 `AGENTS.md` §3.3
        #: 对"先画好的插座"的口径：要么接上，要么在文档里标"预留、未接线"）。
        self.roles = roles
        self.loop = loop
        #: 识图器的**工厂**：`(usage_store) -> 有 describe()/enabled 的对象 | None`。
        #: 由 `vision` 插件在 `register()` 里放上来，核心只问"有没有"——
        #: `runtime.build_engine` 因此不再 import 任何插件模块（见
        #: `plugins/vision/plugin.py`）。缺省 `None` = 这次部署没有识图，
        #: 有图的消息只留 `[图片]` 占位符。
        self.vision = vision
        #: 对话那一侧的全部能力（四个函数）。缺省是"什么都没有"。
        self.chat: ChatSeams = chat if chat is not None else ChatSeams()
        #: 写日报/写信要的东西（快照、模型 client、记账、写信给人）。
        self.report: ReportSeams = report if report is not None else ReportSeams()
        #: 控制面板要的东西（见 `UiSeams`）。面板权限最大，所以更不该拿到引擎。
        self.ui: UiSeams = ui if ui is not None else UiSeams()
        #: 连接事件（掉线）的接缝（见 `LinkSeams`）。
        #:
        #: **这一个没有"注入"参数**（不像上面三个要由 `runtime` 递进引擎绑定的闭包）：
        #: 接收者名单本来就归注册表自己管（同 `_prompts` / `_shared_roles`），
        #: 核心的看门狗只问 `disconnect_receivers()`。少一个旋钮就少一处"装配漏了"。
        self.link: LinkSeams = LinkSeams(disconnect_sink=self._add_disconnect_receiver)
        #: **"给操作者发一封普通邮件"**那条接缝（见 `MailSeams`）。
        #:
        #: 与 `link` 同一形状、同一个理由：**名单/能力本来就归注册表自己管**
        #: （由 `mail` 插件在 `register()` 里 `provide()` 填），不需要核心递引擎绑定的闭包。
        #: 缺省是空的：没有 mail 插件时 `knows()` 为假，使用者据此**不登记**
        #: （所以要在这里就造好，而不是等某个插件来赋值——`__slots__` 也不允许那样）。
        self.mail: MailSeams = MailSeams()
        #: 已登记的掉线接收者（`registry.link.on_disconnect(...)` 往这里放）。
        self._disconnect_receivers: list[object] = []
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
        #: **某套 prompt 的内置原稿**：`{名字: 文本}`，由插件经 `provide_prompt()` 登记。
        #: 与上面那份分得很清：`_prompts` 是"往 prompt 里加不可信材料"（扩展），
        #: 这里登记的是"整套 prompt 的默认文本"（识图那一套），`prompt_library.builtin()`
        #: 读它。**它替代了核心原来那句 `from .vision import VISION_SYSTEM_PROMPT`**。
        self._prompt_defaults: dict[str, str] = {}
        #: **群管理动作的执行函数**：`{分组名: 函数}`，由插件经 `provide_group_action()`
        #: 登记，核心的 `stage3_main.execute_action` 按 `ActionRequest.group` 取。
        #: 与 `_prompt_defaults` 同一套形状（一个名字一个东西、后到者覆盖先到者）：
        #: 核心因此**不认识任何插件模块名**——把 `plugins/group_admin/` 改名不会让功能
        #: 静默消失，只会变成"这次部署没有群管理能力"（fail-closed，那句话本身是对的）。
        self._group_actions: dict[str, object] = {}
        #: **哪些渠道声明了"可以声明主人"**（`provide_owner_channel()` 往这里放）。
        #:
        #: 为什么是声明而不是核心写死一张表：核心原来有一个
        #: `runtime._OWNER_CHANNELS = ("mail",)`，也就是**核心知道有个叫 mail 的渠道**。
        #: 那不是安全洞（`claims_owner` 只影响给模型看的 `sender_role` 标签，
        #: 不授予任何命令权限），但它是扩展性耦合：以后加一个同样可信的渠道，
        #: 得回来改核心、还得知道"这里有一张表"。改成渠道自己声明之后，
        #: 核心只问"这个渠道说过它可以吗"，一个新渠道名都不用认识。
        self._owner_channels: set[str] = set()
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
        """把"角色查询"放到共享位上。**现在由核心自己调**（`runtime.build_engine`）。

        两个去处：

        - 记在注册表里（`shared_roles()`），供**其它插件**在 `register()` 里取用——
          插件之间的共享方式，不经过引擎；
        - 同时交给核心的 `chat.roles_sink`，落到引擎上（`engine.group_roles`），
          因为动作的**执行端在核心**（`stage3_main`），它执行群主动作时需要一个角色来源。

        ## 2026-10-06：角色事实**进核心**了，这个口的主语换了

        用户拍板"行，进核心"之后，放上来的那份是核心自己造的
        `group_roles.GroupRoles`（回答"某人在某群是什么角色"与"她自己是什么角色"），
        插件要问就调 `shared_roles()`，**不自己取**（`AGENTS.md` §2.3）。

        这个口仍然留给插件：`None` 之外的**任何一个**实现都能放上来，后到者覆盖先到者。
        所以插件侧的 `plugins/roles/` 若有自己的实现，它照样能在这里替换掉核心那份——
        这是**已知的、故意的**覆盖点（本分支的 `plugins/` 下没有插件文件夹，所以现在
        不会发生）；要不要在插件侧撤掉那份重复实现，由插件侧那条线自己决定。

        为什么不让插件直接 `engine.group_roles = cache`：那是逆流——换一套思维链路时，
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

    def provide_prompt(self, name: str, text: str) -> None:
        """登记**某一套 prompt 的内置原稿**：`{名字: 文本}`，供核心的 `prompt_library` 读。

        与 `provide_prompts`（扩展）不是一回事，别混：

        | | 登记的是什么 | 谁读 |
        | --- | --- | --- |
        | `provide_prompts(plugin)` | 往 prompt 里加的**不可信材料** | `PromptSources` → user 段 |
        | `provide_prompt(name, text)` | **整套** prompt 的默认文本 | `prompt_library.builtin(name)` |

        为什么要这条口（2026-10-05 搬识图时加）：识图那一套 prompt 原来是核心
        `prompt_library.builtin("vision")` 里 `from .vision import VISION_SYSTEM_PROMPT`——
        核心于是必须知道插件模块叫什么、放在哪。现在反过来：**插件把自己的原稿放上来**，
        核心只按名字取（`registry.provided_prompt`），删掉插件就自然"这次部署没有这一套"。

        **同名后到者覆盖先到者**：不报错是刻意的——发现机制本来就是"一个名字一个目录"，
        同名只可能出现在测试里手造两个注册表或插件被热重载的情形，那里覆盖比抛错有用。
        """

        self._prompt_defaults[str(name)] = str(text)

    def provided_prompt(self, name: str) -> str | None:
        """取插件登记的某套 prompt 原稿；没人登记就返回 `None`（调用方自己决定怎么降级）。"""

        return self._prompt_defaults.get(str(name))

    def provide_group_action(self, group: str, execute: object) -> None:
        """登记**某一组群动作的执行函数**：`{分组名: 函数}`，供核心的 `execute_action` 取。

        分组名就是 `ActionRequest.group`（`"group_admin"` / `"group_owner"`），
        与 `registry.vision` 同一个形状：**插件把自己的东西放上来，核心只按名字取**。

        为什么要这个口（2026-10-06 外部审查实测的那处耦合）：核心原来在
        `stage3_main.execute_action` 里直接
        `from .plugins.group_admin.group_admin import execute`——
        也就是**按插件模块名**找执行函数。插件文件夹改个名字，插件照样装上、
        `discover()` 照样说它装上了，而这条命令**静默**回一句"这条部署没有群管理能力"
        （`except ModuleNotFoundError` 吞掉）。那不是"插件提供能力"，是核心认识具体插件。

        这一口只登记**执行函数**，不改变任何边界：权限判定（`_plugin_level_allowed`）、
        动作白名单（`capabilities` 那道闸）、护栏与审计**仍在核心**，
        函数拿到的还是核心造的**已过闸门**的 `call`（见 `_group_action_caller`），
        不是 transport。

        **同名后到者覆盖先到者**（同 `provide_prompt`）：发现机制本来就是"一个名字一个
        目录"，同名只可能出现在测试里手造两个注册表或热重载的情形。
        """

        if execute is not None:
            self._group_actions[str(group)] = execute

    def group_action(self, group: str) -> object | None:
        """取某一组群动作的执行函数；没人登记就返回 `None`。

        调用方（`stage3_main.execute_action`）拿到 `None` 时**必须 fail-closed**：
        不执行、记一行日志、回一句"这条部署没有群管理能力"。
        """

        return self._group_actions.get(str(group))

    def provided_group_actions(self) -> tuple[str, ...]:
        """已经登记了执行函数的分组名（按名字排序）。

        给测试与面板用：**"到底装上了什么"必须是可读的事实**，而不是只能靠
        `grep` 核心源码去猜（这正是 2026-10-06 那处耦合藏了这么久的原因）。
        """

        return tuple(sorted(self._group_actions))

    def provide_owner_channel(self, channel: str) -> None:
        """**渠道自己声明**："我这个渠道比过发件人是谁，可以声明这条来自主人。"

        由来（2026-10-06，外部审查判定的"扩展性耦合"）：核心原来在
        `runtime.py` 里写死 `_OWNER_CHANNELS = ("mail",)`。那不是安全洞——
        `claims_owner` 只影响给模型看的 `sender_role` 标签，**不授予命令权限**
        （命令权限一律走 QQ 那条路）。它的问题是：核心知道有个叫 `mail` 的渠道，
        以后加一个同样可信的渠道得回来改核心。

        现在反过来：渠道在 `register()` 里说一句（例如邮件通道说
        `registry.provide_owner_channel("mail")`），核心只问"这个渠道声明过吗"。
        **判定仍在核心**（`runtime._SeamBinder.deliver`）：声明只是"这个渠道愿意
        为这句话负责"，最终仍然要**同时**满足 `claims_owner is True`。

        限度（别读成安全边界）：这是**进程内受信任的插件声明**，插件当然可以说谎——
        与 `provide_group_action` / `provide_prompt` 同一个信任级。它换来的不是安全，
        是"核心不认识任何具体渠道名"。
        """

        name = str(channel or "").strip()
        if name:
            self._owner_channels.add(name)

    def declared_owner_channels(self) -> tuple[str, ...]:
        """已经声明"可以声明主人"的渠道（排序后的元组，给核心与测试读）。"""

        return tuple(sorted(self._owner_channels))

    def knows_owner_channel(self, channel: str) -> bool:
        """某个渠道声明过吗（核心的判定读这一条，不读任何写死的渠道名）。"""

        return str(channel) in self._owner_channels

    def register_reporter(self, reporter: object) -> None:
        """登记"每日汇报器"（面板要读它判断今天发没发）。同 `provide_roles` 的道理。"""

        if callable(getattr(self.chat, "reporter_sink", None)):
            self.chat.reporter_sink(reporter)  # type: ignore[attr-defined]

    def _add_disconnect_receiver(self, receiver: object) -> None:
        """`LinkSeams.on_disconnect()` 的落点：把接收者放进本注册表的名单。

        这是**绑定方法**，所以 `registry.link.disconnect_sink.__self__` 就是注册表本身。
        这里不违反 `_SeamBinder` 那条纪律（见 `runtime._SeamBinder`）：泄露出去的是
        **插件的调用方本来就拿着的那个注册表对象**（`register(registry)` 的第一个参数），
        不是引擎、不是传输层——多给不了任何东西。
        """

        if callable(receiver):
            self._disconnect_receivers.append(receiver)

    def knows_link(self) -> bool:
        """**接缝接上了吗**——用来区分"没人听"与"没接上"（同 `ChatSeams.knows_reserved()`）。

        缺省那个 `LinkSeams()`（`disconnect_sink=None`）就是"没接上"；
        注册表自己造的那个永远是接上的，只是名单可能为空。
        """

        return callable(self.link.disconnect_sink)

    def disconnect_receivers(self) -> tuple[object, ...]:
        """取**此刻**已登记的掉线接收者（按登记顺序）。核心的看门狗读它。

        返回元组快照，不是活列表——看门狗在广播时不该看见"名单中途变化"。
        """

        return tuple(self._disconnect_receivers)

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
    - **`REQUIRES = ("mail",)`**（可选，**前置插件**）

    ### 关于 `REQUIRES`（2026-10-01 用户纠正）

    用户原话："一个东西搬进插件另一个插件失效不代表前者不能作为插件，
    只需要把前者作为前置插件就行。"

    也就是说：**"B 依赖 A"从来不是"A 该进核心"的理由。** 正确做法是 A 也是插件，
    B 声明 `REQUIRES = ("A",)`，由这里保证 A 先装上。把 A 塞进核心才是错的——
    那样换一套思维链路时，A 会跟着核心一起被换掉，而它其实只是个功能。

    **但反过来也成立：进了核心的东西不再是插件依赖**（2026-10-05）。用户把
    **身份/权限事实**判给核心，`plugins/roles/` 于是不再是 `group_admin` /
    `join_approval` 的前置——那两个插件的 `REQUIRES` 已经撤掉（角色由核心注入，
    见 `PluginRegistry.roles`）。这条规矩现在活着的正例是
    `outage_notice → mail`（`REQUIRES = ("mail",)`，方向不许反）。

    本函数据此做三件事：

    1. **按依赖顺序装**：`_load(name)` 先递归装它的 `REQUIRES`，再装自己；
    2. **前置装不上就跳过自己**（并记一行日志）：`mail` 没装成，
       `outage_notice` 就不该半死不活地挂在那里；
    3. **环依赖不会转不出来**：正在装的集合里出现重复就报错跳过。
    """

    available = {m.name for m in pkgutil.iter_modules(__path__)
                 if m.ispkg and not m.name.startswith("_")}
    loaded: list[str] = []
    loading: set[str] = set()
    failed: set[str] = set()
    # 记下"进程里那一份"：核心有一处要**按名字**读插件登记的 prompt 原稿
    # （`prompt_library.builtin("vision")`），那次调用拿不到这个对象——见 `registry()`。
    # 放在这里而不是 `attach_plugins()`：`discover()` 是那个"登记发生了"的时刻，
    # 而 `attach_plugins` 只是它的一个调用方（测试会直接调 `discover`）。
    global _REGISTRY
    _REGISTRY = registry

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
