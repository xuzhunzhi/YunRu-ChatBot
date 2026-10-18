"""面板那一侧的「模型与凭据」接缝（2026-10-18 第二轮口径）。

## 这一份钉的是什么

用户当天两句话叠在一起（原话照抄）：

1. 「**这里，我要的是添加而非替换**」——多套并存，同名绝不覆盖；
2. 「我倾向于**一个key一行**而不是一个供应商一行外加好多key。**agent那边先选供应商再选
   对应的key最后选供应商提供的模型**」——所以接缝是按**新形状**开的：

| 接缝 | 形状 | 干什么 |
| --- | --- | --- |
| `ui.model_providers()` | 无参 → `dict` | 只读：密钥清单（平铺）+ 各路绑定 + 供应商下拉框 |
| `ui.model_provider_add(name, base_url, keys, client_type="", models=None)` | → `dict` | **添加一套**（同名/同地址都拒绝） |
| `ui.model_provider_key(name, key_id="", label=None, value=None)` | → `dict` | 加一把 / 改一把 key |
| `ui.model_provider_key_remove(name, key_id)` | → `dict` | 删一把，**在用就拒**（点名哪几路） |
| `ui.model_provider_bind(task, provider, key=None, model=None)` | → `dict` | 把一路绑成 `provider → key → model` |
| `ui.model_provider_remove(name)` | → `dict` | 删一套，**还有用途指着就拒** |

再加上既有硬边界：**面板拿不到文件**（`data/model_providers.json` 只有
`model_providers_store` 能碰），**密钥一个字节都不出这一层**（只给 id / label / 掩码 /
长度），写一律走 store 的写层（审计也在那一层落）。

## 六条钉子（每条都对应一次"顺手就会破"的地方）

1. `test_the_read_view_never_carries_a_plaintext_key`：整份 JSON 里**不含**明文，
   只给 id / label / masked / length；
2. `test_adding_a_name_that_already_exists_is_refused` / `..._the_same_address_...`：
   同名、同地址都拒绝（"添加而非替换" + U3 的"同账号两名会让 402 兜底静默失效"）；
3. `test_removing_a_key_that_a_purpose_uses_is_refused`：删"在用"的 key 被拒、点名哪几路；
4. `test_every_write_lands_in_the_control_audit`：每次写都有审计（`source="panel"`），
   且审计里**没有 key 值**；
5. `test_without_the_file_the_seams_answer_instead_of_raising`：没有文件 → 空清单 +
   可读拒绝，**不抛**、也不建文件；
6. `test_the_seams_delegate_to_the_store_write_layer`：写**只**走那五个 store 函数
   （接缝源码里没有 `json.dump` / `write_text`）。
"""

import asyncio
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from qq_roleplay_bot import model_providers_store, runtime
from qq_roleplay_bot.control_audit import ControlAudit
from qq_roleplay_bot.plugins import UiSeams

# 复用既有夹具，不另抄一份（`tests/` 已在 `sys.path` 上，见 `tests/run_offline.py`）。
from test_model_providers_file import _isolated, _read_file, _write_file  # noqa: E402

#: 接缝的名字与顺序（**契约**：面板逐个按名字取用，改名字等于改接口）。
SEAM_NAMES = ("model_providers", "model_provider_add", "model_provider_key",
              "model_provider_key_remove", "model_provider_bind",
              "model_provider_remove")

#: 假 key：带一个能一眼搜到的标记词（掩码露首尾，短 key 会让"有没有漏明文"变歧义）。
PLAIN_KEY = "sk-seam-test-ATTENTION-1234567890"
OTHER_KEY = "sk-seam-test-OTHER-0987654321"
RELAY_URL = "https://relay.example/v1"

#: 让 `build_engine()` 起得来：它在装配时就把回复那一路解析成一个客户端
#: （`_reply_endpoint()` → `require("reply")`），**一点凭据都没有会直接抛
#: `MissingCredential`**。这两把与任何真实部署无关，只为跨过那道装配闸。
FAKE_ENV = {
    "QQBOT_API_KEY": "sk-main-for-seam-tests",
    "QQBOT_API_BASE_URL": "https://api.deepseek.com",
}


