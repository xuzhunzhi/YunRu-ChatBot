"""**渠道自己声明"我这条消息可以算主人"**——核心不许再写死一串渠道名。

由来（2026-10-06 外部审查判定）：`runtime.py` 里原来有

    _OWNER_CHANNELS: tuple[str, ...] = ("mail",)

也就是**核心知道有一个叫 `mail` 的渠道**。审查的判定是：**这不是安全洞**
（`claims_owner` 只影响给模型看的 `sender_role` 标签，**不授予任何命令权限**——
命令权限一律走 QQ 那条路，见 `stage3_main._is_super_admin_control`），
它是**扩展性耦合**：以后加一个同样可信的渠道，得回来改核心、还得先知道有这张表。

改成"渠道自己声明"（方案 ii）：插件在 `register()` 里
`registry.provide_owner_channel("<渠道名>")`，核心只问"这个渠道声明过吗"。
核心那一份只剩一条**过渡引导项**（`_OWNER_CHANNELS_BUILTIN = ("mail",)`）——
留着不是为了以后往里加，是为了"这次改动不要求插件侧同时改"：插件侧一个字不动，
行为与改之前完全一致。

这个文件钉四件事：

1. **声明是有效的**：声明过的渠道 + `claims_owner=True` → `sender_role == "owner"`；
2. **没声明的渠道仍然不行**：`claims_owner=True` 也照样是 `mailer`（fail-closed）；
3. **"这个渠道允许吗"只有一个判定点**：`runtime._SeamBinder._owner_channel_ok`
   （源码扫描：别处不许再出现那张写死的渠道名清单）；
4. **过渡引导项只有一条**，且说明里写着"新渠道走声明、不许往这里加"。
"""
from __future__ import annotations

import asyncio
import pathlib

from qq_roleplay_bot import runtime
from qq_roleplay_bot.plugins import DeliveredMessage, PluginRegistry

SRC = pathlib.Path(runtime.__file__).resolve()


class _Recorder:
    """够 `_SeamBinder.deliver` 用的最小引擎：只记下那条 `IncomingMessage`。"""

    def __init__(self, registry: PluginRegistry | None = None) -> None:
        self.seen: list[object] = []
        self.plugin_registry = registry

    async def handle(self, message):
        self.seen.append(message)
        return "OK"


def _deliver(engine: _Recorder, *, channel: str, claims_owner: bool) -> object:
    parcel = DeliveredMessage(channel=channel, sender="someone@example.com",
                              text="今天回来了。", message_id="m1",
                              session_namespace=channel, claims_owner=claims_owner)
    return asyncio.run(runtime._SeamBinder(engine).deliver(parcel))


# --- 1/2. 声明 → 算主人；没声明 → 不算 ---------------------------------------

def test_a_declared_channel_can_claim_owner() -> None:
    """声明过的渠道 + `claims_owner` → `sender_role` 是 `owner`。"""

    registry = PluginRegistry()
    registry.provide_owner_channel("telegram")
    engine = _Recorder(registry)

    assert _deliver(engine, channel="telegram", claims_owner=True) == "OK"
    assert engine.seen and engine.seen[-1].sender_role == "owner", engine.seen


def test_an_undeclared_channel_never_becomes_owner() -> None:
    """**没声明的渠道，`claims_owner=True` 也照样是普通发件人**（fail-closed）。"""

    registry = PluginRegistry()          # 谁都没声明
    engine = _Recorder(registry)

    assert _deliver(engine, channel="telegram", claims_owner=True) == "OK"
    assert engine.seen and engine.seen[-1].sender_role == "mailer", engine.seen


def test_mail_still_works_without_any_declaration() -> None:
    """**过渡期行为不变**：插件一个字不改，`mail` 照样能声明主人。

    这就是留 `_OWNER_CHANNELS_BUILTIN` 那条引导项的全部理由——不是"以后往这里加"。
    """

    engine = _Recorder(PluginRegistry())
    assert _deliver(engine, channel="mail", claims_owner=True) == "OK"
    assert engine.seen and engine.seen[-1].sender_role == "owner", engine.seen

    # 不声明主人时仍然是普通发件人（这一条与渠道清单无关，是 claims_owner 那一侧）
    engine2 = _Recorder(PluginRegistry())
    assert _deliver(engine2, channel="mail", claims_owner=False) == "OK"
    assert engine2.seen and engine2.seen[-1].sender_role == "mailer", engine2.seen


