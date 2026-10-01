"""群主专属能力：她自己角色的现查、群主动作的护栏、入群申请自动审批。

由来（2026-09-30 用户）："群主的接口应该比管理员更多来着，能适配自动审批，添加管理这些吗"，
随后点明"yunru 现在是测试群的群主了"。实测（`get_group_member_info`）：
QQ 900000002「YunRu」在测试群 717151356 里 role=owner，别的群是 member。

所以这里钉三件事：

1. **角色现查、查不到不放行**（fail-closed）——她可以被降级，群主也能转让；
2. **群主动作按 QQ 自己的规矩分级**：改群名/设管理员/发头衔只有群主能做，
   改群名片/发群公告群主和管理员都能做；
3. **自动审批的判据优先级写死**：黑名单 → 白名单 → 正则 → 兜底（默认不表态）。
"""
import asyncio

from qq_roleplay_bot.builtin_group_commands import parse_owner_action
from qq_roleplay_bot.capabilities import CapabilityRegistry
from qq_roleplay_bot.command_plugins import level_of
from qq_roleplay_bot.group_owner import SPECS, execute as owner_execute, clamp_text
from qq_roleplay_bot.join_approval import (
    APPROVE,
    HOLD,
    REJECT,
    JoinApprovalPolicy,
    JoinApprovalPoller,
    parse_pending,
)
from qq_roleplay_bot.qq_roles import ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, ROLE_UNKNOWN, SelfRoleCache
from qq_roleplay_bot.stage3_main import SuperAction, parse_super_command
from qq_roleplay_bot.transport import IncomingMessage, MessageTarget

GROUP = "717151356"
ME = "900000001"
SELF_ID = "900000002"


def _message(text: str) -> IncomingMessage:
    """造一条群消息（只用于"插件认不认这条命令"这类判断）。"""

    return IncomingMessage(message_id=f"m:{text}", session_id=f"group:{GROUP}", user_id=ME,
                           text=text, target=MessageTarget(group_id=GROUP))


