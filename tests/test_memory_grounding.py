"""写入闸门与脏数据体检（2026-10-09 用户实测）。

由来（用户原话，两条都在面板「人物画像 → 零散记录」里看见的）：

1. "**同一个键重复多份**"：`participating_in_diansai` 三行、`planning_gpu_purchase` 两行；
2. "**这里全是错的**"：`participating_in_diansai` 写的是"他在参与电赛"，而他的原话是
   "笨蛋，**大家只是在讨论**电赛"、"那你咋还**催别人**去准备电赛"——他真正说的是
   "**我没在打电赛**，我说要参加的是**雏雁和大创**"。

翻码 + 翻真库 + 翻真日志（`run/data/logs/memory.jsonl`）得到的两条结论：

| 现象 | 判据（实测） |
| --- | --- |
| 同键落成多行 | `MemoryStore.commit` 的 ADD/UPDATE/MERGE 分支从前是"把目标行标 `superseded`，再 INSERT 一条新的"，于是每一轮改写都留一行；旧行的 `updated_at` 被写成新行的 `created_at`（同一秒两行就是这么来的） |
| 归属挂错 | 归属一直是 `batch.user_id`（那一批的说话人），**内容说的是谁从来没被检查过**；没有任何"依据必须是本人说的"这道闸 |

所以这里钉住三件事：

- **同键就地更新**：同一 `(scope, key, normalized_key)` 第二次写入落成**一行**，
  正文是较新的那条、`updated_at` 刷新、`revision` 仍 +1；而 **supersede 链本身不许坏**
  （MERGE 的目标行、改键的 UPDATE、强化搬层、DELETE 仍然要标 `superseded` 并留痕）。
- **依据闸门**（`memory_grounding`）：归属不是他本人说的、没有本人发言就改正文、
  依据里既没有第一人称也不是照抄、数字与型号凭空补全、正文为空 —— 一律不落库，
  逐条计数（`store.rejection_counts()`）。
- **脏数据看得见**（`store.hygiene_counts()`）：同键多行 / 正文空行 / 无证据的生效记录
  三类计数进 `/super status` 与 `/super memory`。历史行**一条不删**（AGENTS §五：
  删记忆必须用户点头）。

**汉字专名（"雏雁/大创"被写成"电赛"）机械上判不出来**——没有词典就不能区分"电赛"
是专名还是普通词，两条替代判据都量过（按词元覆盖率会拒掉约 89% 的真实写入；按"同群
别人记忆里的词"会命中 47%），所以那一条只落在维护 prompt 的规矩里（见
`test_the_maintenance_prompt_states_the_four_rules`），机械侧只做数字与型号。
"""
import atexit
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path
from dataclasses import replace

from qq_roleplay_bot import memory_grounding
from qq_roleplay_bot.memory_maintenance_agent import MEMORY_SYSTEM_PROMPT
from qq_roleplay_bot.memory_model import (
    InboxEvent,
    MemoryBatch,
    MemoryOperation,
    MemoryValidationError,
    parse_operations,
)
from qq_roleplay_bot.memory_store import INTENTION_KIND, MemoryStore

GROUP = "1084401296"
OWNER = "2639157559"
OTHER = "242003347"

# 临时目录放在项目内（与 tests/test_memory.py 同一套理由：protect_directory 会改
# 临时目录权限，系统 temp 下的目录之后连删除都会被拒）。
_SCRATCH_ROOT = (Path(__file__).resolve().parents[1] / ".tmp_test_run"
                 / f"grounding-{os.getpid()}-{uuid.uuid4().hex[:8]}")
# 整个根目录在进程退出时收掉：只收每个用例的子目录会把这层空壳留在
# `.tmp_test_run/` 里，跑几次就攒一串（与 tests/test_memory.py 同一手法）。
atexit.register(shutil.rmtree, _SCRATCH_ROOT, ignore_errors=True)


