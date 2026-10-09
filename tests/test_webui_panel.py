"""控制面板（`plugins/webui`）的离线覆盖：**路由层 + 接缝 + 授权**，不起线程、不连网。

为什么补这个文件（2026-10-06）：这条分支上**一条面板测试都没有**
（`wire.py` 的注释一直写着"见 test_webui_panel.py"，而那个文件在插件侧重建时没了），
于是两个接缝断掉都没有任何东西拦：

1. `webui_data.read_health` 里 `from ...onebot_ws import port_in_use`——那个函数
   **从来没存在过**（核心那边叫 `stage3_main._port_in_use`）→ `/health` 与
   `/api/overview` 500；而前端那道登录门正是拿 `/api/overview` 验令牌的，
   **令牌再对也进不去**（截图：一直停在登录页）；
2. `webui_data.read_usage` 里 `from ...api_usage import APICHECK_ROLES`——本体把它
   搬去了 `stage3_main`，这里没跟着改 → `/api/usage` 500（总览页整块用量读不出来）。

两条都不是"启动就炸"的那种：面板的 import 大多在函数体里，**点到了才 500**。
所以这里除了逐个钉住这两条，还加了一条"**任何读接口都不许回 500**"的冒烟，
以及一条"**插件里每一条 import 的目标都得还在**"的静态扫描——那正是这两次断线的
共同形状（核心搬了名字，插件这边没人知道）。
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import unittest
from importlib import resources
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qq_roleplay_bot.plugins.webui import webui_data  # noqa: E402
from qq_roleplay_bot.plugins.webui.webui_access import LOCAL, WebAccess  # noqa: E402
from qq_roleplay_bot.plugins.webui.webui_panel import Panel  # noqa: E402

#: 测试用令牌（`WebAccess.configured` 要求至少 16 个字符）。
TOKEN = "test-token-0123456789abcdef"

#: 走一遍全部**读**接口。每一项都只读文件/接缝，不产生任何外部动作。
READ_PATHS = (
    "/health",
    "/api/overview",
    "/api/state",
    # 账号页的资料头卡（她自己是谁 ← 既有的 `self_id` 接缝）与设备页的占用空间
    # （面板自己的 data/ 落点）：两个都只读，接缝/目录不在时也必须回 200。
    "/api/self",
    "/api/storage",
    "/api/plugins",
    "/api/usage",
    "/api/settings",
    "/api/prompts",
    "/api/prompts/versions?name=reply",
    "/api/knowledge/overview",
    "/api/knowledge/chunks",
    "/api/knowledge/operator",
    # 「知识库 → 金句 / 黑话」（她学来的东西）：接缝没接上时也必须回 200、
    # 并且**如实说读不到**（见 `LearnedSeamTests`），不是 500、也不是编一份空表。
    "/api/knowledge/quotes?group_id=717151356",
    # 金句**审核**的证据（2026-10-06 用户："你连原文和上下文都不贴出来怎么审核"）：
    # 接缝少了 `quote_evidence` 时也要回 200 + 如实说读不到。
    "/api/knowledge/quotes/evidence?group_id=717151356",
    "/api/knowledge/slang",
    "/api/approvals/pending",
    "/api/actions/preview",
    "/api/history",
    "/api/logs?feature=judge",
    "/api/memory/overview",
    "/api/memory/problem?filter=stale",
    "/api/memory/records",
    "/api/memory/archive",
    "/api/memory/inbox",
    "/api/memory/audit",
    "/api/memory/relationships",
    "/api/memory/people",
    "/api/memory/tombstones",
    "/api/session/messages?session_id=group:717151356",
)


class _PanelCase(unittest.TestCase):
    """造一个**真的 `Panel`**（真路由、真授权），seams 只给数据根目录。

    数据根目录指到临时目录：面板的读接口一律只读文件，指过去就不会读到这台机器上
    真实的 `data/`（跑测试不该碰它，`wire.py` 里同一条纪律）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"QQBOT_DATA_DIR": str(self.root)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.panel = Panel({"data_root": str(self.root)},
                           WebAccess(mode=LOCAL, token=TOKEN))

    def request(self, method: str, path: str, body: bytes = b"", *,
                token: str | None = TOKEN, accept: str = "application/json"):
        headers = {"accept": accept}
        if token is not None:
            headers["authorization"] = "Bearer " + token
        return self.panel.handle(method, path, body, headers, "127.0.0.1")

    def payload(self, response) -> dict:
        return json.loads(response.body.decode("utf-8"))


class ReadRoutesTests(_PanelCase):
    def test_no_read_route_answers_500(self) -> None:
        """**任何读接口都不许回 500**——断了接缝的那两个正是这么暴露的。"""

        for path in READ_PATHS:
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertNotEqual(
                    response.status, 500,
                    f"{path} 回了 500（多半是某条 import 的目标没了）："
                    f"{response.body[:200]!r}")

    def test_health_reports_a_real_port_probe(self) -> None:
        """`/health` 得能落地：bot_port 是配置里的端口，bot_live 是探出来的真假。"""

        response = self.request("GET", "/health")
        self.assertEqual(response.status, 200)
        data = self.payload(response)["data"]
        self.assertIn("bot_port", data)
        self.assertIsInstance(data["bot_live"], bool)
        self.assertIn("data_dir", data)

    def test_overview_works_because_the_login_gate_probes_it(self) -> None:
        """前端登录门拿 `/api/overview` 验令牌——它 500 就等于**面板打不开**。"""

        response = self.request("GET", "/api/overview")
        self.assertEqual(response.status, 200)
        data = self.payload(response)["data"]
        for key in ("health", "state", "usage", "flags", "access"):
            self.assertIn(key, data)

    def test_usage_lists_the_agents_from_the_core_catalog(self) -> None:
        """角色名必须来自核心那份 `APICHECK_ROLES`，不是面板自己抄的一份。"""

        from qq_roleplay_bot.stage3_main import APICHECK_ROLES

        response = self.request("GET", "/api/usage")
        self.assertEqual(response.status, 200)
        rows = self.payload(response)["data"]["role_list"]
        self.assertEqual([(row["role"], row["label"]) for row in rows[:len(APICHECK_ROLES)]],
                         list(APICHECK_ROLES))

    def test_unknown_api_path_is_404_not_500(self) -> None:
        self.assertEqual(self.request("GET", "/api/nope").status, 404)


