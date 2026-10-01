"""人物画像（kind=profile）：存储、边界与渲染。

由来（2026-09-29 用户："要构建人物画像了"）：记忆里有一条条零散事实，却没有
"我对这个人整体的印象"。画像补的就是这一层：一个人一条、跨群同一条、由记忆
维护 agent 慢慢更新，说话人一开口就渲染进她的眼前（跟着人走，不跟着话题走）。
"""
import atexit
import itertools
import json
import os
import shutil
import unittest
import uuid
from pathlib import Path

from qq_roleplay_bot.memory_maintenance_agent import MEMORY_SYSTEM_PROMPT
from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.stage3_runtime import ConversationMode, ContextState, build_dialogue_messages
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
OTHER = "999999999"

_SCRATCH_ROOT = (Path(__file__).resolve().parents[1] / ".tmp_test_run"
                 / f"profile-{os.getpid()}-{uuid.uuid4().hex[:8]}")
_SCRATCH_COUNTER = itertools.count()
atexit.register(shutil.rmtree, _SCRATCH_ROOT, ignore_errors=True)


class _Scratch:
    def __init__(self) -> None:
        _SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.name = str(_SCRATCH_ROOT / f"t{next(_SCRATCH_COUNTER)}")
        Path(self.name).mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


PROFILE_TEXT = "他叫老明，做后端的，喜欢把话说清楚，讨厌被追问家里的事。跟他说话可以直接一点。"


def operation(batch, *, op="ADD", scope="user_global", key="profile", content=PROFILE_TEXT,
              kind="profile", targets=(), ttl_days=None):
    result = dict(op=op, scope_type=scope,
                  evidence_event_ids=[e.id for e in batch.events if e.speaker == "user"],
                  targets=[{"id": r.id, "revision": r.revision} for r in targets])
    if op != "DELETE":
        result.update(kind=kind, normalized_key=key, content=content, confidence=0.9,
                      ttl_days=ttl_days, subjects=[], durable=True)
    return result


def output(*ops):
    return json.dumps({"operations": list(ops)}, ensure_ascii=False)


class PersonProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = _Scratch()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000_000.0
        self.sequence = 0
        self.store = MemoryStore(Path(self.temp.name) / "memory.sqlite3", clock=lambda: self.now)

    def batch(self, *, text="我叫老明", group=GROUP, user="100"):
        self.sequence += 1
        self.now += 1
        self.store.append(InboxEvent(str(self.sequence), group, user, "user", text, self.now))
        claimed = self.store.claim({group})
        self.assertIsNotNone(claimed)
        return claimed

    def test_profile_round_trip_and_single_row(self) -> None:
        batch = self.batch()
        self.store.commit(batch, output(operation(batch)))
        self.assertEqual(self.store.profile("100"), PROFILE_TEXT)
        # 一个人只有一条：键固定 `profile`，第二次写入是 UPDATE 同一行。
        again = self.batch(text="再聊两句")
        record = self.store.profile_record("100")
        self.assertIsNotNone(record)
        self.store.commit(again, output(operation(again, op="UPDATE", targets=(record,),
                                                 content="他叫老明，做后端，说话很直。")))
        self.assertEqual(self.store.profile("100"), "他叫老明，做后端，说话很直。")

    def test_profile_lives_in_the_long_term_table(self) -> None:
        batch = self.batch()
        self.store.commit(batch, output(operation(batch)))
        with self.store.connection() as db:
            rows = db.execute("SELECT kind FROM memory_long WHERE scope_key='100'").fetchall()
        self.assertEqual([row["kind"] for row in rows], ["profile"])

    def test_profile_is_not_part_of_ordinary_memory_retrieval(self) -> None:
        """画像由说话人触发单独取；混进"你记得的旧事"只会重复占位。"""

        batch = self.batch()
        self.store.commit(batch, output(operation(batch),
                                        operation(batch, kind="name", key="preferred_name",
                                                  content="他希望大家叫他老明", scope="user_global")))
        records = self.store.retrieve(GROUP, "100", "")
        self.assertEqual([r.kind for r in records], ["name"])

    def test_profile_scope_and_key_are_fixed(self) -> None:
        """画像只挂在本人身上、跨群同一条：群内的画像与自造键都要被拒。"""

        from qq_roleplay_bot.memory_model import MemoryValidationError

        for scope, key in (("user_group", "profile"), ("group", "profile"), ("user_global", "self_intro")):
            batch = self.batch()
            with self.assertRaises(MemoryValidationError, msg=f"{scope}/{key} 不该写得进去"):
                self.store.commit(batch, output(operation(batch, scope=scope, key=key)))
            self.assertEqual(self.store.profile("100"), "")
            # 校验失败的批次不会提交，租约还占着；等它过期再试下一种写法。
            self.now += 200

    def test_profile_is_kept_out_of_other_peoples_view(self) -> None:
        batch = self.batch()
        self.store.commit(batch, output(operation(batch)))
        self.assertEqual(self.store.profile("200"), "")

    def test_dialogue_prompt_shows_the_profile_of_the_speaker(self) -> None:
        current = IncomingMessage("m1", f"group:{GROUP}", "100", "在吗", MessageTarget(group_id=GROUP))
        request = build_dialogue_messages(
            [current], current=current, mode=ConversationMode.ACTIVE, trigger="mention",
            context=ContextState(), group_chat=True, profile_note=PROFILE_TEXT,
        )
        volatile = request[2]["content"]
        assert "--- 你对这个人的印象 ---" in volatile, volatile[-500:]
        assert "他叫老明，做后端的" in volatile

    def test_no_profile_means_no_empty_block(self) -> None:
        current = IncomingMessage("m1", f"group:{GROUP}", "100", "在吗", MessageTarget(group_id=GROUP))
        request = build_dialogue_messages(
            [current], current=current, mode=ConversationMode.ACTIVE, trigger="mention",
            context=ContextState(), group_chat=True,
        )
        assert "你对这个人的印象" not in request[2]["content"]

    def test_agent_prompt_documents_profile_maintenance(self) -> None:
        assert "人物画像" in MEMORY_SYSTEM_PROMPT
        assert "normalized_key 固定写 `profile`" in MEMORY_SYSTEM_PROMPT
        # 别指望它每批都改：画像要慢，绝大多数批次不该动。
        assert "大多数批次根本不该动它" in MEMORY_SYSTEM_PROMPT


if __name__ == "__main__":
    unittest.main()