class _Scratch:
    def __init__(self) -> None:
        _SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.name = str(_SCRATCH_ROOT / uuid.uuid4().hex[:8])
        Path(self.name).mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp = _Scratch()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_800_000_000.0
        self.path = Path(self.temp.name) / "memory.sqlite3"
        # 这两条闸门与近重复/配额无关，关掉后它们不会来抢拒绝码。
        self.store = MemoryStore(self.path, clock=lambda: self.now,
                                 daily_add_limit=0, near_duplicate_ratio=9.9)
        self.sequence = 0

    # --- 夹具：走真实的 append → claim → commit -----------------------------

    def event(self, text, *, user=OWNER, speaker="user", group=GROUP, when=None):
        self.sequence += 1
        return InboxEvent(f"e{self.sequence}", group, user, speaker, text,
                          self.now if when is None else when)

    def write(self, text, op, **kwargs):
        """把一条事件喂进真实的 claim→commit 路径，返回 (生效的 op, 拒绝码)。"""

        self.now += 1
        event = self.event(text)
        self.store.append(event)
        batch = self.store.claim({GROUP}, lease_seconds=60)
        assert batch is not None, "事件应当能被认领成一批"
        return self.commit(batch, op, evidence=[event.id], **kwargs)

    def commit(self, batch, op, *, evidence=(), targets=(), **fields):
        payload = {"op": op, "scope_type": fields.pop("scope", "user_global"),
                   "evidence_event_ids": list(evidence),
                   "targets": [{"id": row["id"], "revision": row["revision"]} for row in targets]}
        if op != "DELETE":
            payload.update(kind=fields.pop("kind", "status"),
                           normalized_key=fields.pop("key", "subject"),
                           content=fields.pop("content", "在准备比赛"),
                           confidence=fields.pop("confidence", 0.8),
                           ttl_days=fields.pop("ttl", 7),
                           subjects=fields.pop("subjects", []),
                           durable=fields.pop("durable", False))
        assert not fields, f"没用上的字段：{fields}"
        applied = list(self.store.commit(
            batch, json.dumps({"operations": [payload]}, ensure_ascii=False)))
        return applied

    def rows(self, key=None):
        """两张表里的行（可选按 key 过滤），带层级。"""

        out = []
        with self.store.connection() as db:
            for tier in ("memory_short", "memory_long"):
                clause = " WHERE normalized_key=?" if key else ""
                params = (key,) if key else ()
                for row in db.execute(f"SELECT * FROM {tier}{clause}", params):
                    item = dict(row)
                    item["tier"] = tier
                    out.append(item)
        return out

    def counts_for(self, key):
        active = [row for row in self.rows(key) if row["status"] == "active"]
        return active[0] if active else None


# --- 1. 同键就地更新 ---------------------------------------------------------


class SameKeyUpdatesInPlaceTests(_Base):
    def test_two_writes_of_the_same_key_land_in_one_row_with_the_newer_content(self):
        """同一秒两条同键 → **一行**，正文是较新的那条，updated_at 已刷新。

        从前这里是两行：旧行被标 `superseded`，新行另一行；旧行的 `updated_at`
        被写成新行的 `created_at`，于是面板上出现"同一秒两行、正文几乎一样"。
        """

        self.write("我正在打电赛，明天去准备", "ADD", key="diansai",
                   content="正在打电赛", ttl=7)
        first = self.rows("diansai")
        self.assertEqual(len(first), 1, "第一次写入应当只有一行")
        self.assertAlmostEqual(first[0]["created_at"], first[0]["updated_at"], places=3)

        self.write("我把电赛改到下个月了", "UPDATE", key="diansai",
                   content="下个月再打电赛", ttl=7, targets=[first[0]])
        rows = self.rows("diansai")
        self.assertEqual(len(rows), 1, "同键第二次写入必须是就地更新，不许长第二行")
        self.assertEqual(rows[0]["content"], "下个月再打电赛", "正文要是较新的那条")
        self.assertEqual(rows[0]["revision"], 2, "revision 仍然每次 +1（强化判据靠它）")
        self.assertEqual(rows[0]["created_at"], first[0]["created_at"], "created_at 不许被改写")
        self.assertGreater(rows[0]["updated_at"], first[0]["updated_at"], "updated_at 要刷新")
        self.assertEqual(self.store.retrieve(GROUP, OWNER, "")[0].content, "下个月再打电赛")

    def test_an_update_that_renames_the_key_keeps_the_superseded_chain(self):
        """**supersede 链本身保留**：改键的 UPDATE 仍然把旧行标 superseded 并留痕。

        （这正是"就地更新"不许做过头的地方：同键才就地刷新，换了键就是另一件事。）
        """

        self.write("我正在打电赛", "ADD", key="diansai", content="正在打电赛")
        first = self.rows("diansai")[0]
        self.write("我说要参加的是雏雁", "UPDATE", key="chuyan",
                   content="在准备雏雁", ttl=7, targets=[first])
        old = self.rows("diansai")
        new = self.rows("chuyan")
        self.assertEqual([(r["status"], r["tier"]) for r in old], [("superseded", "memory_short")])
        self.assertEqual([r["content"] for r in old], ["正在打电赛"], "旧行正文要留着")
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["revision"], 2)

    def test_merge_still_supersedes_every_target_and_bumps_the_revision(self):
        """MERGE 两件事 → 目标行全部 `superseded`（历史不丢），只留一条新的活跃行。

        判据（突变 ③）：把 revision/supersede 语义弄坏（例如就地覆盖掉合并来源、
        或者新行的 revision 不涨），这一条必须红。
        """

        self.write("我喜欢喝威士忌", "ADD", key="whisky", kind="preference",
                   content="我喜欢喝威士忌", ttl=None)
        self.write("我也喜欢金酒", "ADD", key="gin", kind="preference",
                   content="我也喜欢金酒", ttl=None)
        whisky = self.rows("whisky")[0]
        gin = self.rows("gin")[0]
        self.write("威士忌和金酒我都喜欢", "MERGE", key="whisky", kind="preference",
                   content="威士忌和金酒我都喜欢", ttl=None, targets=[whisky, gin])
        sources = {row["normalized_key"]: row for row in self.rows()}
        self.assertEqual(sources["gin"]["status"], "superseded", "被并掉的那条要留 supersede 痕")
        self.assertEqual(sources["gin"]["revision"], 1, "被并掉的行不许被就地覆盖")
        # 同键那条：MERGE 的目标里就有它 → 就地刷新（不再多一行），revision 涨。
        self.assertEqual(sources["whisky"]["status"], "active")
        self.assertEqual(sources["whisky"]["revision"], max(whisky["revision"], gin["revision"]) + 1)
        self.assertEqual(sources["whisky"]["content"], "威士忌和金酒我都喜欢")
        self.assertEqual(len([row for row in self.rows() if row["status"] == "active"]), 1)

    def test_a_new_fact_still_inserts_and_add_on_an_existing_key_is_refused(self):
        """没有同键时照旧 INSERT；ADD 撞上活着的键仍然照旧报错（模型该用 UPDATE）。"""

        self.write("我在准备雏雁", "ADD", key="chuyan", content="在准备雏雁")
        self.assertEqual(len(self.rows("chuyan")), 1)
        self.now += 1
        event = self.event("在准备大创")
        self.store.append(event)
        batch = self.store.claim({GROUP}, lease_seconds=60)
        from qq_roleplay_bot.memory_model import MemoryValidationError

        with self.assertRaises(MemoryValidationError):
            self.commit(batch, "ADD", evidence=[event.id], key="chuyan",
                        content="在准备大创")


