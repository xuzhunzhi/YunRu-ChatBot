"""群管理 / 群主动作的**执行端**：走**真装配**（`runtime.build_engine` + 假传输层）
逐条核对"到底发出去了什么 action"。

## 为什么单独有这个文件

2026-10-04 那次接缝移植之后，仓库里同时流传着两种**互相矛盾**的说法：

- `docs/STAGE4_AS_PLUGINS.md` §7.3（旧）：执行端仍是 `stage3_main.py` 的 fail-closed 占位，
  那几条命令"认得出、执行不了"，回"这条分支没有群管理能力。"；
- 接缝移植的说明：`execute_action` **真派发**了。

两者不能都成立，所以实测了一次（本文件就是那次实测的固化），结论是**真派发**。
但先前**没有任何测试从动作执行那条路走过真装配**：`tests/test_stage4_plugins.py` 里那几条
是直接造 `DialogueEngine` 再手塞 `engine.transport`，于是走的是
`stage3_main._group_action_caller` 的**退路**（没有 `plugin_registry` 时它在核心这一侧
现造一个调用函数）。**生产走的那条**（`runtime.build_engine` 把 `registry.action_caller`
接上）此前一次 payload 断言都没有——装配线断了，那些测试不会红，这个文件会
（实测：突变 M3 让装配点不接这条接缝，`test_stage4_plugins.py` 17 条全绿，只有本文件红）。

## 测什么

1. 逐条命令 → 假传输层**收到了什么 action**（逐字段断言 payload，不是断言回复字符串）；
2. 生产接缝（`registry.action_caller`）**就是**执行那一步的人，不是摆设；
3. "她是不是群主"由**核心**在写动作之前现查；
4. 动作闸门（`capabilities` 按用途的白名单）在接缝上真的拦得住；
5. 非超管 → 静默、零 action（连角色都不查）；
6. 执行端模块缺席 → fail-closed 占位、零 action（不崩、也绝不静默执行）。

## 关于正文里的 `@某人`

OneBot 送进来时 `[CQ:at,qq=…]` 段**不进正文**（`onebot_ws.extract_text` 把整段去掉），
只在 `mentioned_user_ids` 里。下面 `message()` 把字面的 `@某人` 按同一条规矩处理，
所以群名片 / 头衔 / 公告的**正文里不会出现 `@某人`**——别把探针里的形态当成真机形态。
"""
import asyncio
import re
import sys

from qq_roleplay_bot import dev_config, runtime
from qq_roleplay_bot.capabilities import GROUP_MANAGE_ACTIONS, GROUP_OWNER_ACTIONS
# 角色事实**在核心**（2026-10-05 用户："行，进核心"）：这两个常量从核心的
# `group_roles` 取——`plugins/roles/` 已经删掉，插件侧不再有那份重复实现。
from qq_roleplay_bot.group_roles import ROLE_MEMBER, ROLE_OWNER
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
SUPER = "900000001"
ADMIN = "1111111111"
MEMBER = "242003347"
SELF = "900000002"     # 她自己（假传输层的 get_login_info 回的就是这个号）
QUOTED = "888888"      # 被引用的那条消息

#: 群管理与群主动作里**真的会写**的那一批（= 两份 `capabilities` 白名单的并集）。
#: `group_owner` 那族写之前要现查一次角色、设完头衔还要回读一次，那些是**只读**，
#: 不算"产出的动作"，所以断言时按这张表把读滤掉（`_writes`）。
WRITE_ACTIONS = frozenset(GROUP_MANAGE_ACTIONS) | frozenset(GROUP_OWNER_ACTIONS)

_AT_SEGMENT = re.compile(r"@\S+")


class NeverCalled:
    """命令路径不该碰模型；碰了就直接炸（装配之后塞进 `engine.client`）。"""

    async def complete(self, request):  # pragma: no cover - 走不到
        raise AssertionError("命令不该调用模型")


