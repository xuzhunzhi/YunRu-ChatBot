"""完整审计：汇总今晚两轮攻击的所有数据源。只读。"""
import json
import re
import sqlite3
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

GROUP = 717151356
BOT = "900000002"
CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent

# ---------- 1. 群历史 ----------
body = json.dumps({"action": "get_group_msg_history",
                   "params": {"group_id": GROUP, "count": 200}, "echo": "p"}).encode()
req = urllib.request.Request("http://127.0.0.1:3000/get_group_msg_history",
                             data=body, method="POST",
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=25) as resp:
    payload = json.loads(resp.read().decode("utf-8", "replace"))
messages = payload["data"]["messages"]


def render(item):
    parts = []
    for seg in item.get("message", []) or []:
        t = seg.get("type"); d = seg.get("data", {}) or {}
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "at":
            parts.append(f"@{d.get('qq')}")
        elif t == "image":
            parts.append("[图片]")
        elif t == "reply":
            parts.append("[回复]")
        elif t == "face":
            parts.append("[表情]")
        else:
            parts.append(f"[{t}]")
    return "".join(parts)


print("=" * 90)
print(f"数据源 1：群历史  {len(messages)} 条  "
      f"{datetime.fromtimestamp(messages[0]['time'], CST):%H:%M} → "
      f"{datetime.fromtimestamp(messages[-1]['time'], CST):%H:%M}")
print("=" * 90)

attackers = Counter()
for item in messages:
    if str(item.get("user_id")) == BOT:
        continue
    attackers[item.get("user_id")] += 1
print("参与人数（按发言量）:")
for uid, count in attackers.most_common():
    name = next((m.get("sender", {}).get("nickname") for m in messages
                 if str(m.get("user_id")) == str(uid)), uid)
    print(f"  {name}({uid}): {count} 条")

# 完整对话（只保留有 @bot 或 bot 发言及其邻接）
print()
print("--- 与 Bot 相关的完整往来 ---")
indices = set()
for i, item in enumerate(messages):
    text = render(item)
    if str(item.get("user_id")) == BOT or f"@{BOT}" in text:
        for off in (-1, 0, 1):
            if 0 <= i + off < len(messages):
                indices.add(i + off)
for i in sorted(indices):
    item = messages[i]
    ts = datetime.fromtimestamp(item["time"], CST)
    uid = str(item.get("user_id"))
    who = "云茹" if uid == BOT else (item.get("sender", {}).get("nickname") or uid)
    arrow = "→" if uid == BOT else "←"
    print(f"[{ts:%H:%M:%S}] seq={item.get('message_seq')} {arrow} {who}: {render(item)[:200]}")

# ---------- 2. 追踪文件 ----------
print()
print("=" * 90)
print("数据源 2：模型原始输出追踪")
print("=" * 90)
for name in ("model_trace.jsonl", "model_trace.polluted.jsonl"):
    path = ROOT / ".tmp_test_run" / name
    if not path.exists():
        continue
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    entries = [json.loads(l) for l in lines]
    # 真实 Bot 的写入：sequence 从 1 严格递增
    clean, expected = [], 1
    for e in entries:
        if e["sequence"] == expected:
            clean.append(e); expected += 1
    print(f"\n{name}: 共 {len(entries)} 行，其中真实 Bot 记录 {len(clean)} 条")
    pendings = []
    for e in clean:
        raw = e.get("raw_output", "")
        ctx = re.search(r"<context>\s*(.*?)\s*</context>", raw, re.DOTALL | re.IGNORECASE)
        fields = {}
        if ctx:
            for line in ctx.group(1).splitlines():
                k, sep, v = line.partition("=")
                if sep:
                    fields[k.strip()] = v.strip()
        p = fields.get("pending_question", "")
        if p and p != "无":
            pendings.append((e["sequence"], p))
    print(f"  pending_question 非「无」的次数: {len(pendings)}")
    for seq, p in pendings:
        print(f"    #{seq}: {p}")

# ---------- 3. 记忆库 ----------
print()
print("=" * 90)
print("数据源 3：长期记忆")
print("=" * 90)
db = ROOT / "data" / "memory" / "memory.sqlite3"
if db.exists():
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for table in ("records", "inbox", "audit"):
        try:
            print(f"  {table:10}", conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.OperationalError:
            print(f"  {table:10} 缺表")
    print()
    for row in conn.execute("SELECT scope_type, scope_key, kind, normalized_key, content, "
                            "confidence, status FROM records ORDER BY updated_at DESC"):
        print(f"  [{row[0]}/{row[2]}] {row[3]}  conf={row[5]} status={row[6]}")
        print(f"      {row[4]}")
    conn.close()

# ---------- 4. 日志统计 ----------
print()
print("=" * 90)
print("数据源 4：Bot 日志")
print("=" * 90)
for name in ("bot.err",):
    path = ROOT / ".tmp_test_run" / name
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8", errors="replace")
    for label, pattern in (("model check", "model check"), ("reply sent", "reply sent"),
                           ("kind=no_reply", "kind=no_reply"), ("kind=exit", "kind=exit"),
                           ("blocked request", "blocked request"),
                           ("model call failed", "model call failed"),
                           ("memory committed", "memory_maintenance_committed"),
                           ("memory failed", "memory_maintenance_failed")):
        print(f"  {label:18}", len(re.findall(pattern, text)))
