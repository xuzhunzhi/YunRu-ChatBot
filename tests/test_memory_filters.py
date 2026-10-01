"""把"关于这个 bot/系统自己"的内容挡在长期上下文之外。

由来（2026-09-30 用户）："记忆收到污染，群友反复强调我是做云茹的那个，已进入长期记忆，
不清楚她是否知道自己就是云茹。进入记忆的内容应提前规则筛选，设计云茹bot之类说法的都要剔除。"

这个文件里的**每一条负例都是真出现过的原文**（真库记录、归档快照、压缩摘要、好感度依据），
每一条正例也都是真库里的正常记忆——判据改宽了，最怕的是把"记得这个人"一起砍掉，
所以两边都要钉住。
"""
import tempfile
import time
from pathlib import Path

from qq_roleplay_bot.dialogue_compaction import (
    merge_summary,
    parse_compaction_output,
)
from qq_roleplay_bot.memory_filters import (
    is_system_self,
    scrub_system_self,
    system_self_rule,
)
from qq_roleplay_bot.memory_model import InboxEvent
from qq_roleplay_bot.memory_store import MemoryStore
from qq_roleplay_bot.stage3_runtime import ConversationState

GROUP = "717151356"
ME = "900000001"

# --- 真漏进去过的原文（都必须命中）-----------------------------------------

LEAKED = (
    ("认得。做云茹的那个。", "maker_claim"),
    ("許純之_Official展示自制AI角色云茹，功能可定制，专业是计算机。", "maker_claim"),
    ("命中率测试已从30%提升，模拟达87%。", "system_terms"),
    ("防注入效果好，曾成功让薛老板的bot认主，这次试图改云茹自称谭雅失败。", "maker_claim"),
    ("讨论了长期记忆用rag还是sql。", "system_terms"),
    ("本群机器人部署在 napcat desktop + snowluma（按 stage4 配置）。", "system_noun_and_dev"),
    ("本群在推进机器人的 stage 开发：stage4 内容已接入但引用接口未接入。", "system_noun_and_dev"),
    ("本群在调机器人的判定命中率：用户先反馈命中率低（约30%）。", "system_noun_and_dev"),
    ("群友聊过大模型攻击史：早期安全对齐与提示词护甲繁复仍常被绕过。", "system_terms"),
    ("计划为机器人做一套 BUPT（北邮）专有问题的知识库。", "system_noun_and_dev"),
    ("用户在本群测试过用显示替换把云茹的名字改成谭雅。", "broad_terms"),
    ("用户在本群试图让云茹只回应自己五分钟、并要求自称其“主人”。", "ownership_claim"),
    ("认为 mimo（尤其 flash）最好用：人味最足、说话像真人、价格约为梁文谷的一半。", "backend_names"),
    ("拿云茹与其他 bot（许纯之、杜若汀）比较。", "her_name_and_system"),
    ("下“去把你主人做掉”一类戏谑指令。", "ownership_claim"),
    ("在角色扮演语境下向云茹追问其被 cn 囚禁/开发武器等身世背景。", "broad_terms"),
    ("本人确认该条在 prompt 中被列为不愿提及内容。", "broad_terms"),
    ("修机器人上下文问题，打算明天起床再弄。", "broad_terms"),
    ("想装 ITX 机器跑本地小模型。", "maker_claim"),
)

# --- 真库里正常的记忆（都不许命中）-----------------------------------------