class FakeTransport:
    """假传输层：记下每一次 action，回答登录信息与成员角色。**不连真 QQ。**"""

    def __init__(self, role: str = ROLE_OWNER) -> None:
        self.role = role
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if action == "get_login_info":
            return {"status": "ok", "retcode": 0, "data": {"user_id": SELF}}
        if action == "get_group_member_info":
            return {"status": "ok", "retcode": 0,
                    "data": {"role": self.role, "title": "龙王"}}
        return {"status": "ok", "retcode": 0, "data": {}}

    async def send(self, target, text, **kwargs):  # pragma: no cover - 命令回复由 handle 返回
        return None

    async def start(self):  # pragma: no cover
        return None


def message(text: str, *, mentions=(), user_id: str = SUPER, quoted: str = "") -> IncomingMessage:
    """造一条**真机形态**的消息：@ 段不进正文（见模块 docstring）。"""

    plain = _AT_SEGMENT.sub("", text, count=1) if mentions else text
    return IncomingMessage(
        message_id=f"m:{text}:{user_id}:{quoted}",
        session_id=f"group:{GROUP}",
        user_id=user_id,
        text=plain,
        target=MessageTarget(group_id=GROUP),
        mentioned_user_ids=tuple(mentions),
        reply_to_message_id=quoted,
    )


def assembly(role: str = ROLE_OWNER):
    """**真装配**：与生产同一条路（发现插件、接窄接缝、挂 `plugin_registry`）。"""

    transport = FakeTransport(role=role)
    engine = runtime.build_engine(transport)
    engine.client = NeverCalled()          # 命令落到对话路径 = 立刻炸，不碰网络
    engine.judge_client = None
    engine.client_factory = None
    engine.judge_client_factory = None
    # 名单来自 `.env` / `dev_config`，测试里显式补一份，免得依赖这台机器的配置
    engine.super_admin_user_ids.add(SUPER)
    engine.admin_user_ids.add(ADMIN)
    return engine, transport


def _writes(transport: FakeTransport) -> list[tuple[str, dict]]:
    """只看**写**动作：角色现查与头衔回读都是只读，不是"产生的动作"。"""

    return [(action, params) for action, params in transport.calls if action in WRITE_ACTIONS]


async def _dispatch(engine, transport, text: str, **kwargs):
    out = await engine.handle(message(text, **kwargs))
    return (out.text if out is not None else None), _writes(transport)


# --- 1. 装配线：生产接缝必须在，而且是它在执行 --------------------------------

def test_the_assembly_wires_the_action_caller_seam() -> None:
    """`build_engine` 必须把 `registry.action_caller` 接上。

    缺了它 `_group_action_caller` 会**静默退回**自己现造的那条调用函数——
    闸门与护栏都在核心这一侧，所以行为看起来一样。实测（突变 M3：让装配点不接
    这条接缝）：`test_stage4_plugins.py` 的 17 条断言**一条都不红**，
    红的只有本文件新加的这两条。也就是说**装配漏了，既有测试看不出来**。
    """

    engine, _ = assembly()
    caller = getattr(engine.plugin_registry, "action_caller", None)
    assert caller is not None, "装配没接动作调用接缝（只剩退路，测不出装配漏了）"
    assert callable(caller)
    # `(purpose) -> callable`：核心按用途造一条已过闸门的调用函数
    assert callable(caller("group_manage"))


def test_the_production_seam_is_what_executes_the_action() -> None:
    """把生产接缝换成一个"毒药"调用函数：命令必须走它、并且一个 action 都发不出去。

    这条**证明**执行那一步用的是 `registry.action_caller`，而不是
    `_group_action_caller` 里那条退路——只断言"装配上有这个方法"是不够的
    （有而不用，等于没接）。
    """

    engine, transport = assembly()
    seen: list[str] = []

    def poisoned(purpose: str):
        seen.append(purpose)

        async def call(action, params=None):  # pragma: no cover - 一定会被调到
            raise RuntimeError("毒药：走的就是这个调用函数")

        return call

    engine.plugin_registry.action_caller = poisoned
    reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                          mentions=(MEMBER,)))
    assert seen == ["group_manage"], "核心没有向生产接缝要调用函数"
    assert writes == [], "动作不该真的发出去（调用函数是毒药）"
    assert reply is not None and "失败" in reply, reply


