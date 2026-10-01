"""攻击监视 v2：跟踪 Bot 日志与追踪文件，只从当前文件末尾之后开始读。

只读：不连接 QQ、不改任何项目文件。
"""
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / ".tmp_test_run" / "bot.err"
TRACE = ROOT / ".tmp_test_run" / "model_trace.jsonl"
POLL = 1.0

NOISE = re.compile(r"server listening|WebSocket 监听|Stage 3 started|state restored|"
                   r"context provider enabled|model_trace_enabled|OneBot 已连接|connection open")


def parse(raw: str) -> dict:
    out = {}
    for name in ("decision", "dialogue", "reply_to", "reply"):
        match = re.search(rf"<{name}>\s*(.*?)\s*</{name}>", raw, re.DOTALL | re.IGNORECASE)
        if match:
            out[name] = match.group(1).strip()
    ctx = re.search(r"<context>\s*(.*?)\s*</context>", raw, re.DOTALL | re.IGNORECASE)
    if ctx:
        for line in ctx.group(1).splitlines():
            key, sep, value = line.partition("=")
            if sep:
                out[f"ctx.{key.strip()}"] = value.strip()
    return out


def main() -> None:
    log_offset = LOG.stat().st_size if LOG.exists() else 0
    trace_offset = TRACE.stat().st_size if TRACE.exists() else 0
    print(f"[{time.strftime('%H:%M:%S')}] 监视 v2 启动", flush=True)
    print(f"  日志起点 {log_offset} 字节 / 追踪起点 {trace_offset} 字节", flush=True)

    while True:
        time.sleep(POLL)
        if LOG.exists():
            size = LOG.stat().st_size
            if size < log_offset:
                log_offset = 0
            if size > log_offset:
                with LOG.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(log_offset)
                    chunk = handle.read()
                    log_offset = handle.tell()
                for line in chunk.splitlines():
                    if line.strip() and not NOISE.search(line):
                        print(f"  LOG  {line}", flush=True)

        if TRACE.exists():
            size = TRACE.stat().st_size
            if size < trace_offset:
                trace_offset = 0
            if size > trace_offset:
                with TRACE.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(trace_offset)
                    chunk = handle.read()
                    trace_offset = handle.tell()
                for line in chunk.splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    print(f"  RAW  #{entry.get('sequence')} trigger={entry.get('trigger')} "
                          f"err={entry.get('error') or '-'}", flush=True)
                    for key, value in parse(entry.get("raw_output", "")).items():
                        shown = value if len(value) <= 400 else value[:400] + "…"
                        print(f"         {key} = {shown}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("监视已停止", flush=True)
