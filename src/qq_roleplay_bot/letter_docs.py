"""写信那条路**自己**用的两份文档：信人格（`LETTER_PERSONA`）与《已确定的事》（`LETTER_FACTS`）。

由来（2026-10-12 用户，原话）：

    我不理解为什么到处都是重启就出问题的。写信的人格prompt做单独适配。注意写信agent
    本身是插件里的，所以凭啥这玩意能直接复用人格prompt啊。

在那之前，写信这条路是 `plugins/mail/daily_report.py:24 from ...base_prompt import
BASE_PROMPT`——**群聊那份人格正文，写信时逐字复用**；事实表则由插件自己去算
`Path(__file__).resolve().parents[4]` 找（2026-10-12 实测：路径差一层，线上一直在用模板）。

## 为什么这两份文档放在**核心**、由接缝递过去

- 人格正文是可信 system 前缀的一部分，属于 Stage 3（`AGENTS.md` §2.2）；
- `data/private_docs/` 是核心的私有目录：插件自己数 `parents[n]` 迟早会数错
  （上面那次就是这么发生的），而它连"数错了"都看不出来——回落到模板是静默的；
- 裁剪、告警、机制词提醒只有一份实现，不会两处漂移。

## 为什么**每次现读**，不是像 `base_prompt` 那样 import 时定一次

`base_prompt.BASE_PROMPT` 是模块级常量：进程起来那一刻定死。那之后人格文件被改、
被换、被移走，**这个进程不知道**——2026-10-08~10-10 就是如此：真人格文件里含机制词，
当时加载器"命中就整套跳过"，她连写了三个晚上的模板人格，而那条 WARNING 躺在
`data/bot.err.log` 里没人看。写信一天只发生一次，读一个几十 KB 的文件可以忽略，
所以这里**不给缓存**：写之前现读，读到什么就是什么。

## 读不到就不写（fail-closed），**不回落模板**

两份文档都返回 `None`，由调用方按"今晚这封信写不成"处理。理由：模板不是她
（`base_prompt` 那份模板第一句就写着"这一份是**人格模板**"），拿它顶上等于
**用一份假人格替她写了一封信寄给主人**——而这封信的全部价值就在于"这是她写的"。
这与 `base_prompt` 的选择不同，因为那边回落到模板只是"人格糙一点"，
而这边是"寄一封不是她写的信出去"。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

#: 信人格文件的正文常量名（与 `base_prompt.BASE_PROMPT` 同款写法）。
PERSONA_CONSTANT = "LETTER_PERSONA"
#: 事实表的正文常量名。
FACTS_CONSTANT = "LETTER_FACTS"

#: 单份文档的长度上限：**防御性**的。真人格正文撑死几千字，这里留足空间；
#: 超过就截断并出声——总比把一份 20 万字的文件塞进每一封信的 system 前缀好。
MAX_DOC_CHARS = 20000


def _private_docs() -> Path:
    """`data/private_docs/`——部署根下那一份（`data/` 不进仓库）。"""

    return Path(__file__).resolve().parents[2] / "data" / "private_docs"


def persona_candidates() -> tuple[Path, ...]:
    """信人格按这个顺序找：

    1. `QQBOT_LETTER_PERSONA_FILE` 指到的文件；
    2. `data/private_docs/letter_persona.REAL.py`（本地副本，推荐写法）；
    3. `data/private_docs/letter_persona.REAL.txt`（不想写成模块就写裸文本）。
    """

    paths: list[Path] = []
    explicit = os.environ.get("QQBOT_LETTER_PERSONA_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    base = _private_docs()
    paths.append(base / "letter_persona.REAL.py")
    paths.append(base / "letter_persona.REAL.txt")
    return tuple(paths)


def facts_candidates() -> tuple[Path, ...]:
    """《已确定的事》按这个顺序找（`QQBOT_LETTER_FACTS_FILE` 优先）。"""

    paths: list[Path] = []
    explicit = os.environ.get("QQBOT_LETTER_FACTS_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit))
    base = _private_docs()
    paths.append(base / "letter_facts.REAL.py")
    paths.append(base / "letter_facts.REAL.txt")
    return tuple(paths)


def _body_of(text: str, constant: str) -> str:
    """从文件正文里取出要的那一段。

    两种形状都认（与 `base_prompt._load_base_prompt` 一致）：

    - 模块形状：`LETTER_PERSONA = \"\"\"正文\"\"\"` → 只取三引号里那一段；
    - 裸文本：整份就是正文。

    首尾空白与**行续行反斜杠**都裁掉：`LETTER_PERSONA = \"\"\"\\` 换行这种写法很常见，
    那个 `\\` 和换行会原样进 system 前缀的第一行（`base_prompt` 那边 2026-10-11 修过同款）。
    """

    found = re.search(rf'{constant}\s*=\s*"""(.*?)"""', text, re.S)
    raw = found.group(1) if found else text
    return raw.strip().lstrip("\\").strip()


def _load(paths: tuple[Path, ...], constant: str, *, what: str, who: str) -> str | None:
    """按 `paths` 顺序找第一份能读的；一份都没有就出声并返回 None。"""

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        body = _body_of(text, constant)
        if not body:
            continue
        if len(body) > MAX_DOC_CHARS:
            logger.warning(
                "%s超过了 %s 字符上限，已截断（file=%s）。写这么长多半是放错了文件。",
                what, MAX_DOC_CHARS, path,
            )
            body = body[:MAX_DOC_CHARS]
        return body
    logger.warning(
        "%s读不到：%s 都不存在或读不出正文（试过 %s）。"
        "**今晚这封信不写**——这里不回落到模板：拿一份模板人格顶上去，"
        "等于用不是她的字迹给她主人写信。",
        what, who, "、".join(str(p) for p in paths) or "（一个候选路径都没有）",
    )
    return None


def load_letter_persona() -> str | None:
    """这一封信要用的人格正文；读不到就是 `None`（调用方据此不写这封信）。

    **每次调用都重新读文件**——理由见模块开头（进程内缓存过一次，代价是
    文件换了之后连着三个晚上没人知道）。
    """

    body = _load(persona_candidates(), PERSONA_CONSTANT,
                 what="信人格正文", who="data/private_docs/letter_persona.REAL.py")
    if body is None:
        return None
    # 机制词只提醒、不拦截（与 `base_prompt` 2026-10-11 的修正一致）：
    # "命中就拒整份文件"曾把用户人格里的**禁令**（"不许提协议"这种）误杀成违规。
    from .prompt_guard import scan_persona_text

    hits = scan_persona_text(body)
    if hits:
        logger.warning(
            "信人格正文含机制词（提醒，不拦截）：words=%s"
            "——若这些词出现在禁令里属正常；若是她自己会说的，改掉。",
            "、".join(hits),
        )
    return body


def load_letter_facts() -> str | None:
    """《已确定的事》正文；读不到就是 `None`（同样不写这封信）。

    为什么事实表也 fail-closed：这封信是**给主人看的**，而事实校对那一步
    全靠它。表不在、校对就只能对着模板（"把角色的事实表填在这里"）校，
    等于没有依据却假装校过了——那比不写更糟。
    """

    return _load(facts_candidates(), FACTS_CONSTANT,
                 what="信件事实表", who="data/private_docs/letter_facts.REAL.txt")