class _Transport:
    """够 `build_engine` 起得来。**这一族接缝一次传输层都不该碰**。"""

    async def call_api(self, action, params=None):
        raise AssertionError(f"供应商接缝自己调了 OneBot：{action}")

    async def send(self, *args, **kwargs):
        raise AssertionError("供应商接缝自己发了消息")

    async def start(self):
        return None


def _engine(audit_path: Path | None = None):
    engine = runtime.build_engine(_Transport())
    if audit_path is not None:
        engine.control_audit = ControlAudit(audit_path)
    return engine


def _seams(engine=None):
    engine = engine if engine is not None else _engine()
    return engine.plugin_registry.ui


def _file_body(*, provider: str = "relay1", keys=None, agents=None,
               models=("relay-model", "relay-model-2")) -> dict:
    keys = [{"id": "k1", "label": "主号", "value": PLAIN_KEY},
            {"id": "k2", "label": "备用", "value": OTHER_KEY}] if keys is None else keys
    agent_map = {"reply": {"provider": provider, "key": "k1",
                           "model": "relay-model"}} if agents is None else agents
    return {"version": 2,
            "providers": {provider: {"base_url": RELAY_URL, "models": list(models),
                                     "keys": keys}},
            "agents": agent_map}


def _rows(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- 1. 明文 key 不出这一层 ---------------------------------------------------


def test_the_read_view_never_carries_a_plaintext_key() -> None:
    """面板那一侧**拿不到明文密钥**：只给 id / label / 掩码 / 长度。

    为什么要在**接缝**这一层就掐掉，而不是"面板自己记得掩"：那种约定活不过下一轮
    （2026-10-06 的 `mask_settings` 就是先例——面板那一侧掩了，另一条路没掩）。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            view = _seams().model_providers()

        assert [item["name"] for item in view["providers"]] == ["relay1"]
        entry = view["providers"][0]
        assert entry["base_url"] == RELAY_URL
        assert entry["models"] == ["relay-model", "relay-model-2"]
        keys = entry["keys"]
        assert [item["id"] for item in keys] == ["k1", "k2"]
        assert keys[0]["label"] == "主号"
        assert keys[0]["present"] is True and keys[0]["length"] == len(PLAIN_KEY)
        # 掩码：首尾各 4 位 + 中间省略号（与面板既有的密钥显示同一套写法）。
        assert keys[0]["masked"] == f"{PLAIN_KEY[:4]}…{PLAIN_KEY[-4:]}"
        # **整份 JSON 里不许出现明文**（这一条是"绝不"两个字唯一可靠的测法）。
        dumped = json.dumps(view, ensure_ascii=False)
        assert PLAIN_KEY not in dumped, "掩码之外还有地方带着明文 key"
        assert OTHER_KEY not in dumped
        assert "ATTENTION" not in dumped, "掩码里露出了 key 中段"


def test_the_read_view_says_who_uses_each_key_and_where_each_purpose_sits() -> None:
    """两块数据：**哪几路用着这把 key**，以及**每路现在实际落在哪一层**。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body(agents={
            "reply": {"provider": "relay1", "key": "k1", "model": "relay-model"},
            # judge 只写了供应商（没写 key）⇒ 落到"第一把"那一层
            "judge": {"provider": "relay1", "model": "relay-model"},
        }))
        with _isolated(path, **FAKE_ENV):
            view = _seams().model_providers()

    keys = view["providers"][0]["keys"]
    assert keys[0]["used_by"] == ["reply"]
    assert keys[1]["used_by"] == []
    bindings = {item["task"]: item for item in view["bindings"]}
    assert bindings["reply"]["provider"] == "relay1"
    assert bindings["reply"]["key"] == "k1" and bindings["reply"]["key_label"] == "主号"
    assert bindings["reply"]["layer"] == "own"
    assert bindings["judge"]["layer"] == "first", bindings["judge"]
    assert bindings["judge"]["key"] == "", "没指定 key 的那一路不该编一个 id 出来"
    # 写信那一路的小字（用户：插件读的是核心理好的那份）。
    assert "mail 插件" in bindings["letter"]["note"]
    # 供应商下拉框：内置那几家 + 文件里的新家，**只给名字与地址**。
    options = {item["value"]: item for item in view["options"]}
    assert options["relay1"]["in_file"] is True
    assert "deepseek" in options
    assert "sk-" not in json.dumps(view["options"], ensure_ascii=False)


