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
import os
import threading

logger = logging.getLogger(__name__)

#: 开关名 → 说明（面板直接用这份说明渲染"关掉会怎样"）。
FLAG_NOTES: dict[str, str] = {
    "judge_enabled": "判定 agent。关掉＝退回单 agent：判定与回复在同一次调用完成，回复照常。",
    "review_enabled": "风格审核 agent。它**只判断、不改写**：关掉＝她的原稿直接发出，"
                      "连那一道判断也不过。",
    "vision_enabled": "识图。关掉＝有图的消息回到 `[图片]` 占位，不描述内容。",
    "memory_enabled": "记忆维护 agent。关掉＝不再批记忆（已有记忆照常检索）。",
    "compaction_enabled": "对话压缩。关掉＝长对话不再压成摘要，靠活窗口滚动："
                          "压缩请求一次都不发（按阈值触发的那条与切走当值群那条都不发），"
                          "历史原样留着——结果与「压缩失败跳过」一致，只是不烧那次调用。"
                          "环境变量 `QQBOT_COMPACTION=0` 同样能关。",
    "letter_enabled": "写信 / 每日汇报。关掉＝当天不发信，读信回信不受影响。",
    "group_manage_enabled": "群管理动作（踢/禁言/撤回/全员禁言）的总开关。",
    "group_owner_enabled": "群主专属动作（设管理员/名片/群名/头衔/公告）的总开关。",
    "ask_when_unsure": "没把握就别断言：没被叫到又没懂的具体话题，她不出声；"
                       "被叫到、话里却有具体断言又没有根据时，打回让她重写一次。"
                       "关掉＝退回从前：那两条确定性规则都不生效。"
                       "（2026-10-05 晚改口径：旧版是「不懂就问」——以问回应；那条路已拆。）"
                       "这一层一个字都不进常驻 system。",
    "quote_enabled": "金句学习 agent（定期学「她怎么说、什么场合说什么」）。"
                     "关掉＝**不再学**，也**不再往回复里加那几条材料**——"
                     "回复 prompt 与改动前逐字相同。",
    "data_marker_enabled": "不可信数据的边界记号（每轮一个随机短码，包住聊天、资料、"
                           "旧事、插件材料）。关掉＝**不取短码、不包裹、也不中和入站的"
                           "伪造记号**，**DATA 段**与改动前逐字相同；system 里那**两条**"
                           "常量规则（短码那条、时间那条）照旧在——所以是整个请求里"
                           "只有 DATA 段回到从前。",
    "data_marker_cache_friendly": "边界记号的**档位**（2026-10-11 用户定案的两档）。"
                                  "**开（默认）＝缓存友好型**：记号是一组全角生僻符号、"
                                  "**每次压缩**才换一次，于是带记号的材料落进前缀缓存、"
                                  "同一季里逐轮一字不差；"
                                  "**关＝增强型**：每轮一个随机短码，防注入最强（漏一次也"
                                  "只废这一轮），代价是记号每轮都变、它包住的内容每轮断一次。"
                                  "两档共用同一套形状判据与入站中和——切档不会留下绕过。"
                                  "（`data_marker_enabled` 仍是总闸门：关掉就不取记号。）",
}


class RuntimeFlags:
    """十二个开关的可变容器。线程安全够用（面板线程写、asyncio 线程读）。"""

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

    @property
    def quote_enabled(self) -> bool:
        return self.get("quote_enabled")

    @property
    def data_marker_enabled(self) -> bool:
        return self.get("data_marker_enabled")

    @property
    def data_marker_cache_friendly(self) -> bool:
        """边界记号的档位：True = 缓存友好型（一季一个生僻符号组），False = 增强型（每轮短码）。"""

        return self.get("data_marker_cache_friendly")


def _truthy(value: object) -> bool:
    return str(value).strip().casefold() in _TRUE_WORDS


#: 认得的真值 / 假值写法。口径与 `operator_config.BOOL_TRUE` 相同
#: （另外收下 `no`；拼错的一律当"没配"，见 `_env_flag`）。
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _env_flag(name: str, default: bool) -> bool:
    """读一个布尔环境变量，**调用时现读**（与 `dev_config.persona_offset_hours()` 同一纪律）。

    - 没设、或者设成空串 → `default`（"没配"与"配了个空"都当没配）；
    - 认得的值 → 真值解析（`1/true/yes/on` 为真，`0/false/no/off` 为假）；
    - 认不出的值 → `default`（fail-safe：与 `dev_config` 里
      `get(name, "1").lower() not in {"0", "false", "off"}` 那套既有写法同向，
      写错了仍按默认跑，不会因为一个手滑把功能关掉）。

    之所以不用 import 期常量：面板与 `.env` 都要能拉动它（见模块头"每一次调用就读到新值"）。
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    text = str(raw).strip().casefold()
    if not text:
        return default
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


#: `QQBOT_DATA_MARKER_MODE` 认得的两档写法。**两档都认**（`season`/`turn` 是主写法，
#: 其余是"改成大白话也不至于静默退回默认"的同义词）；认不出 / 空 / 没设 → **默认那档**
#: （缓存友好型，与 `_env_flag` 同一条 fail-safe 纪律）。返回 True = 缓存友好型。
_MODE_SEASON_WORDS = frozenset(
    {"season", "cache", "cache_friendly", "cache-friendly", "friendly", "缓存", "缓存友好", "缓存友好型"})
_MODE_TURN_WORDS = frozenset(
    {"turn", "enhanced", "strong", "每次", "每轮", "增强", "增强型"})


def _env_mode(name: str, default: bool) -> bool:
    """读一个**两档**环境变量，`True` = 缓存友好型（`QQBOT_DATA_MARKER_MODE=turn|season`）。

    与 `_env_flag` 同一纪律：**调用时现读**、没配/空串/认不出都回默认。
    为什么要有它而不是复用 `_env_flag`：那一份只认真假词（`1/true/on`），
    而这里的两档是**名字**（`turn`/`season`）——用布尔词给"档位"下配置的写法
    读起来像"要不要开某个功能"，实际问的是"哪一档"，两者混在一起最容易配错。
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    text = str(raw).strip().casefold()
    if not text:
        return default
    if text in _MODE_SEASON_WORDS:
        return True
    if text in _MODE_TURN_WORDS:
        return False
    return default


