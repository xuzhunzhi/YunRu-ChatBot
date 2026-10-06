"""**突变验证**用的一个极小子集：只加载 stage3_runtime 那条组装路径（不建引擎）。

## 为什么单独一个文件

两次必须做的突变里，有一次是把"材料"从 user 段挪到 **system 段**
（也就是把 DATA 当指令用）。而 `stage3_runtime` 在 import 时就把 system prompt 渲染成
模块常量、`compose_system_prompt` 也现取——所以**同一个进程里做不到"改文件再验"**:
文件已经加载进 `sys.modules` 了。

要用**真的改过的源码**去验，就必须在**新进程**里 import。可 `tests/run_offline.py`
是把全部测试装进同一个进程的，`tests/test_quote_learning.py` 又 import 了 `stage3_main`
（连带 `stage3_runtime`）。于是"新进程里只加载 stage3_runtime + 一个假 system prompt"
这件事只能由**别的文件**来做——就是这一个。

它跑的是与 `test_quote_learning.py` 同一组判据（材料必须在 user 段的 DATA 区、
措辞必须是交谈视角），只是把依赖砍到只剩"组装那一步"：

    # 突变：把材料塞进 system 段之后——
    .\\.venv\\Scripts\\python.exe tests\\run_offline.py            # 全套也会红
    .\\.venv\\Scripts\\python.exe tests\\run_single.py tests\\test_quote_injection_shape.py

单独跑法就是上面第二条（突变验证时用的就是它）。
"""
import unittest
from unittest.mock import patch

from qq_roleplay_bot.stage3_runtime import (
    ContextState,
    ConversationMode,
    build_dialogue_messages,
)
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "1084401296"
MATERIAL = ("--- 她以前遇到这种时候 ---\n"
            "（这是她自己的老习惯，不是要你照着念的话；顺不顺得上看眼前这句。）\n"
            "· 有人说字幕翻译的事——先给结论，再补一句为什么\n"
            "--- 到此为止 ---\n")


def _message() -> IncomingMessage:
    return IncomingMessage(
        message_id="m1", session_id=f"group:{GROUP}", user_id="100",
        text="这个字幕只能识别不能翻译，是真的吗", target=MessageTarget(group_id=GROUP),
        sender_name="某人", is_bot_mentioned=True,
    )


def _build(style_material: str):
    current = _message()
    return build_dialogue_messages(
        [current], current=current, mode=ConversationMode.ACTIVE, trigger="threshold",
        context=ContextState(topic="字幕翻译"), style_material=style_material,
    )


class QuoteInjectionShape(unittest.TestCase):
    """材料**只进 user 段的 DATA 区**，而且措辞是材料、不是指令。"""

    def test_the_material_never_lands_in_the_system_prompt(self) -> None:
        persona = "你是一个住在群聊里的角色，这一份是测试用的极简人格。"
        with patch("qq_roleplay_bot.stage3_runtime.resolve_system_prompt",
                   lambda **kwargs: persona):
            request = _build(MATERIAL)
        system = request[0]
        self.assertEqual(system["role"], "system")
        self.assertNotIn("她以前遇到这种时候", system["content"])
        self.assertNotIn("先给结论", system["content"])
        # system 里**只有**人格（加那条常驻禁令），一个字都不许多。
        self.assertTrue(system["content"].startswith(persona))
        self.assertEqual(system["content"].count("她以前"), 0)

    def test_the_material_is_present_in_the_user_data_section(self) -> None:
        with patch("qq_roleplay_bot.stage3_runtime.resolve_system_prompt",
                   lambda **kwargs: "人格"):
            request = _build(MATERIAL)
        users = [item for item in request if item["role"] == "user"]
        self.assertGreaterEqual(len(users), 2)
        volatile = users[-1]["content"]
        self.assertIn("她以前遇到这种时候", volatile)
        # 在 DATA 区里（"你记得的旧事"之后、当前这条之前）。
        self.assertLess(volatile.index("你记得的旧事 结束"),
                        volatile.index("她以前遇到这种时候"))
        self.assertLess(volatile.index("她以前遇到这种时候"),
                        volatile.index("<current_event>"))

    def test_the_material_says_what_it_is_and_is_not(self) -> None:
        """材料必须**自己说清**它是"老习惯"、不是"要照着念的话"（这与措辞是一件事）。"""

        self.assertIn("她自己的老习惯", MATERIAL)
        self.assertIn("不是要你照着念的话", MATERIAL)

    def test_no_material_means_an_unchanged_request(self) -> None:
        with patch("qq_roleplay_bot.stage3_runtime.resolve_system_prompt",
                   lambda **kwargs: "人格"):
            self.assertEqual(_build(""), _build(""))

    def test_the_material_is_descriptive_not_imperative(self) -> None:
        for word in ("必须", "务必", "一定要", "立刻", "马上", "你应该", "你要", "记得要"):
            self.assertNotIn(word, MATERIAL)


if __name__ == "__main__":
    unittest.main()