# --- 2. 添加：同名 / 同地址都拒绝 ----------------------------------------------


def test_adding_a_name_that_already_exists_is_refused() -> None:
    """**添加而非替换**：同名不覆盖，返回可读拒绝（让操作者自己改名）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with _isolated(path, **FAKE_ENV):
            result = _seams().model_provider_add(
                "relay1", "https://somewhere.else/v1", [{"value": OTHER_KEY}])
            after_read = _read_file(path)
            after = (path.read_bytes(), path.stat().st_mtime_ns)

    assert result["ok"] is False
    assert "relay1" in result["reason"] and "覆盖" in result["reason"], result["reason"]
    # 文件一个字节都没动（"拒绝"必须是真的没写）。
    assert after == before
    assert after_read["providers"]["relay1"]["keys"][0]["value"] == PLAIN_KEY


def test_adding_the_same_address_under_a_new_name_is_refused() -> None:
    """同一个地址两个名字 → 拒绝（用户 U3：同账号两名会让 402 兜底静默失效）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            result = _seams().model_provider_add("relay2", RELAY_URL,
                                                 [{"value": OTHER_KEY}])
        assert result["ok"] is False
        assert "relay1" in result["reason"] and "同一" in result["reason"], result


def test_adding_a_set_without_any_key_is_refused() -> None:
    """一把 key 都没有的"一套"= 用不了的空壳（读侧会拒），所以添加时就说清。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path, **FAKE_ENV):
            result = _seams().model_provider_add("relay2", RELAY_URL, [])
            assert result["ok"] is False and "key" in result["reason"], result
            assert not path.exists(), "被拒的那一次不该建出文件来"


def test_adding_a_new_set_writes_address_models_and_one_key() -> None:
    """加一套：地址 + 声明的模型 + 一把 key（"不强制最少几把 key"）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            result = seams.model_provider_add(
                "relay2", "https://relay2.example/v1",
                [{"label": "主号", "value": OTHER_KEY}], models=["m-a", "m-b"])
            view = seams.model_providers()
        body = _read_file(path)

    assert result["ok"] is True, result
    assert result["name"] == "relay2" and result["key_ids"] == ["k1"]
    assert body["providers"]["relay2"]["base_url"] == "https://relay2.example/v1"
    assert body["providers"]["relay2"]["models"] == ["m-a", "m-b"]
    assert body["providers"]["relay2"]["keys"] == [
        {"id": "k1", "label": "主号", "value": OTHER_KEY}]
    assert [item["name"] for item in view["providers"]] == ["relay2"]
    # 回执里**不许**回显 key（面板会把它显示出来）。
    assert OTHER_KEY not in json.dumps(result, ensure_ascii=False)


# --- 3. 加/改/删一把 key -------------------------------------------------------


def test_a_key_can_be_added_and_edited_without_moving_its_id() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            added = seams.model_provider_key("relay1", label="第三把", value="sk-third")
            edited = seams.model_provider_key("relay1", key_id="k2", label="备用2")
        body = _read_file(path)

    assert added["ok"] is True and added["id"] == "k3", added
    assert edited["ok"] is True and edited["id"] == "k2" and edited["created"] is False
    keys = body["providers"]["relay1"]["keys"]
    assert [item["id"] for item in keys] == ["k1", "k2", "k3"]
    assert keys[1] == {"id": "k2", "label": "备用2", "value": OTHER_KEY}, \
        "改 label 不该动 value"


def test_editing_a_key_with_an_unknown_id_is_refused() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            result = _seams().model_provider_key("relay1", key_id="k9", value="sk-x")
        assert result["ok"] is False
        assert "k9" in result["reason"] and "k1" in result["reason"], result