class AuthorizationTests(_PanelCase):
    def test_api_without_a_token_is_unauthorized(self) -> None:
        response = self.request("GET", "/api/overview", token=None)
        self.assertEqual(response.status, 401)
        self.assertEqual(self.payload(response)["error"], "unauthorized")

    def test_a_wrong_token_is_unauthorized(self) -> None:
        response = self.request("GET", "/api/overview", token="x" * 30)
        self.assertEqual(response.status, 401)

    def test_the_shell_is_public_so_the_login_page_can_load(self) -> None:
        """外壳（HTML/CSS/JS）与 `/health` 不认证：不然"页面要令牌、令牌要在页面里填"死锁。"""

        for path in ("/", "/index.html", "/app.css", "/app.js", "/health"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=None).status, 200)

    def test_api_paths_are_not_public(self) -> None:
        for path in ("/api/state", "/api/plugins", "/api/prompts"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, token=None).status, 401)

    def test_static_is_a_whitelist_not_a_path_join(self) -> None:
        """静态资源**只认三个名字**，没有路径拼接——所以也就没有穿越。

        带着令牌问（认证在路由之前：未认证的请求一律 401，**不告诉外面某个路径存不存在**，
        那是 fail-closed 的一半，另一条用例钉住它）。
        """

        for path in ("/../.env", "/app.css/../index.html", "/secrets.txt", "/webui_data.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path).status, 404)

    def test_a_write_without_confirm_is_refused(self) -> None:
        """危险动作必须带 `confirm: true`——挡住的是"前端忘了问"，不是"用户点错了"。"""

        body = json.dumps({"group_id": "717151356", "enabled": True}).encode()
        response = self.request("POST", "/api/groups", body)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.payload(response)["error"], "needs_confirmation")