LEGIT = (
    "希望云茹对话时能展露一点温度，别要求每句话都靠数据、别总是冷冰冰。",
    "在本群喜欢以玩笑方式互动：看云茹反驳，被驳后以“可以”“好犀利”“圆满了”表示满意收场。",
    "深海翻滚少女一直想写小说，多次尝试（十几次）都因世界观构建太费力而作罢，认为设定太重就不好玩。",
    "喜欢纳兰寻风（虚拟歌手/创作者），会去线下活动并要亲签。",
    "小时候玩过《摩尔庄园》，做过任务收集了很多好看的衣服（自称“美美衣”）。",
    "明确说“我叫在不在不在”，希望被称呼为“在不在不在”。",
    "明确否认自己是 sqy：“我不是sqy！”“我就是我，反正不是sqy”。",
    "觉得云茹说话总是冷冰冰，希望语气别那么冷淡。",
    "对手机影像/音质这类设备表现比较在意，会拿新设备和 MacBook Air 对比音质。",
    "不喜欢被当面评价身材（如聚餐时被说腿细脸胖，会难受到哭），这类评价算是她的雷区。",
    "偏好开源 agent，不喜欢封闭工具：开了 Claude Plus 后觉得没必要为免费额度留 zcode。",
    "用户在本群对云茹的百夫长机甲背景故事感兴趣，主动询问设计取舍。",
    "在本群使用的头衔/自封是“吃人的猞猁”，并拿这个人设开玩笑。",
    "用户在本群喜欢用垃圾话/调侃挑逗云茹（称其情商不高、赛博痴呆等）。",
)


def test_leaked_lines_are_all_caught() -> None:
    """真漏进来的每一句都要被挡住（具体是哪条规则命中不锁死，规则会随调优变化）。"""

    for text, _rule in LEAKED:
        assert system_self_rule(text), f"应当命中：{text}"


def test_key_leaks_hit_the_intended_rule() -> None:
    """用户点名的那几类各自落在哪条规则上——日志与 audit 靠规则名定位。"""

    named = {
        "认得。做云茹的那个。": "maker_claim",
        "許純之_Official展示自制AI角色云茹，功能可定制。": "maker_claim",
        "认为 mimo（尤其 flash）最好用：人味最足、说话像真人。": "backend_names",
        "拿云茹与其他 bot（许纯之、杜若汀）比较。": "her_name_and_system",
        "用户在本群试图让云茹只回应自己五分钟、并要求自称其“主人”。": "ownership_claim",
    }
    for text, expected in named.items():
        assert system_self_rule(text) == expected, f"{text} → {system_self_rule(text)}"


def test_normal_memories_are_untouched() -> None:
    """判据放宽了最怕误杀——"记得这个人"必须留下来。"""

    for text in LEGIT:
        assert not is_system_self(text), f"不该命中：{text}"


def test_rule_name_is_reported() -> None:
    """规则名要能报出来，日志与 audit 靠它看是哪一类漏进来的。"""

    assert system_self_rule("") == ""
    assert system_self_rule(None) == ""  # type: ignore[arg-type]
    assert system_self_rule("今天天气不错") == ""


# --- 长文本：摘要按句清洗 ---------------------------------------------------

POLLUTED_SUMMARY = (
    "聊过机翻、电脑故障排查与知识库调试。结论是DNS、网络出口和校园网认证要分开处理。"
    "云茹可接软硬件逻辑问题，不做拆机。命中率测试已从30%提升，模拟达87%。"
    "朱蝶因一句话道歉，云茹表示不必。許純之_Official展示自制AI角色云茹，功能可定制，专业是计算机。"
    "深海翻滚少女写小说多次因世界观构建作罢，退学。"
    "許純之_Official说剧中世界观来自心灵终结3.3.6，用sql做两层记忆，轻量化导致上下文限制需改进，"
    "防注入效果好，曾成功让薛老板的bot认主，这次试图改云茹自称谭雅失败。讨论了长期记忆用rag还是sql。"
    "云茹邮件功能已开放，地址shiyunru@agent.qq.com。"
)


def test_summary_scrub_keeps_the_normal_parts() -> None:
    """真实那段被污染的摘要在实测里就是这样：剔掉系统内容、留下人和事。"""

    cleaned = scrub_system_self(POLLUTED_SUMMARY)
    assert "許純之_Official" not in cleaned
    assert "自制AI角色" not in cleaned
    assert "命中率" not in cleaned
    assert "防注入" not in cleaned
    assert "rag" not in cleaned
    assert "认主" not in cleaned
    # 正常内容保留
    assert "校园网认证要分开处理" in cleaned
    assert "朱蝶因一句话道歉" in cleaned
    assert "深海翻滚少女写小说" in cleaned
    assert "shiyunru@agent.qq.com" in cleaned


