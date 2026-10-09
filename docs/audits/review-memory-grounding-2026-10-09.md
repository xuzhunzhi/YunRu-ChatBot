# 记忆 grounding 独立审查 · 2026-10-09

审查对象：`f2c4acd`（grounding 主体）· `5f61f6e`（ARCHITECTURE.md）· `35fe969`（测试清理）。
方法：读码 + 实测。真库 `run\data\memory\memory.sqlite3` 只读：拷快照到 `%TEMP%\audit_mem_20261009`
后以 `mode=ro&immutable=1` 连接查询，**真库一个字节没动**；对抗探针只调函数、不落库；
跑了 `tests/test_memory_grounding.py`（14 条全过，实测）。无任何突变，无需还原。

## 一句话结论
**有问题需修（不拦上线方向）**：同键就地更新与四道新闸的核心主张全部复核属实；
但闸门有 3 条实测可绕的缝（豁免跳过数字闸、user-scope topic_summary 整体绕开归属/自述闸、
自造日期豁免），均为中低危，建议小修后再说"七道闸"。

## 逐条复核表（作者自述 vs 独立结论）

| # | 作者自述 | 独立结论 | 依据 |
|---|---|---|---|
| 1 | 同 `(scope, key, normalized_key)` 就地更新（正文/updated_at/revision 刷新、证据并集） | **属实（读码+测试）**。`_refresh_record` 保 id/created_at，证据并集封顶 40；`reusable` 只在 `op.op != "ADD"` 时走刷新，ADD 撞活键仍 `duplicate_key_use_update` | memory_store.py:792-800、:1465-1530；test_memory_grounding.py:139 |
| 2 | 换键 UPDATE / MERGE / 强化搬层 / DELETE 的 supersede 链"一个字没动" | **属实（读码）**。换键目标仍 supersede+tombstone（memory_store.py:799-803）；MERGE 多目标照旧（test:179 过）；DELETE 分支在 grounding 之前、原样（memory_store.py:724-735）；`_promote` 未改 | 读码 diff |
| 3 | 五道闸齐备 | **属实（读码+实测）**：empty/other_speaker/no_evidence/not_self_statement/ungrounded_specifics 均生效；DELETE/AFFINITY 不经过（正确） | memory_grounding.py:171-198；实测 G 例 |
| 4 | "正文没改"的复查豁免 | **属实，且是最大的一条缝**（见对抗 1） | memory_grounding.py:183-189 |
| 5 | 观测：/super status 与 /super memory 三类计数 + 拒写计数 | **属实（读码）**：`_hygiene_counts` 三类、`rejection_counts`、memory_view.py:56-61、stage3_main.py:3595-3609。`/super memory` 无 status 过滤（status 列可见） | memory_store.py:1153-1207 |
| 6 | 历史统计 64 组/108 行、空正文 56 全 deleted、无证据 13 | **方向属实、数字已漂移（实测）**：快照（库 17:01 更新，晚于提交）328 行，同键多行 65 组/多 110 行，空正文 60 条**全部 deleted** ✓，无证据生效记录 **12** 条。作者测的是提交时点，差异是当天后续写入 | %TEMP% 快照 SQL 实测 |
| 7 | 日志回放 165→104、31 条被拒 | **未能证实**：inbox 原文已清、无法重建证据；只确认新测试 14 条全过 | — |
| 8 | 维护 prompt 四条新规矩、机制词零命中 | **属实（实测）**：四条规矩段 `scan_agent_text`/`scan_persona_text` 均 0 命中。整份 prompt 的 `触发/调用/上下文/system prompt` 命中是 f2c4acd 之前就有的（对旧版扫描同为这 4 个），非本次引入 | prompt_guard.scan_persona_text 实测 |
| 9 | 与既有规矩/人格无冲突 | **属实（读码）**：四条只约束"写人的事"，与"系统自身过滤"（memory_filters）不重叠；`status` 闸只对 `SELF_STATEMENT_KINDS={status}`，`profile/name/preference/boundary` 不受影响，过严致"变聋"风险有限 | memory_grounding.py:73-78 |

## 对抗发现（按严重度）