class LearnedSeamTests(unittest.TestCase):
    """「知识库 → 金句 / 黑话」：**只走** `registry.ui.learned` 那七个函数。

    用户 2026-10-06 的位置口径是*"金句我看应该划到知识库里"*、*"还有黑话"*，
    而这两份数据都是 Stage 3 的（`AGENTS.md` §2.3）。所以这一组用例钉三件事：

    1. 读：`/api/knowledge/quotes` → `learned.quote_view`，`/api/knowledge/slang`
       → `learned.slang_list`；写：四个动作各自只调对应的那一个函数；
    2. 接缝**没接上**（或少了某一个函数）时的降级：读如实说"读不到"、写回 503
       ——不许 500、不许编一份空数据、更不许自己去 `data/` 里翻 JSON；
    3. 取值表不复制：`QUOTE_AUTO` 必须是核心 `QUOTE_OVERRIDE_AUTO` 里的一个，
       而"不认识的方向"要在**碰接缝之前**就被挡住（塞进去会落一条静默改行为的覆盖）。
    """

    TOKEN = TOKEN

    def _panel(self, learned) -> Panel:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"QQBOT_DATA_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        return Panel({"data_root": tmp.name, "learned": learned},
                     WebAccess(mode=LOCAL, token=TOKEN))

    @staticmethod
    def _call(panel: Panel, method: str, path: str, body: dict | None = None):
        raw = json.dumps(body or {}).encode() if method != "GET" else b""
        return panel.handle(method, path, raw,
                            {"accept": "application/json",
                             "authorization": "Bearer " + TOKEN}, "127.0.0.1")

    class _FakeLearned:
        """七个口，逐个记账。返回的形状**照核心那份契约**（见 `LearnedSeams`）。"""

        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def quote_view(self, group_id):
            self.calls.append(("quote_view", group_id))
            if group_id != "717151356":
                return {}
            return {"group_id": group_id,
                    "meanings": [{"emoji_id": "76", "sense": "positive",
                                  "sense_label": "赞成（好句子）", "note": "这是捧她",
                                  "manual": False}],
                    "notes": [{"id": "abc123", "kind": "style", "when": "被夸",
                               "note": "一句少来带过", "enabled": True}],
                    "path": "D:/isolated/quote/profile.json"}

        def quote_correct(self, group_id, emoji_id, sense, note=""):
            self.calls.append(("quote_correct", group_id, emoji_id, sense, note))
            return {"group_id": group_id, "emoji_id": emoji_id, "sense": sense,
                    "cleared": sense == "auto", "written": True}

        def quote_evidence(self, group_id, limit=10, offset=0):
            """证据：形状照核心 `learned_quote_evidence` 的那一份（只列页面用到的键）。"""

            self.calls.append(("quote_evidence", group_id, limit, offset))
            if group_id != "717151356":
                return {}
            return {"group_id": group_id, "total": 21, "offset": offset, "limit": limit,
                    "error": "", "path": "D:/isolated/quote/profile.json",
                    "evidence": [
                        {"message_id": "6", "group_id": group_id, "at": 2.0,
                         "她说的": "我们那次是先按学校凑齐三个人。",
                         "她开口之前群里在说": ["阿澈：你们那个比赛几个人一组"],
                         "她说完之后群里接的": ["小满：行吧"],
                         "被点了": [{"emoji_id": "76", "by": "10001", "count": 2}],
                         "meanings": [{"emoji_id": "76", "sense": "positive",
                                       "sense_label": "赞成（好句子）", "note": "这是捧她",
                                       "quote": "我们那次是先按学校凑齐三个人",
                                       "manual": False}],
                         "notes": [{"emoji_id": "76", "id": "abc123", "note": "一句少来带过",
                                    "manual": False}],
                         "当时的话题": "比赛分组", "对方想干什么": "想要去年的名单"},
                        {"message_id": "9", "group_id": group_id, "at": 1.0,
                         "她说的": "听不懂是因为我说话快。",
                         "她开口之前群里在说": ["小满：她昨天那个语音我听三遍才听懂"],
                         "她说完之后群里接的": [],
                         "被点了": [{"emoji_id": "999", "by": "10002", "count": 1}],
                         "meanings": [],
                         "notes": [{"emoji_id": "999", "id": "", "note": "",
                                    "manual": False}],
                         "为什么没有结论": "这些表情学习 agent 还没判过"},
                    ]}

        def quote_note_enabled(self, note_id, enabled):
            self.calls.append(("quote_note_enabled", note_id, enabled))
            return {"id": note_id, "enabled": bool(enabled), "written": True}

        def slang_list(self, group_id=None):
            self.calls.append(("slang_list", group_id))
            return [{"id": "717151356:电赛", "group_id": "717151356", "word": "电赛",
                     "definition": "电子设计竞赛", "status": "ok", "wrong": False,
                     "times": 2, "first_seen": 1.0, "last_seen": 2.0,
                     "explainers": [{"user_id": "1", "name": "甲", "at": 2.0,
                                     "definition": "电子设计竞赛",
                                     "quote": "电赛就是电子设计竞赛", "count": 1,
                                     "source": "heard"}],
                     "revisions": []}]

        def slang_update(self, group_id, word, definition):
            self.calls.append(("slang_update", group_id, word, definition))
            return {"group_id": group_id, "word": word, "definition": definition}

        def slang_delete(self, group_id, word):
            self.calls.append(("slang_delete", group_id, word))
            return True

        def slang_mark_wrong(self, group_id, word, wrong=True):
            self.calls.append(("slang_mark_wrong", group_id, word, wrong))
            return {"group_id": group_id, "word": word, "wrong": wrong}

    def _request(self, learned, method, path, body=None):
        panel = self._panel(learned)
        return panel, self._call(panel, method, path, body)

    @staticmethod
    def _data(response) -> dict:
        return json.loads(response.body.decode("utf-8"))["data"]

    def test_the_call_names_are_the_core_contract(self) -> None:
        """面板认的那几个名字**就是** `LearnedSeams` 的字段，**连顺序也一样**
        （多一个、少一个、换个次序都红）。

        名字里那个数（原来是"seven"）**故意不写进测试名**：核心 2026-10-07 加了第八个
        `quote_evidence`，测试名里带数就成了第二个真相源——一加字段连名字都得改。
        """

        from dataclasses import fields

        from qq_roleplay_bot.plugins import LearnedSeams
        from qq_roleplay_bot.plugins.webui.webui_panel import LEARNED_CALLS

        self.assertEqual(tuple(item.name for item in fields(LearnedSeams)), LEARNED_CALLS)

    def test_the_auto_value_comes_from_the_core_table(self) -> None:
        """「恢复自动」送的取值必须在核心那张表里（面板不另立一份）。"""

        from qq_roleplay_bot.plugins.webui.webui_panel import QUOTE_AUTO
        from qq_roleplay_bot.stage3_main import QUOTE_OVERRIDE_AUTO

        self.assertIn(QUOTE_AUTO, QUOTE_OVERRIDE_AUTO)

    def test_quotes_read_goes_through_quote_view_only(self) -> None:
        learned = self._FakeLearned()
        _, response = self._request(learned, "GET", "/api/knowledge/quotes?group_id=717151356")
        self.assertEqual(response.status, 200)
        data = self._data(response)
        self.assertTrue(data["available"])
        self.assertEqual(data["meanings"][0]["emoji_id"], "76")
        self.assertEqual(data["notes"][0]["id"], "abc123")
        # 方向的可选值来自核心那份 `SENSE_LABELS`，不是面板自己写的表。
        from qq_roleplay_bot.quote_learning import SENSE_LABELS

        self.assertEqual([item["value"] for item in data["senses"]], list(SENSE_LABELS))
        self.assertEqual([call for call in learned.calls if call[0] == "quote_view"],
                         [("quote_view", "717151356")])

    def test_quotes_view_that_returns_nothing_reads_as_unavailable(self) -> None:
        """接缝在、但这一份读不到（`{}`）→ `available: false`，**不冒充空表**。"""

        learned = self._FakeLearned()
        _, response = self._request(learned, "GET", "/api/knowledge/quotes?group_id=999999")
        data = self._data(response)
        self.assertFalse(data["available"])
        self.assertEqual(data["meanings"], [])

    def test_correct_calls_quote_correct_and_refuses_a_bogus_sense(self) -> None:
        learned = self._FakeLearned()
        panel, response = self._request(learned, "POST", "/api/knowledge/quotes",
                                        {"confirm": True, "action": "correct",
                                         "group_id": "717151356", "emoji_id": "76",
                                         "sense": "negative"})
        self.assertEqual(response.status, 200)
        self.assertIn(("quote_correct", "717151356", "76", "negative", ""), learned.calls)

        # 垃圾取值：**接缝一次都不许被碰到**（塞进去会落一条静默改行为的覆盖）。
        before = list(learned.calls)
        _, bad = self._request(learned, "POST", "/api/knowledge/quotes",
                               {"confirm": True, "action": "correct",
                                "group_id": "717151356", "emoji_id": "76",
                                "sense": "banana"})
        self.assertEqual(bad.status, 400)
        self.assertEqual(learned.calls, before)

    def test_note_toggle_calls_quote_note_enabled(self) -> None:
        learned = self._FakeLearned()
        _, response = self._request(learned, "POST", "/api/knowledge/quotes",
                                    {"confirm": True, "action": "note",
                                     "note_id": "abc123", "enabled": False})
        self.assertEqual(response.status, 200)
        self.assertIn(("quote_note_enabled", "abc123", False), learned.calls)

    def test_evidence_read_goes_through_quote_evidence_only(self) -> None:
        """审核页的证据：`/api/knowledge/quotes/evidence` → `learned.quote_evidence`。

        用户 2026-10-06：*"你连原文和上下文都不贴出来怎么审核"*。这一条钉三件：
        参数**原样**传下去（群 / limit / offset）、证据**原样**端回来（含"为什么没有结论"
        这种如实字段），而且只碰了 `quote_evidence` 这一个口。
        """

        learned = self._FakeLearned()
        _, response = self._request(
            learned, "GET", "/api/knowledge/quotes/evidence?group_id=717151356&limit=2&offset=1")
        self.assertEqual(response.status, 200)
        data = self._data(response)
        self.assertTrue(data["available"])
        self.assertEqual(data["total"], 21)
        self.assertEqual(data["offset"], 1)
        self.assertEqual([call for call in learned.calls if call[0] == "quote_evidence"],
                         [("quote_evidence", "717151356", 2, 1)])
        # 除了证据本身，只会碰 `slang_list`——那是**候选群**（黑话里出现过的群），
        # 页面上那个"看哪个群"的下拉要用；别的口一个都不该碰。
        self.assertEqual(sorted({call[0] for call in learned.calls}),
                         ["quote_evidence", "slang_list"],
                         f"证据那一页碰了别的口：{learned.calls}")
        first, second = data["evidence"]
        self.assertEqual(first["她说的"], "我们那次是先按学校凑齐三个人。")
        self.assertEqual(first["被点了"][0]["emoji_id"], "76")
        self.assertEqual(first["meanings"][0]["sense_label"], "赞成（好句子）")
        self.assertEqual(first["notes"][0]["id"], "abc123")
        # 没结论的那一条：**如实**端出来（页面照实写"还没判过"），不是编一个方向。
        self.assertEqual(second["meanings"], [])
        self.assertEqual(second["为什么没有结论"], "这些表情学习 agent 还没判过")
        self.assertEqual(second["notes"][0]["id"], "",
                         "对不上笔记时必须是空 id（页面据此写「没落到任何笔记」）")

    def test_evidence_stays_honest_when_the_seam_has_no_such_call(self) -> None:
        """接缝少了 `quote_evidence`（老的装配点）→ 200 + `available: false`，不编空表。"""

        learned = self._FakeLearned()
        learned.quote_evidence = None       # 半接缝：别的口都在，就缺这一个
        _, response = self._request(
            learned, "GET", "/api/knowledge/quotes/evidence?group_id=717151356")
        self.assertEqual(response.status, 200)
        data = self._data(response)
        self.assertFalse(data["available"])
        self.assertEqual(data["evidence"], [])
        self.assertEqual(data["total"], 0)

    def test_the_evidence_page_has_no_write_route_of_its_own(self) -> None:
        """证据那条路径**只读**：写仍然只有 `/api/knowledge/quotes` 那一条。

        用户的要求是"不许新开写通道"——所以这里对**路由表**钉：往证据路径 POST
        不该被当成写动作接住（面板会回 404/405 那类"没这个动作"），
        而真正的写动作仍然落在既有那条口上。
        """

        learned = self._FakeLearned()
        _, posted = self._request(learned, "POST", "/api/knowledge/quotes/evidence",
                                  {"confirm": True, "action": "correct",
                                   "group_id": "717151356", "emoji_id": "76",
                                   "sense": "positive"})
        self.assertNotEqual(posted.status, 200,
                            "证据那条路径被当成写口接住了——那等于新开了一条写通道")
        self.assertEqual([call for call in learned.calls if call[0] == "quote_correct"], [],
                         "往证据路径 POST 居然碰到了 `quote_correct`")
        # 正经的写路径照旧能改（而且只有这一条）。
        _, ok = self._request(learned, "POST", "/api/knowledge/quotes",
                              {"confirm": True, "action": "correct",
                               "group_id": "717151356", "emoji_id": "76",
                               "sense": "positive"})
        self.assertEqual(ok.status, 200)
        self.assertIn(("quote_correct", "717151356", "76", "positive", ""), learned.calls)

    def test_slang_writes_hit_one_function_each(self) -> None:
        learned = self._FakeLearned()
        self._request(learned, "POST", "/api/knowledge/slang",
                      {"confirm": True, "action": "update", "group_id": "717151356",
                       "word": "电赛", "definition": "电子设计竞赛"})
        self._request(learned, "POST", "/api/knowledge/slang",
                      {"confirm": True, "action": "mark_wrong", "group_id": "717151356",
                       "word": "电赛"})
        self._request(learned, "POST", "/api/knowledge/slang",
                      {"confirm": True, "action": "delete", "group_id": "717151356",
                       "word": "电赛"})
        self.assertIn(("slang_update", "717151356", "电赛", "电子设计竞赛"), learned.calls)
        self.assertIn(("slang_mark_wrong", "717151356", "电赛", True), learned.calls)
        self.assertIn(("slang_delete", "717151356", "电赛"), learned.calls)

    def test_an_empty_definition_is_refused_before_the_seam(self) -> None:
        learned = self._FakeLearned()
        _, response = self._request(learned, "POST", "/api/knowledge/slang",
                                    {"confirm": True, "action": "update",
                                     "group_id": "717151356", "word": "电赛",
                                     "definition": "   "})
        self.assertEqual(response.status, 400)
        self.assertEqual(learned.calls, [])

    def test_without_the_seam_reads_say_so_and_writes_are_503(self) -> None:
        """接缝没接上：读**如实说读不到**（200）、写回 503——都不是 500。"""

        for path in ("/api/knowledge/quotes?group_id=717151356", "/api/knowledge/slang"):
            with self.subTest(path=path):
                _, response = self._request(None, "GET", path)
                self.assertEqual(response.status, 200)
                self.assertFalse(self._data(response)["available"])
        _, write = self._request(None, "POST", "/api/knowledge/quotes",
                                 {"confirm": True, "action": "note",
                                  "note_id": "abc123", "enabled": True})
        self.assertEqual(write.status, 503)

    def test_a_half_wired_seam_only_breaks_its_own_page(self) -> None:
        """少一个函数只坏对应那一件事：`slang_list` 缺席不该让金句也读不出来。"""

        learned = self._FakeLearned()
        learned.slang_list = None            # 核心没给这个口
        _, quotes = self._request(learned, "GET", "/api/knowledge/quotes?group_id=717151356")
        _, slang = self._request(learned, "GET", "/api/knowledge/slang")
        self.assertTrue(self._data(quotes)["available"])
        self.assertFalse(self._data(slang)["available"])

    def test_the_shapes_are_the_ones_the_core_really_returns(self) -> None:
        """**真 store + 真接缝函数 + 真 `Panel`**：面板读到的形状就是核心给的那一份。

        前面那些用例用的是假接缝，只证明"面板会调那几个函数"。这一条把契约钉在真东西上：
        `quote_learning.QuoteStore` / `slang_learning.SlangStore` 落到临时文件，
        经 `runtime._SeamBinder(...).learned_seams()`（**核心装配点用的就是它**）交给面板，
        再按页面上真正用到的字段逐个核对——包括"你手工改过"（`manual`）、
        "这条笔记停用了"（`enabled`）、改口后的释义与 `revisions`、原句证据。
        """

        import types

        from qq_roleplay_bot import runtime
        from qq_roleplay_bot.quote_learning import QuoteStore, note_id
        from qq_roleplay_bot.slang_learning import SlangStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)

        quotes = QuoteStore(root / "quote.json")
        quotes.apply_run(
            notes=[{"kind": "style", "when": "被夸", "note": "一句少来带过"}],
            meanings={"717151356": {"76": {"sense": "positive", "note": "这是捧她"}}},
            seen=[])
        quotes.override_sense("717151356", "76", "negative")
        quotes.set_note_enabled(note_id("被夸", "一句少来带过"), False)

        slang = SlangStore(root / "slang.json")
        slang.apply_explanation("717151356", "电赛", "电子设计竞赛", by_user_id="10001",
                                by_name="甲", quote="电赛就是电子设计竞赛", at=1000.0)
        slang.apply_explanation("717151356", "电赛", "电子设计大赛", by_user_id="10002",
                                by_name="乙", quote="我们说的电赛是电子设计大赛", at=2000.0)
        slang.mark_wrong("717151356", "电赛", True)

        engine = types.SimpleNamespace(
            quote_profile=types.SimpleNamespace(store=quotes), slang_library=slang)
        binder = runtime._SeamBinder(engine)
        self.addCleanup(runtime._ENGINES.pop, binder.token, None)

        panel = self._panel(binder.learned_seams())

        quotes_data = self._data(self._call(
            panel, "GET", "/api/knowledge/quotes?group_id=717151356"))
        self.assertTrue(quotes_data["available"])
        meaning = quotes_data["meanings"][0]
        self.assertEqual(meaning["emoji_id"], "76")
        self.assertEqual(meaning["sense"], "negative")        # 覆盖压过模型判的
        self.assertTrue(meaning["manual"])                     # "你手工改过"
        self.assertIn("不赞成", meaning["sense_label"])
        note = quotes_data["notes"][0]
        self.assertEqual((note["kind"], note["when"], note["note"]),
                         ("style", "被夸", "一句少来带过"))
        self.assertFalse(note["enabled"])                      # 停用了
        self.assertTrue(quotes_data["path"].endswith("quote.json"))

        slang_data = self._data(self._call(panel, "GET", "/api/knowledge/slang"))
        entry = slang_data["entries"][0]
        self.assertEqual((entry["group_id"], entry["word"]), ("717151356", "电赛"))
        self.assertEqual(entry["definition"], "电子设计大赛")   # 改口之后的那份
        self.assertEqual(entry["times"], 2)
        self.assertTrue(entry["wrong"])                        # 标错了
        self.assertEqual([row["quote"] for row in entry["explainers"]],
                         ["电赛就是电子设计竞赛", "我们说的电赛是电子设计大赛"])
        self.assertEqual([row["name"] for row in entry["explainers"]], ["甲", "乙"])
        self.assertEqual([(row["from"], row["to"]) for row in entry["revisions"]],
                         [("电子设计竞赛", "电子设计大赛")])

        # 写回去也落在真 store 上：恢复自动 → 回到模型学的那份。
        response = self._call(panel, "POST", "/api/knowledge/quotes",
                              {"confirm": True, "action": "correct",
                               "group_id": "717151356", "emoji_id": "76",
                               "sense": "auto"})
        self.assertEqual(response.status, 200)
        after = self._data(self._call(panel, "GET", "/api/knowledge/quotes?group_id=717151356"))
        self.assertEqual(after["meanings"][0]["sense"], "positive")
        self.assertFalse(after["meanings"][0]["manual"])

    def test_the_panel_never_reads_the_learned_files_itself(self) -> None:
        """`self._seam("learned")` **只在一处**取（`_learned_fn`），而且没有读那两份文件的代码。

        ⚠️ 判据落在**语法树**上，不是源码文本上：这一段的注释里就写着那两个文件名
        （"面板不许自己去读 `data/quote/profile.json`"），按文本扫会把注释也算成违规
        ——第一版就是这么写的，红得毫无道理。注释与 docstring 都不是"代码在做什么"。
        """

        source = pathlib.Path(
            importlib.import_module("qq_roleplay_bot.plugins.webui.webui_panel").__file__
        ).read_text(encoding="utf-8")
        self.assertEqual(source.count('self._seam("learned")'), 1,
                         "`learned` 接缝在多处被取——那说明某一页绕开了 `_learned_fn`")

        tree = ast.parse(source)
        # `QuoteStore` / `SlangStore` / `profile_path`：核心那两个 store 的名字。
        # 面板直接碰它们就等于绕开接缝自己读文件。
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for forbidden in ("QuoteStore", "SlangStore", "profile_path"):
            self.assertNotIn(forbidden, attributes | identifiers,
                             f"面板直接拿到了 {forbidden}——那一份只能走 `learned` 接缝")
        # 字符串常量（**跳过 docstring**）：不该出现这两个文件名。
        docstrings = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
        strings = [node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str)
                   and id(node) not in docstrings]
        for forbidden in ("quote/profile.json", "slang/profile.json"):
            hits = [text for text in strings if forbidden in text]
            self.assertEqual(hits, [],
                             f"面板里出现了她学来的那份文件名 {forbidden}：{hits}")


