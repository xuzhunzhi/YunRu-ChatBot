"""开发阶段配置；凭据只能从环境变量或配置文件读取。

优先级：真实环境变量 > 配置文件 > 代码内默认值。

配置文件默认是项目根目录的 `.env`。**设置 `QQBOT_ENV_FILE` 可以整体换掉它**
（不是合并）——这样 Bot 和开发环境能各用一份 key，互不干扰：

    QQBOT_ENV_FILE=.env.bot

之所以不做合并：两份配置文件里出现同名键时，"哪份生效"会变得难以预测，
而凭据类配置最怕的就是"我明明改了却没生效"。

`.env` 及任何 `.env.*` 都已在 .gitignore 中忽略，绝不要把真实密钥写进源码、文档或提交记录。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = _PROJECT_ROOT / ".env"
ENV_FILE_VARIABLE = "QQBOT_ENV_FILE"


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：支持 KEY=VALUE、# 注释、可选引号；不引入第三方依赖。"""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_env_file() -> Path:
    """当前生效的配置文件路径。

    `QQBOT_ENV_FILE` 为空或不存在时回落到默认 `.env`。**不合并两份文件**：
    同名键由哪份生效必须一眼可知，否则凭据改了却没生效会很难查。
    """

    configured = os.environ.get(ENV_FILE_VARIABLE, "").strip()
    if not configured:
        return DEFAULT_ENV_FILE
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = _PROJECT_ROOT / candidate
    if not candidate.is_file():
        # 显式指定却不生效是最坏的情况：说清楚，然后回落到默认文件。
        logger.warning(
            "%s 指向的配置文件不存在，回落到默认 .env: %s",
            ENV_FILE_VARIABLE,
            candidate,
        )
        return DEFAULT_ENV_FILE
    return candidate


def load_env_file(path: Path | None = None) -> int:
    """把配置文件中的键写入 os.environ（不覆盖已存在的真实环境变量）。

    返回注入的键数量。这样 `QQBOT_MEMORY_MODEL` 等只从 os.environ 读取的配置
    也能统一从配置文件生效。
    """

    target = path or resolve_env_file()
    values = _parse_env_file(target)
    injected = 0
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            injected += 1
    if target != DEFAULT_ENV_FILE:
        logger.info("已从 %s 载入配置（%s 个键）", target.name, injected)
    return injected


# 面板写的运行配置覆盖层要先于 `.env` 注入：两处都遵守"不覆盖已存在的 key"，
# 所以谁先进 `os.environ` 谁赢。优先级（2026-10-01 用户定的）是
# `operator_config.json > 真实环境变量 > .env > 代码默认`。
# 延迟导入：`operator_config` 不该依赖这个模块，反过来也不该在 import 期成环。
def _inject_operator_config() -> int:
    try:
        from . import operator_config

        return operator_config.inject()
    except Exception:  # noqa: BLE001 - 覆盖层坏了绝不能让 bot 起不来
        logger.warning("运行配置覆盖层注入失败，按 .env 运行", exc_info=True)
        return 0


_inject_operator_config()
load_env_file()


def get(name: str, default: str = "") -> str:
    """读取配置；真实环境变量优先，其次是已被 load_env_file 注入的 .env 值。"""

    return os.environ.get(name, default).strip()


# --- 模型 / 凭据 / 用途：三层配置（2026-10-06 重做）---------------------------
#
# 现场事故：`.env` 里全局地址指着 mimo、主 key 是 mimo 那把，而判定 / 记忆 / 审核的
# key 是 deepseek 那把 —— **地址与 key 来自两个不同的地方**，于是那三个子系统
# 拿 deepseek 的 key 去打 mimo，全 401。根因是形状，不是某个人配错了。
#
# 现在那件事归 `model_config`：providers（只放连接信息）/ keys（按用途分开、
# 每条显式绑定属于哪家）/ tasks（用途 → 哪条 key + 哪个模型名）。
# 一个客户端的三个字段**永远由同一条 key 条目解析出来** —— 见那个模块的说明。
#
# 这个文件里保留下来的东西是**兼容层**：下面这些名字改动前就在用，现在降级成
# "默认值"那一档（新配置缺失时行为与改动前**逐字相同**，有测试钉住）。
# 装配点不再直接读它们，而是调 `model_config` —— 单独留常量只会让旧读法复活。
from . import model_config as _model_config  # noqa: E402 - 见上：这一段是兼容层的说明


def _compat(name: str, fallback: str) -> str:
    """兼容档取值：环境里没有这一项时用 `.env` 带入的那份（与改动前一致）。

    `os.environ` 与"`.env` 的值"在改动前是同一条路（`load_env_file` 把后者填进前者），
    但**测试会从环境里删掉某一项**来验"回落"，那时常量里那份仍在——所以这里要
    两级都看，否则"清空某一项回落到 `.env`"会变成"回落不到"。给 `agent_api_key`
    这类历史读法用的。
    """

    return os.environ.get(name, "").strip() or str(fallback or "").strip()


def agent_api_key(env_name: str) -> str:
    """按 agent 取 key（**兼容层，历史语义**）：没配就回落用主 key。

    ⚠️ 装配点**不再用它**（核心改调 `model_config` 的用途解析）。留着它是因为
    `tests/` 与外部部署在按它断言/取值，而且它的语义就是"改动前那一套"。
    新代码要某一路的凭据，用 `model_config.resolve(用途).api_key`。

    **分 key 不等于分会话隔离**：同一个 DeepSeek 账号下的多把 key 共享并发限额与缓存
    容量，KVCache 隔离靠请求里的 `user_id`（与 key 无关）。分 key 的实际意义是
    "某一环出问题只吊销那一把"；如果是三个不同账号，那才真的分开配额与账单。

    调用时才读环境，所以测试可以直接改环境变量验证回落行为。
    """

    return (get(env_name, "") or _compat("QQBOT_API_KEY", API_KEY)
            or os.environ.get("API_KEY", ""))


# 兼容档的那份取值（`.env` 与真实环境变量都算，`get()` 已经把它们合并好了）。
_ENV_API_BASE_URL = get("QQBOT_API_BASE_URL", "")
_ENV_API_MODEL = get("QQBOT_API_MODEL", "")
_ENV_API_KEY = get("QQBOT_API_KEY", os.environ.get("API_KEY", ""))

# 把**主 key 那一对**所绑定/所用的值显式放进 `os.environ`：`model_config` 只从环境读，
# 而"只配一把 key 的部署 / 只有 `.env` 的机器"必须能解析出那一对（改动前就是靠
# `load_env_file` 填进环境的）。只补**没有**的键，绝不覆盖已有的值——
# 优先级因此仍是 `运算符层 > 真实环境变量 > .env > 代码默认`。
#
# ⚠️ 为什么只补 key、不补地址与模型：地址与模型有"任务级覆盖"那一档，如果在这里
# 把全局值写进环境，`_reapply_reply_overrides` 那种"看环境里有没有专属值"的判断
# 就会把全局值误当成专属值。key 没有这个问题（它的回落路径本来就是读环境）。
if _ENV_API_KEY and not os.environ.get("QQBOT_API_KEY"):
    os.environ["QQBOT_API_KEY"] = _ENV_API_KEY