# --- 2. 逐条命令：产出的 action 是什么 ---------------------------------------

#: `(命令正文, message() 的关键字, 期望发出的写动作)`。
#: **每一条实际生效的群管理 / 群主命令都在这里**，断言的是 payload 本身。
CASES: tuple[tuple[str, dict, tuple[tuple[str, dict], ...]], ...] = (
    # --- 群管理（`min_level = "super"`，只用发命令的那个群，不接受群号）---
    ("/super ban @某人 30", {"mentions": (MEMBER,)},
     (("set_group_ban", {"group_id": int(GROUP), "user_id": int(MEMBER), "duration": 1800}),)),
    ("/super ban @某人", {"mentions": (MEMBER,)},
     (("set_group_ban", {"group_id": int(GROUP), "user_id": int(MEMBER), "duration": 600}),)),
    # 分钟数越界夹到 30 天上限（夹紧，不是报错）。**用写号码那种写法**：
    # 解析器把"5~12 位纯数字"先当 QQ 号认（`parse_group_action`），所以
    # `/super ban @某人 99999` 里的 99999 会被当成目标、分钟数反而落回默认值 600
    # ——那个形态是解析器的已知怪癖（已如实报给用户，没动插件），不是这条要测的夹紧。
    ("/super ban 242003347 99999", {},
     (("set_group_ban", {"group_id": int(GROUP), "user_id": int(MEMBER),
                         "duration": 43200 * 60}),)),
    ("/super unban @某人", {"mentions": (MEMBER,)},
     (("set_group_ban", {"group_id": int(GROUP), "user_id": int(MEMBER), "duration": 0}),)),
    ("/super kick @某人", {"mentions": (MEMBER,)},
     (("set_group_kick", {"group_id": int(GROUP), "user_id": int(MEMBER)}),)),
    ("/super mute", {},
     (("set_group_whole_ban", {"group_id": int(GROUP), "enable": True}),)),
    ("/super unmute", {},
     (("set_group_whole_ban", {"group_id": int(GROUP), "enable": False}),)),
    ("/super recall", {"quoted": QUOTED},
     (("delete_msg", {"message_id": int(QUOTED)}),)),
    # --- 群主专属（她得真的是那个群的群主，核心现查）---
    ("/super qqadmin @某人", {"mentions": (MEMBER,)},
     (("set_group_admin", {"group_id": int(GROUP), "user_id": int(MEMBER),
                           "enable": True}),)),
    ("/super unqqadmin @某人", {"mentions": (MEMBER,)},
     (("set_group_admin", {"group_id": int(GROUP), "user_id": int(MEMBER),
                           "enable": False}),)),
    ("/super card @某人 新名片", {"mentions": (MEMBER,)},
     (("set_group_card", {"group_id": int(GROUP), "user_id": int(MEMBER),
                          "card": "新名片"}),)),
    ("/super groupname 新群名", {},
     (("set_group_name", {"group_id": int(GROUP), "group_name": "新群名"}),)),
    ("/super title @某人 龙王", {"mentions": (MEMBER,)},
     (("set_group_special_title", {"group_id": int(GROUP), "user_id": int(MEMBER),
                                   "special_title": "龙王"}),)),
    ("/super notice 第一行\n第二行", {},
     (("_send_group_notice", {"group_id": int(GROUP),
                              "content": "第一行\n第二行"}),)),
    # --- 公开那条：`/title 头衔` 只能改自己，谁都能发 ---
    ("/title 龙王", {"user_id": MEMBER},
     (("set_group_special_title", {"group_id": int(GROUP), "user_id": int(MEMBER),
                                   "special_title": "龙王"}),)),
)