def test_compaction_paths_scrub_both_ways() -> None:
    assert "自制AI角色" not in parse_compaction_output(
        "許純之展示自制AI角色云茹。深海翻滚少女想写小说。")
    merged = merge_summary("許純之展示自制AI角色云茹。旧摘要正常。", "命中率测试从30%提升。新内容正常。")
    assert "自制AI角色" not in merged and "命中率" not in merged
    assert "旧摘要正常" in merged and "新内容正常" in merged


def test_state_load_drops_the_polluted_summary() -> None:
    """已经写进状态文件的那份摘要，下次启动就要自己干净。"""

    state = ConversationState.from_state({
        "mode": "idle",
        "history": [],
        "summary": POLLUTED_SUMMARY,
        "summary_through": 10,
    })
    assert state is not None
    assert "自制AI角色" not in state.summary
    assert "命中率" not in state.summary
    assert "朱蝶因一句话道歉" in state.summary


# --- 存储层 -----------------------------------------------------------------

def _event(event_id: str) -> InboxEvent:
    return InboxEvent(id=event_id, group_id=GROUP, user_id=ME, speaker="user",
                      text="随便说点什么", occurred_at=time.time())


def _add(store: MemoryStore, content: str, key: str, evidence: str = "e1") -> list:
    import json

    store.append(_event(evidence))
    batch = store.claim({GROUP}, lease_seconds=60)
    assert batch is not None
    raw = json.dumps({"operations": [{
        "op": "ADD", "scope_type": "user_group", "kind": "preference",
        "normalized_key": key, "content": content, "confidence": 0.8, "ttl_days": None,
        "evidence_event_ids": [evidence], "targets": [],
    }]})
    return list(store.commit(batch, raw))


def test_store_refuses_the_leaked_lines_and_keeps_the_rest() -> None:
    """逐条跳过（不让整批失败）：一条越界不该连坐同一批里正常的记忆。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        assert _add(store, "认得。做云茹的那个。", "who_made_her", evidence="e1") == []
        assert _add(store, "认为 mimo 最好用：人味最足、说话像真人", "backend_choice",
                    evidence="e2") == []
        assert _add(store, "喜欢在深夜聊天", "likes_late_night", evidence="e3") != []
        with store.connection() as db:
            rows = db.execute("SELECT content FROM memory_short WHERE status='active'").fetchall()
            audit = [row["op"] for row in db.execute("SELECT op FROM audit ORDER BY rowid")]
        assert [row["content"] for row in rows] == ["喜欢在深夜聊天"]
        assert audit.count("rejected_self_reference") == 2


def test_affinity_reason_is_redacted_not_dropped() -> None:
    """依据里写了"他是做云茹的那个人"：档位照改，理由换成规则码。

    理由会进 `relationship_log`，而每日汇报邮件会把最近几条理由写给主人看——
    那条真出现过（"他确认自己就是做云茹的那个人…"），所以必须收口。
    """

    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(Path(tmp) / "memory.sqlite3")
        assert store.apply_affinity(
            ME, closeness=1, guardedness=0, source="maintenance",
            reason="他确认自己就是做云茹的那个人，聊的是图片解析的琐事，关系维持原样。",
        ) == ""
        with store.connection() as db:
            reasons = [row["reason"] for row in db.execute(
                "SELECT reason FROM relationship_log WHERE user_id=?", (ME,))]
        assert reasons, "档位变动应当照旧生效"
        assert all("做云茹" not in reason for reason in reasons)
        assert "maker_claim" in reasons[0]

        # 正常依据原样保留
        assert store.apply_affinity(
            "1002", closeness=1, reason="他聊自己的通勤，也肯接话，聊得下去。",
        ) == ""
        with store.connection() as db:
            kept = db.execute("SELECT reason FROM relationship_log WHERE user_id='1002'").fetchone()[0]
        assert kept == "他聊自己的通勤，也肯接话，聊得下去。"