# --- 2. 依据闸门：归属 / 自述 / 具体记号 / 空正文 -----------------------------


class AttributionGateTests(_Base):
    def other_user_batch(self, other_text, owner_text="我在看比赛"):
        """手工造一批：这一批的归属人是 OWNER，但**引用的是别人说的话**。

        `claim` 只会认领同一个人的消息，所以"引用别人的话"这一路只能这样造出来；
        它正是 `memory_store.commit` 里那道闸要拦的东西（归属得来自发言人本身，
        不是"这一批的会话人"）。
        """

        owner = self.event(owner_text)
        other = self.event(other_text, user=OTHER)
        self.store.append(owner)
        self.store.append(other)
        batch_id = uuid.uuid4().hex
        with self.store.connection() as db:
            db.execute("INSERT INTO leases VALUES (?,?,?,?)",
                       (batch_id, GROUP, OWNER, self.now + 600))
            db.execute("UPDATE inbox SET lease_id=? WHERE id IN (?,?)",
                       (batch_id, owner.id, other.id))
        return MemoryBatch(batch_id, GROUP, OWNER, (owner, other), (), self.now + 600)

    def test_evidence_from_someone_else_is_not_his_fact(self):
        """别人开的玩笑/吩咐（"你去准备比赛吧"）不能变成他的事实——不落库。

        突变 ①：把这道闸去掉（"拿不准就挂到当前会话那个人"），这条必须红。
        """

        batch = self.other_user_batch("桓衍：你去准备比赛吧，别划水")
        applied = self.commit(batch, "ADD", evidence=["e2"], key="go_contest",
                              content="要去准备比赛", ttl=7)
        self.assertEqual(applied, [])
        self.assertEqual(self.rows("go_contest"), [], "依据是别人说的话 → 不许落库")
        self.assertEqual(self.store.rejection_counts(), {"rejected_other_speaker": 1})

    def test_the_mentioned_person_never_becomes_the_owner(self):
        """归属按**发言人**算：A 说"我在打电赛" → 只在 A 名下，B 名下一行都没有。

        突变 ②：把归属改成"话题里出现的名字"，这条必须红（B 名下会冒出这条）。
        """

        self.write("我在打电赛，明天去准备", "ADD", key="diansai",
                   content="我在打电赛", ttl=7)
        mine = self.rows("diansai")
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["scope_key"], OWNER, "归属必须是说话人自己")
        self.assertEqual([row for row in self.rows() if row["scope_key"] == OTHER], [],
                         "B 什么都没说，B 名下一行都不许有")
        self.assertEqual(self.store.retrieve(GROUP, OTHER, "", mentioned=(OTHER,)), ())

    def test_a_joke_or_a_group_topic_is_not_his_status(self):
        """他本人只是在说"大家只是在讨论电赛" → 不能记成"他在参与电赛"（不落库）。"""

        applied = self.write("笨蛋，大家只是在讨论电赛", "ADD", key="participating_in_diansai",
                             content="在参与电赛（电子设计竞赛），近期群里在讨论电赛准备，"
                                     "他也在其中；被别人催着去准备。", ttl=30)
        self.assertEqual(applied, [])
        self.assertEqual(self.rows("participating_in_diansai"), [])
        self.assertEqual(self.store.rejection_counts(), {"rejected_not_self_statement": 1})

    def test_a_quoted_sentence_still_passes(self):
        """正文就是照抄他那句话 → 放行（"没有第一人称"不该拦掉原话本身）。"""

        applied = self.write("放假了！", "ADD", key="on_holiday", content="放假了！", ttl=7)
        self.assertEqual([op.op for op in applied], ["ADD"])
        self.assertEqual(self.counts_for("on_holiday")["content"], "放假了！")

    def test_rewriting_the_content_without_any_evidence_is_refused(self):
        """"没有本人发言就要改正文"→ 不落库；正文一个字没改的复查照旧放行。

        真库里那条 `participating_in_diansai` 的 rev3 就是 evidence=[] 改写出来的。
        """

        self.write("我会参加比赛", "ADD", key="contest", content="我会参加比赛", ttl=7)
        row = self.rows("contest")[0]
        self.now += 1
        event = self.event("绷不住")
        self.store.append(event)
        batch = self.store.claim({GROUP}, lease_seconds=60)
        # 引用的是"绷不住"（他自己的话，但没有第一人称）→ 自述那道闸先拦。
        self.assertEqual(self.commit(batch, "UPDATE", evidence=[event.id], key="contest",
                                     content="在参加电赛相关的事", ttl=7, targets=[row]), [])
        self.assertEqual(self.store.rejection_counts(), {"rejected_not_self_statement": 1})
        # 一条依据都不给、还想改正文 → 拒。
        self.now += 1
        event2 = self.event("嗯")
        self.store.append(event2)
        batch2 = self.store.claim({GROUP}, lease_seconds=60)
        self.assertEqual(self.commit(batch2, "UPDATE", evidence=[], key="contest",
                                     content="换一个说法", ttl=7, targets=[row]), [])
        self.assertEqual(self.store.rejection_counts()["rejected_no_evidence"], 1)
        # 正文一个字没改的复查（周期复查常见）→ 放行，而且是就地刷新。
        self.now += 1
        event3 = self.event("嗯")
        self.store.append(event3)
        batch3 = self.store.claim({GROUP}, lease_seconds=60)
        applied = self.commit(batch3, "UPDATE", evidence=[], key="contest",
                              content="我会参加比赛", ttl=7, targets=[self.rows("contest")[0]])
        self.assertEqual([op.op for op in applied], ["UPDATE"])
        self.assertEqual(len(self.rows("contest")), 1)

    def test_a_topic_summary_at_user_scope_still_passes_attribution(self):
        """user-scope 的 `topic_summary` 引用**本人**的话 → 照旧放行（不许误伤）。"""

        applied = self.write("我说要参加的是雏雁和大创", "ADD", kind="topic_summary",
                             key="contest_plan", scope="user_group",
                             content="他打算参加雏雁和大创", ttl=30)
        self.assertEqual([op.op for op in applied], ["ADD"])
        self.assertEqual(self.counts_for("contest_plan")["content"], "他打算参加雏雁和大创")

    def test_a_topic_summary_citing_someone_else_is_refused(self):
        """user-scope 的 `topic_summary` 引用**别人**的原话 → 不落库。

        独立审查（2026-10-09）发现 2 的实测绕过形状：`topic_summary` 不在旧的
        `PERSONAL_KINDS` 里，归属/自述两道闸对它全部不生效——"他在准备电赛"
        （引用别人 `242003347` 的"带我打电赛"）就这么放行了，正是用户报过的
        `participating_in_diansai` 那类错误换个 kind 原样再现。
        突变：把归属闸收回"只对个人 kind 生效"的分支 → 这条必须红。
        """

        batch = self.other_user_batch("带我打电赛", owner_text="我在看比赛")
        applied = self.commit(batch, "ADD", evidence=["e2"], kind="topic_summary",
                              scope="user_group", key="participating_in_diansai",
                              content="他在准备电赛，被别人催", ttl=30)
        self.assertEqual(applied, [])
        self.assertEqual(self.rows("participating_in_diansai"), [],
                         "引用别人的话当依据 → 换成 topic_summary 也不许落库")
        self.assertEqual(self.store.rejection_counts(), {"rejected_other_speaker": 1})

    def test_an_evidence_free_recheck_cannot_flag_durable_or_change_kind(self):
        """无证据的复查豁免**不许顺手升档**：标 durable / 换 kind 都要被拒。

        独立审查（2026-10-09）发现 1 的另一半：`return ""` 早退让无任何本人发言的
        UPDATE 把脏记录翻成 durable 推进长期。升档必须过全部闸。
        """

        self.write("我在准备雏雁", "ADD", key="chuyan", content="在准备雏雁", ttl=7)
        self.now += 1
        event = self.event("嗯")
        self.store.append(event)
        batch = self.store.claim({GROUP}, lease_seconds=60)
        # 正文一个字没改，但顺手标 durable → 拒（新码），库里的 durable 不许被翻。
        applied = self.commit(batch, "UPDATE", evidence=[], key="chuyan",
                              content="在准备雏雁", ttl=None, durable=True,
                              targets=[self.rows("chuyan")[0]])
        self.assertEqual(applied, [])
        self.assertEqual(self.store.rejection_counts(), {"rejected_ungrounded_promotion": 1})
        self.assertEqual(self.counts_for("chuyan")["durable"], 0, "无证据不许翻 durable")
        # 换 kind 同理 → 拒。
        self.now += 1
        event2 = self.event("嗯")
        self.store.append(event2)
        batch2 = self.store.claim({GROUP}, lease_seconds=60)
        applied = self.commit(batch2, "UPDATE", evidence=[], kind="preference", key="chuyan",
                              content="在准备雏雁", ttl=None, targets=[self.rows("chuyan")[0]])
        self.assertEqual(applied, [])
        self.assertEqual(self.store.rejection_counts()["rejected_ungrounded_promotion"], 2)

    def test_the_recheck_exemption_does_not_skip_the_specifics_gate(self):
        """复查豁免只豁免"无本人发言"那一道，**数字闸照走**（审查发现 1 的主缝）。

        审查实测形状：对已有记录发一条 `evidence_event_ids=[]` 的 UPDATE，
        早退 `return ""` 连 `invented_specifics` 都没跑。这里直接打函数——
        存储路径上 ground 里总含目标正文，正文没改时数字必然"出现过"，
        所以这道缝只能在函数级看见、也就在函数级钉死。
        突变：把豁免改回提前 `return ""` → 这条必须红。
        """

        event = self.event("我在看比赛")
        op = MemoryOperation("UPDATE", "user_global", "status", "dirty", "在用 4060 打游戏",
                             0.8, 7, [])
        self.assertEqual(
            memory_grounding.grounding_rejection(
                op, cited=(), ground=event.text,
                target_contents=("在用 4060 打游戏",), target_kinds=("status",)),
            "rejected_ungrounded_specifics",
            "正文没改的复查豁免不许跳过数字闸")
        # 豁免仍然有效的那一半：正文没改、也没有数字要查 → 照旧放行。
        clean = replace(op, content="在看比赛")
        self.assertEqual(
            memory_grounding.grounding_rejection(
                clean, cited=(), ground=event.text,
                target_contents=("在看比赛",), target_kinds=("status",)),
            "")

    def test_an_invented_date_is_refused_like_any_other_specific(self):
        """自造日期不再整段免检："他在 2026-11 参加电赛" → 不落库。

        独立审查（2026-10-09）发现 3：原先 `DATE_PATTERN` 先把日期删掉再查，
        "上旬/前后"的许可被放大成整段日期免检。现在日期与型号同一条规则。
        突变：恢复"先删日期再查" → 这条必须红。
        """

        applied = self.write("我要参加比赛", "ADD", key="diansai_2026",
                             content="在 2026-11 参加电赛", ttl=30)
        self.assertEqual(applied, [])
        self.assertEqual(self.rows("diansai_2026"), [])
        self.assertEqual(self.store.rejection_counts(), {"rejected_ungrounded_specifics": 1})

    def test_a_date_that_was_actually_said_still_passes(self):
        """原话里真有的日期照旧放行（不许误伤合法的时间标注）。"""

        applied = self.write("我 2026-11 参加电赛", "ADD", key="diansai_date",
                             content="2026-11 参加电赛", ttl=30)
        self.assertEqual([op.op for op in applied], ["ADD"])
        self.assertEqual(self.counts_for("diansai_date")["content"], "2026-11 参加电赛")

    def test_a_conclusion_with_numbers_that_were_never_said_is_refused(self):
        """数字与型号必须照原话（"5050/4060/7700"不在被引用的那句话里）→ 不落库。"""

        applied = self.write("我来商场看手机的", "ADD", key="planning_gpu_purchase",
                             content="正在考虑买显卡（5050/4060/7700 之间选）", ttl=30)
        self.assertEqual(applied, [])
        self.assertEqual(self.rows("planning_gpu_purchase"), [])
        self.assertEqual(self.store.rejection_counts(), {"rejected_ungrounded_specifics": 1})

    def test_an_empty_content_never_lands_and_is_counted(self):
        """空正文不许入库，而且要计数（防御性的一道：正常路径上 parse 已经拦掉）。

        真库里那两条"只有键、内容栏空"是**删除路径**故意清空的（见
        `test_deleted_rows_are_counted_as_empty_content`），不是这里漏进来的。
        """

        event = self.event("我发烧了")
        op = MemoryOperation("ADD", "user_global", "status", "fever", "   ", 0.8, 7, (event.id,))
        self.assertEqual(
            memory_grounding.grounding_rejection(op, cited=(event,), ground=event.text),
            "rejected_empty_content")
        self.assertEqual(
            memory_grounding.grounding_rejection(replace(op, content=""), cited=(event,), ground=event.text),
            "rejected_empty_content")
        # 非空正文走同一入口 → 放行（证明上面拒绝的是"空"，不是别的原因）。
        self.assertEqual(
            memory_grounding.grounding_rejection(replace(op, content="我在发烧"),
                                                 cited=(event,), ground=event.text),
            "")