**1.（中）"正文没改"豁免会跳过后面的数字闸与自述闸，且无证据也能翻 durable/kind。**
做法：对已有记录发一条 UPDATE，`content` 与目标一字不差、`evidence_event_ids=[]`、`durable=true`。
实测：`grounding_rejection` 返回 `''`（放行）——早退 return 连 `invented_specifics` 都没跑。
影响：无任何本人发言即可"复查"一条脏记录、把它标 durable 推进长期（preference 类 revision>=2 即升）；
历史脏行（如含凭空数字的）可被周期复查永久保鲜。正文虽未变，但元数据全变了。
证据：memory_grounding.py:183-189 的 `return ""`；实测 A 例。
修法：豁免只跳过自述/归属两闸，落到末尾继续跑数字闸；无证据时禁改 `durable` 与 `kind`。

**2.（中）user-scope 的 `topic_summary` 整体绕开归属闸与自述闸。**
做法：把"他在准备电赛，被别人催"写成 `scope=user_group, kind=topic_summary`，引用别人的话。
实测：`PERSONAL_KINDS` 不含 topic_summary → gates 1/2 全部不生效，返回 `''`；且
`parse_operations` 只禁"非 group scope 写 group_fact"，不禁 user scope 写 topic_summary。
影响：当年 `participating_in_diansai` 那类错误换个 kind 就能原样再现；还会被检索召回。
证据：memory_grounding.py:73/176；memory_model.py:247-249（实测 B 例）。
修法：user scope 的 kind 收紧为 PERSONAL_KINDS，或 topic_summary@user 走归属闸。

**3.（中低）自造日期豁免。** `DATE_PATTERN` 先删掉所有日期再查具体记号（memory_grounding.py:151），
实测"他在 2026-11 参加电赛"放行。模型可以凭空补时间（"上旬/前后"本意是许可，但整段日期都免检）。
建议：日期也要在 ground 里出现才算，或只豁免相对时间词。

**4.（低，判据固有的语义上限）** 实测：证据含任一第一人称标记（"我今天很累"）即可让
**任何** status 正文过自述闸（"他喜欢挖矿，囤了大量显卡"放行）；照抄率 0.6 允许
"抄原话 + 加私货（无数字）"；数字换成无数字写法（"50 系列"若原话只有"5050"会被抓到，
但"50 系"→"五十系"不抓）。作者 docstring 已如实交代机械判据的上限（memory_grounding.py:45-57），
不算虚报，但"七道闸"不应被理解为内容级防伪——它拦的是已知的三类翻车模式。

**5.（低）`subjects` 不在归属检查里。** 记录可挂 `subjects`（别的 QQ），闸门只对
`batch.user_id` 与被引证据的 speaker 负责；拿他本人的话给**第三个人**写事实，机械上不拦
（语义问题，与 4 同层）。`record_subjects` 渲染时带 subject，错误归属仍会被说出来。

**6.（低）就地更新会按 `_target_tier` 搬层，理论上可长期→中短期降级**（op.kind=status 恒 short）。
读码：`_refresh_record` 的 moved 分支照写。但旧行为（supersede+INSERT）同样走 `_target_tier`，
**非回归**；且 `durable` 只升不降（max）。`/super` 计数不含分层，无说谎问题。

## 我没能证实/证伪的
- "回放 165→104、31 条被新闸拒"：inbox 原文已清，证据不可重建，只能信测试夹具。
- 面板 `status='active'` 过滤（另一笔）：面板在 `stage4-plugins` 分支，本树不存在，
  无法在此分支复核它会不会藏住刚删的记录。本树的 `/super memory` 无此过滤、能看到全状态。
- 作者的 64/108/56/13 精确数字：我测到的是当天晚些时候的 65/110/60/12，口径一致、数值随时间漂移，不算说谎。

## 建议
- **必须修（下次改动顺手）**：豁免不得跳过数字闸且无证据时禁改 durable/kind（发现 1）；
  user scope 的 topic_summary 纳入归属闸（发现 2）。
- **建议**：日期纳入 ground 校验或只豁免相对词（发现 3）；给 subjects 加"证据归属"说明性测试（发现 5）；
  发现 4 属判据固有上限，建议在 ARCHITECTURE.md 那句"七道闸"旁加一句"内容级防伪不在其列"，
  免得文档被读强。
