"""插件档位写错**不再放行**（`docs/audits/security-clock-2026-10-08.md` A-03）。

修之前：`level_of` 把"拼错的档位"与"没声明"一视同仁，都回落成最低档 `public`，
而核心的放行判据是"不是 super / admin 就 return True"——于是 `min_level = "Supre"`
这种一个字母的手误让"踢人 / 禁言 / 改群名"这类特权命令变成**谁都能用**，
而且过程静默：没有日志、没有拒绝、没有告警。

现在分成两件事（fail-closed）：

- **没声明**（属性缺失 / 空串 / 不是字符串）→ 按协议默认 `public`（公开命令照旧能用）；
- **声明了却不认识** → `CommandRegistry.add()` **拒绝装载**并记一行 WARNING；
  另外 `level_of` 对这种情况返回空串，`_plugin_level_allowed` 对不在 `LEVELS` 里的
  一律拒绝——第二道网，绕过装载点也放不进来。

突变（还原）：把这三处改回"不认识就按 public 放行" → 本文件里那几条红。
"""
import asyncio
import logging
from contextlib import contextmanager

from qq_roleplay_bot.command_plugins import LEVELS, CommandRegistry, declared_level, level_of
from qq_roleplay_bot.stage3_main import DialogueEngine
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
MEMBER = "100"
TARGET = MessageTarget(group_id=GROUP)


class _NeverCalled:
    async def complete(self, request):
        raise AssertionError("命令这条路径不该调用模型")


class _Echo:
    def __init__(self, name: str = "echo", level: str | None = "public") -> None:
        self.name = name
        if level is not None:
            self.min_level = level
        self.calls = 0

    def match(self, message) -> bool:
        return (message.text or "").startswith("/echo")

    async def handle(self, message):
        self.calls += 1
        return "echo"

    def help_text(self) -> str:
        return ""


@contextmanager
def _warnings(logger_name: str):
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    target = logging.getLogger(logger_name)
    target.addHandler(handler)
    previous = target.level
    target.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)


def _message(text: str = "/echo", user_id: str = MEMBER) -> IncomingMessage:
    return IncomingMessage(f"m:{text}", f"group:{GROUP}", user_id, text, TARGET)


# --- 1. 装载点：认不出的档位不装 -------------------------------------------------


def test_an_unrecognized_level_is_rejected_at_load_time() -> None:
    registry = CommandRegistry()
    bad = _Echo(level="Supre")
    with _warnings("qq_roleplay_bot.command_plugins") as records:
        added = registry.add(bad)

    assert added is False, "拼错的档位装进链里了"
    assert registry.plugins == ()
    assert any("command plugin rejected" in record.getMessage() for record in records), \
        "拒绝了却不报警"


def test_every_valid_level_is_still_accepted() -> None:
    registry = CommandRegistry()
    for level in LEVELS:
        assert registry.add(_Echo(name=f"c-{level}", level=level)) is True
    assert len(registry.plugins) == len(LEVELS)
    assert [level_of(plugin) for plugin in registry.plugins] == list(LEVELS)


def test_a_missing_level_still_means_public() -> None:
    """协议里的默认：没声明 = 公开命令（不该被这条修正误伤）。"""

    plugin = _Echo(level=None)
    assert declared_level(plugin) is None
    assert level_of(plugin) == "public"
    assert CommandRegistry().add(plugin) is True


def test_a_blank_or_non_string_level_also_means_public() -> None:
    for raw in ("", "   ", None, 3):
        plugin = _Echo(level=None)
        plugin.min_level = raw
        assert declared_level(plugin) is None, raw
        assert level_of(plugin) == "public", raw
        assert CommandRegistry().add(plugin) is True, raw


def test_an_unrecognized_level_never_resolves_to_a_usable_level() -> None:
    assert level_of(_Echo(level="Supre")) == "", "拼错的档位被降级成可用的档位了"
    assert level_of(_Echo(level="ADMIN ")) == "admin", "大小写与空白要照旧容忍"


# --- 2. 第二道网：核心的放行判据 ---------------------------------------------


def test_the_engine_refuses_a_level_outside_the_known_set() -> None:
    engine = DialogueEngine(_NeverCalled(), enabled_group_ids=frozenset({GROUP}))
    assert engine._plugin_level_allowed("public", _message()) is True
    for level in ("Supre", "", "root"):
        assert engine._plugin_level_allowed(level, _message()) is False, level


def test_a_smuggled_in_plugin_with_a_bad_level_cannot_be_dispatched() -> None:
    """绕过装载点（直接塞进链里）也放不进来：核心不回复、也不交给模型。"""

    async def run():
        plugin = _Echo(level="Supre")
        registry = CommandRegistry()
        registry.plugins = (plugin,)          # 绕过 `add()` 的坏插件
        engine = DialogueEngine(_NeverCalled(), command_registry=registry,
                                enabled_group_ids=frozenset({GROUP}))
        reply = await engine.handle(_message())
        return plugin, engine, reply

    plugin, engine, reply = asyncio.run(run())
    assert reply is None, "坏档位的插件居然执行了"
    assert plugin.calls == 0
    assert engine._stats["ignored_messages"] == 1


if __name__ == "__main__":
    import unittest

    unittest.main()
