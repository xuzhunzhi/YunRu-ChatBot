from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class MessageTarget:
    """一条回复的目标，目前只支持私聊和群聊。"""

    user_id: str | None = None
    group_id: str | None = None

    def __post_init__(self) -> None:
        if (self.user_id is None) == (self.group_id is None):
            raise ValueError("MessageTarget 必须且只能设置 user_id 或 group_id")


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

    async def send_typing(self, target: MessageTarget, notice: str = "typing") -> None:
        """尽力而为地广播一次"正在输入"状态。

        这是可选能力：不支持的实现可以什么都不做，调用方必须容忍失败，
        绝不能因为状态提示失败而影响正文发送。
        """