def build_flags(config=None) -> RuntimeFlags:
    """按当前配置造一份。`config` 给了就先套覆盖层（`operator_config.OperatorConfig`）。

    初值与既有装配口径对齐（`runtime._build_judge_client` 等处的判断条件）：
    `judge_enabled` 取 `DUAL_AGENT_ENABLED`——"判定 agent 存不存在"本来就等于
    "是不是双 agent 模式"，这里不多造一个概念。
    """

    from . import dev_config as _cfg

    values = {
        "judge_enabled": bool(_cfg.DUAL_AGENT_ENABLED),
        # 判据是函数（2026-10-18，契约 D15）：常量是 import 期算的，文件/面板给了
        # review 的 key 也不会让它变 True，面板还会显示成"关"。
        "review_enabled": bool(_cfg.review_enabled()),
        "vision_enabled": bool(_cfg.VISION_ENABLED),
        # 记忆维护的开关不在 dev_config 里（它的启用条件是 key + 允许的群），
        # 所以这里默认 True：面板关掉它才需要一个新的、显式的闸门。
        "memory_enabled": True,
        # 对话压缩：默认开，**这一项从环境变量现读**（`QQBOT_COMPACTION`——
        # 就是 `operator_config.SETTINGS` 给这条开关声明的那个 env 名）。
        # 在这之前它是硬编码 True：面板拉不动，`QQBOT_COMPACTION=0` 也拉不动，
        # 真出事时没有闸门（失败降级审计第 4 条 + 核查的加强版）。
        "compaction_enabled": _env_flag("QQBOT_COMPACTION", True),
        "letter_enabled": bool(_cfg.MAIL_REPORT_ENABLED),
        "group_manage_enabled": bool(_cfg.GROUP_MANAGE_ENABLED),
        "group_owner_enabled": bool(_cfg.GROUP_OWNER_ENABLED),
        # 不懂就问：默认开（用户点名要上的那条）。关掉即退回"被叫到就直接答"。
        "ask_when_unsure": bool(_cfg.ASK_WHEN_UNSURE),
        # 金句学习：默认开（`QQBOT_QUOTE_LEARN=0` 关）。关掉时回复 prompt 与改动前逐字相同。
        "quote_enabled": bool(_cfg.QUOTE_LEARN_ENABLED),
        # 不可信数据的边界记号：默认开（`QQBOT_DATA_MARKERS=0` 关）。
        # 关掉时不取短码、不包裹、不中和——请求与改动前逐字相同。
        "data_marker_enabled": bool(_cfg.DATA_MARKERS_ENABLED),
        # 边界记号的**档位**：默认缓存友好型（`QQBOT_DATA_MARKER_MODE=turn` 切增强型）。
        # 这一项**从环境变量现读**（与 `compaction_enabled` 同一理由：面板要能拉动它）。
        "data_marker_cache_friendly": _env_mode("QQBOT_DATA_MARKER_MODE", True),
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


def install(flags: RuntimeFlags | None) -> None:
    """装配点装一份进程内共用的；**传 `None` = 卸掉它**。

    "卸掉"是给"跑完一段真正装配之后的复原"用的：`runtime.build_engine()` 每次都会
    `install(build_flags(...))`，而这是一份**进程级单例**——直接造出来的引擎
    （`DialogueEngine(...)` 的 `_flags()` **优先读共用那份**）会跟着它走。

    2026-10-08 实测到的坑：`tests/test_memory.py` 里那次真装配把
    `DUAL_AGENT_ENABLED` patch 成 False，于是共用那份的 `judge_enabled=False`
    留在进程里，后面**显式指定顺序**同进程跑的 `test_affinity_behavior.py`
    就整轮不跑判定了（红 3 条 + 1 条 error；整套按文件名字母序跑时 affinity 在
    memory 之前，所以一直没暴露）。

    所以：**谁改了它，谁负责还回去**（装配过的测试在 `finally` 里 `install(旧的)`）。
    """

    global _SHARED
    _SHARED = flags


_SHARED: RuntimeFlags | None = None
