r"""门槛：**插件线上只许有插件**（"插件线只管插件"的机械检查）。

## 为什么要有它

用户的边界（原话）：*"插件线只管插件，不要塞核心和 stage3"*。

实测过一次（2026-10-08 的范围清理）：插件线自 `6262def` 起改过 **55** 个文件，
其中 4 个不属于插件——`src/qq_roleplay_bot/dev_config.py`（**核心**配置）、
`docs/STAGE3_PENDING_DESIGNS.md`（**Stage 3** 文档）、以及两个 `git commit -F` 用的
临时消息文件（`.commit_msg_plugin_relimports.txt` / `.commit_msg_plugin_selftest.txt`）。
它们**已经提交在插件线历史里**了，靠"下次注意"拦不住——所以写成门槛：
**每一次往这条线上提交，都有一道机械检查看着。**

## 判据

在**插件树**上比对 `main...HEAD`（三点 = 从两条线的分叉点算起，**只看插件线自己改了什么**），
再加上工作区（暂存 / 未暂存 / 未跟踪）。改动的文件只允许落在两处：

1. `src/qq_roleplay_bot/plugins/**`——插件包（含 `plugins/__init__.py` 那套发现器与注册表，
   它是插件侧的装配面，不是 Stage 3 的核心模块）；
2. 下面 `WHITELIST` 里**逐个列明**的测试文件——每一项都在字典里写清"为什么它算插件范围"。

出现别的文件 → **退出码 1，并把那些文件一个个指名**。

## 为什么用三点，不用两点

`git diff main..HEAD`（两点）是"两棵树的差异"，**本体前进会混进来**。实测：
本体侧的 `5d624c6` / `bc5517c` 动了 `data_boundary.py` / `dialogue_judge.py` 与 4 个测试，
于是两点的清单是 **61** 个文件、三点的清单是 **55** 个。这道门槛问的是
"**插件线自己塞了什么**"，所以用三点。rebase 到 `main` 之后两种写法结果相同，
但**三点在 rebase 之前也照样给对答案**（这道门槛不该因为"还没 rebase"就失灵）。

## 工作区里的东西也算

判据不只看提交：**正改着的东西**同样算（暂存 + 未暂存两列都看）。
不然"往插件线塞一个核心文件"只要**不提交**就能绕过这道门槛，而那一瞬间它已经在那儿了。

**未跟踪**的文件只看 `src/`、`tests/`、`docs/` 三个前缀下的：仓库根目录本来就躺着
未跟踪的既有产物（`yunru-source/`），把它算进来会让这道门槛在**没做错任何事**的树上变红，
那是假警报。**假警报会让门槛被关掉**，比漏报更糟。

## 这棵树探不了的时候：显式说明 + 退出 0

照 `tests/check_module_removal.py` 的先例：**本体树（`dev`）上探不了**——那棵树不是插件线
（`plugins/` 下只有 `__init__.py`，没有插件文件夹），压根没有"插件线塞了什么"这回事。
这时**显式打印为什么探不了**，然后**退出码 0**。

⚠️ 这里是"**不适用**"，不是"通过"——所以那一段措辞里**不许出现“通过”两个字**。
把不适用说成通过，就是 `check_module_removal.py` 里记的那个教训的翻版
（名单里的一项查不了却照样打印"全部通过：7 个"）。

同理：`main` 这条分支不存在、git 用不了、`git diff` 有一条没跑成——全都是"探不了"，
一律**显式说明 + 退出 0**，**不**退到"没看见违规就算过"。

用法（在**插件线**的仓库根目录）：

    .\\.venv\\Scripts\\python.exe tests\\check_plugin_branch_scope.py

**不吃参数**：判据是这棵树的 git 状态，不是命令行。给了参数就等于留了一条绕过判据的路。

退出码：插件线且只有插件范围内的文件 → 0；出现白名单之外的文件 → 1；
探不了（本体树 / 没有 `main` / 没有 git）→ 0，并显式说明"不适用"。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG_PLUGINS = ROOT / "src" / "qq_roleplay_bot" / "plugins"

#: 比对基准那条线（本体）。
BASE_BRANCH = "main"
#: 插件线那条线的分支名（"这棵树是不是插件线"的第一判据）。
PLUGIN_LINE_BRANCH = "plugins"

#: 允许出现在插件线上的**源码**前缀。只有插件包——核心（`src/qq_roleplay_bot/*.py`）
#: 与本体文档都在这个前缀之外。
ALLOWED_PREFIXES = ("src/qq_roleplay_bot/plugins/",)

#: 未跟踪文件里要看的目录前缀（根目录下那些既有产物不算，理由见模块头那段）。
UNTRACKED_ROOTS = ("src/", "tests/", "docs/")

#: 允许出现在插件线上的**测试**：路径 → 为什么它算插件范围。
#:
#: 口径（用户 2026-10-08）：只留"**因为插件存在而必须适配**"的那部分——
#: 清单口径、装配、`check_module_removal` 的插件侧、面板 / 布局。
#:
#: 这些文件**必须逐个列明**，不许写成 `tests/**` 这样的通配：通配等于把门槛放开到
#: "测试随便改"，而这次清理抓到的 4 个越界文件里就有 2 个是"顺手加进去"的临时文件。
#: 加一项之前先问自己：**这个文件在本体树上跑得起来吗？**
#: （下面每一项都答了这个问题——答不上来的就不该进来。）
WHITELIST = {
    "tests/check_plugin_branch_scope.py":
        "这道门槛自己。它是**插件线**的范围护栏（本体侧没有可核的范围，脚本在那棵树上"
        "显式说明并退出 0），所以它属于这条线。",
    "tests/test_background_plugins.py":
        "后台插件那一半（入群审批 / 读信回信 / 每日汇报）：节拍只有一份、装配只有一个"
        "入口、插件拿不到 transport。它在模块顶层 import `plugins.join_approval.*`，"
        "本体树没有插件文件夹 → 放在本体侧根本收集不起来。",
    "tests/test_group_action_execution.py":
        "群管理动作的**执行端**：真装配 + 假传输层，逐字段核对到底发出去了什么 action。"
        "要 `group_admin` 插件在场才谈得上“执行端”，本体侧没有它。",
    "tests/test_mail_channel.py":
        "读信回信插件（`plugins/mail/`）的只认主人 / 幂等 / 旧信 / 上限 / 不回自己。"
        "整份都在测这个插件。",
    "tests/test_mail_client_errors.py":
        "邮件客户端（`plugins/mail/mail_client.py`）的错误面：连不上 SMTP、认证失败、"
        "收件人被拒之后**不许**把半截状态留在 `mail_state` 里。它第 28 行 `from "
        "qq_roleplay_bot.plugins.mail.mail_client import …`、第 201 行还直接读插件包源码"
        "文本——本体侧没有 `plugins/mail/`，它在那边根本收集不起来。",
    "tests/test_mail_letter_agent.py":
        "写信 agent 自己的那一半（`plugins/mail/letter_writer.py`）：人格与事实表由核心"
        "按次递进来、旧事她自己翻（`<lookup>`，翻到上限就收手）、`<skip>` 就是今晚不写。"
        "整份 import `plugins.mail.letter_writer` / `plugins.mail.daily_report`，还直接读"
        "插件包源码文本核对“不再自己找人格”——本体侧没有这些模块，搬过去只会收集失败。",
    "tests/test_mail_letter_grounding.py":
        "写信那条路的**判据**那一半（2026-10-16 用户：「材料里没有的不许写」）："
        "`letter_writer.parse_verdicts` / `apply_verdicts`（逐句编号、模型只标 "
        "`fact`/`self`/`none`、**删句由代码按标签做**、引话要有出处、删得太多就不发这一封）、"
        "每步一条 `mail.jsonl`、以及素材里那几样结构怎么"
        "渲染（`daily_report._his_marks` / `_his_lines_rendered`）。整份 import "
        "`plugins.mail.letter_writer` / `plugins.mail.daily_report`，还直接调 "
        "`DailyReporter._log_letter`——本体侧没有 `plugins/mail/`，在那边根本收集不起来。"
        "（**结构从哪来**那一半是核心的 `letter_context.py`，不在这一份里："
        "已落到本体线上，这一份只钉插件侧的接口。）",
    "tests/test_mail_subject.py":
        "主题寄不出去那件事的两层修法（2026-10-16 用户批的「两层都做」）："
        "`mail_client.sanitize_subject`（发送前把不能进 argv 的字符换成全角、引号配成全角引号）"
        "+ `daily_report._safe_subject`（清完为空就给 `DEFAULT_SUBJECT`），"
        "位置在 `clean_subject` 那道判**之前**；回归样本是 23:01 那封"
        "『…和那句「你别掺和」』（`mail_state.json` 里三条 `bad_subject`）。"
        "那三条判据（能寄出 / 空主题给兜底 / 干净主题一字不改）走的都是插件里的函数"
        "（`clean_subject` 在 `plugins/mail/mail_client.py`），本体侧没有 `plugins/mail/`。",
    "tests/test_mail_report_window.py":        "日报那条接缝（`ReportSeams.facts` / `window_facts`：「这一窗里群里发生了什么」）"
        "从核心递到插件、再落进日报正文的过程。第 37-39 行 import `plugins` / "
        "`plugins.mail.daily_report` / `plugins.mail.mail_state`——同上，本体侧没有那三个"
        "模块，搬过去只会收集失败。",
    "tests/test_mail_send_to.py":
        "邮件接缝上**第二条窄口**（`MailSeams.send_to`：「寄给指定的一个地址」）的覆盖，"
        "连「旧那条（发给操作者）不许变」一起钉住。整份都在测插件包里的那条口子——"
        "本体侧没有它，在那边根本收集不起来。",
    "tests/test_media_segments_plugins.py":
        "从本体 `tests/test_media_segments.py` 拆出来的三条：它们要真造一个识图器"
        "（`ImageDescriber` 在 `plugins/vision/`）。本体侧的正常形态是插件不在，"
        "所以那三条**在那个树上根本不该存在**（不是“存在但跳过”）。",
    "tests/test_optional_capabilities.py":
        "**本体侧那一份**，只改了两处——“怎么造识图缺席”：判据在本体那边改成了"
        "“插件**登记过**这一套 prompt 没有”（`prompt_library` 读注册表），于是光藏"
        "子模块 `vision.vision` 不再等于缺席；新写法要藏**整个插件包**再装配一次，"
        "**只有插件树上造得出来**。断言一字未改。",
    "tests/test_optional_capabilities_plugins.py":
        "从本体那份拆出来的“要真 import 插件 `plugin.py`”那一条（拿 `vision_plugin`"
        "验“能力经由注册表注入”）。本体侧的正常形态是插件不在。",
    "tests/test_outage_notice.py":
        "掉线通知插件（用户那半边）：端到端——用真实 `discover()` 装上插件、喂一次掉线"
        "事件、断言信真的被发了（且只发一封）。",
    "tests/test_plugin_install_root.py":
        "**本体侧那一份**，改的是“第二个根”那几条用例的**断言口径**：本体树 0 个插件、"
        "插件树 6 个，原来比的是**整个集合**（`== (“probe_good”,)`），于是只有插件树会红；"
        "新写法只钉“我写进临时根的那个在结果里”。断言的**强度没降**——降强度那部分"
        "（坏插件逐个不在、键集合逐个在）反而补齐了。",
    "tests/test_plugin_inventory.py":
        "**本体侧那一份**，插件线在它后面追加了整段“面板读的那份清单”的离线覆盖"
        "（`plugins.inventory()`：11 键口径、什么算预装、只有 webui 带 panel 标记、"
        "缺依赖的插件不许隐身、`inventory()` 不改装配记录）。这些断言句句要有插件在场。",
    "tests/test_plugin_self_imports.py":
        "钉“插件不许绝对自引用”（改名即隐身）+ 一条改名实测。它扫的就是 `plugins/*/` 下"
        "每个插件的源码，本体侧没有可扫的东西。",
    "tests/test_prompt_library_plugins.py":
        "六套 prompt 里第六套（`vision`）从 2026-10-05 起由**插件**在 `register()` 里"
        "`provide_prompt(...)` 登记，所以“六套都有原稿”那两条必须先 `discover()`。"
        "本体侧那六套不全，那两条在那里根本不该存在。",
    "tests/test_remind_plugin.py":
        "到点提醒插件（`plugins/remind/`）那一半：三个选择只问一次、没会话时那句人话、"
        "到点只对到期的发、邮件走邮件插件的能力，外加“插件拿不到记忆 API”那条钉子。"
        "整份都在测这个插件、装配走的是真的 `discover()`——本体侧没有那个插件文件夹，"
        "它在那边根本收集不起来。",
    "tests/test_rss_plugin.py":
        "RSS/Atom 订阅插件（`plugins/rss/`）那一半：只用标准库抓取与解析、10 秒 / 2MB"
        "护栏、首次订阅只建基线（绝不推历史积压）、每源每轮与每小时额度、连续失败降频、"
        "群走 `notify` 邮件走 `registry.mail`（缺一路不许弄坏另一路），以及“外部正文投递前"
        "必须洗掉 CQ 码/@/控制字符、截断 200 字、链接只留 http(s)”。整份 import "
        "`plugins.rss.*`，本体侧没有这个插件文件夹，它在那边根本收集不起来。",
    "tests/test_runtime_assembly.py":
        "**装配**的接线：核心造的那份角色事实服务与插件共用**同一个**对象、入群审批插件"
        "取到的就是它（“她是不是群主”不许有两个真相）。要插件在场才验得到“共用一份”。",
    "tests/test_stage4_plugins.py":
        "Stage 4 的命令走插件：档位声明 + 动作意图，判定与执行留在核心。整份都在测"
        "`plugins/group_admin/` 那族命令与它们的边界。",
    "tests/test_timeout_alert.py":
        "超时通知插件（`plugins/timeout_alert/`；用户 2026-10-12：“还有就是短时间内频繁"
        "超时则发送邮件通知我”）那一半：窗口 / 门槛 / 降温各是多少、快照读不出来怎么办、"
        "措辞是不是她那口气（不调模型、不带机制词），外加“发信失败只记一行”与“删掉整个"
        "插件包核心与 mail 照跑”。它在模块顶层 import `plugins.timeout_alert.*`，装配走的是"
        "真的 `discover()`——本体侧没有那个插件文件夹，它在那边根本收集不起来。",
    "tests/test_vision.py":
        "识图插件：`describe` 的缓存 / 降级、占位符替换、以及引擎里的接线。"
        "`ImageDescriber` 已从包根搬进 `plugins/vision/`，本体侧没有这个模块。",
    "tests/test_webui_layout_assertions.py":
        "面板（`plugins/webui/`）左栏布局与“同一张卡下面的项是平级的”的**静态断言**："
        "读的就是插件包里的 `app.css` / `app.js` / `index.html`。",
    "tests/test_webui_panel.py":
        "面板的路由层 + 接缝 + 授权（不起线程、不连网）：测的是 `plugins/webui/` 的"
        "`webui_data` / `webui_access` / `webui_panel`。",
    "tests/test_webui_v2_static.py":
        "面板（`plugins/webui/`）的 `/v2/` 静态路由（2026-10-18 用户批准的唯一一处后端改动）："
        "扩展名白名单、二进制按 `read_bytes`、`resolve()` 之后的防穿越、`index.html` 的"
        "`no-store` 与 `.wasm`/字体的缓存策略、以及**改代码前**抓下来的老外壳基线逐字比对。"
        "它测的对象全在 `plugins/webui/webui_panel.py` 与 `plugins/webui/webui/v2/` 里，"
        "本体侧没有这两个东西。",
}


def _git(*args: str) -> tuple[int, str, str]:
    """跑一条**只读**的 git 命令：`(退出码, stdout, stderr)`。跑不起来就退出码 127。"""

    try:
        proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              errors="replace", timeout=60)
    except Exception as exc:  # noqa: BLE001 - 拿不到就当"探不了"，由调用方如实说
        return 127, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _lines(*args: str) -> list[str] | None:
    """一条 git 查询的逐行结果；命令失败时返回 `None`（**不许**当成"空"）。"""

    code, out, _err = _git(*args)
    if code != 0:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def _text(*args: str) -> str | None:
    """一条 git 查询的单行结果；命令失败时返回 `None`。"""

    code, out, _err = _git(*args)
    return out.strip() if code == 0 else None


def _has_plugin_folders() -> bool:
    """这棵树上有没有**真的**插件文件夹（判据用 git 跟踪的文件，理由同 check_module_removal）。

    光看目录会出错：`plugins/__pycache__/` 会让"有插件"成真。所以问 git；git 问不了才退回
    磁盘形状。拿不准时它宁可说"有插件"（那会让本脚本**真的去查**，而不是把树判成本体、
    从而什么都不查就退出 0）。
    """

    tracked = _lines("ls-files", "--", "src/qq_roleplay_bot/plugins")
    if tracked is not None:
        return any(not path.endswith("/plugins/__init__.py") for path in tracked)
    if not PKG_PLUGINS.is_dir():
        return False
    return any(child.is_dir() and (child / "plugin.py").is_file()
               for child in PKG_PLUGINS.iterdir())


def _is_plugin_line() -> tuple[bool, str]:
    """这棵树是不是**插件线**：`(是不是, 依据)`。

    分支名是第一判据（插件线就是 `plugins`），形状是第二判据（`plugins/` 下有被跟踪的
    插件文件夹）。**两个判据都用**：前者在 detached HEAD（rebase 中途）时会失灵，
    后者在"插件文件夹被整个移走"时会失灵，而这两种情况都会让门槛**悄悄放行**。
    """

    branch = _text("rev-parse", "--abbrev-ref", "HEAD") or "(问不出来)"
    if branch == PLUGIN_LINE_BRANCH:
        return True, f"当前分支就是 `{PLUGIN_LINE_BRANCH}`"
    if _has_plugin_folders():
        return True, ("`plugins/` 下有被 git 跟踪的插件文件夹"
                      "（分支名不是判据，形状才是）")
    return False, (f"当前分支是 `{branch}`，而 `plugins/` 下除 `__init__.py` 之外没有"
                   f"被跟踪的插件文件夹——这是**本体树**的正常形状")


def _changed() -> dict[str, list[str]] | None:
    """这棵树上"改了"的文件 → 它出现的地方（提交 / 已暂存 / 工作区 / 未跟踪）。

    `None` = 有一条 git 查询没跑成。**不许**把它当成"没有违规"（见模块头）。
    """

    found: dict[str, list[str]] = {}

    def add(label: str, paths: list[str] | None) -> None:
        for path in paths or ():
            found.setdefault(path, [])
            if label not in found[path]:
                found[path].append(label)

    committed = _lines("diff", "--name-only", f"{BASE_BRANCH}...HEAD")
    staged = _lines("diff", "--name-only", "--cached")
    worktree = _lines("diff", "--name-only")
    untracked = _lines("ls-files", "--others", "--exclude-standard")
    if committed is None or staged is None or worktree is None or untracked is None:
        return None
    add(f"{BASE_BRANCH}...HEAD", committed)
    add("已暂存", staged)
    add("工作区", worktree)
    add("未跟踪", [path for path in untracked if path.startswith(UNTRACKED_ROOTS)])
    return found


def _cannot_probe(why: str) -> int:
    """探不了时**唯一**允许的出口：显式说明 + 退出 0（措辞里不许出现“通过”）。"""

    print("=" * 96)
    print("插件线范围检查：**不适用**（这棵树探不了）")
    print("=" * 96)
    print(why)
    print(f"本脚本问的是“插件线自己往 `{BASE_BRANCH}...HEAD` 里塞了什么”。")
    print("探不了时它**退出 0 并且不称自己通过**——不适用不是通过。"
          "（先例：tests/check_module_removal.py 处理本体侧那条。）")
    return 0


def main() -> int:
    if _lines("rev-parse", "--is-inside-work-tree") is None:
        return _cannot_probe("这里不是 git 工作树（或者这台机器上 git 用不了）。")

    on_plugin_line, why = _is_plugin_line()
    if not on_plugin_line:
        print("=" * 96)
        print("插件线范围检查：**不适用**（这棵树不是插件线）")
        print("=" * 96)
        print(f"依据：{why}")
        print(f"`{BASE_BRANCH}` 就是这棵树自己，没有“插件线塞了什么”这回事。")
        print("所以这里**退出 0**，并且**不打印“通过”**：不适用不是通过。")
        print("（照 tests/check_module_removal.py 处理本体侧那条的先例。）")
        return 0

    if _text("rev-parse", "--verify", f"refs/heads/{BASE_BRANCH}") is None:
        return _cannot_probe(f"这棵树里没有 `{BASE_BRANCH}` 这条分支，没有可比对的基准。")

    changed = _changed()
    if changed is None:
        return _cannot_probe("`git diff` / `git ls-files` 有一条没跑成，"
                             "没法确定这棵树上到底改了什么。")

    branch = _text("rev-parse", "--abbrev-ref", "HEAD") or "(detached)"
    plugins_files = sorted(p for p in changed if p.startswith(ALLOWED_PREFIXES))
    whitelisted = sorted(p for p in changed
                         if not p.startswith(ALLOWED_PREFIXES) and p in WHITELIST)
    violations = sorted(p for p in changed
                        if not p.startswith(ALLOWED_PREFIXES) and p not in WHITELIST)
    unused = sorted(p for p in WHITELIST if p not in changed)

    print("=" * 96)
    print(f"插件线范围检查：`{BASE_BRANCH}...HEAD` + 工作区"
          f"（这棵树：{branch}；依据：{why}）")
    print("=" * 96)
    print(f"允许 · 插件包 `src/qq_roleplay_bot/plugins/**`（{len(plugins_files)} 个）")
    for path in plugins_files:
        print(f"  {path}")
        print(f"      ← {', '.join(changed[path])}")
    print(f"允许 · 白名单测试（{len(whitelisted)} 个）")
    for path in whitelisted:
        print(f"  {path}")
        print(f"      ← {', '.join(changed[path])}")
        print(f"      为什么算插件范围：{WHITELIST[path]}")
    print("-" * 96)

    if violations:
        print(f"**白名单之外的文件（{len(violations)} 个）**——插件线不该有它们：")
        for path in violations:
            print(f"  {path}")
            print(f"      ← {', '.join(changed[path])}")
        print("  这些属于**本体**（核心 / Stage 3 / 本体文档 / 临时文件），"
              "不该藏在插件线上。")
        print("  两条路：把它移到本体那条线上；或者——**如果它真的是插件范围**——"
              "在 WHITELIST 里写明理由再加进来。")
        print("  **不要**为了让门槛变绿而把它改成通配（那等于把门槛关掉）。")
        print(f"不通过：{len(violations)} 个文件落在范围之外。")
        return 1

    if not changed:
        return _cannot_probe(f"这棵树上 `{BASE_BRANCH}...HEAD` 与工作区**都没有改动**"
                             "——没有可核的范围。")

    if unused:
        print(f"白名单里有 {len(unused)} 项在这条线的改动里**没出现**"
              "（显式列出来，不静默）：")
        for path in unused:
            print(f"  {path}    （{WHITELIST[path]}）")
        print("  没出现**不算失败**：判据是“有没有白名单之外的文件被改”。写成失败会让"
              "“插件测试被整份删掉”这件事把门槛变红，那不是这道门槛要问的问题。"
              "但它要显式说出来，别让人以为白名单每一项都在被用。")
        print("-" * 96)

    tail = (f"范围内的文件 {len(plugins_files) + len(whitelisted)} 个"
            f"（插件包 {len(plugins_files)} + 白名单测试 {len(whitelisted)}）")
    if unused:
        tail += f"，另有 {len(unused)} 个白名单项在这条线上没出现"
    print(f"通过：{tail}，没有一个文件落在范围之外。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print("（本脚本不吃参数——判据是这棵树的 git 状态，给的参数会被忽略。）")
    raise SystemExit(main())