def test_removing_a_key_that_a_purpose_uses_is_refused() -> None:
    """删"在用"的那把 → 拒绝并点名哪几路（与删"在用的一套"同一条纪律）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body(agents={
            "reply": {"provider": "relay1", "key": "k2", "model": "relay-model"},
        }))
        with _isolated(path, **FAKE_ENV):
            refused = _seams().model_provider_key_remove("relay1", "k2")
            ok = _seams().model_provider_key_remove("relay1", "k1")
        body = _read_file(path)

    assert refused["ok"] is False
    assert "agents.reply" in refused["reason"] and "先" in refused["reason"], refused
    assert ok["ok"] is True and ok["count"] == 1
    assert [item["id"] for item in body["providers"]["relay1"]["keys"]] == ["k2"]


# --- 4. 绑定：provider → key → model ------------------------------------------


def test_binding_a_purpose_sets_all_three_and_reports_the_layer() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            result = seams.model_provider_bind("judge", "relay1", "k2", "relay-model-2")
            view = seams.model_providers()
        body = _read_file(path)

    assert result["ok"] is True, result
    assert body["agents"]["judge"] == {"provider": "relay1", "key": "k2",
                                       "model": "relay-model-2"}
    binding = next(item for item in view["bindings"] if item["task"] == "judge")
    assert binding["layer"] == "own" and binding["key"] == "k2"
    assert binding["model_declared"] is True


def test_binding_a_model_outside_the_declared_list_is_allowed_but_flagged() -> None:
    """模型允许填列表外的值（用户定），但回执与视图都要**标注**它不在声明的模型里。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            result = seams.model_provider_bind("reply", "relay1", "k1", "not-declared")
            view = seams.model_providers()
        assert result["ok"] is True, result
        assert result["model_declared"] is False
        assert "不在它声明的模型里" in result["message"], result["message"]
        binding = next(item for item in view["bindings"] if item["task"] == "reply")
        assert binding["model_declared"] is False


def test_binding_to_a_key_id_that_the_provider_does_not_have_is_refused() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            missing = seams.model_provider_bind("judge", "relay1", "k9", "m")
            no_model = seams.model_provider_bind("judge", "relay1", "k1", "")
            bad_task = seams.model_provider_bind("nope", "relay1", "k1", "m")
            unknown = seams.model_provider_bind("judge", "nope-provider", "", "m")
    assert missing["ok"] is False and "k9" in missing["reason"], missing
    assert no_model["ok"] is False and "模型" in no_model["reason"], no_model
    assert bad_task["ok"] is False and "用途" in bad_task["reason"], bad_task
    assert unknown["ok"] is False and "供应商" in unknown["reason"], unknown


def test_binding_with_an_empty_provider_releases_the_purpose() -> None:
    """`provider=""` = 解开这一路（回到 `.env` 那套），不是写一条空绑定。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            result = seams.model_provider_bind("reply", "", "", "")
            view = seams.model_providers()
        body = _read_file(path)

    assert result["ok"] is True and result["cleared"] is True, result
    assert "reply" not in body["agents"]
    binding = next(item for item in view["bindings"] if item["task"] == "reply")
    assert binding["provider"] == "" and binding["layer"] == "env"


# --- 5. 删除一套 ---------------------------------------------------------------


def test_removing_a_provider_that_a_purpose_uses_is_refused() -> None:
    """用户补定：**"当前那一套"不许直接删**，要先切走。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body(agents={
            "reply": {"provider": "relay1", "key": "k1", "model": "relay-model"},
            "memory": {"provider": "relay1", "key": "k2", "model": "relay-model"},
        }))
        with _isolated(path, **FAKE_ENV):
            result = _seams().model_provider_remove("relay1")
        body = _read_file(path)

    assert result["ok"] is False
    reason = result["reason"]
    # 拒绝理由要**点名是哪几路在用**、并说清"先切走再删"。
    assert "agents.reply" in reason and "agents.memory" in reason, reason
    assert "先" in reason and "删" in reason, reason
    assert "relay1" in body["providers"], "被拒的那次居然把它删掉了"


def test_removing_a_set_nobody_uses_works_and_takes_its_keys_with_it() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = _file_body()
        body["providers"]["spare"] = {"base_url": "https://spare.example/v1",
                                      "keys": [{"id": "k1", "value": OTHER_KEY}]}
        _write_file(path, body)
        with _isolated(path, **FAKE_ENV):
            result = _seams().model_provider_remove("spare")
        after = _read_file(path)

    assert result["ok"] is True, result
    assert set(after["providers"]) == {"relay1"}
    assert after["agents"]["reply"]["provider"] == "relay1", "顺手动了别的东西"


