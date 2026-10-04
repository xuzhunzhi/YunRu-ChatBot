"""**插件不准干涉对话**（2026-10-04 用户原话）。

> "不不不，这是不被允许的，插件不准干涉对话"

这个文件是**反向守卫**：插件线曾经提供过一条"给判定 agent 递极短提示"的通道
（`extensions.MAX_JUDGE_HINT_LENGTH` + `PromptSources.judge_hints()` +
`dialogue_judge.build_judge_messages(plugin_hints=…)`），它让插件能影响
**"要不要接这句话"**——也就是干涉对话。用户明确禁掉了它。

那三个名字**从来没有被移植到本体**；这些断言的作用是：
**谁哪天把它加回来，测试立刻红**，而不是等到上线才发现。

（同理，测那条通道的 5 条测试已从 `tests/test_plugin_prompts.py` 删掉——
功能被禁，测它的断言没有存在理由。这不是"为了绿灯删断言"。）
"""

from __future__ import annotations

import inspect
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qq_roleplay_bot import dialogue_judge, extensions  # noqa: E402


class PluginsMustNotTouchDialogueTests(unittest.TestCase):
    def test_the_judge_hint_channel_does_not_exist(self) -> None:
        self.assertFalse(
            hasattr(extensions, "MAX_JUDGE_HINT_LENGTH"),
            "插件给判定 agent 递提示的通道被禁了（用户 2026-10-04：插件不准干涉对话）")

    def test_prompt_sources_expose_no_judge_hints(self) -> None:
        sources = extensions.PromptSources()
        for name in ("judge_hints", "build_judge_hint"):
            self.assertFalse(
                hasattr(sources, name),
                "%s 会让插件影响判定（要不要接话），已被禁" % name)

    def test_the_judge_prompt_builder_takes_no_plugin_hints(self) -> None:
        params = set(inspect.signature(dialogue_judge.build_judge_messages).parameters)
        self.assertNotIn(
            "plugin_hints", params,
            "判定 prompt 一旦能收插件提示，插件就干涉了对话（已禁）")


if __name__ == "__main__":
    unittest.main()