#: 全局那一套的**最终默认值**（兼容档没配时用的）。地址取 `model_config` 里
#: 代码默认那一家，模型名写**真名**（`deepseek-chat` 是随时会没的别名）。
API_BASE_URL = _ENV_API_BASE_URL or _model_config.provider_base_url("deepseek")
API_MODEL = _ENV_API_MODEL or _model_config.TASKS["judge"]["model"]
API_KEY = _ENV_API_KEY
# 三把 key 各管一摊：回复（API_KEY）、判定、记忆维护。后两者不配就回落主 key。
JUDGE_API_KEY = agent_api_key("QQBOT_JUDGE_API_KEY")
MEMORY_API_KEY = agent_api_key("QQBOT_MEMORY_API_KEY")
TARGET_GROUP_ID = "717151356"


# --- 回复 agent 能不能单独换一套地址 / 模型 / key（2026-10-05）------------------
#
# 用户要求：**每个 agent 各用各的 base URL/key**。这一节现在是 `model_config` 里
# **reply 那个用途**的兼容视图（`QQBOT_REPLY_API_*` 就是它的任务级覆盖变量名）：
#
#     地址 = model_config.resolve("reply").base_url
#           （任务级覆盖 → 全局地址 → **reply 那条 key 所绑的 provider 的地址**）
#     模型 = 任务级覆盖 → 全局模型 → `tasks["reply"]` 里的真名
#     key  = QQBOT_REPLY_API_KEY → 主 key
#
# **缺省行为逐字不变**：三个都没配（或配了空串）时，回复 agent 拿到的仍是
# 全局那一套——只配一把 key 的部署、干净 clone、测试全都照旧。
#
# 写成函数而不是 import 期常量：面板热更与测试都靠"**调用时现读**环境"
# （与 `agent_api_key` 同一条纪律）。空串一律当"没配"。


def reply_api_base_url() -> str:
    """回复 agent 的地址（= `model_config` 里 reply 那个用途的解析结果）。"""

    return _model_config.resolve("reply").base_url


def reply_api_model() -> str:
    """回复 agent 的模型名（任务级覆盖 → 全局模型 → `tasks` 里的真名）。"""

    return _model_config.resolve("reply").model


def reply_api_key() -> str:
    """回复 agent 的 key：专属的没配就回落主 key（`QQBOT_API_KEY`）。

    **空串要当成"没有设置"**：环境里留一个空值会把 `.env` 的填充挡在门外
    （`load_env_file` 不覆盖已存在的键），2026-10-01 出过一次这样的事故——
    回复 agent 的 key 变空、模型全 401。所以最后那一项是 `.env` 带入的常量。
    """

    return (_model_config.resolve("reply").api_key
            or _compat("QQBOT_API_KEY", API_KEY) or get("API_KEY", ""))


