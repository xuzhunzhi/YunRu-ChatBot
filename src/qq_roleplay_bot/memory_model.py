"""Memory contracts. IDs are bound by the host, never supplied by the model."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from html import escape

from .security import is_sensitive_local_request, sanitize_chat_text

SCOPES = {"user_global", "user_group", "group"}
# `status` 是 2026-09-28 加的第五类：**只活在短期层**的处境（正在发烧、明天有考试、
# 这周赶 due）。它存在的唯一理由是"过一两天还能自然地问一句"，所以永不进长期，
# 也永不按长期事实活着。
# `profile` 是 2026-09-29 加的第七类：**人物画像**——她对某个人整体的印象
# （怎么称呼、在意什么、怎么跟他相处）。它不参与普通检索（每次都由说话人触发渲染），
# 一个人只有一条（normalized_key 固定 `profile`），只允许挂在 user_global 上。
KINDS = {"name", "preference", "boundary", "group_fact", "topic_summary", "status", "profile"}
OPS = {"ADD", "UPDATE", "MERGE", "DELETE", "IGNORE", "AFFINITY"}
# 每批最多几条好感度 op。它不占 MAX_OPERATIONS 的名额，但也不能让一批里
# 十几个人都改一遍——那样"变化要慢"这条就形同虚设。
MAX_AFFINITY_OPERATIONS = 2
# 好感度每轴的允许增量：只有 -1 / 0 / +1。数值本身是 0..3 的内部档位。
AFFINITY_DELTAS = {"-1": -1, "0": 0, "+1": 1}
MAX_OPERATIONS = 5
MAX_CONTENT = 300
# 一条记忆最多关联几个人（含归属人）。多人条目只该用在"这事确实涉及几个人"上，
# 见 docs/MEMORY_PEOPLE.md。
MAX_SUBJECTS = 4
# 关联人的写法：QQ 号（本群名册里给的），模型不许自己编。
USER_ID_PATTERN = re.compile(r"^\d{4,12}$")
# Deliberately conservative for a group-safe MVP; the agent decides semantic value.
SENSITIVE = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|(?:password|api[_ -]?key|secret|cookie|bearer|access[_ -]?token|"
    r"密码|密钥|私钥|身份证|银行卡|住址|病史|诊断|性取向|政治立场|环境变量)|"
    r"(?<!\d)1[3-9]\d{9}(?!\d)|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|[A-Za-z]:[\\/]|"
    r"-----BEGIN .*PRIVATE KEY|<[/!?]?[A-Za-z]|```",
    re.I,
)


class MemoryValidationError(ValueError):
    """Messages are fixed codes so logs cannot expose model output."""


def safe_memory_text(value: str) -> bool:
    return not SENSITIVE.search(value) and not is_sensitive_local_request(value)


@dataclass(frozen=True, slots=True)
class InboxEvent:
    id: str
    group_id: str
    user_id: str  # Conversation owner; YunRu events still have speaker='yunru'.
    speaker: str
    text: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    scope_type: str
    scope_key: str
    subject_user_id: str | None
    kind: str
    normalized_key: str
    content: str
    confidence: float
    created_at: float
    updated_at: float
    expires_at: float | None = None
    revision: int = 1
    visibility: str = "group_safe"
    status: str = "active"
    # 这条记忆在哪一层（`short` = 中短期，`long` = 长期）。**不是数据库列**：
    # 两张表各自一张，读出来时由 store 标注（见 memory_store.TIERS）。
    tier: str = "short"


@dataclass(frozen=True, slots=True)
class MemoryBatch:
    id: str
    group_id: str
    user_id: str
    events: tuple[InboxEvent, ...]
    records: tuple[MemoryRecord, ...]
    lease_until: float

    def scope_key(self, scope: str) -> str:
        return {"user_global": self.user_id, "user_group": f"{self.group_id}:{self.user_id}",
                "group": self.group_id}[scope]


@dataclass(frozen=True, slots=True)
class MemoryOperation:
    op: str
    scope_type: str = ""
    kind: str = ""
    normalized_key: str = ""
    content: str = ""
    confidence: float = 0.0
    ttl_days: int | None = None
    evidence_event_ids: tuple[str, ...] = ()
    targets: tuple[tuple[str, int], ...] = ()
    # AFFINITY 专用：对谁、两个轴各动几档。`content` 在这里是"依据"这句人话。
    affinity_user_id: str = ""
    closeness_delta: int = 0
    guardedness_delta: int = 0
    # 关联人（含归属人之外的人）：本群名册里的 QQ 号，存储层会再核对一次。
    subjects: tuple[str, ...] = ()
    # 维护 agent 认为这条值得**长期**留（用户选定的强化规则之一）。
    durable: bool = False


def parse_operations(raw: str, batch: MemoryBatch) -> tuple[MemoryOperation, ...]:
    """Reject the entire batch on malformed output or unauthorized targets."""
    def fail() -> None:
        raise MemoryValidationError("invalid_operation")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail()
            result[key] = value
        return result

    if not isinstance(raw, str) or len(raw) > 16000:
        fail()
    try:
        data = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise MemoryValidationError("invalid_json") from None
    if not isinstance(data, dict) or set(data) != {"operations"}:
        fail()
    items = data["operations"]
    # 好感度 op 单独配额：它不占记忆操作的 5 条名额（不然一批里改两个人就把记忆挤没了）。
    if not isinstance(items, list) or len(items) > MAX_OPERATIONS + MAX_AFFINITY_OPERATIONS:
        fail()
    events = {e.id: e for e in batch.events}
    records = {r.id: r for r in batch.records}
    results = []
    touched = set()
    keys = set()
    affinity_count = 0
    content_count = 0
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("op"), str) or item["op"] not in OPS:
            fail()
        op = item["op"]
        if op == "IGNORE":
            if set(item) != {"op"}:
                fail()
            results.append(MemoryOperation(op))
            continue
        if op == "AFFINITY":
            affinity_count += 1
            if affinity_count > MAX_AFFINITY_OPERATIONS:
                fail()
            if set(item) != {"op", "user_id", "closeness", "guardedness", "evidence_event_ids", "reason"}:
                fail()
            target_user = item["user_id"]
            if not isinstance(target_user, str) or not re.fullmatch(r"\d{5,12}", target_user):
                fail()
            # 只能改**本批真实出现过**的人：模型不许凭空写一个 QQ 号进关系表。
            if not any(e.speaker == "user" and e.user_id == target_user for e in batch.events):
                fail()
            deltas = []
            for axis_name in ("closeness", "guardedness"):
                value = item[axis_name]
                if not isinstance(value, str) or value not in AFFINITY_DELTAS:
                    fail()
                deltas.append(AFFINITY_DELTAS[value])
            evidence = item["evidence_event_ids"]
            if not isinstance(evidence, list) or not evidence or len(evidence) > 40:
                fail()
            if any(not isinstance(e, str) or e not in events for e in evidence):
                fail()
            if not any(events[e].speaker == "user" and events[e].user_id == target_user for e in evidence):
                fail()
            reason = item["reason"]
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200:
                fail()
            if not safe_memory_text(reason) or sanitize_chat_text(reason) != reason:
                fail()
            results.append(MemoryOperation(
                op, "user_global", content=reason.strip(),
                evidence_event_ids=tuple(evidence),
                affinity_user_id=target_user,
                closeness_delta=deltas[0], guardedness_delta=deltas[1],
            ))
            continue
        content_count += 1
        if content_count > MAX_OPERATIONS:
            fail()
        fields = {"op", "scope_type", "evidence_event_ids", "targets"}
        if op != "DELETE":
            fields |= {"kind", "normalized_key", "content", "confidence", "ttl_days"}
            # `subjects` 可选：这条记忆还跟本群里的谁有关（QQ 号，服务端会再核对）。
            # `durable` 可选：维护 agent 认为它值得长期留。
            fields |= {"subjects", "durable"}
            item = {**item,
                    "subjects": item.get("subjects", []),
                    "durable": item.get("durable", False)}
        if set(item) != fields or not isinstance(item["scope_type"], str) or item["scope_type"] not in SCOPES:
            fail()
        scope = item["scope_type"]
        evidence = item["evidence_event_ids"]
        if not isinstance(evidence, list) or len(evidence) > 40:
            fail()
        if any(not isinstance(e, str) or e not in events for e in evidence):
            fail()
        # Bot statements can provide context, but cannot establish user facts.
        if evidence and not any(events[e].speaker == "user" for e in evidence):
            fail()
        targets = item["targets"]
        if not isinstance(targets, list) or len(targets) > 5:
            fail()
        if (op == "ADD" and (targets or not evidence)) or (op in {"UPDATE", "DELETE"} and len(targets) != 1) or (op == "MERGE" and len(targets) < 2):
            fail()
        parsed_targets = []
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"id", "revision"}:
                fail()
            if not isinstance(target["id"], str) or type(target["revision"]) is not int:
                fail()
            record = records.get(target["id"])
            if (record is None or record.id in touched or record.revision != target["revision"]
                    or record.scope_type != scope or record.scope_key != batch.scope_key(scope)):
                fail()
            touched.add(record.id)
            parsed_targets.append((record.id, record.revision))
        if op == "DELETE":
            results.append(MemoryOperation(op, scope, evidence_event_ids=tuple(evidence), targets=tuple(parsed_targets)))
            continue
        content, key, kind = item["content"], item["normalized_key"], item["kind"]
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= MAX_CONTENT:
            fail()
        if not safe_memory_text(content) or sanitize_chat_text(content) != content:
            fail()
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            fail()
        if not isinstance(kind, str) or kind not in KINDS:
            fail()
        if scope == "group" and kind not in {"group_fact", "topic_summary"}:
            fail()
        if scope != "group" and kind == "group_fact":
            fail()
        if (scope, key) in keys:
            fail()
        keys.add((scope, key))
        confidence = item["confidence"]
        ttl = item["ttl_days"]
        if type(confidence) not in {int, float} or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            fail()
        if ttl is not None and (type(ttl) is not int or not 1 <= ttl <= 3650):
            fail()
        subjects = item["subjects"]
        if (not isinstance(subjects, list) or len(subjects) > MAX_SUBJECTS
                or any(not isinstance(s, str) or not USER_ID_PATTERN.match(s) for s in subjects)):
            fail()
        durable = item["durable"]
        if type(durable) is not bool:
            fail()
        if kind == "status" and scope == "group":
            fail()  # 状态是某个人的处境，不是群的公共事实
        if kind == "profile":
            # 画像一个人一条、跨群同一条：键固定、必须挂在本人身上、不许关联别人。
            # 挂在 user_group 会让"同一个人在两个群里有两份画像"，渲染时还得二选一。
            if scope != "user_global" or key != "profile":
                fail()
            if subjects:
                fail()
            if ttl is not None and ttl < 30:
                fail()  # 画像不是处境，别随三五天就过期
        results.append(MemoryOperation(op, scope, kind, key, content.strip(), float(confidence), ttl,
                                       tuple(evidence), tuple(parsed_targets),
                                       subjects=tuple(dict.fromkeys(subjects)), durable=durable))
    return tuple(results)


@dataclass(frozen=True, slots=True)
class MemoryMaterial:
    records: tuple[MemoryRecord, ...] = ()
    # 记录 → 关联人（含归属人）；由 MemoryService 在检索时一并取好。
    subjects: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # QQ 号 → 本群显示名（渲染 `about` 用；查不到就退回号码）。
    names: dict[str, str] = field(default_factory=dict)

    def _about(self, record) -> str:
        people = self.subjects.get(record.id) or ((record.subject_user_id,) if record.subject_user_id else ())
        return ", ".join(self.names.get(uid, uid) for uid in people)

    def as_data(self, group_id: str | None, user_id: str) -> str:
        lines = []
        used = 0
        allowed = {("group", group_id), ("user_group", f"{group_id}:{user_id}"), ("user_global", user_id)}
        for r in self.records:
            if not group_id or (r.scope_type, r.scope_key) not in allowed or r.visibility != "group_safe" or r.status != "active":
                continue
            # 正文是文本节点，只转义 `<` 与 `&`；引号转义只对属性值有意义，
            # 写进正文会被模型当成可模仿的写法（实测它真的回显过 `&quot;`）。
            content = escape(sanitize_chat_text(r.content, max_length=MAX_CONTENT), quote=False)
            about = escape(self._about(r), quote=True)
            owner = escape(r.subject_user_id or "group", quote=True)
            detail = f' about="{about}"' if about else ""
            line = f'<memory scope="{r.scope_type}" subject="{owner}"{detail}>{content}</memory>'
            if used + len(line) > 2000 or len(lines) >= 5:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines) or "（暂无可用长期记忆）"

    def as_judge_note(self, group_id: str | None, user_id: str, *, max_items: int = 2) -> str:
        """给判定 agent 的一行人话：这个人怎么称呼、他的红线是什么。

        判定 prompt 有"必须远小于人格 prompt"的硬约束，塞不下整块记忆；而"要不要接话"
        真正用得上的只有这两类。内容是模型写的，所以照样走 DATA 转义。
        """

        if not group_id:
            return ""
        allowed = {("group", group_id), ("user_group", f"{group_id}:{user_id}"), ("user_global", user_id)}
        labels = {"name": "称呼", "boundary": "边界"}
        parts = []
        for r in self.records:
            if r.kind not in labels or (r.scope_type, r.scope_key) not in allowed:
                continue
            if r.visibility != "group_safe" or r.status != "active":
                continue
            content = escape(sanitize_chat_text(r.content, max_length=MAX_CONTENT), quote=False)
            parts.append(f"{labels[r.kind]}：{content}")
            if len(parts) >= max_items:
                break
        return "；".join(parts)


@dataclass(slots=True)
class MemoryMetrics:
    enqueued: int = 0
    dropped: int = 0
    failures: int = 0
    reads: int = 0
    hits: int = 0
    maintenance_calls: int = 0
    maintenance_runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_duration_seconds: float = 0.0
    operations: dict[str, int] = field(default_factory=lambda: dict.fromkeys(sorted(OPS), 0))
