from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MessageTarget:
    """一条回复的目标，目前只支持私聊和群聊。

    `temp_group_id` 是**私聊的一条附带信息**，不是第二个目标：它记的是"这条私聊
    是从哪个群发起的"（QQ 的群临时会话）。QQ 那边私聊实际有两条路——

    * 好友私聊（`c2c`）；
    * 群临时会话（`grpTmp`），协议里必须带上**发起那个群的群号**。

    对方不是好友时只有后一条能送出去：不带群号就按好友私聊发，服务端会拒成
    `result=16 发送失败，请先添加对方为好友`（2026-10-26 实测，`run/data/bot.err.log`）。
    所以这个字段**只影响"私聊怎么发"**，不改变 `MessageTarget` 仍是私聊这件事
    （`user_id` 与 `group_id` 的二选一校验照旧，避免调用方把它当群消息发出去）。
    """

    user_id: str | None = None
    group_id: str | None = None
    temp_group_id: str | None = None

    def __post_init__(self) -> None:
        if (self.user_id is None) == (self.group_id is None):
            raise ValueError("MessageTarget 必须且只能设置 user_id 或 group_id")
        if self.temp_group_id is not None and self.user_id is None:
            # 群临时会话是"私聊"的一种，脱离 user_id 没有意义；允许这种组合
            # 只会让发送端把群号当成群消息目标发出去。
            raise ValueError("temp_group_id 只能与 user_id 一起使用")


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """从 QQ 输入到人格核心的最小消息结构。"""

    message_id: str
    session_id: str
    user_id: str
    text: str
    target: MessageTarget
    is_bot_mentioned: bool = False
    sender_role: str = "unknown"
    sender_name: str = ""
    # 这条消息的**群名片**（群昵称）。身份口径仍以 `sender_name`（QQ 昵称）为准，
    # 这一份只作为**别称**留着：群里人会用名片上的名字指代他（"@旧名甲"、"旧名甲说的"）。
    # 见 `card_from_sender` 与 `ConversationState.note_speaker`。
    sender_card: str = ""
    is_bot_message: bool = False
    # 这条**不是任何人说的话**，而是"这里发出来的通知"（2026-10-19 用户：
    # 「哎要不还是通知吧，不要当人了」）。
    #
    # 与 `is_bot_message` 的区别：那是**她自己**说过的话（渲染成 `speaker="yunru"`），
    # 这一条是**她这条通道替别人发出去的东西**（插件通知 / 命令回复），渲染成
    # `speaker="notice"`。
    #
    # 三条随之而来的规矩（都写在 `stage3_runtime.ConversationState` 那一侧）：
    # 不进名册、不占名册编号（`user_id` 是空串）、不许被 `<reply_to>` 引用。
    # 正文仍然是**不可信 DATA**，渲染时照旧 sanitize。
    is_notice: bool = False
    # 本条消息引用的上一条消息 ID（OneBot reply 段），没有则为空串。
    reply_to_message_id: str = ""
    # 本条消息 @ 到的其他人（不含机器人自己）。命令要用它当参数，
    # 例如 `/super addadmin @某人`。
    mentioned_user_ids: tuple[str, ...] = ()
    # 只包含非文本媒体（图片、表情等）时为 True；此时 text 是媒体占位描述。
    has_media: bool = False
    # 图片段的地址（给识图用，2026-09-30）。**不下载、不上传**：地址原样交给模型厂商。
    # 别的媒体（表情/语音/视频）不进这里——只有图片那条路能看图。
    media_urls: tuple[str, ...] = ()
    # 与 `media_urls` 一一对应的身份：`sticker`（表情包）或 `image`（照片）。
    # **表情包也是 image 段**（NapCat 实测，见 `media_segments`），所以识图要知道
    # 该按贴图还是按照片去描述，不能都当照片。
    media_kinds: tuple[str, ...] = ()
    #: 这条消息**从哪个渠道进来**。空串 = QQ 传输层（`onebot_ws` 那条路）；
    #: 非空 = 插件经 `runtime.deliver` 投进来的（目前只有 `"mail"`）。
    #:
    #: 2026-10-20 加。起因：邮件里正常的一句话被 `check_message_security` 判成
    #: "要求访问本机/系统"、直接拦掉（实测两封信都被拦，她根本没看到正文）。
    #: 用户当天定：**「mail 又碰不到核心」** —— 插件渠道进来的只是一段文字，她既没有
    #: 工具、也执行不了命令；特权命令的形状在 `deliver` 那一步就已经被拒了。所以那道
    #: 本机信息闸只该对 QQ 生效。判据用这个字段，**不要在别处嗅 `session_id` 前缀**。
    channel: str = ""