class SelfAndStorageTests(_PanelCase):
    """账号页「她自己是谁」与设备页「占用空间」两个读口（2026-10-06 两页返工）。

    两条边界都是用户当轮定的：

    * 她自己的号**只走既有的 `self_id` 接缝**（"昵称从已有的自己状态接缝取，不要为它
      新开内部通道"）——所以取不到就 `available: false`，**不编一个名字**；
    * 「占用空间」读的是**面板自己的 data/ 落点**（不是核心数据面），只数文件大小、
      不打开文件：这一条用一个真目录验字节数对得上。
    """

    @staticmethod
    def _ask(panel: Panel, path: str):
        return panel.handle("GET", path, b"",
                            {"accept": "application/json",
                             "authorization": "Bearer " + TOKEN}, "127.0.0.1")

    def test_self_comes_from_the_existing_seam_or_says_it_has_nothing(self) -> None:
        # 有接缝：原样给号。
        panel = Panel({"data_root": str(self.root), "self_id": lambda: "10001"},
                      WebAccess(mode=LOCAL, token=TOKEN))
        response = self._ask(panel, "/api/self")
        data = self.payload(response)["data"]
        self.assertEqual((response.status, data["available"], data["self_id"]),
                         (200, True, "10001"))
        # 没有接缝（或它回空）：available=false，**不编名字**。
        response = self.request("GET", "/api/self")
        data = self.payload(response)["data"]
        self.assertEqual((response.status, data), (200, {"available": False, "self_id": ""}))

    def test_storage_counts_what_is_really_there(self) -> None:
        (self.root / "logs").mkdir()
        (self.root / "logs" / "a.jsonl").write_bytes(b"x" * 100)
        (self.root / "logs" / "b.jsonl").write_bytes(b"y" * 50)
        (self.root / "runtime_state.json").write_text("{}", encoding="utf-8")
        data = self.payload(self.request("GET", "/api/storage"))["data"]
        self.assertTrue(data["available"])
        by_name = {row["name"]: row for row in data["entries"]}
        self.assertEqual(by_name["logs"]["bytes"], 150)
        self.assertEqual(by_name["logs"]["files"], 2)
        self.assertEqual(by_name["runtime_state.json"]["bytes"], 2)
        self.assertEqual(data["total_bytes"], 152)
        # 大的排前面（页面就按这个顺序铺）
        self.assertEqual([row["name"] for row in data["entries"]][0], "logs")

    def test_storage_on_a_missing_directory_is_not_an_error(self) -> None:
        panel = Panel({"data_root": str(self.root / "nope")},
                      WebAccess(mode=LOCAL, token=TOKEN))
        response = self._ask(panel, "/api/storage")
        self.assertEqual(response.status, 200)
        self.assertFalse(self.payload(response)["data"]["available"])

    def test_health_prefers_the_address_the_panel_really_bound(self) -> None:
        """设备页显示的是**实际**监听地址，不是配置里的值（2026-10-06 截图核对抓到：
        夹具听在 8799、配置写 8790——照配置显示就是一句假话）。"""

        panel = Panel({"data_root": str(self.root),
                       "panel_address": lambda: {"host": "127.0.0.1", "port": 43210}},
                      WebAccess(mode=LOCAL, token=TOKEN))
        data = self.payload(self._ask(panel, "/health"))["data"]
        self.assertEqual((data["panel_host"], data["panel_port"], data["panel_port_exact"]),
                         ("127.0.0.1", 43210, True))
        # 没有那个接缝时退回配置值，并**如实标出**它不是实测的。
        fallback = self.payload(self._ask(self.panel, "/health"))["data"]
        self.assertFalse(fallback["panel_port_exact"])
        self.assertIsInstance(fallback["panel_port"], int)