# --- 3. 脏数据体检（只统计，不清理） -----------------------------------------


class HygieneCountTests(_Base):
    def _legacy_row(self, db, *, table, record_id, key, content, status, scope_key=OWNER,
                    source_event_ids="[]", revision=1):
        db.execute(
            f"INSERT INTO {table} (id,scope_type,scope_key,subject_user_id,kind,normalized_key,"
            "content,confidence,created_at,updated_at,expires_at,revision,status,"
            "source_event_ids,durable) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, "user_global", scope_key, scope_key, "status", key, content, 0.7,
             self.now, self.now, None, revision, status, source_event_ids, 0))

    def test_duplicate_and_empty_rows_are_counted_but_left_alone(self):
        """同键多行 / 正文空行 / 无证据的生效记录：三类计数看得见，而且**一行都不删**。"""

        with self.store.connection() as db:
            # 同一秒两行同键（历史形状：旧行 superseded + 新行 active）
            self._legacy_row(db, table="memory_short", record_id="dup1",
                             key="participating_in_diansai", content="在参与电赛",
                             status="superseded")
            self._legacy_row(db, table="memory_short", record_id="dup2",
                             key="participating_in_diansai", content="在参与电赛",
                             status="active", source_event_ids='["e1"]')
            # 删除路径留下的空正文行（status=deleted）
            self._legacy_row(db, table="memory_short", record_id="empty1",
                             key="pc_diy_accident", content="", status="deleted")
            # 生效中、却一条证据都没有（归属查不到依据）
            self._legacy_row(db, table="memory_long", record_id="orphan",
                             key="lonely", content="在准备比赛", status="active")
        hygiene = self.store.hygiene_counts()
        self.assertEqual(hygiene["duplicate_key_groups"], 1)
        self.assertEqual(hygiene["duplicate_key_rows"], 1)
        self.assertEqual(hygiene["empty_content_records"], 1)
        self.assertEqual(hygiene["evidence_free_records"], 1)
        # 计数进得了 `counts()`（`/super memory` 与 `/super status` 读的就是它）。
        counts = self.store.counts()
        self.assertEqual(counts["duplicate_key_groups"], 1)
        self.assertEqual(counts["empty_content_records"], 1)
        # **一行都不许动**（删记忆要用户点头）。
        self.assertEqual(len(self.rows("participating_in_diansai")), 2)
        self.assertEqual(len(self.rows("pc_diy_accident")), 1)

    def test_deleted_rows_are_counted_as_empty_content(self):
        """删除路径本身会把正文清空（AGENTS §2.1：先归档再清空），这类残留行要被数到。"""

        self.write("我会参加比赛", "ADD", key="contest", content="我会参加比赛", ttl=7)
        row = self.rows("contest")[0]
        self.now += 1
        event = self.event("忘掉")
        self.store.append(event)
        batch = self.store.claim({GROUP}, lease_seconds=60)
        self.assertEqual([op.op for op in self.commit(batch, "DELETE", evidence=[event.id],
                                                      targets=[row])], ["DELETE"])
        kept = self.rows("contest")
        self.assertEqual([r["status"] for r in kept], ["deleted"])
        self.assertEqual(kept[0]["content"], "", "删除路径仍然清空正文（既有行为不变）")
        self.assertEqual(self.store.hygiene_counts()["empty_content_records"], 1)

    def test_the_overview_and_the_status_snapshot_expose_the_three_counts(self):
        """观测：`/super memory` 概览与 `/super status` 快照里都能看见这三类数。"""

        from qq_roleplay_bot.memory_view import build_overview

        with self.store.connection() as db:
            self._legacy_row(db, table="memory_short", record_id="empty1",
                             key="pc_diy_accident", content="", status="deleted")
        text = build_overview(self.store.counts()).render()
        self.assertIn("同键多行", text)
        self.assertIn("正文空行", text)
        self.assertIn("无证据的生效记录", text)

        from qq_roleplay_bot.memory_service import MemoryService
        from qq_roleplay_bot.memory_config import MemorySettings

        service = MemoryService(None, lambda: {GROUP}, settings=MemorySettings(),
                                store=self.store)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["hygiene"]["empty_content_records"], 1)
        self.assertEqual(snapshot["rejected"], {})
        self.store.rejected["rejected_not_self_statement"] = 2
        self.assertEqual(service.snapshot()["rejected"], {"rejected_not_self_statement": 2})