def display_name_from_sender(sender: object, *, fallback: str = "") -> str:
    """从 OneBot 的 `sender` 对象里取**用来显示的那个名字**：优先 QQ 昵称。

    2026-10-01 用户定的口径："应当用qq昵称，多群一个人多个群名片怎么用群昵称啊"。

    理由（真机实测，群 800000001 里 59 人有 **42 人**名片与昵称不同）：

    - **群名片是"每个群一份"的**，同一个人在 A 群叫"在不在不在"、B 群可能叫别的；
      拿它当身份，跨群就对不上号——记忆、名册、检索都会跟着漂。
    - **群名片是随时会改的**：实测同一个人隔几条消息名片就从"桓珩"变成"洹桁"。
    - **QQ 昵称是账号级的**，跨群一致（实测 0 人缺 `nickname`）。

    所以显示、记名、喂给模型的说话人名一律走这里，**只有 `nickname` 缺失时**才退回
    `card`（宁可显示个会变的名片，也别把说话人显示成空）。群名片本身没被丢掉：
    "改群名片"那条命令、以及按名字找人的匹配（`stage3_main` 的 relay）照旧用它。
    """

    if not isinstance(sender, dict):
        return fallback
    for key in ("nickname", "card"):
        value = sender.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def card_from_sender(sender, fallback: str = "") -> str:
    """这条消息的**群名片**（群昵称）——跟显示名分开取。

    为什么要单独留一份（2026-10-02 用户："读取群消息的时候艾特信息等才到群昵称里去对应"）：
    群名片虽然在**身份**上不能当准（口径见 `display_name_from_sender`），
    但它**是群里人实际会用来指代那个人的名字**——有人打"@旧名甲"、有人转述"旧名甲说的"。
    丢掉它，这些说法就没人能对上号。

    所以：**显示/记名用 QQ 昵称，群名片作为「别称」留下**，
    让"旧名甲"这种说法仍然能对到同一个人（见 `ConversationState.note_speaker`）。
    """

    if not isinstance(sender, dict):
        return fallback
    value = sender.get("card")
    return value.strip() if isinstance(value, str) and value.strip() else fallback


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """一条待发送的正文，可选引用某条消息。

    **默认是"工具性响应"（paced=False，立刻送出）**，这是刻意选的方向：
    忘记声明时只会少一点拟人化，不会让命令白等一秒多。角色回复必须显式写
    `paced=True`——它只有一处产出点，写错会立刻被测试发现。

    这个默认值曾经是反的（paced=True）：`/admin help` 就因此漏设过，
    命令回复白等了停顿，而漏设是静默的、没人会注意到。

    `typing_notice` 非空时，发送方可以先向目标暴露一次"正在输入"状态。
    它是可选的：传输层不支持时静默跳过，不影响正文送达。
    """

    target: MessageTarget
    text: str
    reply_to_message_id: str = ""
    typing_notice: str = ""
    paced: bool = False
    # 这一条是**命令的回复**（通知），不是她自己的话（2026-10-19 用户：
    # 「这个也进，一起当通知」）。
    #
    # 它只影响"**发出去之后怎么记进她的历史**"：`is_notice=True` 的那一批由
    # `_deliver_reply` 记成 `speaker="notice"`（且进历史的那一份先过
    # `notice_text.for_history`：脱敏 + 截断 200 字）。
    # **发出去的那一份一个字都不变**——操作者该看到的还是命令的完整输出。
    #
    # 默认 `False` 与 `paced` 同一个方向的考虑：漏写只会让一条命令回复被当成
    # 她自己的话（可以接受的退化），不会让正常对话被记成通知。
    is_notice: bool = False


class MessageNotDelivered(RuntimeError):
    """**确定没送出去**，可以安全重发。

    判据是"这条消息根本没走到对面"：连接不在、等连接超时、或写入之前就断了。
    用户 2026-09-28 要的"没发出去的消息等能发了再发"就建立在这一类上——
    `outbox.py` 只重发这个类，别的失败类型一律不重发。
    """


class DeliveryUncertain(RuntimeError):
    """发出去了、但没收到回执（超时/连接在途中断掉）。

    重发可能造成**重复**（对面其实收到了），所以 `Outbox` 不自动重发，只记日志。
    宁可少一句，也不要她同一句话在群里出现两遍。
    """


class DeliveryRejected(RuntimeError):
    """OneBot 明确回了失败（`retcode != 0`，例如被禁言、不是好友）。

    这不是"暂时发不出去"，是**对面不要**：重发没有意义，反复重试只是噪音。
    """


# 私聊被对面拒收时，有**两类原因**是能解释给操作者听的（2026-10-08 实测，
# 来源见 `docs/research/snowluma-api-2026-10-08.md`）：
#
# - 好友私聊那条路：对方不是好友（服务端原话"发送失败，请先添加对方为好友"）；
# - 群临时会话那条路：对方此前从没在这个群里跟她说过话（对面原话 `no such temp session`，
#   这道门在对方本地：收到过才给回）。
#
# 两句话都按**操作者视角**写（"该怎么办"），不出现任何实现词——口径见 `AGENTS.md` §2.2，
# 测试 `tests/test_onebot_ws.py` 用 `prompt_guard` 逐词钉住。
# **这里只做说明**：认出来也不改发送结果、不重发、不放宽任何限制。
_PRIVATE_REFUSAL_NOTES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("请先添加对方为好友", "不是好友"),
        "这条私聊没发出去：对方还不是好友。让他先给云茹发一句话，或者加好友。",
    ),
    (
        ("no such temp session",),
        "这条私聊没发出去：对方还没在这个群里跟云茹说过话。"
        "让他在群里先对她说一句，之后她才能回他。",
    ),
)