class PortProbeTests(unittest.TestCase):
    """`webui_data._port_in_use`：**bind 探法**（不产生半截连接，见那个函数的说明）。"""

    def test_a_listening_port_reads_as_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.assertTrue(webui_data._port_in_use("127.0.0.1", port))

    def test_a_free_port_reads_as_free(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(webui_data._port_in_use("127.0.0.1", port))


class PanelSeamImportTests(unittest.TestCase):
    """插件里**每一条 import 的目标都必须还在**。

    面板的 import 大多写在函数体里（点到了才执行），所以"核心把某个名字搬走"
    在启动时不会报错——只会在用户点到那一页时变成 500。这个扫描把每条
    `from ...x import y` 的目标解析一遍，正是上面两次断线的共同形状。
    """

    PKG = "qq_roleplay_bot"

    def _plugin_files(self):
        root = pathlib.Path(webui_data.__file__).resolve().parents[1]
        return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)

    def test_every_import_target_still_exists(self) -> None:
        src = pathlib.Path(webui_data.__file__).resolve().parents[3]
        missing: list[str] = []
        for path in self._plugin_files():
            current = path.resolve().relative_to(src).with_suffix("")
            parts = list(current.parts)
            if parts[-1] == "__init__":
                parts.pop()
            current_name = ".".join(parts)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue                      # 绝对 import（标准库那种）不相对本模块
                base = current_name.split(".")[: len(current_name.split(".")) - node.level]
                target = ".".join(base + (node.module.split(".") if node.module else []))
                if not target.startswith(self.PKG):
                    continue
                try:
                    module = importlib.import_module(target)
                except Exception as exc:          # noqa: BLE001
                    missing.append(f"{path.name}:{node.lineno} 模块导不进来 {target}（{exc}）")
                    continue
                for alias in node.names:
                    if hasattr(module, alias.name):
                        continue
                    try:                          # `from ... import 子模块` 取的是模块对象
                        importlib.import_module(f"{target}.{alias.name}")
                    except Exception:             # noqa: BLE001
                        missing.append(f"{path.name}:{node.lineno} 名字没了 {target}.{alias.name}")
        self.assertEqual(missing, [], "插件里引用了核心已经不存在的名字：" + "；".join(missing))


