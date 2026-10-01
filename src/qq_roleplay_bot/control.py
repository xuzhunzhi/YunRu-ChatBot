from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """供未来控制台使用的脱敏会话摘要，不包含聊天历史。"""

    session_id: str
    mode: str
    history_size: int
    topic: str
    topic_status: str
    # 会话是否已持久化到本地状态库（重启后是否仍会保留）。
    persisted: bool = False


@dataclass(frozen=True, slots=True)
class EngineSnapshot:
    """未来 WebUI 的只读状态模型；不包含 API Key 或完整聊天内容。"""

    enabled: bool
    target_group_id: str
    enabled_group_ids: tuple[str, ...]
    sessions: tuple[SessionSnapshot, ...]
    accepted_messages: int
    ignored_messages: int
    blocked_messages: int
    deferred_messages: int
    model_calls: int
    replies: int
    history_seeded: int = 0
    # 出站消息被拆成的总段数；与 replies 之比反映"分几句说"的实际情况。
    reply_segments: int = 0
    # 判定 agent 的调用次数。双 agent 结构下与 model_calls 分开计：
    # model_calls 是回复调用，judge_calls 是判定调用。
    judge_calls: int = 0
    # 对话压缩次数。攒满保留上限时触发，把旧对话压成摘要。
    compaction_calls: int = 0
    # 多群焦点的观测计数：排进队列、回了多少"稍等"、切换了几次当值群、
    # 被排队消息最后真的回了多少条、以及答应过"稍等"却没给出结果的条数（异常，应为 0）。
    focus_queued: int = 0
    focus_acks: int = 0
    focus_switches: int = 0
    focus_queued_replies: int = 0
    focus_declined_promised: int = 0
    # 判定说"要回"、回复 agent 却空手而归的次数。双 agent 下它**没有否决权**，
    # 所以这是模型抽风的异常计数（应当为 0），不是一种正常结果。
    empty_forced_reply: int = 0
    # 关系：判定报了几次"这一句越界"（当轮升防备），以及其中被机械上限拒了几次。
    # 前者是"她当场变冷"的次数，后者应当很少（上限是防模型抽风的）。
    guard_raised: int = 0
    guard_rejected: int = 0
    # 风格审核改写的条数（没配审核时恒为 0）。
    reply_reviewed: int = 0
    # 识图成功把 `[图片]` 换成描述的条数（没配识图时恒为 0）。
    media_described: int = 0
    # 当前当值状态：热群、当值多久、排队深度。只有数字与群号，没有正文。
    focus: dict[str, object] | None = None
    memory: dict[str, object] | None = None
    # 脱敏运行指标：固定类别的计数与耗时分布，不含正文、QQ 号或凭据。
    metrics: dict[str, object] | None = None
    # 各功能的输入输出日志状态（判定/回复/记忆/规则）：只有计数、行数、字节数与
    # 最后几条的**预览**，正文只在 data/logs/ 的文件里。
    model_trace: dict[str, object] | None = None
    # 本次进程启动的时间与内存占用（字节）。给 `/super status` 用。
    started_at: float = 0.0
    memory_bytes: int = 0
    memory_peak_bytes: int = 0
    persistence_enabled: bool = False


class EngineControl(Protocol):
    """未来 WebUI/API 可适配的最小控制端口。

    实际 WebUI 必须另行实现认证、CSRF 防护和本机/内网访问策略；这个端口本身不是认证层。
    """

    def snapshot(self) -> EngineSnapshot:
        """读取不含密钥和完整聊天历史的运行状态。"""

    def set_enabled(self, enabled: bool) -> None:
        """启停消息处理；不改变白名单或安全策略。"""

    def leave_session(self, session_id: str) -> bool:
        """清除指定短期会话并返回它是否存在。"""

    def enable_group(self, group_id: str) -> bool:
        """启用指定群聊；返回它是否由未启用变为启用。"""

    def disable_group(self, group_id: str) -> bool:
        """停用指定群聊；返回它是否由已启用变为停用。"""