def test_the_seam_survives_a_bare_engine_without_a_registry() -> None:
    """没有注册表的引擎（只造一台 `DialogueEngine` 的测试）不许炸，且按 fail-closed 走。"""

    engine = _Recorder(None)
    assert _deliver(engine, channel="mail", claims_owner=True) == "OK"
    assert engine.seen and engine.seen[-1].sender_role == "owner"

    engine2 = _Recorder(None)
    assert _deliver(engine2, channel="telegram", claims_owner=True) == "OK"
    assert engine2.seen and engine2.seen[-1].sender_role == "mailer"


# --- 3. "这个渠道允许吗"只有一个判定点 ---------------------------------------

def test_the_owner_channel_check_lives_in_exactly_one_place() -> None:
    """核心那一侧只许有**一处**读渠道清单，而且它读的是注册表，不是写死的名字。

    这条是"方案 ii"的钉子：如果哪天有人又在别处写一句
    `channel in ("mail",)`（或再添一张写死的表），这条就红。
    """

    text = SRC.read_text(encoding="utf-8")
    # 渠道清单的**判定**只有这一个方法（`deliver` 调它）。
    assert text.count("def _owner_channel_ok") == 1
    # 判定里面必须走注册表那一侧（`knows_owner_channel`），不是直接比字符串。
    body = text.split("def _owner_channel_ok", 1)[1].split("def owner_channels", 1)[0]
    assert "knows_owner_channel" in body, body


# --- 4. 过渡引导项只有一条，且写明"不许往这里加" -----------------------------

def test_the_builtin_bootstrap_list_is_one_documented_entry() -> None:
    """写死的那一份只剩一条引导项，而且**只有它一个地方**，写法上也不许再出现直比。"""

    assert runtime._OWNER_CHANNELS_BUILTIN == ("mail",), runtime._OWNER_CHANNELS_BUILTIN

    text = SRC.read_text(encoding="utf-8")
    # 渠道名**逐字比**只有那一句（读 `_OWNER_CHANNELS_BUILTIN`）：
    # 谁哪天又写出 `channel in ("mail", …)` 这样的判定，这条就红。
    assert 'channel in ("mail"' not in text, "渠道判定不许直接比字面量，走 _owner_channel_ok"
    # 这个常量在**可执行代码**里只许当"渠道名 → 是否允许"的成员判定用
    # （`in _OWNER_CHANNELS_BUILTIN`，`_owner_channel_ok` 与那个只读快照各一次）。
    # 谁把它写成等式、或拿去比别的字符串，这条就红。
    # 注释与 docstring 里提到它不算（那是说明，不是判定）。
    code = [line for line in text.splitlines()
            if "_OWNER_CHANNELS_BUILTIN" in line and not line.lstrip().startswith("#")]
    for line in code:
        assert ("in _OWNER_CHANNELS_BUILTIN" in line          # `_owner_channel_ok` 的成员判定
                or "set(_OWNER_CHANNELS_BUILTIN)" in line      # `owner_channels` 的只读快照
                or line.strip().startswith("_OWNER_CHANNELS_BUILTIN")), line.strip()


def test_the_contract_for_new_channels_is_written_down() -> None:
    """注册表那一侧的方法说明里要写明"新渠道走声明"——文档不能撒谎。"""

    from qq_roleplay_bot import plugins as plugins_pkg

    doc = plugins_pkg.PluginRegistry.provide_owner_channel.__doc__ or ""
    assert "渠道自己声明" in doc, doc
    assert "邮件通道" in doc, "说明里要点出过渡期那一条（`mail`）为什么还在"


def test_the_registry_is_the_only_channel_directory() -> None:
    """清单本身由注册表持有（`provided` 侧），核心不另存一份副本。"""

    registry = PluginRegistry()
    assert registry.declared_owner_channels() == ()
    registry.provide_owner_channel("telegram")
    registry.provide_owner_channel("mail")
    assert registry.declared_owner_channels() == ("mail", "telegram")
    assert registry.knows_owner_channel("telegram") is True
    assert registry.knows_owner_channel("nope") is False
    # 空名字不该进名单（防御性：插件写错时不至于让谁都算声明过）
    registry.provide_owner_channel("")
    assert registry.declared_owner_channels() == ("mail", "telegram")