class LayoutIsCssDrivenTests(unittest.TestCase):
    """左栏宽度：**由 CSS 驱动、跨断点连续**。

    由来（2026-10-06 用户）："从**平板视图切换到大屏视图**时，左边栏会缩回去，变得很窄"。
    先说清**实测**的结论（无头 Edge + CDP，在**同一个文档**里改视口）：
    `document.querySelector(".rail").getAttribute("style")` 全程都是空串、`body.className`
    也是空串——**不是**残留内联宽度，也不是类没摘。真正的原因是 CSS 两档宽度不连续：
    中等档 `40%` 在 1099px 处约 425px，而宽屏档写的是 240px（Miuix 平板栏那个 token），
    于是越过 1100px 时栏体从 ~386px 掉到 240px。

    这里钉两条**能在离线套件里算出来**的判据（真机宽度只能靠截图/CDP，见提交信息里的数）：

    1. 宽屏档的固定宽度 ≥ 中等档在断点处的宽度（否则一过界就"缩回去"）；
    2. 布局不许由 JS 写内联尺寸驱动（那才会真的出现"跨断点残留"这一整类 bug）。
    """

    #: 中等档那条媒体查询的上界（`max-width: 1099px`）。
    BREAKPOINT_MAX = 1099

    @staticmethod
    def _asset(name: str) -> str:
        return resources.files("qq_roleplay_bot.plugins.webui.webui").joinpath(
            name).read_text(encoding="utf-8")

    def _body_padding(self, css: str) -> int:
        """`body` 的左右内边距——下面那条算术要用它（百分比针对内层宽度解析）。"""

        for block in re.findall(r"\nbody\s*\{([^}]*)\}", css):
            found = re.search(r"\bpadding:\s*(\d+)px", block)
            if found:
                return int(found.group(1))
        self.fail("app.css 里找不到 body 的 padding")

    def test_the_wide_tier_keeps_the_width_the_medium_tier_ends_with(self) -> None:
        css = self._asset("app.css")
        padding = self._body_padding(css)
        self.assertEqual(padding, 18,
                         "body 内边距变了：这条用例下面的算术（断点 − 2×内边距）要跟着改")

        wide = re.search(r"\.rail\s*\{([^}]*)\}", css)
        self.assertIsNotNone(wide, "找不到 .rail 的基础样式")
        fixed_px = re.search(r"flex:\s*0 0 (\d+)px", wide.group(1))
        self.assertIsNotNone(
            fixed_px,
            "宽屏档的 .rail 必须是**固定宽度**（flex: 0 0 <N>px）——写成百分比就不是"
            "'到达一定尺寸后固定宽度'了")

        medium = re.search(
            r"@media \(min-width: 700px\) and \(max-width: (\d+)px\)\s*\{(.*?)\n\}",
            css, re.S)
        self.assertIsNotNone(medium, "找不到中等档那条媒体查询")
        ratio = re.search(r"\.rail\s*\{[^}]*flex:\s*0 0 ([\d.]+)%", medium.group(2))
        self.assertIsNotNone(ratio, "中等档的 .rail 应当是 40% 那种比例宽度")

        at_breakpoint = float(ratio.group(1)) / 100 * (self.BREAKPOINT_MAX - 2 * padding)
        self.assertGreaterEqual(
            float(fixed_px.group(1)), at_breakpoint - 2.0,
            f"宽屏档 {fixed_px.group(1)}px 比中等档在断点处的 {at_breakpoint:.0f}px 窄："
            "窗口从平板宽度拉到大屏时左栏会**缩回去**（用户 2026-10-06 报的那个 bug）")

    def test_the_frontend_never_writes_inline_sizes(self) -> None:
        """`app.js` 里不该有 `.style.<属性> = …` 这种赋值。

        面板的布局全靠 class + CSS（换页签、切阅读态、切主题都是 `classList`）。
        一旦有人开始"量一下再写死宽度"，跨断点就会留下内联宽度——那正是这次被怀疑的机制
        （实测不是它，但它确实是**真正会出现那类 bug** 的写法）。这条用例把那条路堵上。
        """

        js = self._asset("app.js")
        writes = re.findall(r"\.style\.[A-Za-z]+\s*[^=]*=[^=]", js)
        self.assertEqual(writes, [], f"app.js 里出现了内联样式写入：{writes}")
        self.assertNotIn(".style.setProperty(", js, "app.js 里出现了内联样式写入")


if __name__ == "__main__":
    unittest.main()


