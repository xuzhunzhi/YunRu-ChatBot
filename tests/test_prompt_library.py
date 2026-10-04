"""六套 prompt 的可编辑层：读写、校验、版本、回滚、must-reply 派生。

两条硬约束在这个文件里被钉住：

1. 保存的 prompt 必须**保住派生锚点**（少了那个片段，`derive_must_reply` 会在每条消息的
   路上抛错——等于改一次 prompt 就把回复打瘫），以及**不含机制词**（AGENTS 2.2）。
2. 内置默认永远可用（覆盖文件删掉就回去），所以面板坏了等于没装它。
"""
import tempfile
from pathlib import Path

from qq_roleplay_bot.prompt_library import (
    PROMPTS, PromptLibrary, PromptRejected, derive_must_reply, missing_anchors,
)
from qq_roleplay_bot.stage3_runtime import SYSTEM_PROMPT, SYSTEM_PROMPT_MUST_REPLY


def _library(tmp: str, **kwargs) -> PromptLibrary:
    return PromptLibrary(Path(tmp) / "prompts", **kwargs)


def _load_plugins() -> None:
    """按运行时那条路装一次插件（`discover` 是"登记发生了"的时刻）。

    **为什么这两个用例需要它**（2026-10-05，前提变了不是放宽断言）：识图从包根
    搬进了插件 `plugins/vision/`，它那一套 prompt 不再是核心里的一句
    `from .vision import …`，而是插件在 `register()` 里
    `registry.provide_prompt("vision", …)` 放上来的。"六套都有原稿"这件事于是
    在插件装配**之后**才成立——运行时的顺序本来就是"先 `discover()`、面板才来问"
    （`build_engine` 里 `attach_plugins()` 在 `prompts.backfill_all()` 之前）。
    插件不在时的样子由 `tests/test_optional_capabilities.py` 那两个用例守
    （`available()` 少一套、`backfill_all()` 只写五份）。
    """

    from qq_roleplay_bot.plugins import PluginRegistry, discover

    discover(PluginRegistry())


def test_builtin_derivation_matches_the_shipped_prompt() -> None:
    """派生算法与内置那一份必须逐字相同——否则面板一保存就漂移。"""

    assert missing_anchors(SYSTEM_PROMPT) == []
    assert derive_must_reply(SYSTEM_PROMPT) == SYSTEM_PROMPT_MUST_REPLY


def test_all_six_prompts_have_builtin_defaults() -> None:

    _load_plugins()
    for name in PROMPTS:
        text = PromptLibrary.builtin(name)
        assert len(text) > 100, name


def test_unknown_prompt_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        for name, action in (("nope", "text"), ("nope", "builtin"), ("nope", "validate"),
                             ("nope", "save"), ("nope", "versions"), ("nope", "reset")):
            try:
                if action == "validate":
                    library.validate(name, "x")
                elif action == "save":
                    library.save(name, "正文够长" * 30)
                elif action == "versions":
                    library.versions(name)
                elif action == "reset":
                    library.reset(name)
                else:
                    getattr(library, action)(name)
            except PromptRejected:
                continue
            raise AssertionError(f"{action} 该拒绝不认识的 prompt")


def test_shared_library_points_at_the_installed_one() -> None:
    """`install` 之后 `shared()` 就是那一份（面板保存的正是它）。"""

    from qq_roleplay_bot import prompt_library

    original = prompt_library.shared()
    with tempfile.TemporaryDirectory() as tmp:
        fresh = _library(tmp)
        try:
            prompt_library.install(fresh)
            assert prompt_library.shared() is fresh
        finally:
            prompt_library.install(original)
    assert prompt_library.shared() is original


def test_save_override_and_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        assert library.is_overridden("judge") is False
        body = library.text("judge") + "\n额外一行。"
        library.save("judge", body)
        assert library.is_overridden("judge") is True
        assert library.text("judge").endswith("额外一行。")
        # 另开一份读同一目录：覆盖是落盘的
        assert _library(tmp).text("judge").endswith("额外一行。")
        library.reset("judge")
        assert library.is_overridden("judge") is False
        assert library.text("judge") == library.builtin("judge")


def test_missing_anchor_is_rejected() -> None:
    """少了派生锚点就该拒绝保存（不留"回复会抛错"的坑）。"""

    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        broken = SYSTEM_PROMPT.replace(
            "<decision>REPLY|NO_REPLY|EXIT_DIALOGUE</decision>\n", "")
        try:
            library.save("reply", broken)
        except PromptRejected as exc:
            assert "片段" in str(exc) or "片段" in exc.detail
        else:
            raise AssertionError("缺锚点的 prompt 不该保存成功")


def test_mechanism_words_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        try:
            library.save("persona", library.builtin("persona") + "\n她会检查触发条件。")
        except PromptRejected as exc:
            assert "机制" in str(exc)
        else:
            raise AssertionError("机制词该被拒")


def test_empty_and_oversized_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        for bad in ("   ", "字" * 30000):
            try:
                library.save("vision", bad)
            except PromptRejected:
                continue
            raise AssertionError("空/超长该被拒")


def test_versions_are_kept_and_restorable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        original = library.text("review")
        first_edit = original + "\n第一版改动。"
        library.save("review", first_edit)
        library.save("review", original + "\n第二版改动。")
        versions = library.versions("review")
        # 每次保存留的是**保存前的上一版**：连存两次也不该互相覆盖
        assert len(versions) >= 2, versions
        texts = [library.read_version("review", item["id"]) for item in versions]
        assert any(text == original for text in texts), [len(t) for t in texts]
        assert any(text == first_edit for text in texts), [len(t) for t in texts]
        # 回滚：把找回来的一版再保存回去
        library.save("review", library.read_version("review", versions[-1]["id"]))
        assert library.text("review") == original


def test_version_id_is_validated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        for bad in ("../persona", "judge.abc", "judge.", ""):
            try:
                library.read_version("judge", bad)
            except PromptRejected:
                continue
            raise AssertionError(f"{bad!r} 该被拒")


def test_disabled_library_never_touches_disk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = PromptLibrary(Path(tmp) / "prompts", enabled=False)
        library.save("judge", library.builtin("judge") + "\n改一行。")
        assert not (Path(tmp) / "prompts").exists()
        assert library.text("judge").endswith("改一行。")


def test_resolve_falls_back_and_never_raises() -> None:
    """`resolve` 从不抛异常：拿不到覆盖版就给内置默认（prompt 层出问题不能让她不说话）。"""

    from qq_roleplay_bot import prompt_library
    from qq_roleplay_bot.prompt_library import resolve

    _load_plugins()
    resolved = resolve("vision", "兜底文本")
    assert resolved in {prompt_library.PromptLibrary.builtin("vision"), "兜底文本"}
    assert resolve("nope", "兜底文本") == "兜底文本"


def test_shared_library_is_replaceable() -> None:
    """替换共享库之后，所有读取点都看到新库（面板保存的就是这一份）。"""

    from qq_roleplay_bot import prompt_library

    with tempfile.TemporaryDirectory() as tmp:
        fresh = _library(tmp)
        original = prompt_library.shared()
        try:
            prompt_library.install(fresh)
            assert prompt_library.shared() is fresh
            fresh.save("judge", fresh.builtin("judge") + "\n共享库改动。")
            assert prompt_library.resolve("judge", "x").endswith("共享库改动。")
        finally:
            prompt_library.install(original)
