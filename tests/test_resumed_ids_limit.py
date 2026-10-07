"""「被焦点排队过」的消息记账**有上限**（隔离增长审计 [3]）。

`_resumed_ids` 从前是 `set`，而且**全树只有两个移除点**——都在 `_serve_queue`
（轮到那个群、把消息处理完时）。也就是说：一个群被停用、或者它一直排不出去，
它的条目会**永久**留着；被 disable 的群更是"再也不会被轮到"。核查者把它列在
[3] 里（"有清理点但覆盖不全"），并要求加上限。

现在它是 `dict`（插入序），超 `MAX_RESUMED_IDS`（4096）时淘汰**最旧**的那条
——与 `_promise` 同一写法。这一份把上限调小来验：

1. 超上限 → 只留最新的那些（淘汰的是最旧的，不是随机的）；
2. 没超 → 一条不丢；
3. 上限之外的两条语义仍然成立：`in` 判据管用、处理完那条要 `pop` 干净。

去掉上限那段（突变 M）→ 第 1 条红。
"""
from unittest.mock import patch

from qq_roleplay_bot.stage3_main import DialogueEngine


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("这些路径不该调用模型")


def _engine() -> DialogueEngine:
    return DialogueEngine(_NeverCalled())


def test_the_marker_map_drops_the_oldest_when_over_the_cap() -> None:
    engine = _engine()
    with patch("qq_roleplay_bot.stage3_main.MAX_RESUMED_IDS", 4):
        for index in range(6):
            engine._resume_later(f"m{index}")

    assert len(engine._resumed_ids) == 4
    assert [key for key in engine._resumed_ids] == ["m2", "m3", "m4", "m5"], \
        "淘汰点必须固定在最旧那条"
    assert "m0" not in engine._resumed_ids and "m1" not in engine._resumed_ids


def test_nothing_is_dropped_below_the_cap() -> None:
    engine = _engine()
    with patch("qq_roleplay_bot.stage3_main.MAX_RESUMED_IDS", 4):
        for index in range(4):
            engine._resume_later(f"m{index}")
    assert [key for key in engine._resumed_ids] == ["m0", "m1", "m2", "m3"]


def test_the_marker_still_answers_membership_and_can_be_cleared() -> None:
    """换成 dict 不能改语义：`in` 判据照旧（`handle()` 靠它跳过重复记账）。"""

    engine = _engine()
    engine._resume_later("m1")
    assert "m1" in engine._resumed_ids
    engine._forget_queued_message("m1")
    assert "m1" not in engine._resumed_ids
    engine._resume_later("m2")
    engine._resumed_ids.pop("m2", None)          # `_serve_queue` 处理完那一处
    assert "m2" not in engine._resumed_ids


if __name__ == "__main__":
    import unittest

    unittest.main()
