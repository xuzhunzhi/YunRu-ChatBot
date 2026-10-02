"""起点规则的 A/B：**判定能不能把范围往后退**，以及它对缓存命中率的影响。

> ## ⚠️ 现在别拿它出数——它的两条臂会跑成两条不同的轨迹
>
> 2026-10-02 冒烟实测（25 条 × 2 臂）：A 臂判定 13 次、B 臂只有 3 次；回复 5 条 vs 2 条。
> 原因：**判定和回复是模型**，它俩输出不同 → 她"进没进入在对话中"的模式不同 →
> 触发节奏就不同（`ARCHITECTURE.md:473`：进了话题就每条都过判定）。
> 于是两条臂是**两条不同的随机轨迹**，任何差异都分不清是"规则造成的"还是"模型随机造成的"。
> 那一轮跑出来的命中率（A 73.1% / B 44.4%）**是垃圾**，不是规则的对比。
>
> **要用它，先把模型固定住**：把一次真机跑的模型输出录下来，两条臂都重放它，
> 这样唯一的变量才是规则。命中率那边另配一个**本地前缀缓存模拟器**
> （同一 user_id 上把 prompt 与上次逐字符比，公共前缀算 hit），确定、免费、可重复。
>
> ## 另外：这个问题最后**没用 A/B 就答完了**
>
> 用户提醒"用日志里的现有语料跑啊"——日志里 `judge.jsonl` 每条都同时记着
> **当时的当前起点**（prompt 里的 `这段谈话目前的起点：第 N 条`）和
> **判定请求的起点**（output 里的 `<topic_start>`）。1464 条一比就有了答案：
> **0% 后退、88% 原地确认、12% 前进**。于是"允许后退"这条改动是空转，直接撤掉了。
> 免费、无随机性、比 A/B 更硬。**下次先想"日志里是不是已经有这个数"。**

## 这次要比什么

2026-10-02 的改动只动了一条本地规则：

* **旧（A）**：话题起点只许前进（`advance_topic_start`）。判定想把更早几句也交给她，
  本地不理——于是她平时只看得见 13 条、最少 1 条。
* **新（B）**：起点是**选择**，往前往后都允许；另外"话题换了、摘要脱节"时丢摘要。

用户关心的两条：
1. **命中率**（"尤其注意命中率"）——放开后退会不会把 prompt 前缀弄碎、缓存吃掉。
2. 判定**到底会不会**往后退。**它不报，这次改动就等于没生效**（我只改了本地规则，
   判定提示词一个字没动）。所以 A 臂里要专门记下"判定想后退但被拒"的次数。

## 怎么跑（安全边界照抄 `long_chat_run.py`）

* 直接调 `DialogueEngine.handle()`，**不发 QQ 消息**；
* **不挂记忆服务**（`QQBOT_MEMORY_ENABLED=0`）、**不写运行状态**（`QQBOT_STATE_PERSIST=0`）；
* **不写模型追踪**（`QQBOT_DEBUG_MODEL_IO=0`）+ `QQBOT_DATA_DIR` 指到本次输出目录，
  所以任何落盘都落在 `dev/ab_out/<tag>/`，**碰不到 `run/data`**；
* **每条臂各自一套 `user_id`**，两边缓存互不污染，也污染不了线上缓存；
* 真 key 与真人格**只读** `run/.env` 与 `run/data/private_docs/`，不复制、不打印。

## 输出

    dev/ab_out/<tag>/<arm>/events.jsonl   每条消息一行（含方向、摘要、可见条数、用量）
    dev/ab_out/<tag>/<arm>/report.json    该臂汇总
    dev/ab_out/<tag>/ab_report.json       A/B 对照 + 命中率归因
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

DEV = Path(__file__).resolve().parent
RUN = DEV.parent / "run"
CORPUS = RUN / "data" / "long_chat_500.txt"
GROUP = "717151356"

#: 三条臂。A/B 是主对照；B1 用来**分离变量**（只放开后退、不丢摘要）。
ARMS: dict[str, dict[str, bool]] = {
    "A": {"backward": False, "drop_summary": False},   # 改动前
    "B": {"backward": True, "drop_summary": True},     # 改动后
    "B1": {"backward": True, "drop_summary": False},   # 只放开后退
}


def _env_value(path: Path, key: str) -> str:
    """从配置文件里取一个键（**不打印值**）。"""

    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return ""


def prepare_environment(out_root: Path) -> None:
    """在 import `qq_roleplay_bot` **之前**把环境定死。

    `dev_config` 在 import 期就把配置读成模块常量；而且它的 `_inject_env_file`
    是"不覆盖已存在的键"（`dev_config.py:90`），所以这里先放的键一定赢。
    """

    real_env = RUN / ".env"
    if not real_env.is_file():
        raise SystemExit(f"找不到 {real_env}——真 key 从这里读")
    os.environ["QQBOT_ENV_FILE"] = str(real_env)

    # 安全边界：一条都不能少（照抄 long_chat_run.py）
    os.environ["QQBOT_STATE_PERSIST"] = "0"
    os.environ["QQBOT_DEBUG_MODEL_IO"] = "0"
    os.environ["QQBOT_MEMORY_ENABLED"] = "0"
    os.environ["QQBOT_DUAL_AGENT"] = "1"
    # 任何落盘都去本次输出目录：碰不到 run/data
    os.environ["QQBOT_DATA_DIR"] = str(out_root / "scratch")
    # 真人格只读使用（否则回落到模板人格，"她回得好不好"就没意义了）
    persona = RUN / "data" / "private_docs" / "base_prompt.REAL.py"
    if persona.is_file():
        os.environ["QQBOT_BASE_PROMPT_FILE"] = str(persona)


class Clock:
    """可控时钟：只服务批量触发与活跃超时。"""

    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self.step = step

    def monotonic(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += self.step


class Recorder:
    """包住真实 client，记下每次调用的用量（含缓存命中）。"""

    def __init__(self, inner, sink: list[dict], slot: dict) -> None:
        self.inner = inner
        self.sink = sink
        self.slot = slot

    async def complete(self, request):
        started = time.perf_counter()
        raw = await self.inner.complete(request)
        system = request[0]["content"] if request else ""
        kind = "reply"
        for mark, name in (("回应判定器", "judge"), ("压缩存档", "compact")):
            if mark in system:
                kind = name
                break
        usage = dict(self.inner.last_usage or {})
        self.sink.append({
            "index": self.slot["index"],
            "kind": kind,
            "elapsed": round(time.perf_counter() - started, 3),
            "hit": int(usage.get("prompt_cache_hit_tokens", 0) or 0),
            "miss": int(usage.get("prompt_cache_miss_tokens", 0) or 0),
        })
        return raw


def install_arm(arm: str) -> tuple[list[dict], list[dict]]:
    """把这一臂的规则装上；返回两个记账表（起点请求、摘要丢弃）。

    断言式安装：A 臂必须是**旧语义**（只许前进 + 不丢摘要），否则 A/B 就不成立。
    """

    from qq_roleplay_bot.stage3_runtime import ConversationState

    base = Path(__file__).resolve().parent
    assert (base / "src" / "qq_roleplay_bot").is_dir(), "必须在 dev/ 下跑（用 dev/src）"

    select_original = ConversationState.select_topic_start
    drop_original = ConversationState.drop_summary_if_stale
    moves: list[dict] = []
    drops: list[dict] = []
    spec = ARMS[arm]

    if spec["backward"]:
        select_impl = select_original
    else:
        def select_impl(self, seq):                      # noqa: ANN001 - 旧语义
            if seq is None:
                return False
            current = self.topic_start_seq
            if current is not None and seq <= current:
                return False
            self.topic_start_seq = seq
            return True

    def select_logged(self, seq):                        # noqa: ANN001
        current = self.topic_start_seq
        applied = select_impl(self, seq)
        if seq is not None and current is not None and seq < current:
            moves.append({"current": current, "requested": seq, "applied": applied,
                          "arm_would": spec["backward"]})
        return applied

    ConversationState.select_topic_start = select_logged

    if spec["drop_summary"]:
        def drop_logged(self, topic_start):              # noqa: ANN001
            before = bool(self.summary)
            applied = drop_original(self, topic_start)
            if applied:
                drops.append({"through": topic_start, "had": before})
            return applied
        ConversationState.drop_summary_if_stale = drop_logged
    else:
        ConversationState.drop_summary_if_stale = lambda self, topic_start: False

    return moves, drops


def load_corpus(limit: int) -> list[tuple[str, str, str, bool]]:
    rows: list[tuple[str, str, str, bool]] = []
    for raw in CORPUS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        user_id, name, text = (part.strip() for part in line.split("|", 2))
        mentioned = text.startswith("@YunRu")
        if mentioned:
            text = text[len("@YunRu"):].strip()
        rows.append((user_id, name, text, mentioned))
    return rows[:limit] if limit else rows


async def run_arm(arm: str, script, step: float, out_dir: Path) -> dict:
    from qq_roleplay_bot import dev_config, trigger as trigger_module
    from qq_roleplay_bot.llm_client import OpenAICompatibleClient
    from qq_roleplay_bot.stage3_main import DialogueEngine
    from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

    moves, drops = install_arm(arm)
    clock = Clock(step)
    trigger_module.time = type("FakeTime", (), {"monotonic": staticmethod(clock.monotonic)})()
    calls: list[dict] = []
    slot = {"index": 0}
    target = MessageTarget(group_id=GROUP)
    session = f"group:{GROUP}"

    reply_client = Recorder(OpenAICompatibleClient(
        dev_config.API_BASE_URL, dev_config.API_KEY, dev_config.API_MODEL,
        user_id=f"qqbot-ab-{arm.lower()}-dialogue"), calls, slot)
    judge_client = Recorder(OpenAICompatibleClient(
        dev_config.API_BASE_URL, dev_config.JUDGE_API_KEY, dev_config.API_MODEL,
        user_id=f"qqbot-ab-{arm.lower()}-judge"), calls, slot)
    engine = DialogueEngine(reply_client, judge_client=judge_client,
                            target_group_id=GROUP, enabled_group_ids=frozenset({GROUP}),
                            group_listen=True, typing_sim=False, clock=clock.monotonic)

    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    events = events_path.open("w", encoding="utf-8", newline="\n")
    started = time.perf_counter()
    for position, (user_id, name, text, mentioned) in enumerate(script, start=1):
        slot["index"] = position
        calls_before = len(calls)
        moves_before, drops_before = len(moves), len(drops)
        message = IncomingMessage(message_id=f"ab-{position}", session_id=session,
                                  user_id=user_id, text=text, target=target,
                                  sender_name=name, sender_role="member",
                                  is_bot_mentioned=mentioned)
        reply = await engine.handle(message)
        outgoing = [item for item in [reply, *engine.take_follow_ups()] if item is not None]
        state = engine.sessions.state(session)
        events.write(json.dumps({
            "index": position,
            "user": name,
            "text": text,
            "mentioned": mentioned,
            "calls": [call["kind"] for call in calls[calls_before:]],
            "usage": calls[calls_before:],
            "reply": "\n\n".join(item.text for item in outgoing),
            "visible": len(state.topic_history()),      # ← 她实际看得见多少条
            "window": len(state.history),
            "live": len(state.live_history()),
            "start_before": None if not moves_before else moves[moves_before - 1]["requested"],
            "start": state.topic_start_seq,
            "move_back_requests": moves[moves_before:],
            "summary_chars": len(state.summary),
            "summary_drops": drops[drops_before:],
        }, ensure_ascii=False) + "\n")
        events.flush()
        clock.tick()
    events.close()

    per_kind: dict[str, dict[str, int]] = {}
    for call in calls:
        bucket = per_kind.setdefault(call["kind"], {"calls": 0, "hit": 0, "miss": 0})
        bucket["calls"] += 1
        bucket["hit"] += call["hit"]
        bucket["miss"] += call["miss"]
    report = {
        "arm": arm, "spec": ARMS[arm], "messages": len(script),
        "wall_seconds": round(time.perf_counter() - started, 1),
        "calls_by_kind": per_kind,
        "backward_requests": len(moves),
        "backward_applied": sum(1 for item in moves if item["applied"]),
        "summary_drops": len(drops),
        "replies": int(engine._stats["replies"]),
        "judge_calls": int(engine._stats["judge_calls"]),
        "final": {"window": len(engine.sessions.state(session).history),
                  "summary_chars": len(engine.sessions.state(session).summary),
                  "topic_start_seq": engine.sessions.state(session).topic_start_seq},
    }
    visibles = [json.loads(line)["visible"] for line in
                events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    report["visible_median"] = statistics.median(visibles) if visibles else 0
    report["visible_min"] = min(visibles) if visibles else 0
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return report


def hit_rate(bucket: dict[str, int]) -> float:
    total = bucket["hit"] + bucket["miss"]
    return (bucket["hit"] / total) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="起点规则的 A/B（离线回放，不发消息）")
    parser.add_argument("--limit", type=int, default=60, help="回放多少条（0=全部 500）")
    parser.add_argument("--step", type=float, default=5.0, help="每条推进多少秒（影响触发频率）")
    parser.add_argument("--arms", default="A,B", help="跑哪几条臂，默认 A,B")
    parser.add_argument("--tag", default="", help="输出目录名（默认时间戳）")
    args = parser.parse_args()

    tag = args.tag or time.strftime("%m%d-%H%M%S")
    out_root = DEV / "ab_out" / tag
    prepare_environment(out_root)

    sys.path.insert(0, str(DEV / "src"))
    from qq_roleplay_bot import dev_config  # noqa: E402

    if not dev_config.API_KEY or dev_config.API_KEY.startswith("sk-offline"):
        print("真 key 没读进来——检查 run/.env 里的 QQBOT_API_KEY")
        return 2
    if not CORPUS.is_file():
        print(f"找不到语料 {CORPUS}")
        return 2

    script = load_corpus(args.limit)
    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"未知的臂 {unknown}（可选 {sorted(ARMS)}）")
        return 2

    print(f"语料 {len(script)} 条；臂 {arms}；输出 {out_root}")
    reports = {}
    for arm in arms:
        print(f"\n=== 臂 {arm} {ARMS[arm]}")
        reports[arm] = asyncio.run(run_arm(arm, script, args.step, out_root / arm))
        got = reports[arm]
        print(f"  可见条数 中位 {got['visible_median']} / 最小 {got['visible_min']}；"
              f"判定 {got['judge_calls']} 次、回复 {got['replies']} 条；"
              f"判定想后退 {got['backward_requests']} 次（生效 {got['backward_applied']} 次）；"
              f"丢摘要 {got['summary_drops']} 次")

    (out_root / "ab_report.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    print("\n=== A/B 对照")
    print(f"  {'臂':<4}{'调用':>6}{'判定':>6}{'回复':>6}{'可见中位':>10}{'可见最小':>10}"
          f"{'想后退':>8}{'丢摘要':>8}")
    for arm in arms:
        got = reports[arm]
        print(f"  {arm:<4}{sum(b['calls'] for b in got['calls_by_kind'].values()):>6}"
              f"{got['judge_calls']:>6}{got['replies']:>6}"
              f"{got['visible_median']:>10}{got['visible_min']:>10}"
              f"{got['backward_requests']:>8}{got['summary_drops']:>8}")

    print("\n=== 缓存命中率（按 token；命中率 = hit / (hit+miss)）")
    kinds = sorted({k for arm in arms for k in reports[arm]["calls_by_kind"]})
    print(f"  {'链路':<9}" + "".join(f"{arm:>18}" for arm in arms))
    for kind in kinds:
        row = f"  {kind:<9}"
        for arm in arms:
            bucket = reports[arm]["calls_by_kind"].get(kind)
            row += (f"{hit_rate(bucket):>17.1%} " if bucket else f"{'-':>18}")
        print(row)
    for arm in arms:
        buckets = reports[arm]["calls_by_kind"]
        hit = sum(b["hit"] for b in buckets.values())
        miss = sum(b["miss"] for b in buckets.values())
        print(f"  {arm} 合计: hit={hit:,} miss={miss:,} 命中率={hit/(hit+miss) if hit+miss else 0:.1%}")
    print(f"\n明细：{out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
