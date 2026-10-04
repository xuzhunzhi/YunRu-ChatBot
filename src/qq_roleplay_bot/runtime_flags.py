"""运行期开关：面板能改、**下一次调用就读到新值**的那几个（`applies="live"`）。

由来（2026-10-01 用户）："能热更的就热更，其余表单标'需重启'"。所以要有一份
**活的**开关对象，而不是散在 `dev_config` 里的 import 期常量：

- `dev_config.X` 在 import 时求值一次，改环境变量不会回头改它——那是"需重启"的默认形态；
- 这里这份每处调用点**现读**，所以面板一改就生效。

纪律：

1. **每个开关只守一个调用点**，位置写在这份文件里（找开关时不用翻遍代码）。
2. **关掉一律 fail-safe**：不抛错、不改数据、只在日志记一行 `agent_disabled name=…`。
   关掉判定 agent 不会让她不说话——那只表示"退回单 agent"，那条路一直存在。
3. 初值从 `dev_config` 取（也就是从 `.env`/环境变量取），覆盖层由 `runtime` 装配时套上。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

#: 开关名 → 说明（面板直接用这份说明渲染"关掉会怎样"）。
FLAG_NOTES: dict[str, str] = {
    "judge_enabled": "判定 agent。关掉＝退回单 agent：判定与回复在同一次调用完成，回复照常。",
    "review_enabled": "风格审核 agent。关掉＝她的原稿直接发出，不再过那道校对。",
    "vision_enabled": "识图。关掉＝有图的消息回到 `[图片]` 占位，不描述内容。",
    "memory_enabled": "记忆维护 agent。关掉＝不再批记忆（已有记忆照常检索）。",
    "compaction_enabled": "对话压缩。关掉＝长对话不再压成摘要，靠活窗口滚动。",
    "letter_enabled": "写信 / 每日汇报。关掉＝当天不发信，读信回信不受影响。",
    "group_manage_enabled": "群管理动作（踢/禁言/撤回/全员禁言）的总开关。",
    "group_owner_enabled": "群主专属动作（设管理员/名片/群名/头衔/公告）的总开关。",
    "ask_when_unsure": "不懂就问：没把握时先问一句、具体专业话题没有根据时不许断言。"
                       "关掉＝退回从前：被叫到就直接答，问与不问不再由核心定。",
}


class RuntimeFlags:
    """九个开关的可变容器。线程安全够用（面板线程写、asyncio 线程读）。"""

    __slots__ = ("_values", "_lock")

    def __init__(self, **values: bool) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, bool] = {name: True for name in FLAG_NOTES}
        self._values.update({name: bool(value) for name, value in values.items()
                             if name in FLAG_NOTES})

    def get(self, name: str) -> bool:
        """读一个开关。**未注册的名字一律 True**（fail-safe：不认识就照旧跑）。"""

        with self._lock:
            return bool(self._values.get(str(name), True))

    def set(self, name: str, value: bool) -> None:
        key = str(name)
        if key not in FLAG_NOTES:
            raise KeyError(key)
        with self._lock:
            before = self._values.get(key)
            self._values[key] = bool(value)
        if before is not bool(value):
            logger.warning("agent_disabled name=%s enabled=%s", key, bool(value))

    def snapshot(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._values)

    # 便捷读法：调用点写 `flags.judge_enabled` 比 `flags.get("judge_enabled")` 清楚。
    @property
    def judge_enabled(self) -> bool:
        return self.get("judge_enabled")

    @property
    def review_enabled(self) -> bool:
        return self.get("review_enabled")

    @property
    def vision_enabled(self) -> bool:
        return self.get("vision_enabled")

    @property
    def memory_enabled(self) -> bool:
        return self.get("memory_enabled")

    @property
    def compaction_enabled(self) -> bool:
        return self.get("compaction_enabled")

    @property
    def group_manage_enabled(self) -> bool:
        return self.get("group_manage_enabled")

    @property
    def group_owner_enabled(self) -> bool:
        return self.get("group_owner_enabled")

    @property
    def ask_when_unsure(self) -> bool:
        return self.get("ask_when_unsure")


def _truthy(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def build_flags(config=None) -> RuntimeFlags:
    """按当前配置造一份。`config` 给了就先套覆盖层（`operator_config.OperatorConfig`）。

    初值与既有装配口径对齐（`runtime._build_judge_client` 等处的判断条件）：
    `judge_enabled` 取 `DUAL_AGENT_ENABLED`——"判定 agent 存不存在"本来就等于
    "是不是双 agent 模式"，这里不多造一个概念。
    """

    from . import dev_config as _cfg

    values = {
        "judge_enabled": bool(_cfg.DUAL_AGENT_ENABLED),
        "review_enabled": bool(_cfg.REVIEW_ENABLED),
        "vision_enabled": bool(_cfg.VISION_ENABLED),
        # 记忆维护的开关不在 dev_config 里（它的启用条件是 key + 允许的群），
        # 所以这里默认 True：面板关掉它才需要一个新的、显式的闸门。
        "memory_enabled": True,
        "compaction_enabled": True,
        "letter_enabled": bool(_cfg.MAIL_REPORT_ENABLED),
        "group_manage_enabled": bool(_cfg.GROUP_MANAGE_ENABLED),
        "group_owner_enabled": bool(_cfg.GROUP_OWNER_ENABLED),
        # 不懂就问：默认开（用户点名要上的那条）。关掉即退回"被叫到就直接答"。
        "ask_when_unsure": bool(_cfg.ASK_WHEN_UNSURE),
    }
    if config is not None:
        stored = getattr(config, "values", {}) or {}
        for name in list(values):
            if name in stored:
                values[name] = _truthy(stored[name])
    return RuntimeFlags(**values)


def shared() -> RuntimeFlags | None:
    """进程内共用的一份；没装配时返回 None（调用点据此照旧跑）。"""

    return _SHARED


def install(flags: RuntimeFlags) -> None:
    global _SHARED
    _SHARED = flags


_SHARED: RuntimeFlags | None = None