# --- 4. 维护 prompt 的四条规矩 ------------------------------------------------


def test_the_maintenance_prompt_states_the_four_rules() -> None:
    """四条规矩必须在维护 prompt 里（机械判不出汉字专名，那一条只能靠规矩）。"""

    rules = (
        "只有他本人在说自己，才算他的事",      # 别人的玩笑/吩咐 ≠ 他的事实
        "对象必须照原话说",                    # 手机 ≠ 笔记本，说不清就不记
        "名字必须照原话",                      # 雏雁/大创 不许归纳成"电赛"
        "常识与系统本来的行为",                # 不许把常识写成"问题已修复"
        "拿不准就 IGNORE",                     # fail-closed
    )
    for rule in rules:
        assert rule in MEMORY_SYSTEM_PROMPT, f"维护 prompt 缺少这条规矩：{rule}"


def test_the_gate_module_documents_the_measured_thresholds() -> None:
    """闸门模块里必须留着"为什么不做得更宽"的量出来的数（别让下一个人重拍一遍）。"""

    import inspect as _inspect

    doc = _inspect.getdoc(memory_grounding) or ""
    assert "89%" in doc and "47%" in doc, "别把量过的判据删掉"
    assert memory_grounding.QUOTE_RATIO == 0.6


# --- 5. 「惦记事」：她自己应下的那件事（2026-10-11） ---------------------------