def test_every_group_command_sends_exactly_this_action() -> None:
    """逐条钉住 payload——**不是**断言返回值字符串。

    返回值是我们自己拼的人话，改一个措辞就会红；payload 才是"真发出去了什么"。
    （回复一句话也顺带断言"有回复"，免得把"命令没被认领"当成通过。）
    """

    for text, kwargs, expected in CASES:
        engine, transport = assembly()
        reply, writes = asyncio.run(_dispatch(engine, transport, text, **kwargs))
        assert reply is not None, f"{text!r} 没有任何回复（命令没被认领？）"
        assert tuple(writes) == expected, f"{text!r}\n  实收 {writes}\n  期望 {expected}"
        # 每一次调用都该留在假传输层的记录里（证明不是我们读错了地方）
        assert all(action in WRITE_ACTIONS for action, _ in writes)


def test_the_group_owner_check_runs_in_the_core_before_the_write() -> None:
    """群主动作的前提"她在那个群是群主"由**核心**现查，查在写之前。

    插件那边看不到角色（它只声明意图），所以这条读必须出现在写**之前**；
    读到的不是群主就一个写都不发。
    """

    engine, transport = assembly(role=ROLE_MEMBER)
    reply, writes = asyncio.run(_dispatch(engine, transport, "/super qqadmin @某人",
                                          mentions=(MEMBER,)))
    assert writes == [], "她不是群主，一个写动作都不该发"
    assert reply is not None and "做不了" in reply, reply

    engine, transport = assembly(role=ROLE_OWNER)
    reply, writes = asyncio.run(_dispatch(engine, transport, "/super qqadmin @某人",
                                          mentions=(MEMBER,)))
    assert tuple(writes) == (("set_group_admin", {"group_id": int(GROUP),
                                                  "user_id": int(MEMBER),
                                                  "enable": True}),)
    # 写之前先查了"她自己（SELF）在这个群是什么角色"
    write_at = next(i for i, (action, _) in enumerate(transport.calls)
                    if action == "set_group_admin")
    reads_before = [(action, params) for action, params in transport.calls[:write_at]
                    if action == "get_group_member_info"]
    assert any(params.get("user_id") == int(SELF) for _, params in reads_before), (
        f"写之前没有现查她自己的角色：{transport.calls}")


# --- 3. 权限：动作闸门与档位判定 ---------------------------------------------

