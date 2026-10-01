"""Local, transactional memory storage; no SQL or filesystem tools for the agent."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .memory_filters import system_self_rule
from .memory_model import (MAX_SUBJECTS, InboxEvent, MemoryBatch, MemoryRecord,
                           MemoryValidationError, parse_operations)
from .security import sanitize_chat_text

logger = logging.getLogger(__name__)

DAY = 86400
# v4：加"记忆↔人的对应"（record_subjects）与"本群见过的人"（people）两张表。
# v5：单表 records 拆成**两张表**——`memory_short`（中短期）与 `memory_long`（长期）。
SCHEMA_VERSION = 6

# `别名 → 规范号` 的进程内快照，按库路径分。这张表只有操作者动作会写（脚本 / 面板），
# 所以不需要过期；那些地方改完会调 `MemoryStore.alias_map_changed()` 作废本缓存。
_ALIAS_CACHE: dict[str, dict[str, str]] = {}

# --- 两级记忆（用户 2026-09-28 决定）----------------------------------------
# 新写入的一律先落中短期；够格的（见 PROMOTE_KINDS / revision>=2 / durable）才进长期。
# 用户选了"两张独立表"，所以这里把所有表名集中起来：**语句只按表名参数化**，
# 不把隔离、删除、归档那些逻辑抄两遍——抄两遍必然漂移。
TIERS = ("short", "long")
TABLES = {"short": "memory_short", "long": "memory_long"}
# 称呼、红线与**人物画像**天然属于长期，写入时直接进长期表（用户选定的强化规则之一）。
PROMOTE_KINDS = frozenset({"name", "boundary", "profile"})
# 画像不进普通记忆检索：它由"当前说话的人"每次单独取，混进列表只会重复占位。
PROFILE_KIND = "profile"
# 状态类事实（正在发烧/明天有考试）只活在**中短期**，永远不会被强化进长期。
STATUS_KIND = "status"
STATUS_TTL_DAYS = 7
STATUS_TTL_MAX_DAYS = 14
# 中短期那一层的默认寿命：没被强化的东西不该按长期事实活着。
DEFAULT_SHORT_TTL_DAYS = 14
SHORT_TTL_DAYS = {"group_fact": 30, "topic_summary": 30}
# "这条被强化进长期"的审计行编号偏移（audit 主键是 (batch_id, operation_index)）。
PROMOTION_AUDIT_OFFSET = 1000

_RECORD_COLUMNS = """
    id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
    subject_user_id TEXT, kind TEXT NOT NULL, normalized_key TEXT NOT NULL,
    content TEXT NOT NULL, confidence REAL NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
    revision INTEGER NOT NULL, visibility TEXT NOT NULL DEFAULT 'group_safe',
    status TEXT NOT NULL DEFAULT 'active', last_used_at REAL, last_reviewed_at REAL,
    origin TEXT NOT NULL DEFAULT 'agent_inferred', source_event_ids TEXT NOT NULL,
    durable INTEGER NOT NULL DEFAULT 0, reminded_at REAL"""

# 兼容视图：v5 把 records 拆成两张表，但既有工具脚本（watch_memory.py、full_audit.py、
# data/ 下各种体检脚本）都在查 records。视图让它们一行不用改；应用代码不碰它。
_COMPAT_VIEW_SQL = """
BEGIN IMMEDIATE;
CREATE VIEW IF NOT EXISTS records AS
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,NULL AS promoted_at,'short' AS tier
      FROM memory_short
    UNION ALL
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,promoted_at,'long' AS tier
      FROM memory_long;
COMMIT;
"""

_FRESH_SCHEMA = f"""BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
    speaker TEXT NOT NULL, text TEXT NOT NULL, occurred_at REAL NOT NULL,
    expires_at REAL NOT NULL, lease_id TEXT);
CREATE INDEX IF NOT EXISTS inbox_scope ON inbox(group_id, user_id, occurred_at);
CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS replays (
    id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
    until_at REAL NOT NULL, UNIQUE(group_id,user_id));
CREATE TABLE IF NOT EXISTS participants (
    group_id TEXT NOT NULL, user_id TEXT NOT NULL, reviewed_at REAL NOT NULL,
    PRIMARY KEY(group_id,user_id));
CREATE TABLE IF NOT EXISTS memory_short ({_RECORD_COLUMNS});
CREATE UNIQUE INDEX IF NOT EXISTS active_key_short
    ON memory_short(scope_type,scope_key,normalized_key) WHERE status='active';