class FakeClient:
    """假的 SnowLuma：只认 get_login_info 与 get_group_member_info。"""

    def __init__(self, roles=None, login=SELF_ID, fail=False):
        self.roles = dict(roles or {})
        self.login = login
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def call(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if self.fail:
            raise RuntimeError("通道炸了")
        if action == "get_login_info":
            return {"user_id": self.login, "nickname": "YunRu"}
        if action == "get_group_member_info":
            role = self.roles.get(str((params or {}).get("group_id")))
            return {"role": role} if role else {}
        return {}


class FakeWsClient(FakeClient):
    """**WS 传输层的形状**：只有 `call_api`（没有 `call`），返回的是整个回执。

    这个类存在的唯一理由是钉住 2026-09-30 真机那次故障：角色缓存按 HTTP 的形状写
    （`client.call` + `payload["role"]`），生产传进来的却是 WS 传输层，于是
    `getattr(client, "call")` 是 None → 静默返回 `unknown` → 用户看到
    "我在这个群里是查不到，设置群管理员需要群主权限，做不了"，日志里一条都没有。
    所以这里**故意不提供 `call`**——谁再写回 `client.call`，测试会直接失败。
    """

    async def call_api(self, action, params=None):
        data = await super().call(action, params)
        return {"status": "ok", "retcode": 0, "echo": "e", "data": data}


# --- 角色 -------------------------------------------------------------------

def test_role_is_read_once_then_cached() -> None:
    client = FakeClient({GROUP: "owner"})
    roles = SelfRoleCache(client, ttl=900)
    assert asyncio.run(roles.role(GROUP)) == ROLE_OWNER
    assert asyncio.run(roles.role(GROUP)) == ROLE_OWNER
    assert [action for action, _ in client.calls] == ["get_login_info", "get_group_member_info"]
    assert asyncio.run(roles.is_owner(GROUP)) is True
    # 刷新会再查一次
    assert asyncio.run(roles.role(GROUP, refresh=True)) == ROLE_OWNER
    assert len(client.calls) == 3


def test_role_query_bypasses_the_member_cache() -> None:
    """角色查询必须带 `no_cache=True`。

    真机踩过：NapCat 的 `get_group_member_info` 默认走缓存，诊断时读回来的是变更前的
    role/title，于是"头衔其实写进去了"被误判成"没生效"。角色判断更不能吃缓存——
    她刚被降级成普通成员时，缓存里的"群主"会让命令继续放行。
    """

    client = FakeClient({GROUP: "owner"})
    asyncio.run(SelfRoleCache(client).role(GROUP))
    params = next(params for action, params in client.calls if action == "get_group_member_info")
    assert params["no_cache"] is True
    assert params["group_id"] == int(GROUP) and params["user_id"] == int(SELF_ID)


def test_role_works_over_either_channel_shape() -> None:
    """HTTP 客户端（`call` + data）与 WS 传输层（`call_api` + 整个回执）都行。

    真机故障回归：生产传的是 WS 传输层，而缓存只认 `call`、也只认 data 形状的返回，
    于是她的角色永远是 `unknown`（"我在这个群里是查不到"）。
    """

    http_like = SelfRoleCache(FakeClient({GROUP: "owner"}))
    assert asyncio.run(http_like.role(GROUP)) == ROLE_OWNER
    ws_like = SelfRoleCache(FakeWsClient({GROUP: "owner"}))
    assert asyncio.run(ws_like.role(GROUP)) == ROLE_OWNER
    assert asyncio.run(ws_like.self_id()) == SELF_ID
    # 通道两种方法都没有时：明确抛错，而不是静默当"查不到"
    from qq_roleplay_bot.onebot_client import call_channel

    try:
        asyncio.run(call_channel(object(), "get_login_info", {}))
    except RuntimeError as exc:
        assert "不支持" in str(exc)
    else:  # pragma: no cover - 不该走到
        raise AssertionError("既没有 call 也没有 call_api 时应当抛错")


def test_owner_actions_work_over_the_ws_channel() -> None:
    """同一条路再走一遍完整动作：WS 形状的通道下，设管理员要真的调到 action。"""

    transport = FakeTransport()
    result = _run("qqadmin", transport=transport, roles=SelfRoleCache(FakeWsClient({GROUP: "owner"})))
    assert "已设置" in result, result
    assert transport.calls == [("set_group_admin",
                                {"group_id": int(GROUP), "user_id": 1001, "enable": True})]


def test_role_is_unknown_when_it_cannot_be_read() -> None:
    """查不到、通道不支持、对面没给 role——一律 unknown，绝不猜"大概是群主"。"""

    assert asyncio.run(SelfRoleCache(FakeClient(fail=True)).role(GROUP)) == ROLE_UNKNOWN
    assert asyncio.run(SelfRoleCache(FakeClient({GROUP: "member"})).role(GROUP)) == ROLE_MEMBER
    assert asyncio.run(SelfRoleCache(FakeClient()).role(GROUP)) == ROLE_UNKNOWN
    assert asyncio.run(SelfRoleCache(None).role(GROUP)) == ROLE_UNKNOWN
    assert asyncio.run(SelfRoleCache(FakeClient()).role("")) == ROLE_UNKNOWN


def test_role_aliases_are_normalized() -> None:
    from qq_roleplay_bot.qq_roles import normalize_role

    assert normalize_role("Owner") == ROLE_OWNER
    assert normalize_role("群主") == ROLE_OWNER
    assert normalize_role("管理员") == ROLE_ADMIN
    assert normalize_role("member") == ROLE_MEMBER
    assert normalize_role("") == ROLE_UNKNOWN
    assert normalize_role(None) == ROLE_UNKNOWN


# --- 解析 -------------------------------------------------------------------

def test_owner_commands_are_parsed() -> None:
    # 命令由插件认领（用户 2026-09-30："stage4 的内容都用插件实现"），
    # 核心的 `parse_super_command` 对它返回 None。
    from qq_roleplay_bot.builtin_commands import build_command_registry

    registry = build_command_registry()
    plugin = registry.resolve(_message("/super qqadmin"))
    assert plugin is not None and getattr(plugin, "name", "") == "group_owner"
    assert level_of(plugin) == "super"
    assert parse_super_command("/super qqadmin") is None
    # 真消息里 @ 是**段**、不在正文中，所以这里就是空正文（目标另取 mentioned_user_ids）
    assert parse_owner_action("/super qqadmin") == ("qqadmin", "")
    assert parse_owner_action("/super notice 今晚八点开黑") == ("notice", "今晚八点开黑")
    assert parse_owner_action("/super groupname 云茹测试群") == ("groupname", "云茹测试群")
    # 手打 @ 字样时它只是正文，由调用方剥掉
    assert parse_owner_action("/super qqadmin @某人") == ("qqadmin", "@某人")
    assert parse_owner_action("/super 头衔 龙王") == ("title", "龙王")
    assert parse_owner_action("/super 随便写点什么") is None
    # 不能把别的命令吃掉（群管理归另一个插件）
    assert parse_super_command("/super status") is SuperAction.STATUS
    group_plugin = registry.resolve(_message("/super kick @某人"))
    assert getattr(group_plugin, "name", "") == "group_manage"


# --- 群主动作的护栏 ----------------------------------------------------------

class FakeTransport:
    def __init__(self, response=None, fail=False, stored_title=None):
        self.response = response if response is not None else {"status": "ok", "retcode": 0}
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []
        # 回读头衔时装作对面存下来的值（`None` = 对面不回这条 → 退回原话）。
        self.stored_title = stored_title

    async def call_api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if self.fail:
            raise RuntimeError("对面炸了")
        if action == "get_group_member_info":
            if self.stored_title is None:
                return {}
            return {"status": "ok", "retcode": 0,
                    "data": {"title": self.stored_title}}
        return self.response


def _roles(role: str) -> SelfRoleCache:
    return SelfRoleCache(FakeClient({GROUP: role}))


def _run(kind: str, **kwargs) -> str:
    params = dict(
        transport=FakeTransport(), roles=_roles(ROLE_OWNER),
        registry=CapabilityRegistry(), group_id=GROUP, actor_id=ME,
        target_id="1001", text="", mentioned=True, enabled=True,
    )
    params.update(kwargs)
    return asyncio.run(owner_execute(kind, **params))


def test_owner_only_actions_need_owner_role() -> None:
    """改群名/设管理员/发头衔只有群主能做；群名片与公告管理员也能做。"""

    owner_only = ("qqadmin", "unqqadmin", "groupname", "title")
    for kind in owner_only:
        text = "新群名" if kind == "groupname" else ("龙王" if kind == "title" else "")
        refused = _run(kind, roles=_roles(ROLE_ADMIN), text=text)
        assert "做不了" in refused, (kind, refused)
    # 管理员能做的那两条
    assert "已把" in _run("card", roles=_roles(ROLE_ADMIN), text="已禁言")
    assert "已发出" in _run("notice", roles=_roles(ROLE_ADMIN), text="今晚维护")
    # 普通成员什么都不行
    assert "做不了" in _run("notice", roles=_roles(ROLE_MEMBER), text="今晚维护")
    # 角色查不到也不放行（fail-closed）
    assert "做不了" in _run("notice", roles=SelfRoleCache(None), text="今晚维护")


def test_owner_actions_build_the_right_params() -> None:
    transport = FakeTransport()
    _run("qqadmin", transport=transport)
    assert transport.calls == [("set_group_admin", {"group_id": int(GROUP), "user_id": 1001, "enable": True})]
    transport = FakeTransport()
    _run("unqqadmin", transport=transport)
    assert transport.calls[0][1]["enable"] is False
    transport = FakeTransport()
    _run("groupname", transport=transport, text=" 云茹 测试群 ")
    assert transport.calls == [("set_group_name", {"group_id": int(GROUP), "group_name": "云茹 测试群"})]
    transport = FakeTransport()
    _run("title", transport=transport, text="龙王")
    # 第一条是设置本身；之后会**回读一次**核对对面到底存了什么（QQ 会静默截断）
    assert transport.calls[0] == ("set_group_special_title",
                                  {"group_id": int(GROUP), "user_id": 1001,
                                   "special_title": "龙王"})
    transport = FakeTransport()
    _run("notice", transport=transport, text="第一行\n第二行")
    assert transport.calls == [("_send_group_notice",
                               {"group_id": int(GROUP), "content": "第一行\n第二行"})]
    transport = FakeTransport()
    _run("card", transport=transport, text="")
    assert transport.calls == [("set_group_card",
                               {"group_id": int(GROUP), "user_id": 1001, "card": ""})]


def test_title_length_is_left_to_qq() -> None:
    """头衔**不再按 QQ 的 6 字去卡**（2026-10-01 用户："群头衔不要限制6个字符的长度，
    bot 设置的偶尔可以突破这个长度"）。

    实测（2026-10-01，真机 8 字）：对面**收下了但静默截断到 6 字**。所以判据是
    "照发，回来把真实结果说清楚"，而不是我们自己编一句"太长"。
    """

    seven = "这是七个字的头衔"
    transport = FakeTransport()
    with_length = _run("title", transport=transport, text=seven)
    assert "已给" in with_length, with_length
    assert transport.calls[0] == ("set_group_special_title",
                                  {"group_id": int(GROUP), "user_id": 1001,
                                   "special_title": seven})

    # 对面截断了：回报必须说**真实**的那个值，不能把我们没做到的事说成做到了
    truncated = _run("title", transport=FakeTransport(stored_title="这是七个字的"), text=seven)
    assert "这是七个字的" in truncated and "只留了 6 字" in truncated, truncated
    assert "设置群头衔「这是七个字的头衔」" not in truncated

    # 对面拒绝时要说清楚是"被对面拒绝"
    refused = _run("title", transport=FakeTransport(
        response={"status": "failed", "retcode": 100}), text="头" * 31)
    assert "被对面拒绝" in refused, refused
    assert "太长" not in refused

    # 仍然挡住明显是误操作的长度（防止把一整段话塞进头衔）
    assert "太长" in _run("title", text="头" * 33)


def test_owner_actions_stop_at_the_guardrails() -> None:
    # 没 @ 到人 / 没给正文
    assert "用法" in _run("qqadmin", mentioned=False, target_id="")
    assert "请用 @ 指定人" in _run("qqadmin", mentioned=False, target_id="1001")
    assert "用法" in _run("groupname", text="")
    # **改自己不用 @**（2026-09-30 用户："头衔……自己给自己申请"）
    assert "已给" in _run("title", mentioned=False, target_id=ME, text="龙王")
    # 长度只挡"明显是误操作"的量级
    assert "太长" in _run("groupname", text="群" * 31)
    assert "太长" in _run("title", text="头" * 33)
    # 私聊里没有可管的群
    assert "群里" in _run("notice", group_id=None, text="正文")
    # 开关关掉
    assert "没开" in _run("notice", enabled=False, text="正文")
    # 对面拒绝 / 通道炸了都给一句人话
    assert "被对面拒绝" in _run(
        "notice", transport=FakeTransport({"status": "failed", "retcode": 100}), text="正文")
    assert "失败" in _run("notice", transport=FakeTransport(fail=True), text="正文")


def test_owner_actions_do_not_block_the_super_admin() -> None:
    """**不许**把超管/管理员排除在外——这一族动作不会伤到被 @ 的人。

    由来（2026-09-30 用户实测反馈"添加 qq 群管理这个没做对啊，刚刚试了不行"）：
    第一版从 `group_admin.py` 抄了"不能动超管"那条，用户第一次试就是 @ 自己，
    于是收到 11 个字的"这个人是超管，不动他。"——真机日志里
    `super command: action=owner_action` 后面跟着 `reply_length=11`。
    设管理员/给头衔/改名片都是"给人东西"，没有保护的必要。
    """

    transport = FakeTransport()
    assert "已设置" in _run("qqadmin", transport=transport, target_id=ME)
    assert transport.calls[0][1]["user_id"] == int(ME)
    # 取消管理员同理（QQ 权限，超管自己能加回来）
    transport = FakeTransport()
    assert "已取消" in _run("unqqadmin", transport=transport, target_id=ME)
    # 头衔/名片也不拦
    assert "已给" in _run("title", transport=FakeTransport(), target_id=ME, text="龙王")
    assert "已把" in _run("card", transport=FakeTransport(), target_id=ME, text="新名片")


def test_leave_and_dissolve_are_not_in_the_table() -> None:
    """退群/解散永远不在名单里（`set_group_leave` 带参数就是解散群）。"""

    actions = {spec.action for spec in SPECS.values()}
    assert "set_group_leave" not in actions
    assert "set_group_kick" not in actions  # 踢人在 group_admin 那套里，不在这儿
    assert actions == {"set_group_admin", "set_group_card", "set_group_name",
                       "set_group_special_title", "_send_group_notice"}


def test_owner_actions_are_gated_by_a_named_purpose() -> None:
    """走的是新用途 `group_owner`；`group_manage` 那份短名单没被放宽。"""

    registry = CapabilityRegistry()
    for action in ("set_group_admin", "set_group_card", "set_group_name",
                   "set_group_special_title", "_send_group_notice"):
        registry.check(action, purpose="group_owner")
        assert not registry.is_allowed(action, purpose="group_manage"), action
    # 提权类在 `admin` 用途下仍然被排除（`set_group_card`/公告不是敏感写操作，照旧可写）
    assert not registry.is_allowed("set_group_admin", purpose="admin")
    # 入群审批是另一个用途
    registry.check("set_group_add_request", purpose="join_approval")
    assert not registry.is_allowed("set_group_add_request", purpose="group_owner")
    assert not registry.is_allowed("set_group_add_request", purpose="group_manage")
    # 自毁类仍然一条都不批
    for purpose in ("group_owner", "join_approval", "group_manage", "admin"):
        assert not registry.is_allowed("set_group_leave", purpose=purpose)


def test_text_shape_helper() -> None:
    assert clamp_text(SPECS["groupname"], "  a   b ") == "a b"
    assert clamp_text(SPECS["notice"], "  第一行\n第二行  ") == "第一行\n第二行"


# --- 自动审批：解析与判据 ----------------------------------------------------

def _pending(**kwargs) -> dict:
    base = {"flag": "flag-1", "group_id": GROUP, "requester_uin": 1001,
            "requester_nick": "某人", "message": "我是群友", "checked": False}
    base.update(kwargs)
    return base


def test_pending_requests_are_parsed() -> None:
    items = parse_pending([_pending(), _pending(flag="", group_id=GROUP),
                           _pending(flag="f2", checked=True)])
    assert len(items) == 1
    request = items[0]
    assert (request.flag, request.group_id, request.user_id) == ("flag-1", GROUP, "1001")
    assert request.comment == "我是群友"
    assert request.is_join is True
    assert parse_pending("不是列表") == ()
    assert parse_pending([{"flag": "f"}]) == ()


def test_pending_requests_are_unwrapped_from_a_ws_receipt() -> None:
    """WS 通道给的是整个回执，列表在 `data` 里——不拆开就永远 0 条待处理。"""

    from qq_roleplay_bot.onebot_client import unwrap_result

    receipt = {"status": "ok", "retcode": 0, "data": [_pending()]}
    assert len(parse_pending(unwrap_result(receipt))) == 1
    # HTTP 通道直接给列表：unwrap 不动它
    assert unwrap_result([_pending()]) == [_pending()]
    assert unwrap_result(None) is None
    assert unwrap_result({"role": "owner"}) == {"role": "owner"}


def test_policy_priority_is_blacklist_then_whitelist_then_pattern() -> None:
    policy = JoinApprovalPolicy(whitelist={"1001"}, blacklist={"1002"},
                                pattern="想进来", reject_reason="不行")
    assert policy.decide(_req("1002", "路人", "想进来")).action == REJECT
    assert policy.decide(_req("1002", "路人", "想进来")).rule == "blacklist"
    # 同时出现在两张名单里：黑名单赢
    both = JoinApprovalPolicy(whitelist={"1003"}, blacklist={"1003"})
    assert both.decide(_req("1003", "", "")).action == REJECT
    assert policy.decide(_req("1001", "谁", "随便")).rule == "whitelist"
    assert policy.decide(_req("1009", "谁", "我想进来看看")).rule == "pattern"
    # 都没命中 → 兜底 hold（不表态）
    held = policy.decide(_req("1009", "谁", "路过"))
    assert (held.action, held.rule) == (HOLD, "default")


def _req(user_id: str, nick: str, comment: str):
    from qq_roleplay_bot.join_approval import PendingJoin

    return PendingJoin(flag="f", group_id=GROUP, user_id=user_id, nickname=nick, comment=comment)


def test_pattern_matches_comment_and_nickname() -> None:
    comment_only = JoinApprovalPolicy(pattern=r"^\s*我是")
    assert comment_only.decide(_req("1001", "甲", "我是老王")).action == APPROVE
    nick_only = JoinApprovalPolicy(pattern="老王")
    assert nick_only.decide(_req("1001", "隔壁老王", "无")).action == APPROVE


def test_broken_pattern_never_approves() -> None:
    """配置写错不能变成"全批"：按没配处理，结果还是不表态。"""

    policy = JoinApprovalPolicy(pattern="([")
    assert policy.pattern is None
    assert policy.decide(_req("1001", "甲", "随便")).action == HOLD
    assert "未配置" in JoinApprovalPolicy().explain
    assert "正则" in policy.explain


def test_default_can_be_switched_to_approve_everything() -> None:
    policy = JoinApprovalPolicy(pattern=r"(?s).*")
    assert policy.decide(_req("1001", "甲", "")).action == APPROVE
    catch_all = JoinApprovalPolicy(default=APPROVE)
    assert catch_all.decide(_req("1001", "甲", "")).action == APPROVE


# --- 自动审批：跑一轮 --------------------------------------------------------

class FakeApprovalTransport(FakeTransport):
    """假传输层：**它现在只被核心的接缝包起来用**（插件拿不到它）。

    2026-09-30 用户："入群审批也是插件"——所以插件那一侧拿到的是
    `call_action` / `notify` 两个函数（见 `_poller`），不再是 transport。
    这个类留着是为了模仿核心接缝的行为：过闸门 → 调 action → 把回执拆成 data。
    """

    def __init__(self, pending, response=None, fail_write=False, ws_receipt=False):
        super().__init__(response)
        self.pending = list(pending)
        self.fail_write = fail_write
        # True = 学 WS 传输层：把结果裹进整个回执里（真机就是这条形状）
        self.ws_receipt = ws_receipt
        self.sent: list[tuple[str, str]] = []

    async def call_api(self, action, params=None):
        self.calls.append((action, dict(params or {})))
        if action == "get_group_system_msg":
            data = list(self.pending)
            return {"status": "ok", "retcode": 0, "data": data} if self.ws_receipt else data
        if action == "set_group_add_request":
            if self.fail_write:
                raise RuntimeError("写调用炸了")
            approved = bool((params or {}).get("approve"))
            self.pending = [item for item in self.pending
                            if item.get("flag") != (params or {}).get("flag")]
            return {"status": "ok", "retcode": 0, "approved": approved}
        return self.response

    async def send(self, target, text, **kwargs):
        self.sent.append((str(target.user_id or target.group_id), text))


def _seams(transport):
    """模仿核心注入的那两个窄接缝（`runtime._plugin_action_seams`）。"""

    async def call_action(action, params=None):
        from qq_roleplay_bot.onebot_client import unwrap_result

        return unwrap_result(await transport.call_api(action, params or {}))

    async def notify(text):
        await transport.send(MessageTarget(user_id=ME), text)

    return call_action, notify


def _poller(transport=None, roles=None, call_action=None, notify=None, **kwargs):
    policy = kwargs.pop("policy", JoinApprovalPolicy(whitelist={"1001"},
                                                     blacklist={"1002"},
                                                     reject_reason="本群暂时不加人。"))
    if call_action is None or notify is None:
        default_call, default_notify = _seams(transport)
        call_action = call_action or default_call
        notify = notify or default_notify
    return JoinApprovalPoller(call_action=call_action, notify=notify, policy=policy,
                              roles=roles or _roles(ROLE_OWNER), **kwargs)


def test_tick_approves_rejects_and_holds() -> None:
    transport = FakeApprovalTransport([
        _pending(flag="f-ok", requester_uin=1001),
        _pending(flag="f-no", requester_uin=1002),
        _pending(flag="f-hold", requester_uin=1009),
    ])
    poller = _poller(transport)
    result = asyncio.run(poller.tick())
    assert result == {"approved": 1, "rejected": 1, "held": 1, "invites": 0}
    writes = {params["flag"]: params for action, params in transport.calls
              if action == "set_group_add_request"}
    assert writes["f-ok"]["approve"] is True
    assert writes["f-no"]["approve"] is False
    assert writes["f-no"]["reason"] == "本群暂时不加人。"
    assert "f-hold" not in writes, "兜底是不表态：不该发写调用"
    # 没批的那条要通知超管
    assert any("f-hold" in text or "申请加入" in text for _, text in transport.sent)
    assert poller.stats["approved"] == 1 and poller.stats["held"] == 1


def test_tick_only_handles_groups_where_she_is_owner_or_admin() -> None:
    transport = FakeApprovalTransport([_pending(flag="f1", requester_uin=1001)])
    poller = _poller(transport, roles=SelfRoleCache(FakeClient({GROUP: "member"})))
    assert asyncio.run(poller.tick()) == {"approved": 0, "rejected": 0, "held": 0, "invites": 0}
    assert all(action != "set_group_add_request" for action, _ in transport.calls)


def test_tick_works_when_the_channel_returns_a_full_receipt() -> None:
    """WS 通道的整条回执也要能解析——否则审批会静默地什么都看不到。"""

    transport = FakeApprovalTransport([_pending(flag="f1", requester_uin=1001)], ws_receipt=True)
    poller = _poller(transport)
    assert asyncio.run(poller.tick())["approved"] == 1
    assert any(action == "set_group_add_request" for action, _ in transport.calls)


def test_same_request_is_never_handled_twice() -> None:
    transport = FakeApprovalTransport([_pending(flag="f1", requester_uin=1001)])
    poller = _poller(transport)
    asyncio.run(poller.tick())
    asyncio.run(poller.tick())
    writes = [1 for action, _ in transport.calls if action == "set_group_add_request"]
    assert writes == [1], "同一条申请只能处理一次"


def test_invites_are_never_auto_accepted() -> None:
    """有人邀请她去新群：只记一笔并通知，不自动进陌生群。"""

    transport = FakeApprovalTransport([
        _pending(flag="f-inv", sub_type="invite", invitor_uin=1001, group_id="999888777"),
    ])
    poller = _poller(transport)
    result = asyncio.run(poller.tick())
    assert result["invites"] == 1
    assert all(action != "set_group_add_request" for action, _ in transport.calls)
    assert any("邀请" in text for _, text in transport.sent)


def test_tick_survives_failures() -> None:
    # 写调用失败：这一条不算处理过，也不抛出去
    failing = _poller(FakeApprovalTransport([_pending(flag="f1", requester_uin=1001)], fail_write=True))
    assert asyncio.run(failing.tick()) == {"approved": 0, "rejected": 0, "held": 0, "invites": 0}
    assert failing.stats["failed"] == 1
    # 读调用失败
    broken = _poller(FakeTransport(fail=True))
    assert asyncio.run(broken.tick()) == {}
    assert broken.stats["failed"] == 1
    # 关掉之后一轮都不跑
    assert asyncio.run(_poller(FakeApprovalTransport([]), enabled=False).tick()) == {}
    # 没有待处理申请时是一张零表，不是 None
    assert asyncio.run(_poller(FakeApprovalTransport([])).tick()) == {
        "approved": 0, "rejected": 0, "held": 0, "invites": 0}


def test_poller_respects_per_tick_cap() -> None:
    transport = FakeApprovalTransport([
        _pending(flag=f"f{i}", requester_uin=1001, message=f"第{i}个") for i in range(9)
    ])
    poller = _poller(transport, max_per_tick=3)
    result = asyncio.run(poller.tick())
    assert result["approved"] == 3
    assert poller.max_per_tick == 3


def test_snapshot_is_safe_to_log() -> None:
    poller = _poller(FakeApprovalTransport([]))
    snapshot = poller.snapshot()
    assert "黑名单" in str(snapshot["policy"]) and "白名单" in str(snapshot["policy"])
    assert snapshot["approved"] == 0
    assert "未配置" in str(_poller(FakeApprovalTransport([]),
                                  policy=JoinApprovalPolicy()).snapshot()["policy"])
