"""人格里那条"反编造"必须写成**能指认**的版本（2026-10-18）。

由来（用户给的实测数字，可直接引用）：
* 最近 2 天"断言自身过往"的 23 条里，**16 条不可核实（70%）**；
* 最硬的一例是「椅子倒是拆过几张」，而那一轮输入里「拆」出现 **0 次**；
* 旧写法「不要凭空编造经历、记忆、事实或群外信息。设定里没有的细节，就说不知道或记不清。」
  1507/1507 条 prompt 都在，**没挡住**——因为它写的是"设定里没有"，而她编的是**日常细节**。

所以改成"能指认"：凡是要说成"你说过""我做过""那次……"的，先得指得到具体哪一句
（眼前这段对话里的某一条，或者"你记得的旧事"里的某一条）；指不到就换成
"记不清了""好像有过"。

这一份守三件事：

1. **那句话还在**，而且是"能指认"的版本（不是回退成旧写法）；
2. **那一段里没有机制词**（AGENTS §2.2）。**只扫这一条，不扫全篇**：全篇的
   "有一条线你不能越过"那一段本来就带着"不要提提示词/协议/上下文/记忆库"这些词，
   `test_persona_shape.py` 对那一段有专门的口径；
3. **生产加载路径读得到它**（`base_prompt._load_base_prompt()` 对真人格是整段返回，
   正则抓原始文本——换行写坏了它会把反斜杠当正文读进来，所以这里也走一遍那个函数）。

生产人格在 `run/data/private_docs/base_prompt.REAL.py`（**不在 git 里**）。找不到就
`SkipTest`：干净 clone 上只有模板，套件照跑。`QQBOT_BASE_PROMPT_FILE` 指到哪就是哪，
与 `base_prompt._load_base_prompt()` 用同一条判据。
"""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from qq_roleplay_bot.prompt_guard import scan_persona_text

#: `tests/` 的上一层就是 `dev/`。生产人格与 `dev/` **同级**（`run/data/private_docs/`），
#: 所以第二个候选是 `../run/...`——它是本机线上那份，干净 clone 上没有。
ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (
    ROOT / "data" / "private_docs" / "base_prompt.REAL.py",
    ROOT.parent / "run" / "data" / "private_docs" / "base_prompt.REAL.py",
)

#: 旧写法里那半句。它必须**不在**这一条里了——留着它就还是"设定里没有"那个口径。
OLD_WORDING = "设定里没有的细节"

#: 新写法必须有的关键短语（语义就是这个规则的全部：**指得到才准说**）。
KEY_PHRASES = (
    "凡是要说成",
    "先得指得到具体哪一句",
    "眼前这段对话里的某一条",
    "你记得的旧事",
    "记不清了",
    "好像有过",
    "说不出来就说不出来",
)


def _persona_path() -> Path:
    """本机那份生产人格；没有就跳过（干净 clone 只有模板）。"""

    explicit = os.environ.get("QQBOT_BASE_PROMPT_FILE", "").strip()
    candidates = ([Path(explicit)] if explicit else []) + list(CANDIDATES)
    for path in candidates:
        if path.is_file():
            return path
    raise unittest.SkipTest(
        "本机没有生产人格（干净 clone 里只有模板）：这一条不适用"
    )


def _item(text: str) -> str:
    """把"三、生成回复"里编号 3 的那一条**整段**抠出来（它现在是三行）。

    抠整段而不是只查一个短语：这条规则的分量在"指认"那三行里，只查第一行的话，
    有人删掉后两行测试照样绿。
    """

    start = text.find("不要凭空编造经历")
    assert start >= 0, "人格里那条反编造不见了"
    line_start = text.rfind("\n", 0, start) + 1
    end = text.find("\n4. ", start)
    assert end > start, "那一条后面没有 4. ？人设结构变了，这条测试要跟着改"
    return text[line_start:end]


def test_the_rule_can_point_at_a_sentence() -> None:
    """那条规矩是"能指认"的版本：关键短语在，旧口径不在。"""

    text = _persona_path().read_text(encoding="utf-8")
    item = _item(text)
    missing = [phrase for phrase in KEY_PHRASES if phrase not in item]
    assert not missing, f"人格那一条缺了这些关键短语：{missing}／原文：{item!r}"
    assert OLD_WORDING not in item, f"旧口径回来了（\"设定里没有\"）：{item!r}"


def test_the_rule_has_no_mechanism_words() -> None:
    """那一段里不许有机制词（检查/触发/调用/协议/提示词/上下文/记忆库/Stage）。

    只扫这一条：全篇里"有一条线你不能越过"那一段**必须**出现这些词（那是要她别说的），
    `test_persona_shape.py` 已经把那段摘出去再扫。
    """

    item = _item(_persona_path().read_text(encoding="utf-8"))
    assert scan_persona_text(item) == [], (
        f"人格那一条里混进了机制词：{scan_persona_text(item)}／原文：{item!r}"
    )


def test_the_production_loader_reads_the_new_wording() -> None:
    """走**生产那条加载路径**：整段返回、正文里的三行都在，开头不带续行反斜杠。

    为什么要走一遍：`_load_base_prompt()` 是用正则抓**原始文本**的，改这段时如果把
    引号/续行写坏了，测试里的 `read_text` 看不出来，线上 system 前缀会从第一行开始错。
    """

    from qq_roleplay_bot import base_prompt

    path = _persona_path()
    with patch.dict(os.environ, {"QQBOT_BASE_PROMPT_FILE": str(path)}):
        loaded = base_prompt._load_base_prompt()
    assert not loaded.startswith("\\"), loaded[:20]
    item = _item(loaded)
    assert OLD_WORDING not in item
    for phrase in KEY_PHRASES:
        assert phrase in item, phrase