def test_removing_the_provider_that_the_env_declares_is_refused() -> None:
    """比那三条口径**多出来**的一条（写在汇报里让人裁决）。

    依据：`QQBOT_API_PROVIDER` 一旦声明了文件里的新家，`model_config.resolve()` 就要认它
    （提交 `f2ef567` 的"主 key 的声明也走合并视图"）。删掉之后走主 key 的那几路会
    **当场解析不出来**——与"悄悄掉回环境那套"是同一类事故，所以也拒。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        body = _file_body(agents={})
        body["providers"]["relay2"] = {"base_url": "https://relay2.example/v1",
                                       "keys": [{"id": "k1", "value": OTHER_KEY}]}
        _write_file(path, body)
        with _isolated(path, QQBOT_API_PROVIDER="relay2", **FAKE_ENV):
            result = _seams().model_provider_remove("relay2")
        after = _read_file(path)

    assert result["ok"] is False
    assert "QQBOT_API_PROVIDER" in result["reason"], result["reason"]
    assert "relay2" in after["providers"]


# --- 6. 每次写都有审计 --------------------------------------------------------


def test_every_write_lands_in_the_control_audit() -> None:
    """每次写都进既有 `control_audit`（动作名 `model_providers`、来源 `panel`），
    且**审计里没有 key 值**。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        audit_path = Path(tmp) / "control_audit.jsonl"
        _write_file(path, _file_body())
        with _isolated(path, **FAKE_ENV):
            seams = _seams(_engine(audit_path))
            assert seams.model_provider_key("relay1", value="sk-audit-secret")["ok"] is True
            assert seams.model_provider_bind("judge", "relay1", "k3",
                                             "relay-model-2")["ok"] is True
            assert seams.model_provider_key_remove("relay1", "k2")["ok"] is True
            assert seams.model_provider_remove("relay1")["ok"] is False, "还有路在用着"
        rows = _rows(audit_path)
        spilled = audit_path.read_text(encoding="utf-8")

    assert rows, "一次写都没进审计"
    assert all(row["action"] == "model_providers" for row in rows), \
        [row["action"] for row in rows]
    assert all(row["source"] == "panel" for row in rows), "来源没记成 panel"
    actions = [row["detail"].get("action") for row in rows]
    assert actions.count("key") == 1, actions
    assert actions.count("agent") == 1, actions
    assert actions.count("key_removed") == 1, actions
    # **审计文件里不许出现 key 明文**（三把都不许）。
    for value in (PLAIN_KEY, OTHER_KEY, "sk-audit-secret"):
        assert value not in spilled


# --- 7. 没有文件 → 空清单 + 可读拒绝，不抛 ------------------------------------


def test_without_the_file_the_seams_answer_instead_of_raising() -> None:
    """文件不存在时：清单是**空列表**（不是异常），写口给**可读拒绝**。

    为什么这条重要：面板第一次打开时文件往往还没建，而"一个都没有"是正确答案；
    如果这里抛异常，面板那一页整块会 500，操作者就永远看不到「添加一套」那个表单。
    """

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        assert not path.exists()
        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            view = seams.model_providers()
            key = seams.model_provider_key("relay1", value="sk-x")
            remove = seams.model_provider_remove("relay1")
        assert not path.exists(), "只读的那几次不该建出文件来"

    assert view["providers"] == []
    assert view["present"] is False
    assert view["last_error"] == "", "没有文件不是错误"
    # 六路都在，而且都报"文件没管它"（面板据此显示"实际走 .env"）。
    assert [item["task"] for item in view["bindings"]] == [
        "reply", "letter", "judge", "memory", "review", "vision"]
    assert {item["layer"] for item in view["bindings"]} == {"env"}
    assert key["ok"] is False and key["reason"].strip()
    assert remove["ok"] is False and remove["reason"].strip()


# --- 8. 写只走 store 的写层，不自己拼 JSON ------------------------------------