class GroupPageTests(_PanelCase):
    """「群聊」那一页：列**她所在的全部群**，数据只经 `registry.ui.groups` 这一条接缝。

    用户 2026-10-09：*"群聊页，应当展示**云茹所在的所有群聊**并展示**群名**
    而不是群号"*。这一组钉两件事：

    * 列表**就是**那条接缝给的那些行（不是"开过对话的群"，也不是"有会话的群"）；
    * **不另开取数通道**——面板要是自己去调 `call_action` 列群，这里的替身当场报错。
    """

    def _install_runner(self) -> None:
        """`ui.groups` 是协程：面板靠装配点注入的 runner 跨线程跑它（同核心那套）。"""

        import asyncio

        from qq_roleplay_bot.plugins.webui import webui_panel

        before = webui_panel._ASYNC_RUNNER  # noqa: SLF001 - 测的就是这个注入点
        webui_panel.install_async_runner(lambda coro: asyncio.run(coro))
        self.addCleanup(webui_panel.install_async_runner, before)

    def test_the_list_is_exactly_what_the_seam_gives(self) -> None:
        called: list[str] = []

        async def groups():
            called.append("groups")
            return ({"id": "111", "name": "富哥之家", "dialogue_on": True, "memory_on": False},
                    {"id": "222", "name": "", "dialogue_on": False, "memory_on": True})

        def call_action(*args, **kwargs):
            raise AssertionError(f"面板不该调 action 来列群：{args} {kwargs}")

        self._install_runner()
        panel = Panel({"data_root": str(self.root), "ui_groups": groups,
                       "call_action": call_action},
                      WebAccess(mode=LOCAL, token=TOKEN))
        response = panel.handle("GET", "/api/groups", b"",
                               {"accept": "application/json",
                                "authorization": "Bearer " + TOKEN}, "127.0.0.1")

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(called, ["groups"], "群列表不是从 `ui.groups` 来的")
        data = body["data"]
        self.assertTrue(data["available"], "接缝给了数据，面板却说读不到")
        rows = data["rows"]
        self.assertEqual([row["id"] for row in rows], ["111", "222"],
                         "没把接缝给的那些群**全都**列出来（顺序也该照原样）")
        self.assertEqual(rows[0]["name"], "富哥之家")
        self.assertEqual(rows[1]["name"], "群 222", "空群名没有回落到「群 <号>」")
        self.assertTrue(rows[0]["dialogue_on"] and not rows[0]["memory_on"])
        self.assertTrue(rows[1]["memory_on"] and not rows[1]["dialogue_on"])

    def test_a_missing_seam_says_so_instead_of_guessing(self) -> None:
        """接缝没接上（老核心 / 装错了）→ 如实说读不到，**不猜、不 500**。"""

        response = self.request("GET", "/api/groups")
        self.assertEqual(response.status, 200, "读不到群列表不该是 5xx")
        data = self.payload(response)["data"]
        self.assertFalse(data["available"])
        self.assertEqual(data["rows"], [])


class GroupSeamWiringTests(unittest.TestCase):
    """装配点那一侧：`registry.ui.groups` 必须真的递给面板（名字也要对得上）。

    路由读的是 `self._seam("ui_groups")`，而这一条接缝是 `_panel_seams()` 从
    `registry.ui.groups` 里取的——两边任何一处改了名字，页面就永远显示"读不到群列表"，
    而且**不会有任何东西报错**（面板那侧取不到就当没启用）。所以在这里钉住。
    """

    def test_the_panel_gets_the_groups_seam_under_the_name_it_reads(self) -> None:
        here = pathlib.Path(__file__).resolve().parents[1]
        plugin = here / "src" / "qq_roleplay_bot" / "plugins" / "webui"
        text = (plugin / "wire.py").read_text(encoding="utf-8")
        self.assertRegex(
            text, r'seams\["ui_groups"\] = ui\.groups',
            "装配点没有把 `ui.groups` 递给面板（或者名字与路由读的不一样）")
        panel = (plugin / "webui_panel.py").read_text(encoding="utf-8")
        self.assertIn('self._seam("ui_groups")', panel, "路由读的接缝名字变了")


class GroupMemorySwitchTests(_PanelCase):
    """「记忆」页那颗**按群的记忆开关**：只经 `ui.set_group_memory` 这一条写接缝。

    用户 2026-10-09：*"记忆开关肯定要上面板啊，**在记忆页**"*。它与对话那颗
    （`POST /api/groups`）是**两条写路径**——所以这里拿一个"一被调用就报错"的
    `group_switch` 替身看着：谁要是把两件事混成一条路，当场红。
    """

    def _panel_with(self, seam, *, group_switch=None) -> Panel:
        def boom(*args, **kwargs):
            raise AssertionError(f"记忆那颗开关不该走对话那条路：{args} {kwargs}")

        seams = {"data_root": str(self.root), "set_group_memory": seam,
                 "group_switch": group_switch or boom,
                 "ui_groups": None}
        return Panel(seams, WebAccess(mode=LOCAL, token=TOKEN))

    def _post(self, panel: Panel, body: dict):
        response = panel.handle("POST", "/api/memory/groups",
                                json.dumps(body).encode("utf-8"),
                                {"accept": "application/json", "content-type": "application/json",
                                 "authorization": "Bearer " + TOKEN}, "127.0.0.1")
        return response, json.loads(response.body.decode("utf-8"))

    def test_the_route_calls_the_memory_seam_once_with_confirm(self) -> None:
        calls: list[tuple] = []

        def seam(group_id, enabled):
            calls.append((group_id, enabled))
            return True

        response, body = self._post(self._panel_with(seam),
                                    {"group_id": "123456", "enabled": False, "confirm": True})
        self.assertEqual(response.status, 200)
        self.assertEqual(calls, [("123456", False)], "没有（或者不止一次）调那条写接缝")
        self.assertTrue(body["data"]["ok"])
        self.assertTrue(body["data"]["changed"], "接缝说改了，返回的 changed 却是假")

    def test_idempotent_result_comes_back_as_changed_false(self) -> None:
        """本来就是这个状态 → 核心回 `False`，面板如实回 `changed: false` 并把话说清楚。"""

        response, body = self._post(self._panel_with(lambda group_id, enabled: False),
                                    {"group_id": "123456", "enabled": True, "confirm": True})
        self.assertEqual(response.status, 200)
        self.assertFalse(body["data"]["changed"])
        self.assertIn("本来就是这个状态", body["data"]["message"])

    def test_without_confirm_it_is_refused_and_nothing_is_written(self) -> None:
        calls: list[tuple] = []

        def seam(group_id, enabled):
            calls.append((group_id, enabled))
            return True

        response, body = self._post(self._panel_with(seam),
                                    {"group_id": "123456", "enabled": True})
        self.assertEqual(response.status, 400)
        self.assertEqual(body["error"], "needs_confirmation")
        self.assertEqual(calls, [], "没带 confirm 也把开关写下去了")

    def test_a_bad_group_id_is_refused(self) -> None:
        called: list[tuple] = []
        panel = self._panel_with(lambda group_id, enabled: called.append((group_id, enabled)))
        for bad in ("", "abc", "123"):
            response, body = self._post(panel, {"group_id": bad, "enabled": True,
                                                "confirm": True})
            self.assertEqual(response.status, 400, f"{bad!r} 居然过了")
            self.assertEqual(body["error"], "bad_request")
        self.assertEqual(called, [])

    def test_a_missing_seam_says_engine_unavailable(self) -> None:
        panel = Panel({"data_root": str(self.root)}, WebAccess(mode=LOCAL, token=TOKEN))
        response, body = self._post(panel, {"group_id": "123456", "enabled": True,
                                            "confirm": True})
        self.assertEqual(response.status, 503)
        self.assertEqual(body["error"], "engine_unavailable")


