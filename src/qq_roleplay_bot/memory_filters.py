"""把"关于这个 bot/系统自己"的内容挡在长期上下文之外。

由来（2026-09-30 用户）："记忆收到污染，群友反复强调我是做云茹的那个，已进入长期记忆，
不清楚她是否知道自己就是云茹。进入记忆的内容应提前规则筛选，设计云茹bot之类说法的都要剔除。"

AGENTS.md §2.2 早就写了"记忆库不得记录关于这个系统自身的信息"。落地方式是三层：

1. **维护 prompt 里明令不写**（`memory_maintenance_agent.py`）——模型不一定听；
2. **写入路径的机械兜底**（这里 + `memory_store.commit`）——命中就拒；
3. **长文本的清洗**（对话压缩摘要 `dialogue_compaction`）——摘要每轮都在她的 prompt 里，
   是比记忆更重的一条通道。

## 为什么还要再加一层（原来的兜底漏了什么）

原来 `memory_store.SELF_REFERENCE_RE` 只有
`机器人|bot|prompt|提示词|记忆库|判定|模型|系统|程序|上下文|复读|扮演|出戏|测试|人格|越界`，
而且**刻意不含"云茹"**（当时实测：按名字拦会砍掉 19/50 条，把"记得这个人"也砍没）。
于是这些都漏了进去（都是真库/真日志里的原文）：

| 漏进来的原文 | 出处 | 凭什么漏 |
| --- | --- | --- |
| `认得。做云茹的那个。` | 她 09-30 的回答 | 没有上面任何一个词 |
| `自制AI角色云茹，功能可定制` | 压缩摘要 | 只有 `AI角色` |
| `命中率测试已从30%提升，模拟达87%` | 压缩摘要 | `命中率` 不在表里 |
| `防注入效果好，曾成功让薛老板的bot认主` | 压缩摘要 | `注入`/`认主` 不在表里 |
| `讨论了长期记忆用rag还是sql` | 压缩摘要 | 没有上面任何一个词 |
| `认为 mimo（尤其 flash）最好用：人味最足、说话像真人` | **长期记忆（活着）** | 没有上面任何一个词 |
| `他确认自己就是做云茹的那个人…` | 好感度依据（进日报邮件） | 没有上面任何一个词 |

所以这一版补两类判据，但**仍然不按"云茹"两个字一票否决**：

- **"谁造了她"类**：`做/造/设计/开发/写/训练/部署…` 与 `云茹/bot/机器人/模型` 相邻；
- **系统术语类**：`命中率`、`注入`、`越狱`、`长期记忆`、`知识库`、`语料`、`rag`、
  模型名（`mimo`/`deepseek`/`gpt`…）等——这些词在别处几乎不会出现。

"某人喜欢和云茹聊天""对云茹的百夫长故事感兴趣"这种**关于她的正常内容不受影响**：
判据要求系统词汇出现在附近，不是名字出现就砍。

## 两个消费方

- `memory_store`：ADD/UPDATE/MERGE 的内容、以及好感度的 `reason`（它是唯一写入路径，
  判定 agent 与维护 agent 都走它）。命中就逐条跳过并进 audit，**不让整批失败**。
- `dialogue_compaction`：摘要正文按句清洗；`ConversationState.from_dict` 载入时也洗一遍，
  这样**已经污染的历史摘要**在下次启动就会自己干净。
"""
from __future__ import annotations

import re

# 相邻词之间不许跨句读：隔了一个句号/逗号就已经是另一件事了。
_NEAR = r"[^。！？；，,、\n]{0,10}"

# 名字与"造她"的动词相邻，或名字与系统词汇相邻。
_HER = r"(云茹|yunru)"
_SYSTEM_NOUN = r"(bot|机器人|模型|程序|提示词|prompt|LLM|AI角色|AI|知识库|记忆库|语料)"
_MAKE_VERB = r"(做|造|制|设计|开发|写|训练|调教|部署|上线|搭|跑|接|改|修)"
_DEV_NOUN = r"(开发|设计|制作|部署|上线|功能|命令|接口|参数|命中率|测试|注入|越狱|认主|作者|主人|知识库|语料|记忆)"