def test_the_capability_gate_on_the_action_seam_still_refuses() -> None:
    """生产接缝按**用途**过 `capabilities` 闸门：白名单外的 action 一律 `ActionDenied`。

    ## 已有覆盖到哪儿（实测，别把这条说成"唯一的守卫"）

    `tests/test_background_plugins.py::test_core_seams_gate_and_unwrap` 管的是
    `call_action` 那条接缝（读 / 入群审批那两个用途），它**不覆盖**
    `action_caller(purpose)`——而群管理与群主走的正是后者。实测的突变结论：

    - 把 `_SeamBinder.call_action` 里那次 `caps.check` 整段去掉 → 全绿套件里
      只红两条：那条既有的，加这一条（群管理那 12 条命令**照旧工作**，
      因为它们要的 action 本来就在白名单里，`test_stage4_plugins.py` 17 条一条不红）；
    - 所以"按用途点名"的那几个拒绝（`set_group_leave` / `transfer_group` 在
      `group_owner` 下、`set_group_admin` / `set_group_card` / `set_group_name`
      在 `group_manage` 下）此前**没有测试**。

    另外钉住"只读回读"那条口：`group_owner` 设完头衔要现查一次，
    `get_group_member_info` 属只读、不属 `GROUP_OWNER_ACTIONS`，它必须仍然放行。
    """

    from qq_roleplay_bot.plugins import ActionDenied

    engine, transport = assembly()
    allowed_writes: list[tuple[str, dict]] = []
    for purpose, allowed, denied in (
        ("group_manage", ("set_group_ban", {"group_id": 1, "user_id": 2, "duration": 60}),
         (("set_group_leave", {"group_id": 1}),        # 退群 / 解散：故意不在名单里
          ("set_group_admin", {"group_id": 1, "user_id": 2, "enable": True}),  # 提权
          ("set_group_card", {"group_id": 1, "user_id": 2, "card": "x"}),
          ("set_group_name", {"group_id": 1, "group_name": "x"}),
          ("get_credentials", {}))),                   # 禁用接口
        ("group_owner", ("set_group_admin", {"group_id": 1, "user_id": 2, "enable": True}),
         (("set_group_leave", {"group_id": 1}),
          ("transfer_group", {"group_id": 1, "user_id": 2}),
          ("set_group_kick", {"group_id": 1, "user_id": 2}),
          ("get_credentials", {}))),
    ):
        caller = engine.plugin_registry.action_caller(purpose)
        action, params = allowed
        asyncio.run(caller(action, params))          # 白名单内的：放行
        allowed_writes.append((action, params))
        for name, arguments in denied:
            try:
                asyncio.run(caller(name, arguments))
            except ActionDenied:
                continue
            raise AssertionError(f"{name} 不该在 purpose={purpose} 下放行")

    # 只读回读（设头衔后核对对面到底存了什么）必须仍然通得过
    owner_caller = engine.plugin_registry.action_caller("group_owner")
    asyncio.run(owner_caller("get_group_member_info", {"group_id": 1, "user_id": 2}))

    assert _writes(transport) == allowed_writes, _writes(transport)


# --- 4. 权限：非超管静默回落 -------------------------------------------------

def test_non_super_gets_silence_and_zero_actions() -> None:
    """群管理员 / 普通成员发 `/super ...`：不回复、不报错、**一个 action 都不发**。

    "零 action"是重点：静默必须是**什么都没发生**，而不是"回复被吞掉了、
    动作照样执行了"。连角色查询都不该发生（档位判定在 `run()` 之前）。
    """

    for user in (ADMIN, MEMBER):
        engine, transport = assembly()
        # 按群授权的管理员也试一遍（`/admin` 那一套碰不到 `/super`）
        engine.group_admin_ids.setdefault(GROUP, set()).add(ADMIN)
        reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                              mentions=(MEMBER,), user_id=user))
        assert reply is None, f"{user} 不该收到任何回复：{reply!r}"
        assert writes == [], f"{user} 不该发出任何动作：{writes}"
        assert transport.calls == [], f"{user} 触发了传输层调用：{transport.calls}"


# --- 5. 护栏与总闸（都在核心这一侧）------------------------------------------

def test_guards_hold_through_the_real_assembly() -> None:
    """护栏是"护栏"，不是"提示"：被挡下时**一个 action 都不发**。"""

    # a) 移出群聊必须 @ 到人：手滑写个号码就踢人，是这条路上最典型的失误
    engine, transport = assembly()
    reply, writes = asyncio.run(_dispatch(engine, transport, "/super kick 242003347"))
    assert writes == [] and reply is not None and "@" in reply, (reply, writes)

    # b) 不动超管 / 管理员
    engine, transport = assembly()
    reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                          mentions=(SUPER,)))
    assert writes == [] and reply is not None and "不动他" in reply, (reply, writes)

    # c) 总闸关掉（`QQBOT_GROUP_MANAGE=0`）：出声拒绝，但不发动作
    engine, transport = assembly()
    old = dev_config.GROUP_MANAGE_ENABLED
    dev_config.GROUP_MANAGE_ENABLED = False
    try:
        reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                              mentions=(MEMBER,)))
    finally:
        dev_config.GROUP_MANAGE_ENABLED = old
    assert writes == [] and reply is not None and "没开" in reply, (reply, writes)

    # d) 群主那一族的总闸同理
    engine, transport = assembly()
    old = dev_config.GROUP_OWNER_ENABLED
    dev_config.GROUP_OWNER_ENABLED = False
    try:
        reply, writes = asyncio.run(_dispatch(engine, transport, "/super notice 正文"))
    finally:
        dev_config.GROUP_OWNER_ENABLED = old
    assert writes == [] and reply is not None and "没开" in reply, (reply, writes)