class IntentionEvidenceTests(_Base):
    """`intention` 的依据方向与普通事实**相反**，而且两层判据都得认她自己的原话。

    普通事实的规矩是"依据必须是他本人说的"（`speaker="user"`）——别人的玩笑、
    别人的吩咐都不算他的事。她自己的备忘反过来：依据得是**她发出去过的那句原话**
    （`speaker="yunru"`）。少任何一层，维护 agent 照 prompt 交上来的 `intention`
    都会被整条打回，这条路根本落不了地：

    1. 解析层 `memory_model.parse_operations`——按 kind 分方向挑证据发言者；
    2. 写入层 `memory_store.commit` 的 `deleted_evidence` 判据——键被划掉过（留下墓碑）
       之后，只有**她新发的**那句才算新证据（旧那句不认，不然等于把划掉的记忆写回来）。

    > 测不到什么：`intention` 是谁、什么时候写进记忆的——维护 agent 下一拍照 prompt 写，
    > 不在这一层。这里只测"它交上来之后，这条依据认不认"。
    """

    def op(self, *, evidence, kind=INTENTION_KIND, key="ask_about_api",
           content="回头问他那个接口好没", scope="user_global", ttl=7, durable=False) -> str:
        """一条 ADD 的原始输出（只过解析层，不进 `store.commit`）。"""

        return json.dumps({"operations": [{
            "op": "ADD", "scope_type": scope, "kind": kind, "normalized_key": key,
            "content": content, "confidence": 0.9, "ttl_days": ttl,
            "evidence_event_ids": list(evidence), "subjects": [],
            "durable": durable, "targets": []}]}, ensure_ascii=False)

    def two_voices(self, *, note="回头问他那个接口好没"):
        """一批里同时有"她说的"和"这个人说的"两句话——证据方向拿它俩对照。"""

        self.now += 1
        own = self.event(note, speaker="yunru")
        self.store.append(own)
        self.now += 1
        theirs = self.event("那个接口怎么样了")
        self.store.append(theirs)
        batch = self.store.claim({GROUP}, lease_seconds=60)
        assert batch is not None, "两句都该被认领进同一批"
        return batch, own, theirs

    def test_the_parser_accepts_her_own_words_for_an_intention(self) -> None:
        """解析层：`intention` 只认**她自己的**发言当依据；拿他的话当依据整条打回。"""

        batch, own, theirs = self.two_voices()

        ops = parse_operations(self.op(evidence=[own.id]), batch)
        self.assertEqual([op.kind for op in ops], [INTENTION_KIND])

        with self.assertRaises(MemoryValidationError) as caught:
            parse_operations(self.op(evidence=[theirs.id]), batch)
        self.assertEqual(str(caught.exception), "invalid_operation",
                         "依据是别人说的 → 这条 intention 不该被解析出来")

    def test_the_parser_keeps_the_old_direction_for_ordinary_kinds(self) -> None:
        """别的 kind 方向不变：只有**她自己的**话当依据，照样整条打回。

        这条与上一条成对——放开 `intention` 的那一支不许顺手把别的类别也放开。
        """

        batch, own, theirs = self.two_voices()

        with self.assertRaises(MemoryValidationError) as caught:
            parse_operations(self.op(evidence=[own.id], kind="preference",
                                     key="likes_tea", content="喜欢喝龙井"), batch)
        self.assertEqual(str(caught.exception), "invalid_operation")

        ops = parse_operations(self.op(evidence=[theirs.id], kind="preference",
                                       key="likes_tea", content="喜欢喝龙井"), batch)
        self.assertEqual([op.kind for op in ops], ["preference"], "他本人说的话照旧算数")

    def _deleted_intention(self):
        """写一条 intention → 划掉（留墓碑）→ 返回那句原话（下面两条用例的分岔点）。"""

        batch, own, _theirs = self.two_voices()
        applied = self.commit(batch, "ADD", evidence=[own.id], kind=INTENTION_KIND,
                              key="ask_about_api", content=own.text, ttl=7)
        self.assertEqual(len(applied), 1, applied)
        record = self.counts_for("ask_about_api")
        self.assertIsNotNone(record)
        self.assertEqual(record["kind"], INTENTION_KIND)
        self.assertEqual(record["tier"], "memory_short", "她自己的备忘不进长期")

        self.now += 1
        self.assertEqual(self.store.purge_records([record["id"]], reason="agent_delete"), 1)
        with self.store.connection() as db:
            tomb = db.execute(
                "SELECT deleted_at FROM tombstones WHERE normalized_key='ask_about_api'"
            ).fetchone()
        self.assertIsNotNone(tomb, "删除必须留下墓碑（不然旧证据能把它写回来）")
        return own.text

    def test_a_deleted_key_is_not_revived_by_her_old_words(self) -> None:
        """她**以前**那句（时刻早于墓碑）当依据 → 整批 `deleted_evidence`：划掉就是划掉。"""

        note = self._deleted_intention()

        self.now += 1
        stale = self.event(note, speaker="yunru", when=self.now - 10_000)
        self.store.append(stale)
        stale_batch = self.store.claim({GROUP}, lease_seconds=60)
        with self.assertRaises(MemoryValidationError) as caught:
            self.commit(stale_batch, "ADD", evidence=[stale.id], kind=INTENTION_KIND,
                        key="ask_about_api", content=stale.text, ttl=7)
        self.assertEqual(str(caught.exception), "deleted_evidence")

    def test_a_deleted_key_comes_back_when_she_promises_again(self) -> None:
        """她**新发**的那句（时刻晚于墓碑）当依据 → 写得回来：划掉的是那件旧事，不是这个键。

        与上一条成对，两条一起钉住"谁的哪句话算新证据"：

        * 旧那句（早于墓碑）→ `deleted_evidence`，划掉就是划掉；
        * 新那句（晚于墓碑）→ 算新证据，她重新应下同一件事就该记下来。

        这一条原来是**绊线用例**：2026-10-11 写完这条链路时
        `memory_store.py:763` 按 kind 挑"谁的发言算证据时刻"写成了
        `voices = (("user", INTENTION_KIND) if … else ("user",))`，而发言者只有
        `user` / `yunru` 两种，`"intention"` 永远匹配不上——于是 intention 的
        证据时刻恒为 0、`evidence_time <= tomb[0]` 恒成立，**任何**留过墓碑的键
        都再也写不回来。已改成 `("yunru",)`（与那一行自己的注释、以及
        `qq_roleplay_bot/memory_grounding.py:197` 的
        `own_voice = "yunru" if op.kind == "intention" else "user"` 同一套判据），
        这条随之倒过来断言。突变自检：把 `:763` 改回 `("user", INTENTION_KIND)` → 必红。
        """

        note = self._deleted_intention()

        self.now += 10
        fresh = self.event(note, speaker="yunru")
        self.store.append(fresh)
        fresh_batch = self.store.claim({GROUP}, lease_seconds=60)

        applied = self.commit(fresh_batch, "ADD", evidence=[fresh.id], kind=INTENTION_KIND,
                              key="ask_about_api", content=fresh.text, ttl=7)

        self.assertEqual(len(applied), 1, applied)
        record = self.counts_for("ask_about_api")
        self.assertIsNotNone(record, "她重新应下这件事，就该重新记下来")
        self.assertEqual(record["kind"], INTENTION_KIND)
        self.assertEqual(record["tier"], "memory_short")

    def test_her_own_note_still_has_to_be_hearsay_free(self) -> None:
        """她自己的话当依据也不能凭空补数字/型号（`rejected_ungrounded_specifics`）。"""

        batch, own, _theirs = self.two_voices(note="回头我看看那个接口")

        applied = self.commit(batch, "ADD", evidence=[own.id], kind=INTENTION_KIND,
                              key="ask_about_api", content="回头问他那个接口 8080 端口的事", ttl=7)

        self.assertEqual(list(applied), [], "凭空补出来的端口号不该被记下来")
        self.assertIsNone(self.counts_for("ask_about_api"))
