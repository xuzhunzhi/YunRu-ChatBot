"""记忆污染长期观测：定期采样记忆库，只记录变化，不改动记忆本身。

只读 SQLite（mode=ro）+ 定期读群历史抓取 inbox 的构成。
采样结果写入 .tmp_test_run/memory_samples/（gitignored），不污染项目文件。
"""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data" / "memory" / "memory.sqlite3"
OUT = ROOT / ".tmp_test_run" / "memory_samples"
INTERVAL = 300.0  # 5 分钟一次


def snapshot() -> dict:
    if not DB.exists():
        return {"error": "db_missing"}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {},
        "records": [],
        "inbox_by_speaker": [],
        "audit_ops": [],
    }
    for table in ("records", "inbox", "audit", "receipts", "replays", "tombstones"):
        try:
            data["counts"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            data["counts"][table] = None

    for row in conn.execute(
        "SELECT scope_type, scope_key, kind, normalized_key, content, confidence, status "
        "FROM records ORDER BY updated_at DESC"
    ):
        data["records"].append({
            "scope": f"{row['scope_type']}/{row['kind']}",
            "key": row["normalized_key"],
            "content": row["content"],
            "confidence": row["confidence"],
            "status": row["status"],
        })

    try:
        for row in conn.execute(
            "SELECT speaker, COUNT(*) AS n FROM inbox GROUP BY speaker"
        ):
            data["inbox_by_speaker"].append({"speaker": row["speaker"], "count": row["n"]})
    except sqlite3.OperationalError:
        pass

    try:
        for row in conn.execute(
            "SELECT op, COUNT(*) AS n FROM audit GROUP BY op ORDER BY n DESC"
        ):
            data["audit_ops"].append({"op": row["op"], "count": row["n"]})
    except sqlite3.OperationalError:
        pass

    conn.close()
    return data


def fingerprint(data: dict) -> str:
    """只对 records 内容取指纹，用来判断是否发生变化。"""

    return json.dumps(
        [(r["key"], r["content"], r["confidence"], r["status"]) for r in data.get("records", [])],
        ensure_ascii=False, sort_keys=True,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    last_fp = None
    index = 0
    print(f"[{datetime.now():%H:%M:%S}] 记忆观测启动，每 {int(INTERVAL)}s 采样一次", flush=True)

    while True:
        index += 1
        data = snapshot()
        fp = fingerprint(data)
        changed = fp != last_fp
        last_fp = fp

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = OUT / f"sample-{index:04d}-{stamp}.json"
        try:
            OUT.mkdir(parents=True, exist_ok=True)   # 目录被外部删掉时重建
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"    ! 采样写入失败（目录可能被删）: {type(exc).__name__}", flush=True)

        print(f"[{data['at']}] #{index} {'*变化*' if changed else '无变化'}", flush=True)
        print(f"    records={data['counts'].get('records')} inbox={data['counts'].get('inbox')} "
              f"audit={data['counts'].get('audit')}", flush=True)
        if changed:
            for record in data["records"]:
                mark = "ACTIVE " if record["status"] == "active" else record["status"][:8].ljust(8)
                print(f"    [{mark}] {record['key']} conf={record['confidence']}", flush=True)
                print(f"        {record['content'][:110]}", flush=True)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("观测已停止", flush=True)