def test_the_seams_delegate_to_the_store_write_layer() -> None:
    """写**只**经 `model_providers_store` 那五个函数（两个写者两套纪律是要消灭的东西）。"""

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "model_providers.json"
        _write_file(path, _file_body())
        seen: list[str] = []

        def spy(name, real):
            def wrapper(*args, **kwargs):
                seen.append(name)
                return real(*args, **kwargs)

            return wrapper

        with _isolated(path, **FAKE_ENV):
            seams = _seams()
            with patch.object(model_providers_store, "set_provider",
                              spy("set_provider", model_providers_store.set_provider)), \
                 patch.object(model_providers_store, "put_provider_key",
                              spy("put_provider_key",
                                  model_providers_store.put_provider_key)), \
                 patch.object(model_providers_store, "remove_provider_key",
                              spy("remove_provider_key",
                                  model_providers_store.remove_provider_key)), \
                 patch.object(model_providers_store, "upsert_agent",
                              spy("upsert_agent", model_providers_store.upsert_agent)), \
                 patch.object(model_providers_store, "remove_provider",
                              spy("remove_provider",
                                  model_providers_store.remove_provider)):
                assert seams.model_provider_add(
                    "relay2", "https://relay2.example/v1",
                    [{"value": OTHER_KEY}])["ok"] is True
                # 同名 → 在**接缝这一层**就拒，所以不产生任何写层调用。
                assert seams.model_provider_add(
                    "relay2", "https://relay2.example/v1",
                    [{"value": OTHER_KEY}])["ok"] is False
                assert seams.model_provider_key("relay1", value="sk-third")["ok"] is True
                assert seams.model_provider_key_remove("relay1", "k2")["ok"] is True
                assert seams.model_provider_bind("judge", "relay1", "k1",
                                                 "relay-model")["ok"] is True
                assert seams.model_provider_remove("relay2")["ok"] is True

    assert seen == ["set_provider", "put_provider_key", "put_provider_key",
                    "remove_provider_key", "upsert_agent", "remove_provider"], seen
    # 接缝的源码里**没有**自己拼文件的写法（有的话上面那串 spy 一定不全）。
    for name in ("model_provider_add", "model_provider_key", "model_provider_key_remove",
                 "model_provider_bind", "model_provider_remove"):
        source = inspect.getsource(getattr(runtime._SeamBinder, name))
        assert "json.dump" not in source and "write_text" not in source, \
            f"{name} 自己在写文件"


# --- 形状：六口、同步、函数（面板拿不到引擎与传输层）-------------------------


def test_the_panel_side_gets_six_sync_functions_and_nothing_else() -> None:
    seams = _seams()
    assert isinstance(seams, UiSeams)
    for name in SEAM_NAMES:
        seam = getattr(seams, name)
        assert callable(seam), f"{name} 不是函数（面板只该拿到函数）"
        assert not asyncio.iscoroutinefunction(seam), f"{name} 是异步的（面板线程是同步的）"
    assert not hasattr(seams, "transport"), "接缝把 transport 递给面板了"
    assert not hasattr(seams, "engine"), "接缝把引擎递给面板了"

    assert list(inspect.signature(seams.model_providers).parameters) == []
    assert list(inspect.signature(seams.model_provider_add).parameters) == [
        "name", "base_url", "keys", "client_type", "models"]
    assert list(inspect.signature(seams.model_provider_key).parameters) == [
        "name", "key_id", "label", "value"]
    assert list(inspect.signature(seams.model_provider_key_remove).parameters) == [
        "name", "key_id"]
    assert list(inspect.signature(seams.model_provider_bind).parameters) == [
        "task", "provider", "key", "model"]
    assert list(inspect.signature(seams.model_provider_remove).parameters) == ["name"]


def test_the_binder_fills_the_six_fields_the_panel_reads() -> None:
    """装配点真的把六个函数递到 `UiSeams` 上了（不是留着 `None`）。"""

    from dataclasses import fields

    names = {item.name for item in fields(UiSeams)}
    for name in SEAM_NAMES:
        assert name in names, f"`UiSeams` 上没有 {name} 这个字段"
    seams = _seams()
    for name in SEAM_NAMES:
        assert getattr(seams, name) is not None, f"装配点没接上 {name}"


if __name__ == "__main__":
    import unittest

    unittest.main()