def private_send_refusal_note(text: str) -> str:
    """把"私聊被拒收"的原始回执翻成一句人话；认不出来就返回空串。

    认出哪种原因**不改任何东西**：发送照样已经失败、该抛的照样抛、重不重发由
    `Outbox` 按原来那三分类决定。调用方拿到这句话只做一件事——写给操作者看
    （见 `onebot_ws._note_refusal`）。
    """

    haystack = (text or "").casefold()
    for markers, note in _PRIVATE_REFUSAL_NOTES:
        if any(marker.casefold() in haystack for marker in markers):
            return note
    return ""


def is_missing_temp_session(exc: BaseException) -> bool:
    """这次拒收是不是"群临时会话已经没了"（NapCat 重启或超过 7 天没再收到）。

    只有这一种情况可以退回好友私聊重试；别的拒收（被禁言、真不是好友）退回也没用，
    重试只会多一帧噪音。判据就是 NapCat 的原话，见上面 `_PRIVATE_REFUSAL_NOTES`。
    """

    return isinstance(exc, DeliveryRejected) and "no such temp session" in str(exc).casefold()


def drop_temp_session_params(params: dict[str, object], exc: BaseException) -> dict[str, object] | None:
    """临时会话失效时，把一帧私聊请求退回"好友私聊"（去掉 `group_id`）。

    返回 None 表示**不该重试**（不是临时会话的错、或这一帧本来就不是私聊）。
    这么做是为了"不会比修复前更差"：对方若是好友，去掉群号照样能送到；
    若不是好友，第二帧会以"请先添加对方为好友"如实失败，操作者看到的仍是那句话。
    """

    if not is_missing_temp_session(exc):
        return None
    if params.get("message_type") != "private" or "group_id" not in params:
        return None
    fallback = dict(params)
    fallback.pop("group_id", None)
    return fallback


def note_private_send_refusal(target: MessageTarget, exc: BaseException) -> bool:
    """私聊被**有理由地**拒收时，给操作者补一句人话（一条 WARNING）；返回有没有打。

    由来（2026-10-08 实测）：给非好友发私聊，对面的回执是一句劝告式的话
    （"请先添加对方为好友" / `no such temp session`），操作者在日志里只看得到
    "Stage 3 reply send failed" 加一串回执原文，看不出下一步该做什么。

    **两条出站通道都走这里**（反向 WS 的 `onebot_ws`、客户端的 `onebot_client`）：
    判据与说法只有一份，免得两边漂移——这也是 `AGENTS.md` §2.3"共用模块的改动必须
    同时服务两边"的意思。

    **只是说明**：不改失败分类、不重发、不放宽任何限制，异常由调用方照原样抛。
    只在私聊目标上做：群消息撞不出这两类原因，打了只是噪音。
    """

    if target.group_id is not None or not isinstance(exc, DeliveryRejected):
        return False
    note = private_send_refusal_note(str(exc))
    if not note:
        return False
    logger.warning("%s（私聊对象：%s）", note, target.user_id or "未知")
    return True


class QQTransport(Protocol):
    """QQ 通信层只有输入和输出两个接口。"""

    async def receive(self) -> IncomingMessage | None:
        """等待下一条消息；连接关闭时返回 None。"""

    async def send(self, target: MessageTarget, text: str, *, reply_to: str = "") -> None:
        """向目标发送一条纯文本消息；reply_to 非空时引用对应消息。

        失败要**分类抛出**：`MessageNotDelivered`（没送出去，可重发）、
        `DeliveryUncertain`（不知道，别重发）、`DeliveryRejected`（对面拒收，别重发）。
        不分类地抛一个笼统异常，会让"能不能安全重发"这个判断落到猜上面。
        """

    async def send_segmented(self, target: MessageTarget, text: str, *, reply_to: str = "",
                             part: int | None = None, total: int | None = None,
                             origin: str = "") -> None:
        """与 `send` 一样发一条，额外告诉传输层"这是第几段 / 共几段 / 从哪一轮来"。

        **这三个事实只有调用方知道**：传输层看到的是一条独立的消息，拆成几段是
        上游（`stage3_main._deliver_reply`）干的。对话日志（`chat_log.py`）要的正是
        它们——不然"群里实际收到的那三条"在日志里分不出来，也跟 `reply.jsonl` 对不上。

        这是**可选能力**：没有这个方法的传输层照样能用，调用方必须自己退回 `send`
        （`_deliver_reply` 就是这么做的，与 `send_typing` 同一套规矩）。
        日志信息少一点可以接受，**消息发不出去不行**。
        """

    async def send_typing(self, target: MessageTarget, notice: str = "typing") -> None:
        """尽力而为地广播一次"正在输入"状态。

        这是可选能力：不支持的实现可以什么都不做，调用方必须容忍失败，
        绝不能因为状态提示失败而影响正文发送。
        """