CREATE TABLE IF NOT EXISTS memory_long ({_RECORD_COLUMNS}, promoted_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS active_key_long
    ON memory_long(scope_type,scope_key,normalized_key) WHERE status='active';
CREATE TABLE IF NOT EXISTS tombstones (
    scope_type TEXT NOT NULL, scope_key TEXT NOT NULL, normalized_key TEXT NOT NULL,
    deleted_at REAL NOT NULL, PRIMARY KEY(scope_type,scope_key,normalized_key));
CREATE TABLE IF NOT EXISTS archive (
    id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
    subject_user_id TEXT, kind TEXT NOT NULL, normalized_key TEXT NOT NULL,
    content TEXT NOT NULL, confidence REAL NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
    revision INTEGER NOT NULL, deleted_at REAL NOT NULL, delete_reason TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS archive_deleted_at ON archive(deleted_at);
CREATE TABLE IF NOT EXISTS audit (
    batch_id TEXT NOT NULL, operation_index INTEGER NOT NULL, op TEXT NOT NULL,
    scope_type TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(batch_id,operation_index));
CREATE TABLE IF NOT EXISTS relationships (
    user_id TEXT PRIMARY KEY, closeness INTEGER NOT NULL DEFAULT 0,
    guardedness INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL, last_seen_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS relationship_log (
    user_id TEXT NOT NULL, axis TEXT NOT NULL, before_value INTEGER NOT NULL,
    after_value INTEGER NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
    occurred_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS relationship_log_recent
    ON relationship_log(user_id, axis, occurred_at);
CREATE TABLE IF NOT EXISTS record_subjects (
    record_id TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
    PRIMARY KEY(record_id, user_id));
CREATE INDEX IF NOT EXISTS record_subjects_lookup
    ON record_subjects(group_id, user_id);
CREATE TABLE IF NOT EXISTS people (
    group_id TEXT NOT NULL, user_id TEXT NOT NULL,
    name TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(group_id, user_id));
-- 一个人可以有不止一个 QQ 号（用户 2026-10-01 报的实例：「在不在不在」和「兔子狗可爱喵」
-- 是同一个人）。这里记的是**操作者亲手认定**的"这个号也是他"，方向是 别名 → 规范号。
--
-- 为什么不自动认：昵称相同、说话像，都不足以证明是同一人，认错比想不起来更伤，
-- 所以这张表**只有人能动**（面板 / 脚本），代码里没有任何"猜"的路径。
-- 记忆的隔离仍按人算：读的时候先把号换成规范号，再按规范号取（见 canonical_user）。
CREATE TABLE IF NOT EXISTS person_aliases (
    alias_user_id TEXT PRIMARY KEY, canonical_user_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
    CHECK (alias_user_id <> canonical_user_id));
-- 兼容视图：v5 把 records 拆成两张表，但**所有既有工具脚本**（watch_memory.py、
-- full_audit.py、data/ 下的各种体检脚本）都在查 records。视图让它们一行不用改。
-- 只读：应用代码一律走 MemoryStore，不写这个视图。
CREATE VIEW IF NOT EXISTS records AS
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,NULL AS promoted_at,'short' AS tier
      FROM memory_short
    UNION ALL
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,promoted_at,'long' AS tier
      FROM memory_long;
PRAGMA user_version={SCHEMA_VERSION};
COMMIT;
"""

# v4 → v5：records 改名成 memory_short，建 memory_long，按用户选定的规则分流旧数据。
# `revision>=2` 在真库里有 8/41 条，"后来又被说过一次"是最硬的强化信号。
# `{adds}` 由调用方按"旧表到底缺哪几列"生成：SQLite 没有 ADD COLUMN IF NOT EXISTS，
# 写死会在"本来就是新形状、只是版本号被改小"的库上撞 duplicate column。
_MIGRATE_V5_TEMPLATE = f"""
BEGIN IMMEDIATE;
ALTER TABLE records RENAME TO memory_short;
{'{adds}'}
DROP INDEX IF EXISTS active_key;
CREATE UNIQUE INDEX IF NOT EXISTS active_key_short
    ON memory_short(scope_type,scope_key,normalized_key) WHERE status='active';
CREATE TABLE IF NOT EXISTS memory_long ({_RECORD_COLUMNS}, promoted_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS active_key_long
    ON memory_long(scope_type,scope_key,normalized_key) WHERE status='active';
INSERT INTO memory_long
    (id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
     created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
     origin,source_event_ids,durable,reminded_at,promoted_at)
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,updated_at
    FROM memory_short
    WHERE kind IN ('name','boundary') OR revision >= 2;
DELETE FROM memory_short
    WHERE kind IN ('name','boundary') OR revision >= 2;
-- 兼容视图（见 _FRESH_SCHEMA 里的说明）：老工具脚本仍在查 `records`。
CREATE VIEW IF NOT EXISTS records AS
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,NULL AS promoted_at,'short' AS tier
      FROM memory_short
    UNION ALL
    SELECT id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
           created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
           origin,source_event_ids,durable,reminded_at,promoted_at,'long' AS tier
      FROM memory_long;
PRAGMA user_version={SCHEMA_VERSION};
COMMIT;
"""
MAX_INBOX_EVENTS = 10000
MAX_INBOX_EVENTS_PER_GROUP = 2000
MAX_REPLAY_ATTEMPTS = 2
# 被删除的记忆在归档里保留多久。比 inbox 保留期长得多，因为归档是"后悔药"，
# 是用来救误删的——真正需要回滚时往往已经过了好几天。
ARCHIVE_RETENTION_DAYS = 30

# --- 写入的机械兜底（与模型自觉无关，见维护 prompt 的门槛）------------------
# 同一个 scope 里，新内容与已有活跃记忆的词元重合度超过这个比例，就认为在说同一件事：
# 不再新增，而是逐条拒绝（写进 audit）。词元取自 `_query_terms`（中文 2 字滑窗 + 英文词）。
#
# 阈值是量出来的，不是拍的（同一件事换个说法 vs 相关但不同）：
#   0.50  想装 ITX 机器跑本地小模型 / 打算装 ITX 主机跑本地小模型…      → 同一件事
#   0.46  不喜欢云服务器 / 对云服务器没好感，纯个人偏好                 → 同一件事
#   0.33  喜欢在深夜聊天 / 深夜喜欢听歌                                → 不同的事
#   0.14  喜欢直接有来有回 / 喜欢在深夜聊天                            → 不同的事
# 取 0.4 落在两簇中间。误判的代价不对称：把新事实误判成重复只是晚一点再记
# （而且 UPDATE/MERGE 不受这条限制，agent 仍可把新细节并进已有记录）。
NEAR_DUPLICATE_RATIO = 0.4
# 每个群（含该群成员、含跨群全局）**滚动 24 小时**内最多新增几条记忆。
# 超过之后只接受 UPDATE/MERGE/DELETE：改已有的事实不受限，凭空长新事实受限。
DEFAULT_DAILY_ADD_LIMIT = 8

# --- TTL 收口（2026-09-27 体检后加的机械兜底）--------------------------------
# 由来：真库里 22 条带过期时间的活跃记忆从 +30 天一路铺到 +3648 天，而"最近觉得消费
# 有点高""有工学椅挺好睡""在寝室喝酒，倾向买瓶便宜威士忌"这类**当时的处境**拿到了
# 1 年甚至 10 年；同样是喝酒偏好，有人 null（永不过期）。TTL 由维护 agent 随手填，
# 没人管，结果就是"该忘的忘不掉，三个月前的一次宵夜会被当成今晚的事讲出来"。
#
# 两条规则，都不问模型：
#   1. 每种 kind 一个上限，超了就夹回来；
#   2. 内容里带"最近/现在/打算"这类**时间限定的处境**，一律按短 TTL 处理——
#      哪怕模型填了 null 或 3650。宁可早忘。
TTL_CAP_DAYS = {
    "name": 3650,
    "boundary": 3650,
    "preference": 365,
    "group_fact": 180,
    "topic_summary": 365,
}
# 带时间限定的处境最多留这么久。
STATE_TTL_DAYS = 30
STATE_MARKERS = (
    "最近", "目前", "现在", "暂时", "这几天", "这两天", "这阵子", "今天", "今晚",
    "昨天", "打算", "准备", "计划", "正在", "还没", "月底", "下周", "明天",
)
# 只对"关于某人/某群的事实"生效；称呼和边界是长期承诺，不按处境打折。
STATE_KINDS = frozenset({"preference", "group_fact", "topic_summary"})

# --- 写入的第三道机械兜底：不许写"关于这个系统自身" --------------------------
# 这道闸的由来（2026-09-27）：维护 prompt 里明令不写，但真库里还是活着 6 条
# （"看机器人反驳""本人确认该条在 prompt 中被列为不愿提及内容""修机器人上下文问题"
# "测试过用显示替换…"）。这类内容每轮被检索回来，等于反复提醒她自己是程序——
# AGENTS.md §2.2 要防的就是这个。所以做成机械兜底：命中就拒绝写入，理由进 audit。
#
# 判据 2026-09-30 迁到 `memory_filters.py` 并加厚——那里每条规则都附了**真实漏进来的原文**。
# 原来那份宽判据（机器人|bot|prompt|…）现在只是其中一条，不再是唯一一道：
# "做云茹的那个""自制AI角色云茹""命中率""长期记忆用 rag 还是 sql""mimo 最好用"
# 都没被它挡住——最后一条至今活在长期记忆里，前几条进了压缩摘要。
# 当时刻意不含"云茹"（按名字拦会砍掉 19/50 条），现在改成**要求系统词汇出现在附近**，
# 而不是见名字就砍。
AFFINITY_REASON_REDACTED = "（依据涉及不该留档的内容，已略去：%s）"

# 检索时不算"话题词"的关系词与填充词。
#
# 为什么不是按频率砍：真库里被误命中的词是「群友」（全库 2 条）、「是否」（2 条）、
# 「评价」（2 条）——**频率很低，但一个话题信息都没有**。而正确的那个命中靠的是
# 「喜欢」（全库 12 条）。按"高频词降权"会砍掉对的、留下错的，所以按词性砍：
# 这些词命中只能说明两条文本语法上像，不说明在说同一件事。
WEAK_TERMS = frozenset({
    "群友", "是否", "评价", "觉得", "用户", "本群", "在本", "自称", "表示", "回应",
    "要求", "提到", "身份", "角色", "之后", "会被", "本人", "时候", "有些", "一下",
    "一起", "其他", "别人", "大家", "之类", "这种", "那样", "感觉", "认为", "打算",
    "容易", "倾向", "主动", "接受", "追问", "什么样", "为什么", "怎么样",
})

# --- 好感度（关系）---------------------------------------------------------
# 两个轴各 4 档，内部存 0..3，**数字不给模型看**（模型对数字的分寸感差，
# 同样的分值每次表现会飘，也没法写测试）。档位文案在 `stage3_runtime.py`。
AFFINITY_MIN, AFFINITY_MAX = 0, 3
# 默认：不认识的人 —— 生疏(0) + 如常(0)。查不到、读库失败、记忆关掉，一律回落到这里。
DEFAULT_CLOSENESS = 0
# 防备四档：0=如常 1=收着 2=留意 3=戒备。**如常是最低档**，不是"零防备"——
# 陌生人本来就不该被交底，往下没有更松的档位可言。
DEFAULT_GUARDEDNESS = 0
# 滚动 24 小时每个轴的**净**变化上限。
AFFINITY_DAILY_CAP = 2
# 防备向下：7 天最多 1 档，而且只有维护 agent（复盘）能降。判定 agent 只升不降。
GUARD_RELAX_PER_WEEK = 1
# 多久没说话就衰减。衰减是机械的，不问模型。
AFFINITY_DECAY_SECONDS = 30 * DAY
# 衰减时亲近最低停在这一档（认得）：不熟可以，但"聊过的人"不该退回全然的陌生人。
CLOSENESS_DECAY_FLOOR = 1

# 单字与高频虚词命中没有区分度，只会把不相关记录抬上来。
STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "我", "你", "他", "她", "它", "们",
    "这", "那", "有", "没", "不", "吗", "呢", "吧", "啊", "呀", "就", "也",
    "都", "很", "会", "要", "把", "被", "给", "对", "为", "以", "及", "或",
    "一个", "什么", "怎么", "可以", "自己", "这个", "那个", "我们", "你们",
})


class MemoryStore:
    def __init__(self, path: Path | str, *, retention_seconds: float = 7 * DAY, clock=time.time,
                 protect=False, daily_add_limit: int = DEFAULT_DAILY_ADD_LIMIT,
                 near_duplicate_ratio: float = NEAR_DUPLICATE_RATIO):
        self.path = Path(path)
        self.retention_seconds = retention_seconds
        self.clock = clock
        self._init_lock = threading.Lock()
        self._ready = False
        self._protect = protect
        self.daily_add_limit = daily_add_limit
        self.near_duplicate_ratio = near_duplicate_ratio

    @contextmanager
    def connection(self):
        # All callers run in worker threads; SQLite also has a short lock timeout.
        with self._init_lock:
            if not self._ready:
                if self._protect:
                    from .memory_config import protect_directory
                    protect_directory(self.path.parent)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path, timeout=0.1)) as db:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    # 0=全新；1=旧版本，需要迁移；其余（含未来版本与脏值）一律拒绝。
                    # 这里必须显式列出可迁移的旧版本，否则下面 v1 分支是死代码。
                    if version not in {0, 1, 2, 3, 4, 5, SCHEMA_VERSION}:
                        raise MemoryValidationError("unsupported_schema")
                    db.execute("PRAGMA journal_mode=WAL")
                    if version == 0:
                        db.executescript(_FRESH_SCHEMA)
                    else:
                        if version in {1, 2, 3}:
                            # v1/v2 → v3：只加表，不改动既有数据（v1 顺带补上 archive）。
                            db.executescript("""
                            BEGIN IMMEDIATE;
                            CREATE TABLE IF NOT EXISTS archive (
                                id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_key TEXT NOT NULL,
                                subject_user_id TEXT, kind TEXT NOT NULL, normalized_key TEXT NOT NULL,
                                content TEXT NOT NULL, confidence REAL NOT NULL,
                                created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
                                revision INTEGER NOT NULL, deleted_at REAL NOT NULL, delete_reason TEXT NOT NULL);
                            CREATE INDEX IF NOT EXISTS archive_deleted_at ON archive(deleted_at);
                            CREATE TABLE IF NOT EXISTS relationships (
                                user_id TEXT PRIMARY KEY, closeness INTEGER NOT NULL DEFAULT 0,
                                guardedness INTEGER NOT NULL DEFAULT 0,
                                updated_at REAL NOT NULL, last_seen_at REAL NOT NULL);
                            CREATE TABLE IF NOT EXISTS relationship_log (
                                user_id TEXT NOT NULL, axis TEXT NOT NULL, before_value INTEGER NOT NULL,
                                after_value INTEGER NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
                                occurred_at REAL NOT NULL);
                            CREATE INDEX IF NOT EXISTS relationship_log_recent
                                ON relationship_log(user_id, axis, occurred_at);
                            COMMIT;
                            """)
                        if version in {1, 2, 3}:
                            # v3 → v4：只加两张表（记忆↔人的对应、本群见过的人），
                            # 并按已有的 subject_user_id 给旧记录补一行关联人。
                            db.executescript("""
                            BEGIN IMMEDIATE;
                            CREATE TABLE IF NOT EXISTS record_subjects (
                                record_id TEXT NOT NULL, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                                PRIMARY KEY(record_id, user_id));
                            CREATE INDEX IF NOT EXISTS record_subjects_lookup
                                ON record_subjects(group_id, user_id);
                            CREATE TABLE IF NOT EXISTS people (
                                group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                                name TEXT NOT NULL, updated_at REAL NOT NULL,
                                PRIMARY KEY(group_id, user_id));
                            INSERT OR IGNORE INTO record_subjects(record_id, user_id, group_id)
                                SELECT id, subject_user_id,
                                       CASE WHEN scope_type='user_group'
                                            THEN substr(scope_key, 1, instr(scope_key, ':') - 1)
                                            ELSE '' END
                                FROM records
                                WHERE subject_user_id IS NOT NULL AND subject_user_id != '';
                            COMMIT;
                            """)
                        if version in {1, 2, 3, 4}:
                            # v4 → v5：把单表拆成"中短期 + 长期"两张表。
                            #
                            # 用户 2026-09-28 决定要两库（新记忆先进中短期，强化后才进长期），
                            # 并选了**两张独立表**。为了避免"隔离/删除/归档各写两遍然后漂移"，
                            # 代码里所有语句都按表名参数化（见 _t / TIERS），这里是迁移：
                            #   1. records 改名成 memory_short（老数据默认留在中短期）；
                            #   2. 建 memory_long（列相同，多一个 promoted_at）；
                            #   3. 按用户选定的强化规则把够格的搬到长期：
                            #      name/boundary（称呼与红线）或 revision>=2（被后来的证据再确认过）。
                            columns = {row[1] for row in db.execute("PRAGMA table_info(records)")}
                            adds = "".join(
                                f"ALTER TABLE memory_short ADD COLUMN {name} "
                                f"{'INTEGER NOT NULL DEFAULT 0' if name == 'durable' else 'REAL'};\n"
                                for name in ("durable", "reminded_at") if name not in columns)
                            db.executescript(_MIGRATE_V5_TEMPLATE.format(adds=adds))
                        if version in {1, 2, 3, 4, 5}:
                            # v5 → v6：加 person_aliases（一个人多个 QQ 号）。
                            # 只加表、不猜任何别名——这张表的内容只能由人写进去。
                            db.executescript("""
                            BEGIN IMMEDIATE;
                            CREATE TABLE IF NOT EXISTS person_aliases (
                                alias_user_id TEXT PRIMARY KEY,
                                canonical_user_id TEXT NOT NULL,
                                note TEXT NOT NULL DEFAULT '',
                                created_at REAL NOT NULL,
                                CHECK (alias_user_id <> canonical_user_id));
                            CREATE INDEX IF NOT EXISTS person_aliases_canonical
                                ON person_aliases(canonical_user_id);
                            PRAGMA user_version=6;
                            COMMIT;
                            """)
                        if version == SCHEMA_VERSION:
                            # 已经在 v5 的库也可能缺那张兼容视图（比如它是在视图加进来之前
                            # 就迁过来的）。每次启动补一次，幂等。
                            db.executescript(_COMPAT_VIEW_SQL)
                    self._ready = True
        db = sqlite3.connect(self.path, timeout=0.1)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _record(row, tier: str = "short") -> MemoryRecord:
        data = {name: row[name] for name in MemoryRecord.__dataclass_fields__ if name in row.keys()}
        data["tier"] = tier
        return MemoryRecord(**data)

    @staticmethod
    def _t(tier: str) -> str:
        """中短期 / 长期两张表；语句只按表名参数化，不把逻辑抄两遍。"""

        return TABLES[tier]

    @staticmethod
    def _both_tables() -> tuple[str, ...]:
        return tuple(TABLES[tier] for tier in TIERS)

    def _alias_map(self, db=None) -> dict[str, str]:
        """`别名 → 规范号` 的快照。按库缓存，只有人能改它。

        缓存不设过期：写这张表的只有操作者动作（脚本 / 面板），那些地方收尾时会
        `alias_map_changed()` 主动作废。查询可能要查两次（换号一次、排除本群那份时再用
        号查一次），命中率的收益是实打实的。
        """

        key = str(self.path)
        cached = _ALIAS_CACHE.get(key)
        if cached is not None:
            return cached
        if db is not None:
            rows = db.execute("SELECT alias_user_id, canonical_user_id FROM person_aliases")
        else:
            with self.connection() as own:
                rows = own.execute(
                    "SELECT alias_user_id, canonical_user_id FROM person_aliases").fetchall()
        mapping = {str(row[0]): str(row[1]) for row in rows}
        _ALIAS_CACHE[key] = mapping
        return mapping

    def alias_map_changed(self) -> None:
        """别名表被改过之后叫一声，作废缓存。"""

        _ALIAS_CACHE.pop(str(self.path), None)

    def canonical_user(self, user_id: str, *, db=None) -> str:
        """把 QQ 号换成**这个人的规范号**；没登记过就原样返回。

        这是"一个人可以有多个号"的唯一入口：读取时把号换掉，写入时也按它落库，
        于是记忆天然聚到一处。**没有任何模糊匹配**——只有 `person_aliases` 里
        操作者亲手写下的对应关系才算数。
        """

        current = str(user_id or "")
        if not current:
            return current
        mapping = self._alias_map(db)
        # 防御环形登记（A→B, B→A）：真出现也只是取到一个稳定值，不会转不出来。
        for _ in range(8):
            nxt = mapping.get(current)
            if not nxt or nxt == current:
                return current
            current = nxt
        return current

    def _namespaces(self, group_id: str, user_id: str, *, db=None):
        """这个说话人在本群能看到的三个命名空间。

        三个都按**规范号**算：换号说话的人，本群那份、跨群那份都该是他的。
        `group` 那份本来就跟人无关，原样返回。

        `db`：调用方**已经开着连接**时必须传进来。SQLite 是库级写锁，
        在事务里再开一个连接去读会直接 `database is locked`。
        """

        person = self.canonical_user(user_id, db=db)
        return (("user_group", f"{group_id}:{person}"), ("user_global", person),
                ("group", group_id))

    def note_person(self, group_id: str, user_id: str, name: str, *, now: float | None = None) -> None:
        """记下"这个群里这个 QQ 号显示成什么名字"。

        QQ 号是权威身份，显示名会改；渲染记忆归属、给维护 agent 挑关联人都要用它。
        `append()` 每收一条消息顺手调一次，测试也可以直接调。
        """

        clean = sanitize_chat_text(name or "", max_length=40).strip()
        if not clean:
            return
        with self.connection() as db:
            db.execute(
                "INSERT INTO people VALUES (?,?,?,?) "
                "ON CONFLICT(group_id,user_id) DO UPDATE SET name=excluded.name, "
                "updated_at=excluded.updated_at",
                (group_id, user_id, clean, self.clock() if now is None else now),
            )

    def append(self, event: InboxEvent, *, sender_name: str = "") -> bool:
        now = self.clock()
        if event.occurred_at + self.retention_seconds <= now:
            return False
        if sender_name:
            # 顺手把"本群见过的人"记下来（它有自己的连接，失败也不该挡住消息入库）。
            self.note_person(event.group_id, event.user_id, sender_name, now=now)
        with self.connection() as db:
            if db.execute("SELECT 1 FROM receipts WHERE id=?", (event.id,)).fetchone():
                return False
            # 按群配额 + 全局上限：单个活跃大群不应吃满全部配额，把其他群饿死。
            if db.execute("SELECT COUNT(*) FROM inbox WHERE group_id=?", (event.group_id,)).fetchone()[0] >= MAX_INBOX_EVENTS_PER_GROUP:
                return False
            if db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] >= MAX_INBOX_EVENTS:
                return False
            inserted = db.execute(
                "INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?,NULL)",
                (event.id, event.group_id, event.user_id, event.speaker, event.text,
                 event.occurred_at, event.occurred_at + self.retention_seconds),
            ).rowcount
            # 保留较新的活动时间；OR IGNORE 不会刷新已存在行，否则周期复查会
            # 反复回到同一批长期只产生 IGNORE 的会话。
            db.execute(
                "INSERT INTO participants VALUES (?,?,?) "
                "ON CONFLICT(group_id,user_id) DO UPDATE SET reviewed_at=MAX(reviewed_at,excluded.reviewed_at)",
                (event.group_id, event.user_id, now),
            )
            return bool(inserted)

    def _cleanup(self, db, now):
        db.execute("DELETE FROM inbox WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM receipts WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM replays WHERE expires_at<=?", (now,))
        db.execute("UPDATE inbox SET lease_id=NULL WHERE lease_id IN (SELECT id FROM leases WHERE until_at<=?)", (now,))
        db.execute("DELETE FROM leases WHERE until_at<=?", (now,))
        for tier in TIERS:
            table = TABLES[tier]
            for row in db.execute(
                    f"SELECT * FROM {table} WHERE status='active' AND expires_at<=?", (now,)).fetchall():
                self._tombstone(db, row, row["expires_at"])
                # 保留期到期的记录同样归档，理由与 DELETE 一致：清空前留一份快照。
                self._archive(db, row, now, "retention_expired")
            db.execute(f"UPDATE {table} SET status='deleted',content='',source_event_ids='[]',updated_at=?"
                       " WHERE status='active' AND expires_at<=?", (now, now))
            db.execute(f"DELETE FROM {table} WHERE status!='active' AND updated_at<?",
                       (now - self.retention_seconds,))
        # 关联人行只跟 record_id 走：记录整行被清掉之后要顺手把孤儿删掉。
        db.execute("DELETE FROM record_subjects WHERE record_id NOT IN "
                   "(SELECT id FROM memory_short UNION SELECT id FROM memory_long)")
        db.execute("DELETE FROM audit WHERE created_at<?", (now - 30 * DAY,))
        # 归档保留 30 天：比 inbox 久得多，因为它承担的是"误删之后还能捞回来"。
        db.execute("DELETE FROM archive WHERE deleted_at<?", (now - ARCHIVE_RETENTION_DAYS * DAY,))

    def cleanup(self):
        with self.connection() as db:
            self._cleanup(db, self.clock())

    def claim(self, allowed_groups: set[str], *, lease_seconds=120.0, limit=40) -> MemoryBatch | None:
        now = self.clock()
        with self.connection() as db:
            self._cleanup(db, now)
            # 可认领的事件 = 新入库且未被认领的，或上一轮未被模型引用而进入重放的。
            pending = """SELECT i.group_id,i.user_id,MIN(i.occurred_at) AS age
                FROM inbox i
                LEFT JOIN leases l ON i.group_id=l.group_id AND i.user_id=l.user_id
                LEFT JOIN replays r ON r.id=i.id
                WHERE i.lease_id IS NULL AND l.id IS NULL
                  AND (r.id IS NULL OR (r.attempts<? AND r.expires_at>?))
                GROUP BY i.group_id,i.user_id ORDER BY age"""
            pairs = db.execute(pending, (MAX_REPLAY_ATTEMPTS, now)).fetchall()
            pair = next((p for p in pairs if p[0] in allowed_groups), None)
            if pair is None:
                # Periodic review can merge or forget existing memories even without new chat.
                pairs = db.execute("""SELECT p.group_id,p.user_id FROM participants p
                    LEFT JOIN leases l ON p.group_id=l.group_id AND p.user_id=l.user_id
                    WHERE p.reviewed_at<=? AND l.id IS NULL ORDER BY p.reviewed_at LIMIT 100""", (now - DAY,)).fetchall()
                pair = next((p for p in pairs if p[0] in allowed_groups), None)
            if pair is None:
                return None
            group_id = pair[0]
            # `pair[1]` 是**收件箱里那个号**，只用来取回它自己的消息；
            # 之后一切都按**规范号**算（记忆落库、关联人、租约、participants），
            # 换号说话的人于是永远只有一份记忆，也不会被当成两个人整理两遍。
            inbox_user_id = pair[1]
            user_id = self.canonical_user(inbox_user_id, db=db)
            rows = db.execute(
                "SELECT i.* FROM inbox i LEFT JOIN replays r ON r.id=i.id "
                "WHERE i.group_id=? AND i.user_id=? AND i.lease_id IS NULL "
                "AND (r.id IS NULL OR (r.attempts<? AND r.expires_at>?)) "
                "ORDER BY i.occurred_at,i.id LIMIT ?",
                (group_id, inbox_user_id, MAX_REPLAY_ATTEMPTS, now, limit)).fetchall()
            records = []
            for scope, key in self._namespaces(group_id, user_id, db=db):
                for tier in TIERS:
                    records.extend(self._record(r, tier) for r in db.execute(
                        f"SELECT * FROM {TABLES[tier]} WHERE scope_type=? AND scope_key=? AND status='active'"
                        " AND visibility='group_safe' AND (expires_at IS NULL OR expires_at>?)"
                        " ORDER BY COALESCE(last_reviewed_at,0),updated_at DESC LIMIT 100",
                        (scope, key, now)))
            if not rows and not records:
                db.execute("UPDATE participants SET reviewed_at=? WHERE group_id=? AND user_id=?", (now, group_id, user_id))
                return None
            token = uuid.uuid4().hex
            db.execute("INSERT INTO leases VALUES (?,?,?,?)", (token, group_id, user_id, now + lease_seconds))
            db.executemany("UPDATE inbox SET lease_id=? WHERE id=?", ((token, r["id"]) for r in rows))
            events = tuple(InboxEvent(**{k: row[k] for k in InboxEvent.__dataclass_fields__}) for row in rows)
            return MemoryBatch(token, group_id, user_id, events, tuple(records), now + lease_seconds)

    def discard_disallowed(self, allowed_groups: set[str]):
        """Drop pending material only when the host explicitly changes its allowlist."""
        with self.connection() as db:
            placeholders = ",".join("?" for _ in allowed_groups)
            if not placeholders:
                db.execute("DELETE FROM inbox")
                db.execute("DELETE FROM leases")
                db.execute("DELETE FROM replays")
                return
            db.execute(f"DELETE FROM inbox WHERE group_id NOT IN ({placeholders})", tuple(allowed_groups))
            db.execute(f"DELETE FROM leases WHERE group_id NOT IN ({placeholders})", tuple(allowed_groups))
            # 重放表只按事件 id 关联，需清掉已经没有对应 inbox 行的孤儿记录。
            db.execute("DELETE FROM replays WHERE id NOT IN (SELECT id FROM inbox)")

    def release(self, batch: MemoryBatch):
        with self.connection() as db:
            db.execute("UPDATE inbox SET lease_id=NULL WHERE lease_id=?", (batch.id,))
            db.execute("DELETE FROM leases WHERE id=?", (batch.id,))

    def commit(self, batch: MemoryBatch, raw: str):
        operations = parse_operations(raw, batch)
        now = self.clock()
        with self.connection() as db:
            if db.execute("SELECT 1 FROM audit WHERE batch_id=?", (batch.id,)).fetchone():
                return ()  # Same completed batch can be acknowledged again after a retry.
            lease = db.execute("SELECT until_at FROM leases WHERE id=?", (batch.id,)).fetchone()
            if lease is None or lease[0] <= now:
                raise MemoryValidationError("expired_lease")
            actual = {r[0] for r in db.execute("SELECT id FROM inbox WHERE lease_id=? AND expires_at>?", (batch.id, now))}
            if actual != {e.id for e in batch.events}:
                raise MemoryValidationError("stale_events")
            # 只把**真正生效**的操作返回给调用方：被机械兜底拒掉的 ADD 不算数，
            # 否则指标与"哪些证据被引用了"都会被算错。
            applied = []
            for index, op in enumerate(operations):
                key = batch.scope_key(op.scope_type) if op.scope_type else ""
                # 写入路径上的第一道闸：不许新增/改出"关于这个系统自身"的内容。
                # 放在最前面，三种写入操作（ADD/UPDATE/MERGE）一视同仁。
                rule = system_self_rule(op.content) if op.op in {"ADD", "UPDATE", "MERGE"} else ""
                if rule:
                    db.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                               (batch.id, index, "rejected_self_reference", op.scope_type, now))
                    logger.warning("memory_add_rejected reason=self_reference rule=%s scope=%s group=%s",
                                   rule, op.scope_type, batch.group_id)
                    continue
                targets = []
                for rid, revision in op.targets:
                    row = self._find_record(db, rid, revision, now)
                    if row is None:
                        raise MemoryValidationError("stale_revision")
                    targets.append(row)
                if op.op == "DELETE":
                    for row in targets:
                        self._tombstone(db, row, now)
                        # 先归档整条内容，再清空——顺序不能反，反了就只剩空壳。
                        self._archive(db, row, now, "agent_delete")
                        # Erase all historic versions of the same fact, not only its current version.
                        # 两张表都要清：同一条事实可能刚被强化搬去长期。
                        for tier in TIERS:
                            db.execute(f"UPDATE {TABLES[tier]} SET status='deleted',content='',"
                                       "source_event_ids='[]',updated_at=? "
                                       "WHERE scope_type=? AND scope_key=? AND normalized_key=?",
                                       (now, op.scope_type, key, row["normalized_key"]))
                elif op.op in {"ADD", "UPDATE", "MERGE"}:
                    if op.op == "ADD":
                        rejection = self._add_rejection(db, batch, key, op, now)
                        if rejection:
                            # **逐条跳过**，不让整批失败：整批失败会变成"重试—再失败"的循环，
                            # 而这条证据只会进一次重放，不会无限重来。
                            db.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                                       (batch.id, index, rejection, op.scope_type, now))
                            logger.warning(
                                "memory_add_rejected reason=%s scope=%s group=%s",
                                rejection, op.scope_type, batch.group_id,
                            )
                            continue
                    tomb = db.execute("SELECT deleted_at FROM tombstones WHERE scope_type=? AND scope_key=? AND normalized_key=?",
                                      (op.scope_type, key, op.normalized_key)).fetchone()
                    evidence_time = max((e.occurred_at for e in batch.events if e.id in op.evidence_event_ids and e.speaker == "user"), default=0)
                    if tomb and evidence_time <= tomb[0]:
                        raise MemoryValidationError("deleted_evidence")
                    for row in targets:
                        db.execute(f"UPDATE {TABLES[row['tier']]} SET status='superseded',updated_at=? WHERE id=?",
                                   (now, row["id"]))
                        if row["normalized_key"] != op.normalized_key:
                            self._tombstone(db, row, now)
                    # 同一个键在两张表里都算占用：否则同一条事实会在两边各长一条。
                    for tier in TIERS:
                        existing = db.execute(
                            f"SELECT 1 FROM {TABLES[tier]} WHERE scope_type=? AND scope_key=? "
                            "AND normalized_key=? AND status='active'",
                            (op.scope_type, key, op.normalized_key)).fetchone()
                        if existing:
                            raise MemoryValidationError("duplicate_key_use_update")
                    used = sum(db.execute(
                        f"SELECT COUNT(*) FROM {TABLES[tier]} WHERE scope_type=? AND scope_key=? "
                        "AND status='active'", (op.scope_type, key)).fetchone()[0] for tier in TIERS)
                    if used >= 100:
                        raise MemoryValidationError("namespace_full")
                    revision = max((r["revision"] for r in targets), default=0) + 1
                    # 先定层，再按那一层的窗口算 TTL：长期的窗口比中短期长得多，
                    # 顺序反了会让刚进长期的记录仍然只活 14 天。
                    tier = self._target_tier(op, revision)
                    ttl_days = self._effective_ttl_days(op.kind, op.content, op.ttl_days, tier=tier)
                    record_id = uuid.uuid4().hex
                    common = (record_id, op.scope_type, key,
                              batch.user_id if op.scope_type != "group" else None,
                              op.kind, op.normalized_key, op.content, op.confidence, now, now,
                              now + ttl_days * DAY if ttl_days else None, revision,
                              json.dumps(op.evidence_event_ids), 1 if op.durable else 0)
                    base = ("id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,"
                            "confidence,created_at,updated_at,expires_at,revision,source_event_ids,durable")
                    if tier == "long":
                        # 长期表多一列 promoted_at；短期表没有它。
                        db.execute(f"INSERT INTO {TABLES['long']} ({base},promoted_at) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*common, now))
                    else:
                        db.execute(f"INSERT INTO {TABLES['short']} ({base}) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", common)
                    if tier == "long":
                        # 直接进长期的理由要留痕，方便事后核对"哪些是被强化上来的"。
                        # 编号加个大偏移：audit 的主键是 (batch_id, operation_index)，
                        # 和这一条 op 自己那行不能撞。
                        db.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                                   (batch.id, PROMOTION_AUDIT_OFFSET + index, "promoted_long",
                                    op.scope_type, now))
                    # 关联人行记的是**这条事实是在哪个群学到的**：跨人召回只借本群学到的事，
                    # 所以 user_global 的记录在别的群里不会被别人借走。
                    self._link_subjects(db, batch, op, record_id, batch.group_id)
                elif op.op == "AFFINITY":
                    # 走**同一个**写入路径：上限与流水都在 `_apply_affinity` 里，
                    # 判定 agent 那条通道也调它。这里是维护 agent 一侧，可以降防备。
                    rejection = self._apply_affinity(
                        db, op.affinity_user_id,
                        closeness=op.closeness_delta, guardedness=op.guardedness_delta,
                        reason=op.content, source="maintenance", now=now, may_lower_guard=True,
                    )
                    if rejection:
                        db.execute("INSERT INTO audit VALUES (?,?,?,?,?)",
                                   (batch.id, index, rejection, op.scope_type, now))
                        logger.warning(
                            "memory_affinity_rejected reason=%s group=%s", rejection, batch.group_id
                        )
                        continue
                db.execute("INSERT INTO audit VALUES (?,?,?,?,?)", (batch.id, index, op.op, op.scope_type, now))
                applied.append(op)
            if not operations:
                db.execute("INSERT INTO audit VALUES (?,0,'IGNORE','',?)", (batch.id, now))
            for tier_name in TIERS:
                ids = [r.id for r in batch.records if r.tier == tier_name]
                if ids:
                    db.executemany(f"UPDATE {TABLES[tier_name]} SET last_reviewed_at=? WHERE id=?",
                                   ((now, rid) for rid in ids))
            # 先结算事件（决定哪些进重放表），再清掉本批已认领的 inbox 行；
            # 顺序反了会把刚放进重放表的事件一起删掉，重放就永远不会发生。
            # 传**生效的**操作：被拒的 ADD 引用的证据不算"已引用"，
            # 于是它会进一次重放，agent 还有机会把它并进已有记录。
            self._settle_batch_events(db, batch, applied)
            # 未离开 inbox 的事件（进入重放的）必须解除本次租约，否则它们
            # 会一直挂在已删除的租约 id 上，永远无法再次被 claim。
            db.execute("UPDATE inbox SET lease_id=NULL WHERE lease_id=?", (batch.id,))
            db.execute("DELETE FROM leases WHERE id=?", (batch.id,))
            db.execute("UPDATE participants SET reviewed_at=? WHERE group_id=? AND user_id=?", (now, batch.group_id, batch.user_id))
            # 本批出现过的人刷新"最近说话时间"。只刷新**已有**关系行：没有行的人
            # 还没跟任何人建立过关系，别为了记时间给每个人建一行。
            speakers = {e.user_id for e in batch.events if e.speaker == "user"}
            db.executemany(
                "UPDATE relationships SET last_seen_at=? WHERE user_id=? AND last_seen_at<?",
                ((now, user_id, now) for user_id in speakers),
            )
        return tuple(applied)

    # --- 好感度（关系）：一个写入路径，两个调用者 -------------------------

    def relationship(self, user_id: str) -> tuple[int, int]:
        """读 (亲近, 防备)。没有记录、读失败都回落到默认档——不能因为查不到就当她跟谁都熟。"""

        try:
            with self.connection() as db:
                row = db.execute(
                    "SELECT closeness,guardedness FROM relationships WHERE user_id=?",
                    (self.canonical_user(user_id, db=db),)
                ).fetchone()
        except MemoryValidationError:
            return DEFAULT_CLOSENESS, DEFAULT_GUARDEDNESS
        if row is None:
            return DEFAULT_CLOSENESS, DEFAULT_GUARDEDNESS
        return int(row[0]), int(row[1])

    def relationship_detail(self, user_id: str) -> dict[str, object]:
        """给 `/super affinity` 看：档位 + 最近一次变动的依据、来源与时间。只读。"""

        with self.connection() as db:
            person = self.canonical_user(user_id, db=db)
            row = db.execute(
                "SELECT closeness,guardedness,updated_at FROM relationships WHERE user_id=?",
                (person,),
            ).fetchone()
            last = db.execute(
                "SELECT axis,before_value,after_value,reason,source,occurred_at FROM relationship_log "
                "WHERE user_id=? ORDER BY occurred_at DESC, rowid DESC LIMIT 1",
                (person,),
            ).fetchone()
        return {
            "closeness": int(row["closeness"]) if row else DEFAULT_CLOSENESS,
            "guardedness": int(row["guardedness"]) if row else DEFAULT_GUARDEDNESS,
            "updated_at": float(row["updated_at"]) if row else 0.0,
            "last": dict(last) if last else None,
        }

    def recent_relationship_changes(self, since: float, *, limit: int = 20) -> tuple[dict[str, object], ...]:
        """某段时间里的关系变动（给每日汇报当素材）。只返回档位与那句依据，没有聊天正文。"""

        with self.connection() as db:
            rows = db.execute(
                "SELECT user_id,axis,before_value,after_value,reason,source,occurred_at "
                "FROM relationship_log WHERE occurred_at>=? ORDER BY occurred_at DESC LIMIT ?",
                (since, max(1, min(100, int(limit)))),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def top_relationships(self, *, limit: int = 20) -> tuple[dict[str, object], ...]:
        """最近打过交道的人（按最近说话时间倒序）。给汇报用的"跟谁说过话"。"""

        with self.connection() as db:
            rows = db.execute(
                "SELECT user_id,closeness,guardedness,last_seen_at FROM relationships "
                "ORDER BY last_seen_at DESC LIMIT ?",
                (max(1, min(100, int(limit))),),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def apply_affinity(
        self,
        user_id: str,
        *,
        closeness: int = 0,
        guardedness: int = 0,
        reason: str = "",
        source: str = "unknown",
        may_lower_guard: bool = False,
    ) -> str:
        """**唯一的写入路径**：判定 agent 与维护 agent 都调这里。

        返回空串表示已生效；否则是拒绝码（调用方记日志/审计，不静默改小、
        不做"部分生效"）。上限、流水、防备不对称全在这一处。
        """

        with self.connection() as db:
            return self._apply_affinity(
                db, self.canonical_user(user_id, db=db), closeness=closeness,
                guardedness=guardedness, reason=reason, source=source, now=self.clock(),
                may_lower_guard=may_lower_guard,
            )

    def reset_relationship(self, user_id: str, *, reason: str = "超管重置") -> bool:
        """回落默认档（生疏 + 如常），并留一条流水。

        **不删行**：留痕才能解释"她为什么突然变了"。超管重置是人工纠正，
        不走上限——它本来就是用来盖掉模型误判的。
        """

        now = self.clock()
        with self.connection() as db:
            user_id = self.canonical_user(user_id, db=db)
            row = db.execute(
                "SELECT closeness,guardedness FROM relationships WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                return False
            current = (int(row[0]), int(row[1]))
            if current == (DEFAULT_CLOSENESS, DEFAULT_GUARDEDNESS):
                return False
            db.execute(
                "UPDATE relationships SET closeness=?,guardedness=?,updated_at=?,last_seen_at=? "
                "WHERE user_id=?",
                (DEFAULT_CLOSENESS, DEFAULT_GUARDEDNESS, now, now, user_id),
            )
            for axis, before, after in (
                ("closeness", current[0], DEFAULT_CLOSENESS),
                ("guardedness", current[1], DEFAULT_GUARDEDNESS),
            ):
                if before != after:
                    db.execute(
                        "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                        (user_id, axis, before, after, reason, "super_reset", now),
                    )
        return True

    def decay_relationships(self, *, idle_seconds: float = AFFINITY_DECAY_SECONDS) -> int:
        """很久没说话：亲近 −1（最低停在"认得"），防备回"如常"。机械的，不问模型。

        衰减后把 `last_seen_at` 推到当前时间，否则每一轮都会接着降。
        """

        now = self.clock()
        changed = 0
        with self.connection() as db:
            rows = db.execute(
                "SELECT user_id,closeness,guardedness FROM relationships "
                "WHERE last_seen_at<=? AND (closeness>? OR guardedness<>?)",
                (now - idle_seconds, CLOSENESS_DECAY_FLOOR, DEFAULT_GUARDEDNESS),
            ).fetchall()
            for row in rows:
                user_id = row["user_id"]
                new_closeness = max(CLOSENESS_DECAY_FLOOR, int(row["closeness"]) - 1)
                db.execute(
                    "UPDATE relationships SET closeness=?,guardedness=?,updated_at=?,last_seen_at=? "
                    "WHERE user_id=?",
                    (new_closeness, DEFAULT_GUARDEDNESS, now, now, user_id),
                )
                for axis, before, after in (
                    ("closeness", int(row["closeness"]), new_closeness),
                    ("guardedness", int(row["guardedness"]), DEFAULT_GUARDEDNESS),
                ):
                    if before != after:
                        db.execute(
                            "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                            (user_id, axis, before, after, "很久没说话了", "decay", now),
                        )
                changed += 1
        if changed:
            logger.info("relationship_decay count=%s", changed)
        return changed

    def _apply_affinity(
        self, db, user_id: str, *, closeness: int, guardedness: int, reason: str,
        source: str, now: float, may_lower_guard: bool,
    ) -> str:
        """干活的那个。返回拒绝码或空串。所有上限都在这里，谁调都一样。"""

        # 依据里如果写着"他是做云茹的那个人"这类内容，**改档照旧、依据换掉**：
        # 它是唯一写入路径（判定 agent 与维护 agent 都走），所以在这里收口一次就够。
        # 为什么整条不丢：那句话只是"为什么调档"的说明，档位本身来自真实互动，
        # 因为一句话就放弃一次关系更新反而失真。理由改成规则码，超管看 audit 时有据可查。
        rule = system_self_rule(reason)
        if rule:
            logger.warning("relationship_reason_redacted rule=%s source=%s", rule, source)
            reason = AFFINITY_REASON_REDACTED % rule
        # 单次每轴最多 1 档：模型说什么都越不过这条。
        closeness = max(-1, min(1, int(closeness)))
        guardedness = max(-1, min(1, int(guardedness)))
        if closeness == 0 and guardedness == 0:
            return ""
        if guardedness < 0 and not may_lower_guard:
            # 判定 agent 只升不降：防备受惊之后立刻回暖不合理，也给了刷好感一条新路。
            return "affinity_rejected_judge_downgrade"
        row = db.execute(
            "SELECT closeness,guardedness FROM relationships WHERE user_id=?", (user_id,)
        ).fetchone()
        current_closeness = int(row["closeness"]) if row else DEFAULT_CLOSENESS
        current_guardedness = int(row["guardedness"]) if row else DEFAULT_GUARDEDNESS
        if guardedness < 0:
            relaxed = db.execute(
                "SELECT COALESCE(SUM(before_value-after_value),0) FROM relationship_log "
                "WHERE user_id=? AND axis='guardedness' AND before_value>after_value AND occurred_at>?",
                (user_id, now - 7 * DAY),
            ).fetchone()[0]
            if int(relaxed or 0) >= GUARD_RELAX_PER_WEEK:
                return "affinity_rejected_relax_too_soon"
        # 滚动 24 小时的**净**变化上限，每轴分开算。
        for axis, delta in (("closeness", closeness), ("guardedness", guardedness)):
            if delta == 0:
                continue
            net = db.execute(
                "SELECT COALESCE(SUM(after_value-before_value),0) FROM relationship_log "
                "WHERE user_id=? AND axis=? AND occurred_at>?",
                (user_id, axis, now - DAY),
            ).fetchone()[0]
            if abs(int(net or 0) + delta) > AFFINITY_DAILY_CAP:
                return "affinity_rejected_daily_cap"
        new_closeness = max(AFFINITY_MIN, min(AFFINITY_MAX, current_closeness + closeness))
        new_guardedness = max(AFFINITY_MIN, min(AFFINITY_MAX, current_guardedness + guardedness))
        if (new_closeness, new_guardedness) == (current_closeness, current_guardedness):
            # 已经在顶/底了。这不是错误，但也不该记成一次"生效的变动"。
            return "affinity_rejected_at_boundary"
        db.execute(
            "INSERT INTO relationships VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET closeness=excluded.closeness, "
            "guardedness=excluded.guardedness, updated_at=excluded.updated_at, "
            "last_seen_at=MAX(relationships.last_seen_at, excluded.last_seen_at)",
            (user_id, new_closeness, new_guardedness, now, now),
        )
        for axis, before, after in (
            ("closeness", current_closeness, new_closeness),
            ("guardedness", current_guardedness, new_guardedness),
        ):
            if before != after:
                db.execute(
                    "INSERT INTO relationship_log VALUES (?,?,?,?,?,?,?)",
                    (user_id, axis, before, after, reason[:200], source, now),
                )
        logger.info(
            "relationship_changed user=%s closeness=%s->%s guardedness=%s->%s source=%s",
            user_id, current_closeness, new_closeness, current_guardedness, new_guardedness, source,
        )
        return ""

    # --- 只读查询（供超管查看，不修改任何记录） ---------------------------

    def counts(self) -> dict[str, int]:
        """各类计数概览（含两层各自多少条）。只读。"""

        with self.connection() as db:
            short = db.execute(f"SELECT COUNT(*) FROM {TABLES['short']}").fetchone()[0]
            long = db.execute(f"SELECT COUNT(*) FROM {TABLES['long']}").fetchone()[0]
            result = {
                "records": short + long,
                "active_records": sum(db.execute(
                    f"SELECT COUNT(*) FROM {TABLES[tier]} WHERE status='active'").fetchone()[0]
                    for tier in TIERS),
                "short_records": short,
                "long_records": long,
                "inbox": db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0],
                "audit": db.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            }
            for table in ("tombstones", "receipts", "archive"):
                try:
                    result[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.OperationalError:
                    result[table] = 0
            return result

    def list_records(self, *, limit: int = 40, status: str | None = None) -> tuple[list[dict], int]:
        """按更新时间倒序列出记忆（两层混排，带 `tier` 字段）。返回 (本页记录, 总数)。只读。"""

        limit = max(1, min(200, limit))
        collected: list[tuple[float, dict]] = []
        with self.connection() as db:
            total = 0
            for tier in TIERS:
                table = TABLES[tier]
                if status:
                    total += db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE status=?", (status,)).fetchone()[0]
                    fetched = db.execute(
                        f"SELECT * FROM {table} WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                        (status, limit)).fetchall()
                else:
                    total += db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    fetched = db.execute(
                        f"SELECT * FROM {table} ORDER BY status, updated_at DESC LIMIT ?",
                        (limit,)).fetchall()
                for row in fetched:
                    item = self._public_record(row)
                    item["tier"] = tier
                    collected.append((row["updated_at"], item))
        # 两层混排后按更新时间倒序（`_public_record` 是脱敏后的字段集，不含 updated_at）。
        collected.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in collected[:limit]], total

    def list_inbox(self, *, limit: int = 40) -> tuple[list[dict], int]:
        """按时间倒序列出待处理材料。返回 (本页条目, 总数)。只读。"""

        limit = max(1, min(200, limit))
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
            rows = db.execute(
                "SELECT user_id, speaker, text, occurred_at FROM inbox "
                "ORDER BY occurred_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows], total

    def list_audit(self, *, limit: int = 40) -> tuple[list[dict], int]:
        """按时间倒序列出维护操作。返回 (本页条目, 总数)。只读。"""

        limit = max(1, min(200, limit))
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
            rows = db.execute(
                "SELECT op, scope_type, created_at FROM audit "
                "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows], total

    @staticmethod
    def _public_record(row) -> dict:
        """只挑可对外显示的字段，不带 source_event_ids 等内部标识。"""

        return {
            "scope_type": row["scope_type"],
            "kind": row["kind"],
            "normalized_key": row["normalized_key"],
            "content": row["content"],
            "confidence": row["confidence"],
            "status": row["status"],
            "revision": row["revision"],
        }

    def _settle_batch_events(self, db, batch: MemoryBatch, operations) -> None:
        """收尾本批事件：被模型引用过的变成已处理回执，未被引用的给一次重放机会。

        批次最多带 40 条事件而模型每轮最多 5 个操作，因此“未引用”是常态。
        若无条件丢弃，模型任何一次误判都会让那批聊天内容不可逆消失。
        """

        # 一批里只要有任何一个操作引用了证据，就说明模型确实读到了这批事件；
        # 此时它没有引用的那些才是真正“看过但没选中”，值得给一次重放。
        # 反过来，若整批只有 IGNORE（没有任何证据引用），那是模型的显式忽略，
        # 事件应当直接归档为已读，而不是无限重放。
        reviewed = any(op.evidence_event_ids for op in operations)
        cited = {event_id for op in operations for event_id in op.evidence_event_ids}
        settled: list[str] = []
        for event in batch.events:
            # InboxEvent 不携带 expires_at，回执/重放的到期时间与 inbox 行保持一致。
            expires_at = event.occurred_at + self.retention_seconds
            if event.id in cited or not reviewed:
                db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (event.id, expires_at))
                db.execute("DELETE FROM replays WHERE id=?", (event.id,))
                settled.append(event.id)
                continue
            row = db.execute("SELECT attempts FROM replays WHERE id=?", (event.id,)).fetchone()
            attempts = (row[0] if row else 0) + 1
            if attempts >= MAX_REPLAY_ATTEMPTS:
                db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (event.id, expires_at))
                db.execute("DELETE FROM replays WHERE id=?", (event.id,))
                settled.append(event.id)
            else:
                db.execute(
                    "INSERT INTO replays VALUES (?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET attempts=excluded.attempts, expires_at=excluded.expires_at",
                    (event.id, attempts, expires_at),
                )
        # 只有已归档为回执的事件才离开 inbox；进入重放的必须留下。
        db.executemany("DELETE FROM inbox WHERE id=?", ((event_id,) for event_id in settled))

    def _add_rejection(self, db, batch: MemoryBatch, key: str, op, now: float) -> str | None:
        """新增记忆的机械兜底；返回拒绝原因（写进 audit），允许则返回 None。

        两条规则，都不依赖模型自觉：

        1. **近重复**：同一个 scope 里已有在说同一件事的活跃记忆 → 不新增。
           模型的键名纪律拦不住"换个措辞再写一条"，这一条拦得住。
        2. **每日配额**：这个群（含成员与跨群全局）滚动 24 小时内新增条数达上限 →
           只允许改已有事实（UPDATE/MERGE/DELETE），不许再长新事实。
        """

        terms = self._query_terms(op.content)
        if terms:
            rows = []
            for tier in TIERS:
                rows.extend(db.execute(
                    f"SELECT content FROM {TABLES[tier]} WHERE scope_type=? AND scope_key=? AND status='active'",
                    (op.scope_type, key),
                ).fetchall())
            for row in rows:
                other = self._query_terms(row["content"] or "")
                if not other:
                    continue
                overlap = len(terms & other) / min(len(terms), len(other))
                if overlap >= self.near_duplicate_ratio:
                    return "add_rejected_near_duplicate"
        if self.daily_add_limit > 0:
            count = sum(db.execute(
                f"SELECT COUNT(*) FROM {TABLES[tier]} WHERE created_at>=? AND ("
                " scope_key=? OR scope_key LIKE ?"
                " OR (scope_type='user_global' AND scope_key=?))",
                (now - DAY, batch.group_id, f"{batch.group_id}:%", batch.user_id),
            ).fetchone()[0] for tier in TIERS)
            if count >= self.daily_add_limit:
                return "add_rejected_daily_limit"
        return None

    @staticmethod
    def _effective_ttl_days(kind: str, content: str, ttl_days: int | None,
                            tier: str = "short") -> int | None:
        """把模型填的 TTL 夹进策略里（见文件头 TTL_CAP_DAYS 的由来）。

        `None` 表示"长期有效"；只有 name/boundary 和**不带时间限定**的偏好配得上它。
        中短期那一层另有一道更短的上限（`SHORT_TTL_DAYS`）：它的语义就是"还没被强化的
        东西不该按长期事实活着"，强化进长期时才把窗口放大到 kind 上限。
        """

        cap = TTL_CAP_DAYS.get(kind)
        if kind == STATUS_KIND:
            # 状态类只活在中短期，且有硬上限：它描述的是"这几天"的事。
            default = STATUS_TTL_DAYS
            value = default if ttl_days is None else min(int(ttl_days), STATUS_TTL_MAX_DAYS)
            return value
        if ttl_days is not None and cap is not None:
            ttl_days = min(int(ttl_days), cap)
        if kind in STATE_KINDS and any(marker in (content or "") for marker in STATE_MARKERS):
            ttl_days = STATE_TTL_DAYS if ttl_days is None else min(int(ttl_days), STATE_TTL_DAYS)
        if tier == "short":
            short_cap = SHORT_TTL_DAYS.get(kind, DEFAULT_SHORT_TTL_DAYS)
            ttl_days = short_cap if ttl_days is None else min(int(ttl_days), short_cap)
        return ttl_days

    def clamp_ttls(self) -> int:
        """把**已有**记录的过期时间按当前策略夹一遍；只动 `expires_at`，不动内容。

        为什么需要它：`TTL_CAP_DAYS` / `STATE_TTL_DAYS` 是 2026-09-27 体检之后才有的策略，
        之前的记录带着模型随手填的值（真库里 7/44 条超标，最长 +3648 天）。新策略只管
        新写入，所以历史记录得单独夹一次。

        基准取 `updated_at`（这条事实最后一次被确认的时间）而不是"现在"：刚刚被确认过的
        事实拿满整个窗口，早就没人再提的处境则会很快到期——这正是 TTL 想表达的语义。

        夹是**单向的**：只会把过长的过期时间改短、或给"永不过期"的处境补一个期限，
        **绝不延长**已有记录的寿命（第一版没做这个约束，结果把 6 条记录的过期时间
        顺手往后推了"创建到最近更新"的那段距离——策略收口不该反过来放水）。
        调用者：`data/ttl_dryrun.py --apply`。
        """

        now = self.clock()
        changed = 0
        with self.connection() as db:
            index = 0
            for tier in TIERS:
                rows = db.execute(
                    f"SELECT id, scope_type, kind, content, updated_at, expires_at FROM {TABLES[tier]}"
                    " WHERE status='active'").fetchall()
                for row in rows:
                    current = None if row["expires_at"] is None else (row["expires_at"] - now) / DAY
                    ttl = self._effective_ttl_days(
                        row["kind"], row["content"], None if current is None else int(round(current)),
                        tier=tier)
                    before = row["expires_at"]
                    if ttl is None:
                        new_expiry = before  # 策略不要求期限 → 保持原样（也可能是"永不过期"）
                    else:
                        target = row["updated_at"] + ttl * DAY
                        new_expiry = target if before is None else min(before, target)
                    if (before is None) == (new_expiry is None) and (
                            before is None or abs(before - new_expiry) < 1):
                        continue
                    db.execute(f"UPDATE {TABLES[tier]} SET expires_at=? WHERE id=?",
                               (new_expiry, row["id"]))
                    db.execute("INSERT OR REPLACE INTO audit VALUES (?,?,?,?,?)",
                               ("manual_ttl_clamp", index, "clamp_ttl", row["scope_type"], now))
                    index += 1
                    changed += 1
        if changed:
            logger.info("memory_ttl_clamped changed=%s", changed)
        return changed

    def _link_subjects(self, db, batch: MemoryBatch, op, record_id: str, group_id: str) -> None:
        """写"这条记忆跟谁有关"：归属人 + 模型声明且**能在本群名册里核对上**的其他人。

        模型只能从随批次给它的 `people` 名册里挑人；核对不上的直接丢掉（宁可不召回，
        也不能把别人的事挂到不相干的人身上）。个人偏好与边界**永远单人挂**。
        """

        subjects = [batch.user_id]
        # 群公共事实没有归属人（人人都看得到），不挂关联人行。
        if op.scope_type == "group":
            return
        # 称呼与边界是**关于一个人**的事，机械地只给归属人——这两类一旦挂上别人，
        # 就会出现"把 A 的雷区算到 B 头上"这种最坏的错。
        if op.kind not in {"name", "boundary"}:
            for candidate in op.subjects:
                uid = str(candidate).strip()
                if not uid or uid in subjects:
                    continue
                if len(subjects) >= MAX_SUBJECTS:
                    break
                known = db.execute("SELECT 1 FROM people WHERE group_id=? AND user_id=?",
                                   (batch.group_id, uid)).fetchone()
                if known is None:
                    logger.warning("memory_subject_unknown group=%s", batch.group_id)
                    continue
                subjects.append(uid)
        db.executemany("INSERT OR IGNORE INTO record_subjects VALUES (?,?,?)",
                       ((record_id, uid, group_id) for uid in subjects))

    def _find_record(self, db, record_id: str, revision: int, now: float):
        """按 id+revision 找活跃记录，并附上它属于哪一层（`tier` 键）。"""

        for tier in TIERS:
            row = db.execute(
                f"SELECT * FROM {TABLES[tier]} WHERE id=? AND revision=? AND status='active' "
                "AND (expires_at IS NULL OR expires_at>?)", (record_id, revision, now)).fetchone()
            if row is not None:
                data = dict(row)
                data["tier"] = tier
                return data
        return None

    @staticmethod
    def _target_tier(op, revision: int) -> str:
        """这条新记忆该落哪一层（用户 2026-09-28 选定的强化规则）。

        - 称呼 / 边界：天然长期，直接进；
        - 状态类（正在发烧、明天考试）：**只留在中短期**，永远不强化；
        - 维护 agent 显式标了 `durable`：直接进；
        - `revision>=2`：同一件事被后来的对话**再确认过一次** → 升长期。
        """

        if op.kind == STATUS_KIND:
            return "short"
        if op.kind in PROMOTE_KINDS or getattr(op, "durable", False) or revision >= 2:
            return "long"
        return "short"

    def _promote(self, db, row, now: float, *, reason: str) -> bool:
        """把一条中短期记录搬进长期表（同一事务内：插入 → 把短期那条标为 superseded）。"""

        existing = db.execute(
            f"SELECT 1 FROM {TABLES['long']} WHERE scope_type=? AND scope_key=? AND normalized_key=? "
            "AND status='active'",
            (row["scope_type"], row["scope_key"], row["normalized_key"])).fetchone()
        if existing:
            logger.warning("memory_promote_skipped reason=long_key_exists")
            return False
        ttl_days = self._effective_ttl_days(row["kind"], row["content"], None, tier="long")
        db.execute(f"""INSERT INTO {TABLES['long']}
            (id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
             created_at,updated_at,expires_at,revision,visibility,status,last_used_at,last_reviewed_at,
             origin,source_event_ids,durable,reminded_at,promoted_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["scope_type"], row["scope_key"], row["subject_user_id"], row["kind"],
             row["normalized_key"], row["content"], row["confidence"], row["created_at"],
             row["updated_at"], row["updated_at"] + ttl_days * DAY if ttl_days else None,
             row["revision"], row["visibility"], "active", row["last_used_at"],
             row["last_reviewed_at"], row["origin"], row["source_event_ids"], row["durable"],
             row["reminded_at"], now))
        db.execute(f"UPDATE {TABLES['short']} SET status='superseded',updated_at=? WHERE id=?",
                   (now, row["id"]))
        logger.info("memory_promoted reason=%s kind=%s", reason, row["kind"])
        return True

    def promote_reconfirmed(self) -> int:
        """机械强化：把"被后来的证据再确认过"（`revision>=2`）的中短期记忆升到长期。

        用户选定的强化规则里，这是唯一需要定期扫一遍的那条（称呼/边界与 `durable`
        在写入时就定了层）。不问模型、不额外花钱，挂在维护循环里跑。
        """

        now = self.clock()
        moved = 0
        with self.connection() as db:
            rows = db.execute(
                f"SELECT * FROM {TABLES['short']} WHERE status='active' AND revision>=2 AND kind!=?",
                (STATUS_KIND,)).fetchall()
            for row in rows:
                if self._promote(db, row, now, reason="reconfirmed"):
                    moved += 1
        return moved

    def pending_status_note(self, group_id: str, user_id: str, *, now: float | None = None):
        """这个说话人有没有"该关心一句"的状态（未提醒过、还没过期）。

        返回 `{"id","content","age_days"}` 或 None。只找**中短期**里的 `status`——
        状态类事实永远不进长期，所以这里只看一张表。
        """

        moment = self.clock() if now is None else now
        with self.connection() as db:
            for scope, key in self._namespaces(group_id, user_id, db=db)[:2]:
                row = db.execute(
                    f"SELECT id, content, created_at FROM {TABLES['short']} "
                    "WHERE scope_type=? AND scope_key=? AND kind=? AND status='active' "
                    "AND reminded_at IS NULL AND (expires_at IS NULL OR expires_at>?) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (scope, key, STATUS_KIND, moment)).fetchone()
                if row is not None:
                    return {
                        "id": row["id"],
                        "content": row["content"],
                        "age_days": max(0.0, (moment - row["created_at"]) / DAY),
                    }
        return None

    def mark_reminded(self, record_id: str, *, now: float | None = None) -> None:
        """记下"这条状态已经顺口提过了"——一条状态只提醒一次，这是硬约束。"""

        moment = self.clock() if now is None else now
        with self.connection() as db:
            db.execute(f"UPDATE {TABLES['short']} SET reminded_at=? WHERE id=?",
                       (moment, record_id))
        with self.connection() as db:
            return {tier: db.execute(
                f"SELECT COUNT(*) FROM {TABLES[tier]} WHERE status='active'").fetchone()[0]
                for tier in TIERS}

    def tier_counts(self) -> dict[str, int]:
        with self.connection() as db:
            return {tier: db.execute(
                f"SELECT COUNT(*) FROM {TABLES[tier]} WHERE status='active'").fetchone()[0]
                for tier in TIERS}

    @staticmethod
    def _tombstone(db, row, now):
        db.execute("INSERT INTO tombstones VALUES (?,?,?,?) ON CONFLICT(scope_type,scope_key,normalized_key) DO UPDATE SET deleted_at=excluded.deleted_at",
                   (row["scope_type"], row["scope_key"], row["normalized_key"], now))

    @staticmethod
    def _archive(db, row, now, reason: str):
        """把即将被删除的记录整条存进归档，供 30 天内人工恢复。

        为什么需要它：删除路径会把 records 那行的 content 清空（只留 status='deleted'），
        而 tombstones 只记 (scope_key, normalized_key, deleted_at) 和时间戳判据——
        **都没有内容，所以删掉就真的找不回来了**。

        归档与 tombstone 职责不同，两者都要写：
        - tombstone 是"这个键被删过"的判据，用于 deleted_evidence 校验（防止用旧证据复活）；
        - archive 是完整快照，用于人工恢复。

        同一个 id 重复归档时保留最早的 deleted_at —— 它记录的是"这条内容最早何时
        被判定不该留"，比最后一次时间更有参考价值。
        """

        db.execute(
            """INSERT INTO archive
               (id,scope_type,scope_key,subject_user_id,kind,normalized_key,content,confidence,
                created_at,updated_at,expires_at,revision,deleted_at,delete_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 content=excluded.content,
                 confidence=excluded.confidence,
                 delete_reason=excluded.delete_reason""",
            (row["id"], row["scope_type"], row["scope_key"], row["subject_user_id"], row["kind"],
             row["normalized_key"], row["content"], row["confidence"], row["created_at"],
             row["updated_at"], row["expires_at"], row["revision"], now, reason),
        )

    def archive_count(self) -> int:
        """归档条数。只读，供管理端与计数概览使用。"""

        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM archive").fetchone()[0]

    def list_archive(self, *, limit: int = 40) -> tuple[list[dict], int]:
        """按删除时间倒序列出归档（含完整内容）。只读，用于人工恢复。"""

        limit = max(1, min(200, limit))
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM archive").fetchone()[0]
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM archive ORDER BY deleted_at DESC LIMIT ?", (limit,))]
        return rows, total

    def people_roster(self, group_id: str, *, limit: int = 40) -> tuple[dict[str, str], ...]:
        """本群见过的人（最近出现优先）：维护 agent 只能从这份名单里挑关联人。"""

        with self.connection() as db:
            rows = db.execute(
                "SELECT user_id, name FROM people WHERE group_id=? ORDER BY updated_at DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return tuple({"id": row["user_id"], "name": row["name"]} for row in rows)

    def profile_record(self, user_id: str):
        """这个人的画像那一条记录（给需要修订号的地方用）；没有就返回 None。"""

        if not user_id:
            return None
        now = self.clock()
        with self.connection() as db:
            person = self.canonical_user(user_id, db=db)
            for tier in ("long", "short"):  # 正常在长期表；短期只是写入后还没强化的兜底
                row = db.execute(
                    f"SELECT * FROM {TABLES[tier]} WHERE scope_type='user_global'"
                    " AND scope_key=? AND kind=? AND status='active' AND visibility='group_safe'"
                    " AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC LIMIT 1",
                    (person, PROFILE_KIND, now),
                ).fetchone()
                if row is not None:
                    return self._record(row, tier)
        return None

    def profile(self, user_id: str) -> str:
        """这个人的**人物画像**正文；没有就返回空串。

        画像一个人一条、只挂在 `user_global` 上（键固定 `profile`，见 memory_model 的
        校验），所以这里只查一处。它不进普通检索——每次都由"当前说话的人"触发渲染，
        这样画像跟着人走，而不是跟着话题走。
        """

        record = self.profile_record(user_id)
        return str(record.content) if record is not None else ""

    def person_names(self, group_id: str, user_ids) -> dict[str, str]:
        """QQ 号 → 本群显示名。渲染记忆归属用；查不到就由调用方退回号码。"""

        ids = [str(uid) for uid in user_ids if uid]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as db:
            rows = db.execute(
                f"SELECT user_id, name FROM people WHERE group_id=? AND user_id IN ({placeholders})",
                (group_id, *ids),
            ).fetchall()
        return {row["user_id"]: row["name"] for row in rows}

    def record_subjects_of(self, record_ids) -> dict[str, tuple[str, ...]]:
        """记录 → 关联人（含归属人）。渲染"关于谁"用。"""

        ids = [str(rid) for rid in record_ids if rid]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as db:
            rows = db.execute(
                f"SELECT record_id, user_id FROM record_subjects WHERE record_id IN ({placeholders}) "
                "ORDER BY user_id",
                tuple(ids),
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["record_id"], []).append(row["user_id"])
        return {rid: tuple(users) for rid, users in grouped.items()}

    def _candidate_records(self, db, group_id: str, user_id: str, now: float, mentioned=()):
        """这句话可能用得上的记忆：本人 + 被提到的人（本群范围内）+ 本群公共事实。

        跨人召回**只借本群范围内的记录**（`user_group` 里本群的那些、以及 `group`）：
        `user_global` 是跨群共享的，可能来自私聊或别的群，借给本群等于把一个人在
        别处说的话搬过来。理由见 docs/MEMORY_PEOPLE.md 第四节。
        """

        namespaces = self._namespaces(group_id, user_id, db=db)
        records = []
        for scope, key in namespaces:
            for tier in TIERS:
                records.extend(self._record(r, tier) for r in db.execute(
                    f"SELECT * FROM {TABLES[tier]} WHERE scope_type=? AND scope_key=? AND status='active'"
                    " AND visibility='group_safe' AND confidence>=0.5 AND (expires_at IS NULL OR expires_at>?)"
                    f" AND kind != '{PROFILE_KIND}'"
                    " ORDER BY updated_at DESC LIMIT 100",
                    (scope, key, now)))
        # 本群的局部偏好覆盖同一事实的全局版本。
        local_keys = {r.normalized_key for r in records if r.scope_type == "user_group"}
        records = [r for r in records if not (r.scope_type == "user_global" and r.normalized_key in local_keys)]
        speaker_keys = {(scope, key) for scope, key in namespaces[:2]}
        seen = {r.id for r in records}
        others = [str(uid) for uid in dict.fromkeys(mentioned) if uid and str(uid) != user_id]
        if others:
            placeholders = ",".join("?" for _ in others)
            rows = []
            for tier in TIERS:
                rows.extend({**dict(r), "tier": tier} for r in db.execute(
                    f"SELECT r.* FROM {TABLES[tier]} r JOIN record_subjects s ON s.record_id = r.id "
                    "WHERE r.status='active' AND r.visibility='group_safe' AND r.confidence>=0.5 "
                    "AND (r.expires_at IS NULL OR r.expires_at>?) AND s.group_id=? "
                    f"AND s.user_id IN ({placeholders}) "
                    # 只借本群学到的：s.group_id 就是"学到它的那个群"。
                    # user_global 的记录也只有在**本群**学到时才会带着本群 id。
                    "AND (r.scope_type='user_global' "
                    "     OR (r.scope_type='user_group' AND r.scope_key LIKE ?)) "
                    f"AND r.kind != '{PROFILE_KIND}' "
                    "ORDER BY r.updated_at DESC LIMIT 100",
                    (now, group_id, *others, f"{group_id}:%"),
                ))
            for row in rows:
                record = self._record(row, row["tier"])
                if record.id not in seen:
                    seen.add(record.id)
                    records.append(record)
        return records, speaker_keys, {str(uid) for uid in others}

    def retrieve(self, group_id: str, user_id: str, query: str,
                 mentioned=()) -> tuple[MemoryRecord, ...]:
        now = self.clock()
        with self.connection() as db:
            records, speaker_keys, others = self._candidate_records(
                db, group_id, user_id, now, mentioned)
            # **本人**的称呼与边界不靠词命中：这是"我记得你是谁、你不喜欢什么"，
            # 每轮都该在，而不是取决于这句话里有没有出现同一个词。
            # 别人的边界不进常驻——只在被提到且词命中时才可能出现。
            profile = sorted(
                (r for r in records
                 if r.kind in {"name", "boundary"} and (r.scope_type, r.scope_key) in speaker_keys),
                key=lambda r: (r.kind == "name", r.confidence, r.updated_at),
                reverse=True,
            )[:2]
            terms = frozenset(t for t in self._query_terms(query) if t not in WEAK_TERMS)

            def relevance(record) -> int:
                haystack = f"{record.content}\n{record.normalized_key}".casefold()
                return sum(1 for term in terms if term in haystack)

            def tier(record) -> int:
                """2=说话人自己的，1=被提到的人的，0=本群公共的。"""

                if (record.scope_type, record.scope_key) in speaker_keys:
                    return 2
                if record.subject_user_id and str(record.subject_user_id) in others:
                    return 1
                return 0

            chosen = {r.id for r in profile}
            scored = [(relevance(r), r) for r in records if r.id not in chosen]
            if terms:
                # 有查询词时**只注入真正命中的记录**：一个词都没命中就不注入。
                # 旧写法靠 confidence/updated_at 兜底排序，结果是"最近更新的那几条"
                # 被塞进她嘴里——与当前话题无关，比读不到记忆更糟。
                scored = [item for item in scored if item[0] > 0]
            # 空查询是"取出全部"语义（视图与测试用），不做词过滤。
            # 命中数 → 说话人优先 → **长期优先（强化过的更可信）** → 置信度 → 更新时间。
            scored.sort(
                key=lambda item: (item[0], tier(item[1]),
                                  1 if item[1].tier == "long" else 0,
                                  item[1].confidence, item[1].updated_at),
                reverse=True,
            )
            result = tuple(record for _, record in scored[:max(0, 5 - len(profile))]) + tuple(profile)
            if result:
                for tier_name in TIERS:
                    ids = [r.id for r in result if r.tier == tier_name]
                    if ids:
                        db.executemany(f"UPDATE {TABLES[tier_name]} SET last_used_at=? WHERE id=?",
                                       ((now, rid) for rid in ids))
            return result

    def speaker_identity(self, group_id: str, user_id: str, *, limit: int = 2) -> tuple[MemoryRecord, ...]:
        """只取"这个人是谁、他的红线是什么"，给判定 agent 用。

        判定 prompt 有"必须远小于人格 prompt"的硬约束，塞不下五条记忆；称呼和边界
        是判断分寸时唯一真正用得上、又最便宜的两条。
        """

        now = self.clock()
        with self.connection() as db:
            records, speaker_keys, _ = self._candidate_records(db, group_id, user_id, now)
            picked = sorted(
                (r for r in records
                 if r.kind in {"name", "boundary"} and (r.scope_type, r.scope_key) in speaker_keys),
                key=lambda r: (r.kind == "name", r.confidence, r.updated_at),
                reverse=True,
            )[:limit]
            return tuple(picked)

    def purge_records(self, record_ids, *, reason: str) -> int:
        """人工清理指定的活跃记忆（先归档再清空，和 agent_delete 走同一条路）。

        用途：`data/purge_memory_records.py` 清掉体检发现的违规记忆。删除不可逆的
        部分（内容）在 archive 里留 30 天；tombstones 记键与时间，防止旧证据把它写回来。
        返回真正清掉的条数。
        """

        ids = [str(rid) for rid in record_ids]
        if not ids:
            return 0
        now = self.clock()
        purged = 0
        with self.connection() as db:
            for rid in ids:
                for tier in TIERS:
                    row = db.execute(
                        f"SELECT * FROM {TABLES[tier]} WHERE id=? AND status='active'", (rid,)).fetchone()
                    if row is None:
                        continue
                    self._tombstone(db, row, now)
                    self._archive(db, row, now, reason)
                    # 两张表一起清：同一条事实可能刚被强化搬到长期。
                    for other in TIERS:
                        db.execute(f"UPDATE {TABLES[other]} SET status='deleted',content='',"
                                   "source_event_ids='[]',updated_at=? "
                                   "WHERE scope_type=? AND scope_key=? AND normalized_key=?",
                                   (now, row["scope_type"], row["scope_key"], row["normalized_key"]))
                    purged += 1
                    break
        return purged

    def merge_alias(self, alias_user_id: str, canonical_user_id: str, *,
                    note: str = "", drop_profile: bool = False) -> dict[str, object]:
        """把 `alias_user_id` 认定成 `canonical_user_id` 的另一个号，并把旧记忆并过去。

        **只有人能调它**（面板 / `data/merge_person_accounts.py`）：代码里没有任何
        "猜两个号是不是同一个人"的路径——昵称撞了、说话像，都不算证据，认错一个人
        比想不起来更伤（`docs/MEMORY_PEOPLE.md`）。

        一次事务里做完三件事：

        1. 登记 `别名 → 规范号`（此后读写都按规范号，换号说话也能取到同一份记忆）；
        2. 把旧号名下的记录、墓碑、关联人、关系、归档整体搬到规范号名下——
           只动**身份字段**，正文一个字不改（内容冲突得由人判断）；
        3. 可选地清掉规范号名下那条人物画像（`drop_profile`）：合并后旧画像里
           "自称某某"那类说法常和新号矛盾，而画像每轮都会注入。

        返回一份做了什么事的摘要，给调用方打印/审计用。**不备份**：备份是调用方的事。
        """

        alias = str(alias_user_id or "").strip()
        canonical = str(canonical_user_id or "").strip()
        if not alias or not canonical:
            raise MemoryValidationError("merge_needs_two_ids")
        if alias == canonical:
            raise MemoryValidationError("merge_same_id")

        summary: dict[str, object] = {"alias": alias, "canonical": canonical, "moved": {}}
        now = self.clock()
        with self.connection() as db:
            # 两个号各自顺着链子找到尽头。难点：`{群号}:{号}` 这种键两边不相等，
            # 所以撞键检测不能靠"相等"——按**映射后的键**比才对。
            def _target(one: str) -> str:
                found = self.canonical_user(one, db=db)
                return found or one

            alias_target = _target(alias)
            canonical_target = _target(canonical)
            if alias == canonical_target or alias_target == canonical_target:
                raise MemoryValidationError("merge_same_id")
            if db.execute("SELECT 1 FROM person_aliases WHERE alias_user_id=?",
                          (alias,)).fetchone():
                raise MemoryValidationError("merge_already_done")

            def _after(scope_type: str, scope_key: str) -> str:
                """这条记录的 scope_key 搬完之后会变成什么。"""

                if scope_type == "user_global" and scope_key == alias:
                    return canonical_target
                if scope_type == "user_group" and scope_key.endswith(f":{alias}"):
                    return f"{scope_key[:scope_key.rindex(':') + 1]}{canonical_target}"
                return scope_key

            moved: dict[str, int] = {}
            for tier in TIERS:
                table = TABLES[tier]
                # 搬完之后会不会有两条活跃记录抢同一把键（唯一索引会直接报错）。
                # 按 scope_key 找两边的人：`user_global` 的键就是号本身，
                # `user_group` 的键是 `{群号}:{号}`，都盖住。
                # 一个号都没命中的（比如 `group` 公共事实）搬完不变，不用管。
                # 不猜谁对谁错：报给调用方，让他先删一条再来。
                seen: dict[tuple[str, str, str], int] = {}
                for row in db.execute(
                        f"SELECT scope_type,scope_key,normalized_key FROM {table} "
                        "WHERE status='active' AND (scope_key=? OR scope_key=? "
                        "  OR scope_key LIKE ? OR scope_key LIKE ?)",
                        (alias, canonical, f"%:{alias}", f"%:{canonical}")).fetchall():
                    after = _after(row["scope_type"], row["scope_key"])
                    spot = (row["scope_type"], after, row["normalized_key"])
                    seen[spot] = seen.get(spot, 0) + 1
                if any(count > 1 for count in seen.values()):
                    raise MemoryValidationError("merge_key_clash")
                # 三处身份字段：谁的事实、跨群那份、本群那份（`{群号}:{号}`）。
                moved[f"{table}.subject"] = db.execute(
                    f"UPDATE {table} SET subject_user_id=? WHERE subject_user_id=?",
                    (canonical_target, alias)).rowcount
                moved[f"{table}.scope"] = db.execute(
                    f"UPDATE {table} SET scope_key=? WHERE scope_key=?",
                    (canonical_target, alias)).rowcount
                moved[f"{table}.scope_group"] = db.execute(
                    f"UPDATE {table} SET scope_key = substr(scope_key,1,instr(scope_key,':')) || ? "
                    "WHERE scope_key LIKE ?", (canonical_target, f"%:{alias}")).rowcount

            # 墓碑：不搬的话"删掉的键"在规范号下就不算删过，旧证据能把它写回来。
            moved["tombstones"] = db.execute(
                "UPDATE OR REPLACE tombstones "
                "SET scope_key = CASE WHEN scope_key=? THEN ? "
                "  WHEN scope_key LIKE ? THEN substr(scope_key,1,instr(scope_key,':')) || ? "
                "  ELSE scope_key END "
                "WHERE scope_key=? OR scope_key LIKE ?",
                (alias, canonical_target, f"%:{alias}", canonical_target,
                 alias, f"%:{alias}")).rowcount

            # 关联人：主键是 (record_id, user_id)，两边都有就只留规范号那行。
            moved["record_subjects.dup"] = db.execute(
                "DELETE FROM record_subjects WHERE user_id=? AND record_id IN "
                "(SELECT record_id FROM record_subjects WHERE user_id=?)",
                (alias, canonical_target)).rowcount
            moved["record_subjects"] = db.execute(
                "UPDATE record_subjects SET user_id=? WHERE user_id=?",
                (canonical_target, alias)).rowcount

            # 关系：user_id 是主键。规范号已有行就保留现状，没有才搬。
            existing = db.execute(
                "SELECT 1 FROM relationships WHERE user_id=?", (canonical_target,)).fetchone()
            if existing:
                moved["relationships.dropped"] = db.execute(
                    "DELETE FROM relationships WHERE user_id=?", (alias,)).rowcount
            else:
                moved["relationships"] = db.execute(
                    "UPDATE relationships SET user_id=? WHERE user_id=?",
                    (canonical_target, alias)).rowcount
            moved["relationship_log"] = db.execute(
                "UPDATE relationship_log SET user_id=? WHERE user_id=?",
                (canonical_target, alias)).rowcount
            moved["archive"] = db.execute(
                "UPDATE archive SET subject_user_id=? WHERE subject_user_id=?",
                (canonical_target, alias)).rowcount
            # 名册：这个人只剩规范号那一行。显示名以规范号最近记下的为准。
            moved["people"] = db.execute(
                "DELETE FROM people WHERE user_id=?", (alias,)).rowcount
            # 租约/待整理标记也归到规范号，否则旧号会一直占着一条永远不会被处理的租约。
            moved["leases.dup"] = db.execute(
                "DELETE FROM leases WHERE user_id=? AND group_id IN "
                "(SELECT group_id FROM leases WHERE user_id=?)",
                (alias, canonical_target)).rowcount
            moved["leases"] = db.execute(
                "UPDATE leases SET user_id=? WHERE user_id=?",
                (canonical_target, alias)).rowcount
            moved["participants.dup"] = db.execute(
                "DELETE FROM participants WHERE user_id=? AND group_id IN "
                "(SELECT group_id FROM participants WHERE user_id=?)",
                (alias, canonical_target)).rowcount
            moved["participants"] = db.execute(
                "UPDATE participants SET user_id=? WHERE user_id=?",
                (canonical_target, alias)).rowcount

            dropped: list[str] = []
            if drop_profile:
                # 和 purge_records 同一条路：先归档再清空，墓碑防旧证据复活。
                for tier in TIERS:
                    for row in db.execute(
                            f"SELECT * FROM {TABLES[tier]} WHERE scope_type='user_global' "
                            "AND scope_key=? AND kind=? AND status='active'",
                            (canonical_target, PROFILE_KIND)).fetchall():
                        self._tombstone(db, row, now)
                        self._archive(db, row, now, "operator_merge")
                        for other in TIERS:
                            db.execute(
                                f"UPDATE {TABLES[other]} SET status='deleted',content='',"
                                "source_event_ids='[]',updated_at=? "
                                "WHERE scope_type='user_global' AND scope_key=? "
                                "AND normalized_key=?",
                                (now, canonical_target, row["normalized_key"]))
                        dropped.append(str(row["id"]))
            summary["canonical"] = canonical_target
            summary["moved"] = moved
            summary["dropped_profiles"] = dropped
            db.execute(
                "INSERT INTO person_aliases(alias_user_id,canonical_user_id,note,created_at) "
                "VALUES (?,?,?,?) ON CONFLICT(alias_user_id) DO UPDATE SET "
                "canonical_user_id=excluded.canonical_user_id,note=excluded.note,"
                "created_at=excluded.created_at",
                (alias, canonical_target, str(note)[:200], now))
        self.alias_map_changed()
        return summary

    @staticmethod
    def _query_terms(query: str) -> frozenset[str]:
        """把检索词切成可用于子串命中的词元；忽略单字噪声与常见虚词。"""

        text = query.casefold()[:1000]
        terms = {
            token
            for token in re.findall(r"[a-z0-9_]+", text)
            if len(token) >= 2
        }
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            if len(run) == 1:
                continue
            terms.update(run[index:index + 2] for index in range(len(run) - 1))
            terms.add(run)
        return frozenset(term for term in terms if term not in STOPWORDS)

