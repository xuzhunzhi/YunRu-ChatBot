"""六套 prompt 的可编辑层：读写、校验、版本、回滚、must-reply 派生。

两条硬约束在这个文件里被钉住：

1. 保存的 prompt 必须**保住派生锚点**（少了那个片段，`derive_must_reply` 会在每条消息的
   路上抛错——等于改一次 prompt 就把回复打瘫），以及**不含机制词**（AGENTS 2.2）。
2. 内置默认永远可用（覆盖文件删掉就回去），所以面板坏了等于没装它。

## 两条要装插件的用例搬走了（2026-10-05 分支拆分）

`test_all_six_prompts_have_builtin_defaults`（"六套都有原稿"）与
`test_resolve_falls_back_and_never_raises` 的第一段原来靠
`_load_plugins()` 先 `discover()`——因为识图那套 prompt 从 2026-10-05 起是
**插件**在 `register()` 里 `provide_prompt("vision", …)` 登记的"六套"这件事只在
插件装配之后成立。本体侧的正常形态是插件不在，那两段在那种树上**根本不该存在**
（不是"存在但跳过"），所以搬到了 `tests/test_prompt_library_plugins.py`。

`test_empty_and_oversized_are_rejected` **留在本体侧**，但靶子从插件提供的
`"vision"` 换成核心自己的 `"judge"`——原因见那条的 docstring（原来"超长"那一档
会因为"不认识这个名字"而变绿，是**以错误的理由通过**）。断言一档没少。
"""
import tempfile
from pathlib import Path

from qq_roleplay_bot.prompt_library import (
    PromptLibrary, PromptRejected, derive_must_reply, missing_anchors,
)
from qq_roleplay_bot.stage3_runtime import SYSTEM_PROMPT, SYSTEM_PROMPT_MUST_REPLY


def _library(tmp: str, **kwargs) -> PromptLibrary:
    return PromptLibrary(Path(tmp) / "prompts", **kwargs)


def _load_plugins() -> None:
    """按运行时那条路装一次插件（`discover` 是"登记发生了"的时刻）。

    **留着它，不是因为本文件还在用**（2026-10-05 分支拆分把用它的两条搬去了
    `tests/test_prompt_library_plugins.py`），而是因为它是这两份文件**共用**的辅助：
    识图那套 prompt 现在是插件在 `register()` 里 `provide_prompt("vision", …)` 放上来的，
    "六套都有原稿"只在 `discover()` 之后成立——运行时的顺序本来就是"先 `discover()`、
    面板才来问"（`build_engine` 里 `attach_plugins()` 在 `prompts.backfill_all()` 之前）。
    借而不是抄：装配这条路只该有一份维护。
    """

    from qq_roleplay_bot.plugins import PluginRegistry, discover

    discover(PluginRegistry())


def test_builtin_derivation_matches_the_shipped_prompt() -> None:
    """派生算法与内置那一份必须逐字相同——否则面板一保存就漂移。"""

    assert missing_anchors(SYSTEM_PROMPT) == []
    assert derive_must_reply(SYSTEM_PROMPT) == SYSTEM_PROMPT_MUST_REPLY


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
    """空 / 超长一律拒绝。

    2026-10-05 分支拆分：这条原来拿 `"vision"` 当靶子——那是**插件提供**的 prompt
    名，本体侧（插件不在）根本不是合法名字，于是"超长"那一档会被
    `PromptRejected("不认识的 prompt")` 拦下、**以错误的理由变绿**。所以本体侧这份
    改用核心自己的 `"judge"`；`"vision"` 那份（六套都在时才有意义）在
    `tests/test_prompt_library_plugins.py`。两档判据没变、也没变弱。
    """

    with tempfile.TemporaryDirectory() as tmp:
        library = _library(tmp)
        for bad in ("   ", "字" * 30000):
            try:
                library.save("judge", bad)
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
    """`resolve` 从不抛异常：拿不到覆盖版就给内置默认（prompt 层出问题不能让她不说话）。

    2026-10-05 分支拆分：原来这里第一段拿 `"vision"` 当靶子，那需要**插件先登记**
    那一套 prompt（六套都在才有意义），所以那一段搬到
    `tests/test_prompt_library_plugins.py`。本体侧这份改用核心自己的 `"judge"`——
    **判据没换、也没变弱**（"有内置默认的名字 → 给内置默认"与"没有的名字 → 给兜底"
    两条路都还在），只是换了个本体侧一定存在的名字。
    """

    from qq_roleplay_bot import prompt_library
    from qq_roleplay_bot.prompt_library import resolve

    resolved = resolve("judge", "兜底文本")
    assert resolved == prompt_library.PromptLibrary.builtin("judge")
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