class RecordsDefaultStatusTests(_PanelCase):
    """用户 2026-10-09 看到"空正文的记录"：真库里 56 条空正文**全是 `status='deleted'`**
    （删除路径先把正文清空，物理行等 7 天保留期才清）——问题在**面板把非 active 的行
    也列出来了**。这一组用**真形状**的库钉住：

    * 默认（没提 `status`）→ **只有生效的那条**，计数也只数它；
    * 显式 `status=any` → 那一档仍然看得到历史（「记忆」页那个"全部状态"页签靠它）。
    """

    GROUP = "800000001"
    USER = "1001"

    def _write_tea(self, store, evidence: str = "e1") -> str:
        """写一条 `likes_tea` 进去，返回它的 id（`commit` 回的是操作对象，所以回库问）。"""

        from qq_roleplay_bot.memory_model import InboxEvent

        # 时间戳要用**现在**：太老的待处理材料 `claim` 不认领（认领窗口是活的）。
        store.append(InboxEvent(evidence, self.GROUP, self.USER, "user", "喜欢喝奶茶",
                                time.time()))
        batch = store.claim({self.GROUP}, lease_seconds=60)
        self.assertIsNotNone(batch, "事件应当能被认领成一批")
        # 这份 JSON 的形状照 `tests/test_memory_people.py::add_fact`（少一个字段就是
        # `invalid_operation`——`ttl_days` 与 `evidence_event_ids` 都在里面）。
        raw = ('{"operations":[{"op":"ADD","scope_type":"user_group","kind":"preference",'
               '"normalized_key":"likes_tea","content":"喜欢喝奶茶","confidence":0.9,'
               '"ttl_days":null,"evidence_event_ids":[' + f'"{evidence}"' + '],"targets":[]}]}')
        self.assertTrue(list(store.commit(batch, raw)), "没写进去")
        with store.connection() as db:
            found: list = []
            for table in ("memory_short", "memory_long"):
                found.extend(dict(row) for row in db.execute(
                    f"SELECT id, status FROM {table} WHERE normalized_key='likes_tea'"))
        # 第二次写的时候，同一个键上已经有一条（被上一段改成 deleted 的那条），
        # 所以要认**生效的**那一条——它才是我刚写进去的。
        active = [row for row in found if str(row["status"]) == "active"]
        self.assertTrue(active, "刚写进去的那条（生效的）没找到")
        return str(active[0]["id"])

    def _seed(self) -> str:
        """**同一个键**上两条：一条 deleted（**正文被清空**，用户看到的就是它）、
        一条 active——界面上只该看见后面那一条、计数也只数它。

        真库里的形状就是这样：删掉的那条先归档、正文置空，物理行等保留期到了才清；
        之后同一个键再学一次就是新的一条（生效的）。
        """

        from qq_roleplay_bot.memory_store import MemoryStore

        store = MemoryStore(self.root / "memory" / "memory.sqlite3")
        store.note_person(self.GROUP, self.USER, "乐乐")
        deleted = self._write_tea(store)
        # 照**删除路径的形状**把第一条变成"已删 + 正文清空"（两层的表都要动）。
        with store.connection() as db:
            for table in ("memory_short", "memory_long"):
                db.execute(f"UPDATE {table} SET content='', status='deleted' WHERE id=?",
                           (deleted,))
        # 同一个键再写一条（现在生效的是它）。证据 id 必须换一个：
        # 同一个 id 的事件已经被上一批消费掉了，再来一次没有可认领的东西。
        self._write_tea(store, evidence="e2")
        return deleted

    def test_the_default_view_leaves_the_deleted_ones_out(self) -> None:
        deleted = self._seed()
        data = self.payload(self.request("GET", "/api/memory/records?limit=50"))["data"]
        self.assertEqual(data["total"], 1, "计数把已删的也算进去了")
        self.assertEqual([row["id"] for row in data["rows"]], [
            row["id"] for row in data["rows"] if row["id"] != deleted])
        self.assertEqual(len(data["rows"]), 1, "已删的那条还在列表里")
        self.assertEqual(data["rows"][0]["status"], "active")
        self.assertTrue(data["rows"][0]["content"], "留下那条不该是空正文")

    def test_the_explicit_any_sentinel_still_shows_everything(self) -> None:
        """要历史的地方（「记忆」页"全部状态"）**显式**给哨兵——那一档不能也看不到。"""

        deleted = self._seed()
        data = self.payload(
            self.request("GET", "/api/memory/records?limit=50&status=any"))["data"]
        self.assertEqual(data["total"], 2, "显式要全部状态时反而看不到了")
        by_id = {row["id"]: row for row in data["rows"]}
        self.assertIn(deleted, by_id)
        self.assertEqual(by_id[deleted]["content"], "",
                         "那条本来就是空正文——用户看到的就是它")

    def test_an_explicit_active_wins_over_the_default(self) -> None:
        self._seed()
        data = self.payload(
            self.request("GET", "/api/memory/records?limit=50&status=active"))["data"]
        self.assertEqual(data["total"], 1)
        self.assertTrue(all(row["status"] == "active" for row in data["rows"]))


class RecordsShapeForGroupChipsTests(_PanelCase):
    """② 的**数据侧**前提：接口得把"这条记录落在哪种 scope"给出来，前端才判得出是不是群
    （`scope_type` + `scope_key`）。用户截到的「群 1613078653」就是 `user_global`
    那种个人 scope——它的 `scope_key` 是**人的号**。
    """

    def test_the_api_exposes_the_scope_so_the_page_can_tell_a_group_from_a_person(self) -> None:
        from qq_roleplay_bot.memory_model import InboxEvent
        from qq_roleplay_bot.memory_store import MemoryStore

        store = MemoryStore(self.root / "memory" / "memory.sqlite3")
        store.note_person("800000001", "1001", "乐乐")
        # 一条**群内个人** scope（`<群号>:<人号>`），一条**个人全局** scope（`scope_key` 是人号）。
        for evidence, scope, key in (("e1", "user_group", "likes_tea"),
                                     ("e2", "user_global", "likes_coffee")):
            store.append(InboxEvent(evidence, "800000001", "1001", "user", "记一笔", time.time()))
            batch = store.claim({"800000001"}, lease_seconds=60)
            self.assertIsNotNone(batch, "事件应当能被认领成一批")
            raw = ('{"operations":[{"op":"ADD","scope_type":"%s","kind":"preference",'
                   '"normalized_key":"%s","content":"记一笔","confidence":0.9,'
                   '"ttl_days":null,"evidence_event_ids":["%s"],"targets":[]}]}'
                   % (scope, key, evidence))
            self.assertTrue(list(store.commit(batch, raw)), "没写进去")

        data = self.payload(self.request("GET", "/api/memory/records?limit=50"))["data"]
        scopes = sorted((str(row["scope_type"]), str(row["scope_key"])) for row in data["rows"])
        self.assertEqual(scopes, [("user_global", "1001"), ("user_group", "800000001:1001")],
                         "接口没把 scope 原样给出来（前端就没法区分群与人）")