def _csv_ids(name: str) -> frozenset[str]:
    """把一个逗号分隔的环境变量读成 id 集合（空 → 空集合）。

    ⚠️ **必须定义在本文件靠前的位置。** 这个文件的助手函数都是"用到之前先定义"，
    2026-10-02 我把 `_csv_ids` 的**使用**写在了它的定义之前，`import` 直接
    `NameError: name '_csv_ids' is not defined`——而这个文件 L218 附近那条注释
    记的正是同一个坑（"踩过一次"）。我踩了第二次，所以把定义搬到使用点之前。
    """

    raw = get(name, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


# 谁能用 `/super` 与 `/admin`。
#
# ## ⚠️ 为什么改成读环境（2026-10-02，一次真实的权限丢失）
#
# 这两行原来是**硬编码**的（`frozenset({"900000001"})`），而这是个**被 git 跟踪**的文件。
# 于是部署机上唯一能让真号生效的办法是**改这个文件**——一处未提交的本地修改。
# 后果在 2026-10-02 真实发生：一次 `git checkout`（换分支）把它**还原成仓库里的占位号**，
# 超管当场失去全部 `/super` 权限，而且**没有任何告警**（`900000001` 谁也不是）。
# 证据：`dev_config.py` 的修改时间与那条 `checkout` 的 reflog 时间**同一秒**。
#
# 现在真号放 `.env`（`.gitignore:7` 忽略、永不进仓库、**换分支冲不掉**）：
#
#     QQBOT_SUPER_ADMIN_USER_IDS=你的号[,第二个号...]
#     QQBOT_ADMIN_USER_IDS=你的号[,第二个号...]
#
# 兜底仍是那个占位号：**测试与干净 clone 的行为一个字节都没变**
# （很多测试拿 `900000001` 当 owner；`run_offline.py` 也会显式钉住它，
# 免得测试跟着这台机器的 `.env` 跑）。
SUPER_ADMIN_USER_IDS = _csv_ids("QQBOT_SUPER_ADMIN_USER_IDS") or frozenset({"900000001"})
ADMIN_USER_IDS = (_csv_ids("QQBOT_ADMIN_USER_IDS") or frozenset({"900000001"})) | SUPER_ADMIN_USER_IDS
BATCH_SIZE = 20
COOLDOWN_SECONDS = 60.0


def _int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default


def _float_clamped(name: str, default: float, minimum: float, maximum: float) -> float:
    """读一个浮点配置，**夹到 `[minimum, maximum]`**。

    照 `memory_config._float_env` 那一套：写错（不是数）当没配回落默认，
    写了个离谱的数则夹进来——比"整份配置作废"更可预期。
    """

    try:
        value = float(get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _int_clamped(name: str, default: int, minimum: int, maximum: int) -> int:
    """读一个整数配置，**夹到 `[minimum, maximum]`**（与 `_float_clamped` 同一套）。"""

    try:
        value = int(get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


# --- 对话背景补全那两个缓存的边界（2026-10-08 隔离增长审计 [6]）--------------
# `ConversationContextProvider` 里 `_members` 存的是**整群成员表**（一个几百人的群
# 就是几百条），`_last_refresh` 同样只增不减。两个都按"最后一次刷新/写入"的时间
# 淘汰：先看保留期，再看条数上限（超了丢最旧）。
#
# 写成函数、**调用时现读**（与 `persona_offset_hours` 同一条纪律）：改配置不必重启。


def context_cache_max_sessions() -> int:
    """最多缓存多少个会话的名册与刷新时刻（`QQBOT_CONTEXT_CACHE_MAX_SESSIONS`，
    默认 200，夹在 10..5000）。超过就丢**最旧**的。"""

    return _int_clamped("QQBOT_CONTEXT_CACHE_MAX_SESSIONS", 200, 10, 5000)


def context_cache_ttl_seconds() -> float:
    """多久没刷新就把那份缓存丢掉（`QQBOT_CONTEXT_CACHE_TTL_SECONDS`，
    默认 21600 = 6 小时，夹在 60..604800）。活跃的群每轮刷新都会续期。"""

    return _float_clamped("QQBOT_CONTEXT_CACHE_TTL_SECONDS", 21600.0, 60.0, 604800.0)


# --- 短期会话状态的上限（2026-10-08 隔离增长审计 [1]）------------------------
# `DialogueSessions.states` 从前只增不减（只有 `setdefault` 与"从盘恢复"两条写入口），
# `/admin disable`、`/super clear` 都只把 mode 置回去，真正的回收只发生在进程重启。
# 上限**调用时现读**（改配置不必重启）；淘汰只看"上限 + 最久不活跃"，与开不开对话无关。
# 默认 200 与持久化的 `MAX_PERSISTED_SESSIONS` 对齐：重启能恢复多少，内存里就留多少。


def session_cache_max_sessions() -> int:
    """最多同时留多少个会话的短期状态（`QQBOT_SESSION_CACHE_MAX_SESSIONS`，
    默认 200，夹在 10..2000）。超了就淘汰**最久不活跃**的那个会话。"""

    return _int_clamped("QQBOT_SESSION_CACHE_MAX_SESSIONS", 200, 10, 2000)


# --- 一轮对话的总预算（2026-10-08 失败降级审计 2）----------------------------
# 主循环是**串行**的，而单次 `complete()` 最坏 ≈ 181 秒（60 秒 × 3 次 + 退避），
# 一轮里最多三次（压缩 + 判定 + 回复）——模型"拖着不回"时整个 bot（含 ping、
# `/admin`、私聊）最坏停摆约 9 分钟，而 focus / outbox 还在跑，日志看起来像活着。
# 默认 90 秒是实测（判定约 2 秒、回复约 10 秒）的 9 倍余量；**调用时现读**。


def turn_timeout_seconds() -> float:
    """一条消息从进来到出结果的**总预算**（`QQBOT_TURN_TIMEOUT_SECONDS`，
    默认 90，夹在 5..600）。超时就当这一条没有回复（历史照留）。"""

    return _float_clamped("QQBOT_TURN_TIMEOUT_SECONDS", 90.0, 5.0, 600.0)


def remind_min_interval_seconds() -> int:
    """同一个人**两次提醒投递**之间至少隔多少秒（`QQBOT_REMIND_MIN_INTERVAL_SECONDS`，
    默认 300，夹在 0..3600）。**0 = 关掉这道闸**。

    这是"她刷屏"那条的最后一道兜底（2026-10-09 用户实测：5 分钟里发了 5 条
    `@某人 到点了：…`）。前面两条是"归一化去重"与"她自己的发言不算请求"；
    这一条只管**频次**，所以就算那两条漏了也不会刷屏。

    **调用时现读**（与 `turn_timeout_seconds` / `persona_offset_hours` 同一条纪律）：
    改配置不必重启；坏值回落默认、越界夹取（照 `memory_config` 的写法）。
    """

    return _int_clamped("QQBOT_REMIND_MIN_INTERVAL_SECONDS", 300, 0, 3600)


def groups_cache_seconds() -> int:
    """面板那份**群列表**缓多久（`QQBOT_GROUPS_CACHE_SECONDS`，默认 **600**，夹在 0..86400）。

    由来（2026-10-09 用户："群聊页应当展示云茹所在的所有群聊并展示**群名**"）：群名只能
    靠核心那条 `get_group_list` 取，而面板每开一次页面就问一次是白打 OneBot。
    默认 10 分钟——群列表这种东西慢一点没有关系，取失败还会回落上一次的已知列表。

    **0 = 不缓存**（每次现取，给排查用）。**调用时现读**（与 `turn_timeout_seconds`
    同一条纪律）：改配置不必重启；坏值回落默认、越界夹取。
    """

    return _int_clamped("QQBOT_GROUPS_CACHE_SECONDS", 600, 0, 86400)


# --- 记忆库那张"只增"的关系流水的保留期（2026-10-08 隔离增长审计 [8]）--------
# `relationship_log`（好感/关系的每一次变动）从前**没有任何保留期**，只增不减。
# 它同时是**关系数据**，所以清理必须保守：只按时间删**旧行**，近期一行都不动，
# 写入语义一个字不改。默认 365 天——现有的读取点全是"7 天/1 天窗口"或"最近 N 条"
# （`affinity_rejected_relax_too_soon` / `affinity_rejected_daily_cap` / 关系曲线），
# 所以清更早的行不改变任何判定。**调用时现读**、带钳制（下限 30 天兜住手滑）。


def relationship_log_retention_days() -> int:
    """关系流水保留多少天（`QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS`，默认 365，
    夹在 30..3650）。超期的是**旧的**那些行，近期的一行不动。"""

    return _int_clamped("QQBOT_RELATIONSHIP_LOG_RETENTION_DAYS", 365, 30, 3650)


# --- 墓碑（`tombstones`）的保留期（2026-10-08 隔离增长审计 [9]）--------------
# 墓碑只记**键与时间判据**、不含内容（AGENTS §2.1），但它是"删掉的东西不许被写回来"
# 的凭据：`_cleanup` 里从来没人删它，于是它也只会增。清理同样保守——只按时间删旧行，
# 写入语义一个字不改。默认 365 天：inbox 事件的保留期是 7 天、归档 30 天，
# 365 天远远盖住"旧证据可能把它写回来"的那段时间。**调用时现读**、带钳制。


def tombstone_retention_days() -> int:
    """墓碑保留多少天（`QQBOT_TOMBSTONE_RETENTION_DAYS`，默认 365，夹在 30..3650）。"""

    return _int_clamped("QQBOT_TOMBSTONE_RETENTION_DAYS", 365, 30, 3650)


# --- "群里这边"的时间 -------------------------------------------------------
# 用户口径（2026-10-08）：*"她那边 = 北京 + 3 小时"*。而"这边"从前是**宿主时区**
# （`datetime.fromtimestamp` / `time.localtime`），代码把"本机=北京"写死在注释里。
# 换到 UTC 云主机上，"群里这边"整整差 8 小时，而且**没有任何信号**——刚加的钟表测试
# 在 `TZ=UTC` 下照样全绿（`datetime(2026,10,8,22,12).timestamp()` 与 `fromtimestamp()`
# 是同一台机器上的一对逆运算，两边一起平移，断言不变）。
#
# 所以"这边"现在是**显式配置**：`QQBOT_LOCAL_TZ_OFFSET_HOURS`（默认 8），
# 渲染时刻不再读宿主时区。"她那边"= 这个基准 + `persona_offset_hours()`，
# 两句话（`stage3_runtime._now_text`）措辞不变。
#
# 两个都写成**函数**而不是 import 期常量：与 `reply_api_base_url` 同一条纪律——
# **调用时现读环境**，改配置不必重启，测试也能直接改。写错了当没配（回落默认）。
#
#: "群里这边"的偏移范围（小时）：现实里的时区边界就是 UTC-12 … UTC+14。
#: 写错的值**夹进来**而不是当没配——`800` 这种手滑不该把时间变成几十年后。
LOCAL_TZ_OFFSET_MIN = -12.0
LOCAL_TZ_OFFSET_MAX = 14.0


def local_tz_offset_hours() -> float:
    """「群里这边」相对 UTC 的偏移（`QQBOT_LOCAL_TZ_OFFSET_HOURS`，默认 8）。

    **不读宿主时区**；调用时现读环境，超出 `[LOCAL_TZ_OFFSET_MIN, LOCAL_TZ_OFFSET_MAX]`
    的值夹进来。
    """

    return _float_clamped("QQBOT_LOCAL_TZ_OFFSET_HOURS", 8.0,
                          LOCAL_TZ_OFFSET_MIN, LOCAL_TZ_OFFSET_MAX)


def local_timezone():
    """「群里这边」的时区对象（显式偏移，**与部署机器的时区无关**）。

    `datetime.fromtimestamp(ts, tz=local_timezone())` 与 `time.localtime(ts)` 的区别
    就是前者换机器不变：UTC 云主机上"群里这边"仍是同一个时刻。
    """

    return timezone(timedelta(hours=local_tz_offset_hours()))


def persona_offset_hours() -> float:
    """她那边比「群里这边」快多少小时（`QQBOT_PERSONA_OFFSET_HOURS`，默认 3）。"""

    return _float("QQBOT_PERSONA_OFFSET_HOURS", 3.0)


# --- OneBot 连接方式 -------------------------------------------------------
# Stage 3（run_stage3.bat）使用反向 WS 服务端模式：Bot 监听，SnowLuma 作为
# wsClient 连进来。Stage 4 使用 WS 客户端模式：Bot 主动连 SnowLuma 的 wsServer，
# 因此可以和 Stage 3 同时运行而互不抢占端口。
ONEBOT_WS_HOST = get("ONEBOT_WS_HOST", "127.0.0.1")
ONEBOT_WS_PORT = _int("ONEBOT_WS_PORT", 8080)
ONEBOT_ACCESS_TOKEN = get("ONEBOT_ACCESS_TOKEN", "")
# **掉线事件**的开关（本体侧只广播事件，谁来接是插件的事，见 `plugins.LinkSeams`）。
# 默认开：看门狗每 60 秒查一次"对面还连着没有"（`runtime._watch_connection` 的
# **检测节奏**；那条告警日志仍是 10 分钟一条），这里只是把那个状态变成
# **确定的边沿**：`在线 → 掉线` 那一次广播一次。
# 关掉（`QQBOT_DISCONNECT_NOTICE=0`）= 与没有这个功能时**逐字相同**：告警日志照打，
# 一个接收者都不叫。用户 2026-10-06：*"断一次只发一次，不要反复调用"*、
# *"bot 本身稳定性我认为是很可靠的，不用额外通知"*——所以**恢复不发**。
DISCONNECT_NOTICE_ENABLED = get("QQBOT_DISCONNECT_NOTICE", "1").lower() not in {
    "0", "false", "no", "off",
}
# SnowLuma 侧地址。默认指向它的 OneBot v11 HTTP Server 与 WS Server。
SNOWLUMA_HTTP_BASE_URL = get("QQBOT_SNOWLUMA_HTTP_BASE_URL", "http://127.0.0.1:3000")
SNOWLUMA_ACCESS_TOKEN = get("QQBOT_SNOWLUMA_ACCESS_TOKEN", "")
SNOWLUMA_WS_URL = get("QQBOT_SNOWLUMA_WS_URL", "ws://127.0.0.1:3001")


# --- 余额命令 --------------------------------------------------------------
# 余额是账号资金信息，默认只在超管私聊可用（fail-closed：不配就查不到）。
# 要放开到群，显式写 QQBOT_BALANCE_GROUP_IDS（逗号分隔）。
# （`_csv_ids` 定义在本文件靠前的位置——它现在也被超管名单用着。）
BALANCE_PRIVATE_USER_IDS = _csv_ids("QQBOT_BALANCE_PRIVATE_USER_IDS") or SUPER_ADMIN_USER_IDS
BALANCE_GROUP_IDS = _csv_ids("QQBOT_BALANCE_GROUP_IDS")
BALANCE_CACHE_SECONDS = _float("QQBOT_BALANCE_CACHE_SECONDS", 60.0)

# 谁能和 Bot **私聊**（私聊消息走"强制触发"，立刻得到回应，不进 20 条批量）。
# 默认给超管：名单为空时私聊只能靠 20 条 / 60 秒的批量触发触发，等于没人能在私聊里
# 正常说话——2026-09-27 实测就是这个状态。要改就写 QQBOT_PRIVATE_DEBUG_USER_IDS。
PRIVATE_DEBUG_USER_IDS = _csv_ids("QQBOT_PRIVATE_DEBUG_USER_IDS") or SUPER_ADMIN_USER_IDS

# --- Agent Mail（Stage 4：每天一封汇报）-------------------------------------
# 迁移要点：这三项全在配置里，**没有任何绝对路径**。换机器只要重装 CLI 并授权
# （见 README「迁移」一节），代码与 data/ 一起搬过去就能继续跑。
MAIL_CLI = get("QQBOT_MAIL_CLI", "agently-cli")
# 收件人**写死在配置里**，不从对话、记忆或邮件内容里取。
MAIL_REPORT_TO = get("QQBOT_MAIL_REPORT_TO", "xuzhunzhi@foxmail.com")
# 每晚几点发（本地时间 HH:MM）。窗口 = 距上次汇报满 24 小时，不是日历日。
MAIL_REPORT_AT = get("QQBOT_MAIL_REPORT_AT", "23:00")
# 内联写法而不是用下面的 `_enabled`：那个函数定义在本文件更靠后的位置，
# 这里用它会在 import 时就 NameError（踩过一次）。
MAIL_REPORT_ENABLED = get("QQBOT_MAIL_REPORT", "1").lower() not in {"0", "false", "no", "off"}
# 汇报正文上限（比 CLI 自己的 1MB 严得多：汇报要能读得下去）。
MAIL_REPORT_MAX_CHARS = _int("QQBOT_MAIL_REPORT_MAX_CHARS", 1500)
# 发送失败当天的重试次数上限。
MAIL_REPORT_MAX_TRIES = _int("QQBOT_MAIL_REPORT_MAX_TRIES", 3)

# --- 写信 agent：独立 client / 独立 user_id（2026-09-30 用户要求）------------
# 以前写信借用的是**回复 agent** 的 client（同一把 key、同一个 user_id），
# 于是"信"与"群聊"共用一份缓存隔离空间；信的 prompt 前缀又大又恒定，
# 分开之后它才能自己吃满缓存，而且成本/排障也能分开看。
# 不配 `QQBOT_LETTER_API_KEY` 时：
#   `model_config` 里 letter 那条 key 的回落项是 **reply 那把 mimo key**（同家，
#   所以回落也不会造成"A 家 key 打 B 家地址"）；下面这个常量是**更老的**名字
#   `QQBOT_MAIL_API_KEY` 的兼容读数——历史部署可能只填了它。
LETTER_API_KEY = _compat("QQBOT_LETTER_API_KEY",
                         _compat("QQBOT_MAIL_API_KEY", ""))
#: 老名字，历史部署 / 测试可能在读。语义就是"写信那一路的 key"。
MAIL_API_KEY = _compat("QQBOT_MAIL_API_KEY", "")
MAIL_USER_ID = get("QQBOT_MAIL_USER_ID", "qqbot-letter")

# --- 读信回信（2026-09-30 用户要求）-----------------------------------------
# 她的邮箱会**收**信：只认下面这个主人地址，其余一律不进模型（fail-closed）。
# 主人地址默认就是汇报的收件人——"她写给谁"与"她认谁的信"是同一个人才说得通。
MAIL_OWNER_FROM = get("QQBOT_MAIL_OWNER_FROM", MAIL_REPORT_TO)
# 邮件进来时用哪个 QQ 号当"说话的人"：决定会话、关系档位与私聊闸门是否放行。
# 默认取超管第一个（就是用户本人），所以不用额外配置也能跑。
MAIL_OWNER_USER_ID = get(
    "QQBOT_MAIL_OWNER_USER_ID",
    sorted(SUPER_ADMIN_USER_IDS)[0] if SUPER_ADMIN_USER_IDS else "",
)
# 谁的信会被**读进来走一遍判定**（2026-09-30 用户："每个人发的邮件都能看，都走一遍流程，
# 判断不该回的就不会"）。`any` = 谁都读、判定决定回不回；`owner` = 只读主人的；
# 也可以给逗号分隔的地址名单。注意：这与群聊同一条原则——陌生人的话可以读、可以判定，
# 但**不能变成指令**（正文照旧是不可信 DATA），而且她依然没有"把信转发给任意地址"的能力。
MAIL_SENDERS = get("QQBOT_MAIL_SENDERS", "any")
# 同一天里对**同一个人**最多回几封：防自动回复来回刷。
# 默认 3 而不是 2：陌生邮箱那条路要"打招呼 → 问 QQ 号 → 对方报号 → 回一句"，
# 2 封会在问到一半就被卡住。
MAIL_MAX_REPLIES_PER_SENDER = _int("QQBOT_MAIL_MAX_REPLIES_PER_SENDER", 3)
MAIL_REPLY_ENABLED = get("QQBOT_MAIL_REPLY", "1").lower() not in {"0", "false", "no", "off"}

# --- 识图（多模态，2026-09-30 用户要求）------------------------------------
# 有图的群聊消息在**进判定之前**先让 `deepseek-flash` 看一眼，把"[图片]"占位符换成一句描述。
# 实测（2026-09-30）：它确实能看图（左蓝右黄能分开说），不给图时它会说"我看不到图片"，不编。
# 独立 user_id 且**不分群**：识图每次带的图都不同，前缀注定命中不了缓存，
# 按群拆只会把一份缓存拆散。默认打开（用户要求），`QQBOT_VISION=0` 可关。
VISION_ENABLED = get("QQBOT_VISION", "1").lower() not in {"0", "false", "no", "off"}
VISION_MODEL = get("QQBOT_VISION_MODEL", "deepseek-flash")
VISION_USER_ID = get("QQBOT_VISION_USER_ID", "qqbot-vision")
VISION_API_KEY = _compat("QQBOT_VISION_API_KEY", "")

# --- 群管理（2026-09-30 用户要求；**只有超管能用**）--------------------------
# 命令挂在 `/super` 下（`/super ban @某人 30` 等），所以群管理员那套碰不到。
# 闸门是 `capabilities.py` 里单独列名的 `GROUP_MANAGE_ACTIONS`（禁言/全员禁言/撤回/移出），
# 没有放宽 `admin` 那道闸门。这里给一个总开关，出事可以一键关掉。
GROUP_MANAGE_ENABLED = get("QQBOT_GROUP_MANAGE", "1").lower() not in {"0", "false", "no", "off"}

# --- 帮助改成图片（2026-09-30 用户："后面命令的 help 就用图片展示了"）----------
# 三条 help（公开 / 管理员 / 超管）都画成卡片发出去。**渲染或发送失败一律退回文字**，
# 所以关掉它（或没装 Pillow）只会让帮助变回一屏文本，不会变成"用不了"。
HELP_IMAGE = get("QQBOT_HELP_IMAGE", "1").lower() not in {"0", "false", "no", "off"}
HELP_IMAGE_WIDTH = _int("QQBOT_HELP_IMAGE_WIDTH", 880)

# --- 群主专属能力（2026-09-30 用户："群主的接口应该比管理员更多"）-------------
# 前提是实测的：她（900000002）在测试群 717151356 里 role=owner，别的群是 member。
# 所以这批动作的**执行前提是"她在那个群确实是群主"**（`group_roles.py` 现查），
# 命令一侧仍然只有超管能发。闸门见 `capabilities.GROUP_OWNER_ACTIONS`。
GROUP_OWNER_ENABLED = get("QQBOT_GROUP_OWNER", "1").lower() not in {"0", "false", "no", "off"}
# 自己角色缓存多久（秒）。她不会频繁变更角色，一次查询够用很久。
SELF_ROLE_TTL_SECONDS = _float("QQBOT_SELF_ROLE_TTL_SECONDS", 900.0)

# --- 入群申请自动审批（2026-09-30 用户："自动审批做正则，还有白名单和黑名单"）---
# 三个判据按**固定优先级**走：黑名单 → 白名单 → 正则 → 兜底。
# 兜底默认是 `hold`（不表态，留给超管人工看）——**fail-closed**：
# 没配白名单也没配正则时，等于什么都不会自动批，但事件照样记日志并通知。
AUTO_APPROVE_JOIN = get("QQBOT_AUTO_APPROVE_JOIN", "1").lower() not in {"0", "false", "no", "off"}
APPROVE_WHITELIST = _csv_ids("QQBOT_APPROVE_WHITELIST")
APPROVE_BLACKLIST = _csv_ids("QQBOT_APPROVE_BLACKLIST")
# 正则同时匹配**验证留言**与**申请人昵称**（一行一个，合起来匹配）。
APPROVE_PATTERN = get("QQBOT_APPROVE_PATTERN", "")
# 黑名单被拒时给对方看的原因（QQ 会把它回给申请人）。
APPROVE_REJECT_REASON = get("QQBOT_APPROVE_REJECT_REASON", "本群暂不接受这次申请。")
APPROVE_POLL_SECONDS = _float("QQBOT_APPROVE_POLL_SECONDS", 60.0)
# 一条申请最多通知几个人（私聊）。默认超管；不填就不通知。
APPROVE_NOTIFY_USER_IDS = _csv_ids("QQBOT_APPROVE_NOTIFY_USER_IDS") or SUPER_ADMIN_USER_IDS
# 同一批申请最多处理几条（每处理一条要一次写调用，别让积压把一轮拖死）。
APPROVE_MAX_PER_TICK = _int("QQBOT_APPROVE_MAX_PER_TICK", 5)
MAIL_POLL_SECONDS = _float("QQBOT_MAIL_POLL_SECONDS", 300.0)
# 一天最多回几封：防"对面自动回复 → 她再回"这种来回刷。
MAIL_MAX_REPLIES_PER_DAY = _int("QQBOT_MAIL_MAX_REPLIES_PER_DAY", 5)
# 她自己的邮箱地址（`agently-cli +me` 里那个）。填了就**绝不回自己的地址**——
# 那是死循环。查到过：shiyunru@agent.qq.com。
MAIL_SELF_FROM = get("QQBOT_MAIL_SELF_FROM", "")
# 太旧的来信不回（小时）。理由：上限卡住的那几封会攒到第二天，
# 隔了好几天再回一封"你好吗"很奇怪。
MAIL_MAX_AGE_HOURS = _float("QQBOT_MAIL_MAX_AGE_HOURS", 48.0)
# **掉线通知**（`plugins/outage_notice/`）的开关：断一次给操作者发一封普通邮件。
# 收件人**没有第二个配置**——就用 `MAIL_REPORT_TO`（与每日汇报同一个人），
# 这与"读信回信认谁的信"复用同一个地址是同一条原则（`MAIL_OWNER_FROM` 也回落到它）。
# 关掉只是"这个插件不接那条事件"，与 `QQBOT_DISCONNECT_NOTICE`（更上游的总闸，
# 关掉连广播都不发生）是两层，别混：那个是**事件发不发生**，这个是**发不发邮件**。
# 内联写法而不是用下面的 `_enabled`：那个函数定义在本文件更靠后的位置（同上面那条注释）。
MAIL_OUTAGE_NOTICE_ENABLED = get(
    "QQBOT_MAIL_OUTAGE_NOTICE", "1").lower() not in {"0", "false", "no", "off"}


# --- 双 agent（判定 / 回复分离）--------------------------------------------
# 判定 agent 只决定"要不要接、在聊什么"，prompt 与窗口都小得多（不带人设、不带记忆）；
# 它说不接就不调回复 agent，省掉那一次完整调用。回复 agent 带的是"摘要 + 活窗口"。
# 关掉即退回单 agent：判定与回复在同一次调用里完成。
def _enabled(name: str, default: str = "1") -> bool:
    return get(name, default).lower() not in {"0", "false", "no", "off"}


DUAL_AGENT_ENABLED = _enabled("QQBOT_DUAL_AGENT")
# user_id 用于同一账号下按 agent 隔离 KVCache 与调度（与 API Key 无关）。
DIALOGUE_USER_ID = get("QQBOT_DIALOGUE_USER_ID", "qqbot-dialogue")
JUDGE_USER_ID = get("QQBOT_JUDGE_USER_ID", "qqbot-judge")
MEMORY_USER_ID = get("QQBOT_MEMORY_USER_ID", "qqbot-memory")

# --- 回复风格审核（可选 agent）----------------------------------------------
# 离线实测（data/style_reviewer_ab.py）：光靠 prompt 压不住"冲/抬杠"，而一次窄职责
# 改写能把这类尾巴删掉（10 条改 5 条，中位长度 20→12 字）。默认**不开**：
# 它每条回复多花一次调用（实测 +0.6~0.9 秒、约 360 token），钱要用户点头才花。
#
# **2026-10-05 起它只判不改**（用户："审核只负责打回，不负责修改"）：判不过就把理由
# 交回回复 agent 重写一次（判不过的轮次因此多一次调用）。它不再产出正文，
# 所以上面那句"10 条改 5 条"是**历史**——见 `style_reviewer.py` 的模块说明。
# 语义：配了 `QQBOT_REVIEW_API_KEY` 就自动启用；`QQBOT_STYLE_REVIEW=0/1` 可以强行关或开。
REVIEW_API_KEY = _compat("QQBOT_REVIEW_API_KEY", "")
REVIEW_ENABLED = _enabled("QQBOT_STYLE_REVIEW", "1" if REVIEW_API_KEY else "0")
REVIEW_USER_ID = get("QQBOT_REVIEW_USER_ID", "qqbot-style-review")


# --- 没把握就别断言（2026-10-04 起叫"不懂就问"，2026-10-05 晚改口径）----------
# 用户否掉了"不懂就问"那条路（*"别做不懂就问"* / *"先改掉不懂装懂硬插话"*）。
# 现在是两条确定性规则：**没被叫到 + 没懂 + 具体话题 → 不出声**；
# **被叫到 + 没懂 + 话里有具体断言 + 知识库/记忆/语境三处都没根据 → 打回重写一次**。
# **不多花一次调用**："懂不懂""是不是具体的专业话题"这两个信号由判定那趟顺带给出。
# 旧版那个"问的限量"（`AskBudget`）**已经没有消费者**：现在没有"以问回应"这条路，
# 兜底是一次重写，本身就是上限。面板的运行期开关是 `ask_when_unsure`
# （见 `runtime_flags.py`）——关掉它，这两条规则一起退回从前。
ASK_WHEN_UNSURE = _enabled("QQBOT_ASK_WHEN_UNSURE")


# --- WebUI 面板（2026-10-01 用户："该做webui了"；"面板属于 stage4 内容，本质插件"）---
# 面板跑在 bot 进程里（Stage 4 后台插件），所以**默认只绑本机**：它等于这台机器上 bot
# 的控制面（能改 prompt、换 key、踢人）。`local` 用 Bearer token；`remote` 用口令换会话
# cookie（TLS 交给反向代理终止，见 docs/WEBUI.md）。
WEBUI_ENABLED = get("QQBOT_WEBUI", "1").lower() not in {"0", "false", "no", "off"}
WEBUI_ACCESS_MODE = get("QQBOT_WEBUI_ACCESS_MODE", "local").strip().casefold() or "local"
# remote 模式下监听地址由这里决定（local 恒为 127.0.0.1）。
WEBUI_HOST = get("QQBOT_WEBUI_HOST", "127.0.0.1") if WEBUI_ACCESS_MODE == "remote" \
    else "127.0.0.1"
WEBUI_PORT = _int("QQBOT_WEBUI_PORT", 8790)
# local 模式：留空则启动时自动生成 `data/webui_token`（0600）。
WEBUI_TOKEN = get("QQBOT_WEBUI_TOKEN", "")
# remote 模式：口令（至少 8 位）。首次启动算出加盐哈希存 `data/webui_password`。
WEBUI_PASSWORD = get("QQBOT_WEBUI_PASSWORD", "")
# remote 模式允许的浏览器来源（逗号分隔，含协议与端口）。默认取监听地址本身。
WEBUI_ALLOWED_ORIGINS = tuple(
    part.strip() for part in get("QQBOT_WEBUI_ALLOWED_ORIGINS", "").split(",") if part.strip()
) or ((f"http://{WEBUI_HOST}:{WEBUI_PORT}",) if WEBUI_ACCESS_MODE == "remote" else ())
# 只有显式打开才信任 `X-Forwarded-For`（否则来源 IP 可被伪造）。
WEBUI_BEHIND_PROXY = get("QQBOT_WEBUI_BEHIND_PROXY", "0").lower() in {"1", "true", "yes", "on"}

# --- 多群焦点 --------------------------------------------------------------
# 规则见 docs/MULTI_GROUP_FOCUS.md。默认值就是用户定下的那几个数字。
# 话题没继续多久算结束（秒）；当值上限（秒）；连续回复上限（条）；
# 同一群里"稍等"的冷却（秒）。
FOCUS_QUIET_SECONDS = _float("QQBOT_FOCUS_QUIET_SECONDS", 45.0)
FOCUS_DUTY_SECONDS = _float("QQBOT_FOCUS_DUTY_SECONDS", 300.0)
FOCUS_REPLY_LIMIT = _int("QQBOT_FOCUS_REPLY_LIMIT", 50)
FOCUS_ACK_COOLDOWN_SECONDS = _float("QQBOT_FOCUS_ACK_COOLDOWN_SECONDS", 120.0)
# 单个群的排队上限（条；超了丢最旧）。队列里存的是消息全文，必须有个上界——
# 2026-10-08 隔离增长审计 [7]。与上面那几个一样是**装配期**读一次的常量。
FOCUS_QUEUE_LIMIT = _int("QQBOT_FOCUS_QUEUE_LIMIT", 50)


# --- 对话日志（**实际收发**，与模型日志分开）---------------------------------
# 2026-10-05 用户定的边界："对话日志和模型日志分开"。实现见 `chat_log.py`：
# 它挂在**传输层**（`onebot_ws.py` 的收发边界），所以群里真正收到的每一条、
# 以及她实际发出去的每一段（含插件发的图/文）都在里面；`feature_log.py` 那五个
# 文件是模型日志（模型看到与生成了什么），两边文件、开关、容量各自独立。
#
# 为什么默认留 20000 条：这是**按条**的滚动窗口，不是按天。量级依据——一个活跃群
# 一天几百条，收发两侧合计按 2000 条/天估，20000 条 ≈ 最近一周多；而且"一次回复拆成
# 三段"会占 3 条，所以条数比"消息数"要多算一档。一条就是一行短记录（正文之外不存
# base64、不存图片），2 万行约几 MB，滚动重写的代价可以忽略。
# 文件是 `data/logs/chat.jsonl`（`data/` 已被 .gitignore 忽略），容量见
# `chat_log.chat_log_capacity()`（范围兜在 [10, 1000000]，**不允许无限增长**）。
CHAT_LOG_ENABLED = _enabled("QQBOT_CHAT_LOG")
CHAT_LOG_MAX = _int("QQBOT_CHAT_LOG_MAX", 20000)
# 默认空 = 与模型日志同一个 `data/logs/`（并列的两份）；要单独搬走就写路径。
CHAT_LOG_DIR = get("QQBOT_CHAT_LOG_DIR", "")


# --- 原始入站事件日志（**没被识别/没被处理**的那些事件）-----------------------
# 2026-10-06：想知道 NapCat 到底推不推"表情回应"（用户想拿群里的表情回应定位她说过的好句子），
# 而传输层只认 `post_type=message`，别的事件**被直接丢掉、一点痕迹不留**——所以"没记"
# 与"没发生"分不出来。这一份把没被处理的事件**原样**落进 `data/logs/raw_events.jsonl`
# （实现见 `raw_events.py`）：不解析、不判据、不喂模型、不进核心，只落盘。
#
# 与对话日志（`chat.jsonl`）、模型日志各自独立：文件、开关、容量都分开，
# 写它不会往那两份里写一个字（`tests/test_raw_events.py` 钉住这条）。
#
# 为什么默认留 5000 条：这是**按条**的滚动窗口。raw 事件是"消息之外"的那一类
# （戳一戳、撤回、名片变更、入群申请、上下线…），比消息少一个量级；真出现
# 表情回应推送也只是每条被回应的消息多一行。5000 条 ≈ 几周到一个月，够看清形状。
# 容量见 `raw_events.raw_events_capacity()`（范围兜在 [10, 1000000]，不会无限增长）。
RAW_EVENTS_ENABLED = _enabled("QQBOT_RAW_EVENTS")
RAW_EVENTS_MAX = _int("QQBOT_RAW_EVENTS_MAX", 5000)
# 默认空 = 与另外两份日志同一个 `data/logs/`（并列的三份）；要单独搬走就写路径。
RAW_EVENTS_DIR = get("QQBOT_RAW_EVENTS_DIR", "")

# --- 表情回应（`group_msg_emoji_like`）----------------------------------------
# 2026-10-06（第二批）：`raw_events.jsonl` 已经证明 NapCat 确实推这种 notice（里面有真实帧），
# 于是把它**解析成干净一行**写进对话日志（`kind="reaction"`），与 in/out 并列。解析与落盘
# 见 `reactions.py`（**不判金句、不做 few-shot**：这一批只到"记下来"）。
#
# **它没有自己的开关、容量与目录**：写的是那个同一个 `chat.jsonl`，用同一个 `ChatLog`
# 与同一套 `RollingJsonlFile` 轮转，所以 `QQBOT_CHAT_LOG=0` 一起关掉、`CHAT_LOG_MAX` 一起
# 管着。理由：它本来就是"对话日志里的一类记录"，另开一个开关只会多一条要对齐的配置。

# --- 金句学习 agent（2026-10-06，用户第三批）----------------------------------
# 用户原话：*"富群刚刚提出爆的金句都是优质 rl 训练轨迹…可以通过给消息点的表情来定位"*、
# *"我认为金句更重要的是说话风格和上下文语境，需要调用一个 agent 来专门学习"*、
# *"注意贴表情不是所有都是金句，比如贴祝（猪的谐音）就是不赞同或者 bot 回复不恰当"*。
#
# 四块都在 `quote_samples.py`（选取与上下文）与 `quote_learning.py`（学习 agent、
# 表情含义表、落盘、注入材料）里。这里的配置只有下面四项：
#
# - **默认开**（用户要求的形状是"默认开、能一键关"）：`QQBOT_QUOTE_LEARN=0` 关掉之后
#   **行为与改动前逐字相同**——回复 prompt 里一条材料都不加（有测试钉住这条）；
# - `QQBOT_QUOTE_LEARN_MAX`：一条回复里最多注入几条材料（**不是全塞**）。上限 5，
#   再多就该写进笔记而不是每轮都带；注入块另有一个 320 字的预算（见注入侧）。
# - `QQBOT_QUOTE_LEARN_INTERVAL`：多久跑一次（秒）。**一天一次级别**，不是每轮；
#   实现里还有 60 秒下限，防配置把它写成"每轮都问模型"。
# - `QQBOT_QUOTE_PROFILE`：那份 JSON 的位置（笔记 + 按群的表情含义表 + 操作者覆盖）。
#   默认 `<项目根>/data/quote/profile.json`（`data/` 已 gitignored）。
#
# **热路径零模型调用**：注入只读这份 JSON（按 mtime 缓存）＋算几个字符重叠，
# 模型只在定期那一趟里被调一次。
QUOTE_LEARN_ENABLED = _enabled("QQBOT_QUOTE_LEARN")
QUOTE_LEARN_MAX = _int("QQBOT_QUOTE_LEARN_MAX", 5)
QUOTE_LEARN_INTERVAL = _float("QQBOT_QUOTE_LEARN_INTERVAL", 86400.0)
QUOTE_PROFILE_FILE = get("QQBOT_QUOTE_PROFILE", "data/quote/profile.json")

# --- 黑话词条（2026-10-06，用户口径："还有**黑话**"）--------------------------
# 设计稿在 `docs/STAGE3_PENDING_DESIGNS.md` §②；实现只有一份：
# `slang_learning.py`（判解释句 / 词条库 / 被动观察者）。
#
# **它是被动的**：她在群里听到别人解释就记下来。用户已经否掉"不懂就问"
# （`docs/PENDING.md`：*"别做不懂就问"*），所以那条路上**没有发问的动作**
# ——观察者上连一个能发消息的东西都没有（有测试钉住）。
#
# - **默认开**，`QQBOT_SLANG_LEARN=0` 关掉：关掉＝一条都不记，**但面板/命令
#   照样看得到已经学出来的那份**（"关掉"是"不再记新的"，不是"数据没了"）。
# - `QQBOT_SLANG_PROFILE`：那份 JSON 的位置（词条 + 证据 + 修订）。
#   默认 `<项目根>/data/slang/entries.json`（`data/` 已 gitignored）。
# - **没有模型调用、没有节拍**：判解释句是确定性正则（理由写在那个模块里），
#   所以这里没有"多久跑一次"这种配置——它每条消息都只是扫一眼。
SLANG_LEARN_ENABLED = _enabled("QQBOT_SLANG_LEARN")
SLANG_PROFILE_FILE = get("QQBOT_SLANG_PROFILE", "data/slang/entries.json")


# --- 不可信数据的机器可验证边界（2026-10-08）----------------------------------
# 用户原话：*"给不可信数据加一道机器可验证的边界：每轮回复生成一个随机标记，
# 所有不可信内容都用它包起来；模型若把这个标记写进回复，判违规。"*
#
# 实现只有两份：`data_boundary.py`（取短码 / 包块 / 中和伪造 / 判违规）与
# `stage3_runtime.build_dialogue_messages`（**逐处**把**易变段**的 DATA 块包上）；
# 重试与 fail-closed 在 `stage3_main._model_path`。
# **稳定段（群背景 / 名册 / 更早的摘要 / 历史）不包**（2026-10-08 晚，用户拍板
# "把短码挪走"）：那四块逐轮一字不差，短码进稳定段会让整段前缀复用归零；它们靠
# 分节标注 + system 里那条规矩的续句（`SECTION_IS_DATA_RULE`）划边界。
#
# - **默认开**，`QQBOT_DATA_MARKERS=0` 关掉：关掉之后**DATA 段与改动前逐字相同**
#   （不取短码、不包裹、也不中和入站文本；有测试直接比字符串）。
#   **不是"整个请求逐字相同"**（2026-10-08 更正，对抗性审查必修 4）：system 里那
#   **两条**常量规则（`DATA_BOUNDARY_RULE` / `TIME_IS_GIVEN_RULE`）由
#   `compose_system_prompt` 无条件拼上去，与这个开关无关——关掉开关换不到
#   "prompt 回到改动前"，换到的是 system 前缀逐字不变、前缀缓存不因开关而断。
# - 短码不是用户举例的 8 位十六进制，理由写在 `data_boundary` 的模块说明里
#   （hex 会偶然出现在正常回复里 → 误判违规 → 丢一条正常回复）。
# - system 里那两条规则是**常量、常驻**：关掉时它们无害（规则原文举的例子就是一个
#   记号），而且"关功能不该顺手降安全"——撤掉它们等于在关掉短码的那一刻把
#   "那些带记号的是资料"这句唯一的措辞保障也丢掉。
DATA_MARKERS_ENABLED = _enabled("QQBOT_DATA_MARKERS")


def session_user_id(base: str, session_id: str) -> str:
    """把会话 id 变成服务商能接受的 user_id 后缀。

    `user_id` 只允许 `[a-zA-Z0-9\\-_]`、最长 512；用它把 KVCache 与调度**按会话**隔开，
    这样每个群/每个私聊都有自己的缓存空间，切走再回来时前缀还在（实测：切走一小段
    再回来仍命中 7424 tokens）。同一个会话恒定得到同一个值。
    """

    kind, _, raw = session_id.partition(":")
    prefix = "g" if kind == "group" else "p"
    safe = re.sub(r"[^A-Za-z0-9_-]", "", raw)[:64] or "unknown"
    return f"{base}-{prefix}-{safe}"[:512]


def data_dir() -> Path:
    """运行数据的根目录（默认 `<项目根>/data`，可用 `QQBOT_DATA_DIR` 搬走）。

    **唯一的算法**。以前十来处各自写 `Path(__file__).resolve().parents[N] / "data"`，
    `N` 是按当时那个文件的深度手写的——文件一搬家（面板从 `qq_roleplay_bot/`
    挪进 `plugins/webui/`）就静默指到 `src/data`，令牌、邮箱状态、记忆库全落到别处，
    而且**不报错**。所以：谁要这个路径就调这里，不要再自己数层数。
    """

    base = os.environ.get("QQBOT_DATA_DIR", "").strip()
    return Path(base).resolve() if base else _PROJECT_ROOT / "data"