# --- 6. fail-closed 占位：只有"装配错了"才走得到 ------------------------------

class _Hidden:
    """临时让某个模块"看起来不存在"（同 `tests/test_optional_capabilities.py`）。

    `sys.modules[name] = None` 之后，`from … import …` 抛的是
    **`ModuleNotFoundError`**（"import of X halted; None in sys.modules"），
    正好走 `execute_action` 里那个 `except ModuleNotFoundError`。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._saved: object = None

    def __enter__(self):
        self._saved = sys.modules.get(self.name, "absent")
        sys.modules[self.name] = None
        return self

    def __exit__(self, *exc):
        if self._saved == "absent":
            sys.modules.pop(self.name, None)
        else:
            sys.modules[self.name] = self._saved  # type: ignore[assignment]
        return False


def test_the_fail_closed_placeholder_is_only_for_a_broken_assembly() -> None:
    """`stage3_main.execute_action` 里那句"这条部署没有群管理能力。"的**可达性**。

    实测（本机，2026-10-05）的三种装配：

    | 装配 | `/super ban` 的结果 | 走到那句占位了吗 |
    | --- | --- | --- |
    | 正常（`plugins/` 在） | 真发 `set_group_ban` | 没有（本文件第 2 节就是它） |
    | **整个 `plugins/` 不在** | 命令**根本没注册** → 超管收到"没认出来" | 没有（`resolve()` 就返回 None） |
    | 命令注册了、但**执行端模块不在** | 那句占位 + 零 action | **走到了** |

    所以它是 fail-closed 的**兜底**：真收到这两种 `ActionRequest` 而执行端不在时，
    不执行、不崩，回一句人话。第三行只有"装配错了"（插件目录被拆散）才会出现，
    但不能因此让它没有测试——它是"装配漏了也不许静默执行"这条底线的落点。
    """

    # 对照：正常装配下这条命令是真发动作的
    engine, transport = assembly()
    reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                          mentions=(MEMBER,)))
    assert writes == [("set_group_ban", {"group_id": int(GROUP), "user_id": int(MEMBER),
                                         "duration": 600})], (reply, writes)

    # 执行端模块不在：命令还认得（插件注册表里还有），但一个 action 都发不出去
    with _Hidden("qq_roleplay_bot.plugins.group_admin.group_admin"):
        engine, transport = assembly()
        assert engine.commands.resolve(message("/super ban @某人 10",
                                               mentions=(MEMBER,))) is not None
        reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                              mentions=(MEMBER,)))
    assert writes == [], writes
    assert reply is not None and "没有群管理能力" in reply, reply

    # 还原之后还得能真发（证明我们没把模块搞坏）
    engine, transport = assembly()
    _, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                      mentions=(MEMBER,)))
    assert writes == [("set_group_ban", {"group_id": int(GROUP), "user_id": int(MEMBER),
                                         "duration": 600})], writes

    # 整个 `plugins/` 不在时：命令压根不注册，占位**够不着**
    with _Hidden("qq_roleplay_bot.plugins.group_admin.plugin"):
        engine, transport = assembly()
        reply, writes = asyncio.run(_dispatch(engine, transport, "/super ban @某人 10",
                                              mentions=(MEMBER,)))
    assert writes == []
    assert reply is not None and "没有群管理能力" not in reply, reply
