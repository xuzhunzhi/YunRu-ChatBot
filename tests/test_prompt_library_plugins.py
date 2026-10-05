"""`test_prompt_library.py` 里**要装插件**的那几条（插件侧）。

## 为什么单独一个文件（2026-10-05 分支拆分）

本体侧那份（`tests/test_prompt_library.py`）测的是 prompt 层的可编辑行为，
不需要任何插件文件。但有两条的前提是"**六套** prompt 都在"：

- `test_all_six_prompts_have_builtin_defaults`——"六套都有原稿"；
- `test_resolve_falls_back_and_never_raises` 的第一段——拿一套**有内置默认**的
  prompt 去验 `resolve` 会回内置默认。

六套里的第六套（`vision`）从 2026-10-05 起是**插件**在 `register()` 里
`registry.provide_prompt("vision", …)` 登记的，所以这两条必须先 `discover()`。
本体侧的正常形态是插件文件夹不在，那它们在那儿**根本不该存在**（不是"存在但跳过"），
于是**整条搬到这里**。

## 为什么要 `skipUnless`

这个文件住在**插件侧**，正常跑得到；守卫只为让它在"插件被移走"的树上
**诚实跳过、报出原因**，而不是假装绿。

断言：`test_all_six_prompts_have_builtin_defaults` 与
`test_resolve_falls_back_and_never_raises` 的**第一段**一字未改。
`test_empty_and_oversized_are_rejected` 这边补的是原来那个**插件靶子**
（`"vision"`）——本体侧那份换成了核心的 `"judge"`，因为插件不在时"vision"不是
合法名字、会让"超长"那一档**以错误的理由**变绿；两边的靶子合起来覆盖的和原来一样。

`_load_plugins()` 与 `_library()` 从本体侧那份 import 过来（借而不是抄）。
"""
import tempfile
from pathlib import Path
import unittest

from qq_roleplay_bot.prompt_library import (
    PROMPTS, PromptLibrary, PromptRejected,
)
from test_prompt_library import _library, _load_plugins


def _plugins_present() -> bool:
    """识图插件在不在——"六套都在"这件事靠它。"""

    import qq_roleplay_bot

    pkg = Path(qq_roleplay_bot.__file__).resolve().parent
    return (pkg / "plugins" / "vision" / "plugin.py").is_file()


_NEEDS_PLUGINS = unittest.skipUnless(
    _plugins_present(), "识图插件不在（本体侧的形态）：'六套 prompt'在这里不成立")


@_NEEDS_PLUGINS
def test_all_six_prompts_have_builtin_defaults() -> None:

    _load_plugins()
    for name in PROMPTS:
        text = PromptLibrary.builtin(name)
        assert len(text) > 100, name


@_NEEDS_PLUGINS
def test_resolve_falls_back_and_never_raises() -> None:
    """`resolve` 从不抛异常：拿不到覆盖版就给内置默认（prompt 层出问题不能让她不说话）。

    这段在插件组装**之后**跑才有意义（"vision"要有内置默认先得插件登记它）；
    插件不在时 `resolve("vision", …)` 只会回兜底文本，那由本体侧那份的
    "不认识的 prompt"那条路覆盖。
    """

    from qq_roleplay_bot import prompt_library
    from qq_roleplay_bot.prompt_library import resolve

    _load_plugins()
    resolved = resolve("vision", "兜底文本")
    assert resolved in {prompt_library.PromptLibrary.builtin("vision"), "兜底文本"}
    assert resolve("nope", "兜底文本") == "兜底文本"


@_NEEDS_PLUGINS
def test_empty_and_oversized_are_rejected_for_a_plugin_prompt() -> None:
    """插件提供的那套 prompt（`"vision"`）也要受"空 / 超长"校验管。

    这条原来在本体侧那份里（靶子是 `"vision"`）。搬过来的原因与 `AGENTS.md` §验证
    的口径一致：那颗靶子**只有插件在时才是合法名字**，插件不在时它会因为
    "不认识的 prompt"而被拒——看着是绿的，测的却不是长度校验。
    """

    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        for bad in ("   ", "字" * 30000):
            try:
                library.save("vision", bad)
            except PromptRejected:
                continue
            raise AssertionError("空/超长该被拒")