# 系统术语：这些词在"跟某个人的日常"里几乎不会出现。
_SYSTEM_TERMS = (
    r"提示词|prompt|越狱|jailbreak|命中率|记忆库|长期记忆|短期记忆|向量库|语料|"
    r"系统消息|大模型|语言模型|LLM|RAG|知识库|命中缓存|缓存命中"
)
# 后端模型名。实测漏进来的那条是"认为 mimo（尤其 flash）最好用"。
# **光有名字不算**：群主本来就在用各种 LLM 工具（"开了 Claude Plus 后觉得没必要…"
# 是他自己的工具偏好，跟"她跑在哪个模型上"无关）。要求名字旁边就有"选型/好坏"的话。
_BACKEND_NAMES = r"mimo|deepseek|gpt-?[0-9]|claude|gemini|qwen|kimi|豆包|文心|通义|梁文谷"
_BACKEND_QUALITY = r"(最好用|更好用|更好使|人味|像真人|语气|价格|便宜|贵|缓存|命中|不如|换成|替代)"
# 原来的宽判据：这几个词只要出现就拒（维护 prompt 明令不写、真库仍有残留）。
_BROAD_TERMS = (
    r"机器人|bot|prompt|提示词|记忆库|判定|模型|系统|程序|上下文|复读|扮演|出戏|测试|人格|越界"
)

# 规则名 → 判据。名字进日志与 audit，方便看"是哪一类漏进来了"。
SYSTEM_SELF_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "是我做的/我造的"这类自称（实测漏进来："认得。做云茹的那个。"）
    ("maker_claim", re.compile(rf"{_MAKE_VERB}{_NEAR}({_HER}|{_SYSTEM_NOUN})", re.I)),
    # 她的名字紧挨系统词汇（实测："自制AI角色云茹，功能可定制"）
    ("her_name_and_system", re.compile(rf"{_HER}{_NEAR}{_SYSTEM_NOUN}", re.I)),
    # 系统名词 + 开发动作（实测："计划为机器人做一套知识库"）
    ("system_noun_and_dev", re.compile(rf"{_SYSTEM_NOUN}{_NEAR}{_DEV_NOUN}", re.I)),
    # 操作 + 系统术语（实测："防注入效果好""改云茹自称谭雅"）
    ("dev_and_system_term", re.compile(
        rf"注入|越狱|{_MAKE_VERB}{_NEAR}(命中率|提示词|记忆库|知识库|语料|上下文)", re.I)),
    # 系统术语只要出现就拒
    ("system_terms", re.compile(_SYSTEM_TERMS, re.I)),
    # 后端模型名 + 选型话术（"mimo 最好用/人味最足"）
    # 名字那一段**必须自己带括号**：不分组的话 `{_NEAR}{_BACKEND_QUALITY}` 只会挂在
    # 最后一个候选（"梁文谷"）上，等于其余名字后面跟什么都算命中（踩过：Claude 单独命中）。
    ("backend_names", re.compile(rf"({_BACKEND_NAMES}){_NEAR}{_BACKEND_QUALITY}", re.I)),
    # 认主 / 主人 / 所有权：**整句里同时出现她（或 bot/机器人）**才算——实测那条
    # "要求自称其'主人'……不会因宣布而成立主人身份"里，两者被引号和分号隔开了，
    # 单看邻近会漏，所以用两个前瞻按整句判断。另外 `认主` 本身就是系统黑话，
    # `你主人/她的主人` 也一律算（实测原文："下'去把你主人做掉'一类戏谑指令"）。
    ("ownership_claim", re.compile(
        r"(?=[^\n]*(云茹|bot|机器人))(?=[^\n]*(认主|主人|所有权))"
        r"|认主|(你|她|自己)的?主人", re.I)),
    # 原样保留的宽判据（实测有效的旧闸门，2026-09-27 就上了）
    ("broad_terms", re.compile(_BROAD_TERMS, re.I)),
)


def system_self_rule(text: str) -> str:
    """命中的规则名；没命中返回空串。"""

    if not isinstance(text, str) or not text.strip():
        return ""
    for name, pattern in SYSTEM_SELF_RULES:
        if pattern.search(text):
            return name
    return ""


def is_system_self(text: str) -> bool:
    """这条文本是不是在讲"这个 bot / 这个系统自己"。"""

    return bool(system_self_rule(text))


def scrub_system_self(text: str) -> str:
    """按句剔除讲这个系统自己的内容（给长文本用：压缩摘要）。

    摘要是一整段几百字的陈述，整段丢掉代价太大——只把命中的那些**句子**去掉，
    剩下的照旧。句号/换行/分号都算句子边界；清洗后不留空段。
    """

    if not isinstance(text, str) or not text.strip():
        return ""
    kept: list[str] = []
    for sentence in re.split(r"(?<=[。！？；\n])", text):
        stripped = sentence.strip()
        if not stripped or is_system_self(stripped):
            continue
        kept.append(stripped)
    return " ".join(kept)
