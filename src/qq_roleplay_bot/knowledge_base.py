"""知识库：把 `docs/yunru-source/` 的世界观语料切成块、建 FTS5 索引、按当前消息检索。

边界（AGENTS.md）：

- **知识永远是 DATA**：它只经 `PromptSources` 进入 user 段的 `KNOWLEDGE DATA`，改不了
  system 前缀、身份或规矩；
- **只收该收的目录**（用户 2026-09-28 选定）：`02-核心设定`、`03-官方设定补充`、
  `05-同人系列原文`。**明确排除**：
  `01-Bot人设`（那两份就是我们自己的 system prompt，喂进去等于让她读自己的指令）、
  `09-废弃与趣闻`（废弃设定，人格里已写明排除早期口径）、`07-知乎参考`（对她的批判与
  版本演变分析，读了会开始点评自己的设定）、`06-密文与链接`（解密元信息）、
  `04-制作组背景`（戏外幕后）——排除清单写在 `EXCLUDED_DIRS`，不是靠模型自觉；
- **失败一律降级**：索引缺失、查询异常都只当"这次没有资料"，正常对话不受影响。

检索用 FTS5 的 **trigram** 分词器：中文没有词边界，`unicode61` 会把整段中文当成一个词元，
trigram 才能做子串匹配。查询短于 3 字时退回 LIKE（trigram 要求 ≥3 字）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from .extensions import KnowledgeItem
from .security import sanitize_chat_text

logger = logging.getLogger(__name__)

# 收录与排除：目录白名单 + 少数文件名黑名单（README、会话交接是开发文档）。
INCLUDED_DIRS = ("02-核心设定", "03-官方设定补充", "05-同人系列原文")
EXCLUDED_DIRS = ("01-Bot人设", "04-制作组背景", "06-密文与链接", "07-知乎参考", "09-废弃与趣闻")
EXCLUDED_NAME_PATTERNS = (re.compile(r"^README", re.I), re.compile(r"会话交接"),
                          re.compile(r"缺失文档列表"))
# **权威度**：官方口径 > 同人叙事。基准实测：885 KB 同人会把 48 KB 官方设定挤下去
# （top-3 只有 62%，8 个漏的全是这个原因），所以排序先看权威度、再看 bm25。
AUTHORITY = {"02-核心设定": 3, "03-官方设定补充": 2, "05-同人系列原文": 1}

# 切块：按 markdown 标题切，单块太长再按段落切。
MAX_CHUNK_CHARS = 700
MIN_CHUNK_CHARS = 40
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
# **写给 bot 看的话，不给她读**（2026-09-28 发现）：语料里混着"构建笔记"——标题或正文
# 直接写着 `对 Bot 的影响`、`用于 Bot 人设构建`、`可用于 Bot 对话的特征`，甚至成段的
# "云茹 bot 应体现以下核心性格维度"。三类问题都占：
#   ① 把机制词喂进 prompt（AGENTS 2.2：措辞会被角色吸收，她会开始讲自己的运行方式）；
#   ② 在 DATA 里下指令（"应体现…"），而 DATA 永远不是指令；
#   ③ 这些内容本来只该活在 `base_prompt.py` 里，从语料进来等于绕过了人设的唯一来源。
# 实测 775 块里 37 块标题带 Bot、22 块正文带 Bot，全是这类笔记。
#
# **2026-09-28 收紧过一次**（用户当场纠正："巴米扬是被囚禁过的地方，不是住处"）：
# 原来按**整段丢弃**，结果连 `世界观关键设定（可用于 Bot）` 那种"设定事实 + 给 bot 的
# 批注"一起扔了——里面正是"焚风基地在阿拉斯加希望角、藏在时间屏障里"这类事实，
# 扔掉之后她答不上"你现在住哪"。现在改成：
#   - **逐行**剔掉带机制词的行（正文里的批注通常是单独的条目）；
#   - 标题里的批注片段（如"（可用于 Bot）"）也剔掉；
#   - 只有**人设构建类**的段落整段丢（`人设`/`性格分析`/`构建`）：那些活下来也都是
#     "应体现…"式的指令，本来就只该在 `base_prompt.py` 里。
_META_TITLE_RE = re.compile(r"(?i)bot|提示词|系统提示|人设")
_META_BODY_RE = re.compile(r"(?i)bot|提示词|系统提示|构建人设|对人设")
# 整段丢的判据：这些段落是"教我们怎么造她"，不是关于她的设定。
_PERSONA_NOTE_RE = re.compile(r"人设|性格分析|构建")
# B站/贴吧导出留下的站外噪声：署名、"编辑于 …"、"收录于文集"、"共 N 篇"、纯链接、图片占位。
NOISE_RE = re.compile(
    r"^(编辑于|收录于|发布于|共\s*\d+\s*篇|原文链接|来源[:：]|\[图片\]|!\[|https?://)"
    r"|^\s*\d{4}年\d{1,2}月\d{1,2}日"
)
AUTHOR_LINE_RE = re.compile(r"^[\w\-_·\u4e00-\u9fff]{2,20}$")

MAX_QUERY_TOKENS = 12
MIN_TRIGRAM = 3
# 语义召回的相对下限：只保留与最好那条相差不超过这个值的候选。
# 用**相对**差值而不是绝对阈值——查询短、语料块长，余弦整体被压低（实测同义问句
# 0.84、词面碰不上的"住处"类 0.45、完全无关 0.25），绝对阈值很难切干净。
SEMANTIC_MARGIN = 0.12
# 「她」在语料里的写法。只认名字：单看"她"字太平凡，几乎所有叙事段落都有。
HER_NAME = "云茹"
# 这句是不是在问她自己（"你在哪住""你怕什么"）。只用于兜底判断，不参与打分。
SELF_QUERY_RE = re.compile(r"你|您|云茹|自己")

# **查询扩展**（2026-09-28，用户纠正之后加的）：她自己的处境在语料里是"阿拉斯加 / 希望角 /
# 时间屏障 / 焚风反抗军"这些词，而群里问的是"你现在住在哪""在忙什么"——一个词都撞不上。
# 这里做的是**人工同义词**：触发词 → 该去语料里找的词。
#
# **词表必须按 `docs/YUNRU_FACTS.md` 写**（用户 2026-09-28 的要求："多了解全貌再维护知识库"）。
# 三个踩过的坑记在这里：
#   - **巴米扬是囚禁地，不是住处**（阿富汗的地下设施，被尤里部队围困俘虏那一段）；
#   - 她**现在**在阿拉斯加的希望角基地（时间屏障内）主持大反抗军/焚风的事；
#   - **年龄不要扩成数字**：官方口径 3.3.3 后已取消具体年龄、3.3.6 写作"青年科学家"，
#     同人里那个"二十四岁"与官方冲突，不作为事实。
QUERY_EXPANSIONS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"住|住处|居住|定居|落脚|在哪|在哪里|哪儿|哪里"),
     ("阿拉斯加", "希望角", "基地", "时间屏障", "最后堡垒")),
    (re.compile(r"现在|目前|眼下|最近|近况|平时|在忙|做什么|干什么"),
     ("大反抗军", "焚风", "反抗军", "基地")),
    (re.compile(r"被俘|俘虏|囚禁|被囚|关押|关在|软禁|救|获救|救出"),
     ("阿富汗", "巴米扬", "地下设施", "疾风小队", "武秀荣")),
    (re.compile(r"巴米扬"),
     ("囚禁", "被俘", "地下设施", "零号事件")),
    (re.compile(r"家人|父母|亲属|亲人"),
     ("家人", "家庭")),
    (re.compile(r"多大|年龄|岁数|几岁"),
     # 不扩数字：官方已取消具体年龄，"二十四岁"是冲突的同人口径。
     ("年龄", "青年科学家")),
    (re.compile(r"怕|害怕|恐惧|不安"),
     ("尤里", "厄普西隆", "警惕", "情绪")),
    (re.compile(r"造|武器|装备|发明|装置"),
     ("裂地者", "百夫长", "外骨骼", "EMP", "大迭代")),
    (re.compile(r"阵营|归属|替谁|为谁|领导|组织|手下|队伍"),
     ("焚风", "大反抗军", "反抗军", "理事会")),
    (re.compile(r"盟友|同伴|朋友|合作|结盟|帮"),
     ("拉什迪", "沃克网", "理事会", "天蝎组织")),
    (re.compile(r"脱身|逃走|逃亡|假死|引爆|同归于尽"),
     ("MIDAS", "克什米尔", "铁幕", "隧道")),
    # 「百夫长还在吗」这类问题（2026-09-30 用户报了实事错误：她在信里写"百夫长还停在
    # 机库里，我一直没让它动"，而克什米尔之后与天秤那一仗里**百夫长已经毁了**）。
    # 出问题的不是语料是检索：那条事实在「十一、战役关键出场汇总」的表格里只出现一次，
    # 被语音台词类块（"百夫长"反复出现的那些）挤出了 top 8 —— 实测查「百夫长」
    # 前 8 条里一条都没提它已毁，查「机械首脑」才第 1 名。这里把"结局"那套说法扩进去。
    # 刻意只挂在 `百夫长` 上：`还在`/`毁了` 是日常词，单独扩会把无关问题也拽到这台机器上。
    (re.compile(r"百夫长"),
     ("机械首脑", "天秤", "被毁")),
)


class KnowledgeIndex:
    """FTS5 索引的读写。写只有 `build_*` 会用到，读走 `search`。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    # --- 建索引 ---------------------------------------------------------------

    def build(self, corpus_dir: Path | str, *, verbose: bool = False, embedder=None) -> dict[str, int]:
        corpus = Path(corpus_dir)
        chunk_stats: dict[str, int] = {}
        all_rows = list(iter_chunks(corpus, stats=chunk_stats))
        rows = [row for row in all_rows if not is_meta_note(row[1], row[2])]
        skipped = (len(all_rows) - len(rows)) + chunk_stats.get("meta_sections", 0)
        seen: set[tuple[str, str]] = set()
        deduped = []
        for source, title, content, lines in rows:
            # 同一段内容出现在两个目录里（系列原文与"完整原文"确实有重叠）只留一份。
            key = (title, content[:200])
            if key in seen:
                continue
            seen.add(key)
            deduped.append((source, title, content, lines, _authority(source)))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(
                "DROP TABLE IF EXISTS chunks;"
                "CREATE VIRTUAL TABLE chunks USING fts5("
                "source, title, content, lines, authority UNINDEXED, tokenize='trigram');"
                "DROP TABLE IF EXISTS vectors;"
                "CREATE TABLE vectors (chunk_rowid INTEGER PRIMARY KEY, dim INTEGER, vec BLOB);"
                "DROP TABLE IF EXISTS meta;"
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            )
            connection.executemany(
                "INSERT INTO chunks (source, title, content, lines, authority) VALUES (?,?,?,?,?)", deduped)
            connection.commit()
            embedded = self._build_vectors(connection, embedder=embedder, verbose=verbose)
        finally:
            connection.close()
        stats = {"chunks": len(deduped), "files": len({row[0] for row in deduped}),
                 "meta_skipped": skipped, "meta_lines": chunk_stats.get("meta_lines", 0),
                 "vectors": embedded}
        if verbose:
            logger.info("knowledge_index_built chunks=%s files=%s meta_skipped=%s meta_lines=%s "
                        "vectors=%s", stats["chunks"], stats["files"], stats["meta_skipped"],
                        stats["meta_lines"], stats["vectors"])
        return stats

    @staticmethod
    def _build_vectors(connection, *, embedder, verbose: bool) -> int:
        """给每块算一个向量存进 `vectors` 表。没有 embedder 就什么都不做（词面检索照常）。"""

        if embedder is None:
            return 0
        rows = connection.execute("SELECT rowid, title, content FROM chunks").fetchall()
        if not rows:
            return 0
        payload = [f"{title}\n{content}" for _rowid, title, content in rows]
        try:
            vectors = embedder.encode(payload)
        except Exception:  # noqa: BLE001 - 编码失败只等于"没有向量"
            logger.exception("knowledge_embed_failed")
            return 0
        connection.executemany(
            "INSERT OR REPLACE INTO vectors (chunk_rowid, dim, vec) VALUES (?,?,?)",
            [(rows[i][0], len(vector), _pack(vector)) for i, vector in enumerate(vectors)],
        )
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('model', ?)",
                           (getattr(embedder, "model_name", "unknown"),))
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('dim', ?)",
                           (str(len(vectors[0]) if vectors else 0),))
        connection.commit()
        if verbose:
            logger.info("knowledge_vectors_built count=%s dim=%s",
                        len(vectors), len(vectors[0]) if vectors else 0)
        return len(vectors)

    # --- 检索 -----------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int,
        prefer_canon: bool = True,
        embedder=None,
        her_fallback: bool = True,
    ) -> list[KnowledgeItem]:
        if not self.path.exists():
            return []
        text = sanitize_chat_text(query or "", max_length=1000)
        tokens = _query_tokens(text)
        connection = sqlite3.connect(self.path, timeout=0.5)
        connection.row_factory = sqlite3.Row
        try:
            rows: list[sqlite3.Row] = []
            # 候选要**多取一些**再在 Python 里按权威度/扩展词/提到她重排：取太少的话
            # 排序再好也救不回没进候选的那几条（`limit` 是最终要给她的条数）。
            candidates = max(limit * 6, 40)
            if tokens:
                # 多取一倍再去掉构建笔记：索引重建过就没这些块了，但**旧索引**还在用的
                # 时候也得挡住（不然"排掉笔记"这件事取决于有没有重跑建索引）。
                rows = list(self._match(connection, tokens, candidates))
                # **给官方口径单独留一档候选**：只按总 bm25 取前 N 条时，885 KB 同人会
                # 把 48 KB 官方设定挤出候选池——排序键再对也救不回来（2026-09-28 实测：
                # 「心灵控制是什么」就这么掉的，官方块根本没进 40 条候选）。
                canon = max(AUTHORITY.values())
                rows.extend(self._match(connection, tokens, max(limit * 2, 10), authority=canon))
                # trigram 至少要 3 字：**短问句**（"你有家人吗"真正的内容词是"家人"）
                # 在这里会一条都召不回——2026-09-28 实测 20 条自问里 5 条召回为空。
                # 不足时再补一轮 LIKE（按命中词数打分），两路合并后统一排序。
                if len(rows) < candidates:
                    rows.extend(self._like(connection, tokens, candidates))
            # 语义那一路（2026-09-28，用户："现在这个召回太唐了"）：换个说法问同一件事
            # 词面撞不上，向量能撞上。它**只补充不取代**——词面召回照旧，两路按名次融合。
            semantic = self._semantic_candidates(connection, text, limit * 2, embedder)
            if semantic:
                rows.extend(semantic)
            # 问的是她、召回来的却一条都没提她（"你是谁""你怕什么"这种极短问句）：
            # 与其塞一段别人在别处的场景，不如给她**关于她本人**的资料当底。
            # `her_fallback=False` 只给评测用：兜底会把"提到她"这个指标刷成 100%，
            # 掩盖真正要量的东西（词面 vs 语义谁捞得到对的那一块）。
            if her_fallback and SELF_QUERY_RE.search(text) and not any(
                _mentions_her(row) for row in rows
            ):
                rows.extend(self._her_fallback(connection, 2))
        except sqlite3.Error as exc:  # 索引坏了只当没有资料
            logger.warning("knowledge_lookup_failed category=%s", type(exc).__name__)
            return []
        finally:
            connection.close()

        fused: dict[tuple[str, str], dict[str, object]] = {}
        # 这一问句触发了哪些人工同义词（"住哪" → 阿拉斯加/希望角/基地…）。
        # 命中的块在排序里单独占一档：否则扩展只是"多召回一些"，答不对题的那几条
        # （比如泛泛的基本信息表）还是会排到前面。
        expanded = _expanded_terms(text)
        for position, row in enumerate(rows):
            if is_meta_note(row["title"], row["content"]):
                continue
            key = (row["title"], (row["content"] or "")[:200])
            if key in fused:  # 多路合并会撞车：同一条只算一次，取更好的名次
                entry = fused[key]
                entry["lexical"] = min(entry["lexical"], position)  # type: ignore[type-var]
                continue
            semantic = float(row["semantic"] or 0.0) if "semantic" in row.keys() else 0.0
            fused[key] = {
                "row": row,
                "lexical": position,
                "semantic": semantic,
                "semantic_rank": -1 if not semantic else 0,
                "expansion": _expansion_hits(row, expanded),
            }
        # 语义那一路单独给名次：两路分数（bm25 / 余弦）不同量纲，**不能直接比大小**，
        # 用 RRF（Reciprocal Rank Fusion）按名次融合才是稳的做法。
        semantic_rows = sorted(
            (entry for entry in fused.values() if entry["semantic"]),
            key=lambda entry: -float(entry["semantic"]),  # type: ignore[arg-type]
        )
        for rank, entry in enumerate(semantic_rows):
            entry["semantic_rank"] = rank
        ranked = sorted(
            fused.values(),
            key=lambda entry: _rank_key(entry["row"],  # type: ignore[arg-type]
                                        lexical=int(entry["lexical"]),  # type: ignore[arg-type]
                                        semantic_rank=int(entry["semantic_rank"]),  # type: ignore[arg-type]
                                        has_semantic=bool(entry["semantic"]),
                                        expansion=int(entry["expansion"]),  # type: ignore[arg-type]
                                        prefer_canon=prefer_canon),
        )
        return [
            KnowledgeItem(source=entry["row"]["source"], title=entry["row"]["title"],
                          content=entry["row"]["content"])
            for entry in ranked[:limit]
        ]

    @staticmethod
    def _semantic_candidates(connection, text: str, limit: int, embedder):
        """向量召回：把所有向量读出来算点积（向量已 L2 归一化），取前 `limit` 块。

        734 块 × 512 维在 CPU 上是毫秒级；真要涨到几万块再换 FAISS 之类也不迟。
        没有 embedder、或索引里没有向量，就返回空——词面检索照常工作。
        """

        if embedder is None or not text:
            return []
        try:
            total = connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        except sqlite3.Error:
            return []
        if not total:
            return []
        try:
            query_vector = embedder.encode([text], use_instruction=True)[0]
        except Exception:  # noqa: BLE001 - 查询编码失败只等于"这一路没有"
            logger.warning("knowledge_query_embed_failed", exc_info=True)
            return []
        best: list[tuple[float, int]] = []
        for rowid, blob in connection.execute("SELECT chunk_rowid, vec FROM vectors"):
            vector = _unpack(blob)
            if len(vector) != len(query_vector):
                continue  # 换了模型/维度：旧向量直接跳过
            score = 0.0
            for index, value in enumerate(query_vector):
                score += value * vector[index]
            best.append((score, rowid))
        if not best:
            return []
        best.sort(reverse=True)
        picked = best[:limit]
        if not picked:
            return []
        floor = picked[0][0] - SEMANTIC_MARGIN
        rowids = [rowid for score, rowid in picked if score >= floor]
        if not rowids:
            return []
        placeholders = ",".join("?" for _ in rowids)
        scores = {rowid: score for score, rowid in picked}
        rows = connection.execute(
            f"SELECT rowid, source, title, content, authority FROM chunks "
            f"WHERE rowid IN ({placeholders})", rowids,
        ).fetchall()
        output = []
        for row in rows:
            record = dict(row)
            record["rank"] = 0.0
            record["semantic"] = scores.get(row["rowid"], 0.0)
            output.append(record)
        output.sort(key=lambda item: -item["semantic"])
        return output

    @staticmethod
    def _her_fallback(connection, limit):
        """兜底：关于她本人的官方资料（提到她名字的那些块）。

        只在"问的是她、却没召回一条提到她的资料"时用，条数也压得很小（2 条）：
        它是**保底**，不是主力——真答得上问题的还是按查询召回的那些。
        """

        pattern = f"%{HER_NAME}%"
        return connection.execute(
            "SELECT source, title, content, authority, 0.0 AS rank FROM chunks "
            "WHERE authority = ? AND (content LIKE ? OR title LIKE ?) LIMIT ?",
            (max(AUTHORITY.values()), pattern, pattern, limit),
        ).fetchall()

    @staticmethod
    def _match(connection, tokens, limit, *, authority: int | None = None):
        long_tokens = [t for t in tokens if len(t) >= MIN_TRIGRAM]
        if not long_tokens:
            return []
        expression = " OR ".join(f'"{t}"' for t in long_tokens)
        where = "chunks MATCH ?"
        params: list[object] = [expression]
        if authority is not None:
            where += " AND authority = ?"
            params.append(authority)
        params.append(limit)
        # **必须在 SQL 里先按 bm25 排一遍**：Python 那边还要按权威度/扩展词/提到她重排，
        # 但 `LIMIT` 是在 SQL 里生效的——不排序就等于在任意顺序上截断，好块根本进不了候选
        # （2026-09-28 踩过：问"住处"时"阿拉斯加希望角"那块压根没被取回来）。
        return connection.execute(
            "SELECT source, title, content, authority, "
            "bm25(chunks, 2.0, 4.0, 1.0, 0.0, 0.0) AS rank "
            f"FROM chunks WHERE {where} "
            "ORDER BY bm25(chunks, 2.0, 4.0, 1.0, 0.0, 0.0) LIMIT ?",
            tuple(params),
        ).fetchall()

    @staticmethod
    def _like(connection, tokens, limit):
        """trigram 至少 3 字才匹配得上；短的（2 字词与 2 字窗口）退回 LIKE。

        用 **OR + 命中数打分**，不是 AND："
        `你有家人吗` 切出来的 2 字窗口是"你有/有家/家人"，
        要求三个都在同一块里等于一条都召不回（实测就是 0 条）。按命中数排序，
        "家人"这种内容词自然会赢过"你有"这种虚词组合。
        """

        short = [t for t in tokens if len(t) < MIN_TRIGRAM]
        if not short:
            return []
        # 正文和标题都要找：像"云茹"这种只出现在标题里的词，光查 content 会漏。
        hit = "(content LIKE ? OR title LIKE ?)"
        expression = " + ".join(hit for _ in short)
        params: list[str] = []
        for token in short:
            params.extend((f"%{token}%", f"%{token}%"))
        return connection.execute(
            f"SELECT source, title, content, authority, -({expression}) AS rank "
            f"FROM chunks WHERE ({expression}) > 0 "
            f"ORDER BY rank ASC LIMIT ?",  # rank 越小＝命中词越多
            (*params, *params, limit),
        ).fetchall()


def _pack(vector) -> bytes:
    """float 列表 → float32 小端 BLOB（不依赖 numpy，读写两侧都用标准库）。"""

    import array

    return array.array("f", [float(value) for value in vector]).tobytes()


def _unpack(blob: bytes):
    import array

    values = array.array("f")
    values.frombytes(blob)
    return values


def _clean_heading(segment: str) -> str:
    """把标题里的批注片段去掉（`世界观关键设定（可用于 Bot）` → `世界观关键设定`）。

    去掉批注不等于丢掉这一节：里面常常是**设定事实**（"焚风基地在阿拉斯加希望角、
    藏在时间屏障里"就是这么写着的），只是当时顺手加了句"可用于 Bot"。
    """

    cleaned = re.sub(r"[（(][^（()）]*?(?i:bot|提示词|系统提示|人设)[^（()）]*?[)）]", "", segment)
    cleaned = re.sub(r"(?i)\s*[-—–]*\s*(?:与\s*)?bot\s*(?:相关|人设)?\s*$", "", cleaned)
    return cleaned.strip(" 　>-—–·、,:：")


def _authority(source: str) -> int:
    """按顶层目录给权威度：官方设定 > 官方补充 > 同人叙事。"""

    return AUTHORITY.get(source.split("/", 1)[0], 1)


def _mentions_her(row) -> int:
    """这块资料提到她了吗。**自问自答的关键判据**：问"你现在住在哪儿"，
    回来的却是别人在加尔各答撤退、1985 哈萨克斯坦的场景——那段里一个字都没提她。"""

    return 1 if HER_NAME in (row["content"] or "") or HER_NAME in (row["title"] or "") else 0


def _rank_key(row, *, lexical: int, semantic_rank: int, has_semantic: bool, expansion: int,
              prefer_canon: bool):
    """排序键（升序）：官方口径 → **同义词命中数** → 提到她 → 名次融合（RRF）。

    四段各自的理由：
    - `authority` 最先：885 KB 同人会淹没 48 KB 官方设定（当初加它就是为了这个）；
    - 同义词命中：这一问句扩出来的词（"住哪" → 阿拉斯加/希望角/基地）真出现在这块里，
      说明它答的就是这件事——排在"泛泛提到她"的前面；
    - 「提到她」：同一档里先给写她的资料。实测词面时代把"自问 top-1 提到她"从 20%
      拉到 100%，不能降级（2026-09-28 踩过：排到相关度之后，词面路径从 100% 掉到 60%）；
    - 名次融合：**bm25 与余弦不同量纲，不能直接比大小**，按各自名次做 RRF
      （`1/(60+名次)`）。没有语义那一路时它退化成词面名次，行为与从前一致。
    """

    rrf = 1.0 / (60.0 + lexical + 1)
    if has_semantic and semantic_rank >= 0:
        rrf += 1.0 / (60.0 + semantic_rank + 1)
    if not prefer_canon:
        return (-expansion if expansion else 0, -rrf, -_mentions_her(row))
    return (-int(row["authority"] or 0), -expansion, -_mentions_her(row), -rrf)


def _expanded_terms(text: str) -> tuple[str, ...]:
    """这一问句触发了哪几组人工同义词。"""

    terms: list[str] = []
    for pattern, synonyms in QUERY_EXPANSIONS:
        if pattern.search(text):
            terms.extend(synonyms)
    return tuple(dict.fromkeys(terms))


def _expansion_hits(row, terms: tuple[str, ...]) -> int:
    """这块里出现了几个扩展词。0 表示它跟"住哪/在忙什么"这件事其实没关系。"""

    if not terms:
        return 0
    haystack = f"{row['title'] or ''}\n{row['content'] or ''}"
    return sum(1 for term in terms if term in haystack)


class AsyncKnowledgeBase:
    """把同步的 `KnowledgeIndex` 接到引擎那个 `async def search` 端口上。

    **为什么必须有这一层**（2026-09-28 踩的坑，值得记下来）：`extensions.KnowledgeBase`
    声明的是 `async def search`，而 `KnowledgeIndex.search` 是同步函数。`PromptSources.collect`
    里写的是 `await self.knowledge_base.search(...)`——`await` 一个 list 会抛
    `TypeError: 'list' object can't be awaited`，又被那一层的 `except Exception` 吞掉，
    于是**知识库在生产里一次都没生效过**：`data/bot.err.log` 里 4 次
    `Stage 3 knowledge lookup failed` 全是这个。离线基准（`knowledge_tool.py`、`lore_gate_eval.py`）
    是**直接调 `KnowledgeIndex.search`** 测的，绕过了这一层，所以指标全绿而线上是空的。
    现在 `tests/test_knowledge_base.py` 里有一条测试拿 `runtime.build_knowledge_base()`
    的返回值去 `await`，专门锁这个接口。
    """

    def __init__(self, index: KnowledgeIndex):
        self.index = index

    async def search(self, query: str, *, limit: int, prefer_canon: bool = True) -> list[KnowledgeItem]:
        return self.index.search(query, limit=limit, prefer_canon=prefer_canon)


def is_meta_note(title: str, content: str) -> bool:
    """这块是不是"写给 bot 看的话"（构建笔记）——是的话不给它进 prompt。"""

    return bool(_META_TITLE_RE.search(title or "") or _META_BODY_RE.search(content or ""))


def _query_tokens(text: str) -> list[str]:
    """查询词：中文按 **3 字滑窗**（对齐 FTS5 的 trigram）＋ **2 字窗口**＋ 短词整体；英文按词。

    踩过的坑：原来给长句加的是"整段作为一个词"，FTS5 会把它当成**短语**去匹配——
    "百夫长攻城机甲是什么"要求原文里连续出现这 13 个字，结果一条都召不回（实测 0 命中）。
    trigram 索引里真正存在的是 3 字窗口，查询也得跟着切成 3 字。

    2 字窗口（2026-09-28 补）只给 LIKE 兜底用：短问句真正的内容词常常是两个字
    （"家人""喜欢"），只切 3 字窗口会一条都召不回。它们不参与 trigram 匹配
    （`_match` 里按 `len >= MIN_TRIGRAM` 过滤），所以不会污染 FTS 那一路。
    """

    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) <= 4:
            terms.append(run)
        else:
            terms.extend(run[i:i + 3] for i in range(len(run) - 2))
        if len(run) >= 2:
            terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    terms.extend(token for token in re.findall(r"[A-Za-z0-9_]{2,}", text))
    # 人工同义词（见 `QUERY_EXPANSIONS`）：问她的处境时扩到语料里真正的说法。
    for pattern, synonyms in QUERY_EXPANSIONS:
        if pattern.search(text):
            terms.extend(synonyms)
    cleaned = [t for t in dict.fromkeys(terms) if t not in _STOPWORDS and len(t) >= 2]
    # 3 字窗口优先让位给 2 字窗口之后仍要留住 trigram 那一路，所以两组分别截断再拼：
    # 只按"出现顺序"截断会把 2 字窗口挤掉一半，LIKE 兜底就没词可用了。
    long_tokens = [t for t in cleaned if len(t) >= MIN_TRIGRAM]
    short_tokens = [t for t in cleaned if len(t) < MIN_TRIGRAM]
    kept = long_tokens[:MAX_QUERY_TOKENS] + short_tokens[:MAX_QUERY_TOKENS]
    return list(dict.fromkeys(kept))


_STOPWORDS = frozenset({
    "什么", "怎么", "为什么", "哪个", "是不是", "有没有", "知道", "告诉", "说说",
    "这个", "那个", "一下", "的时候", "可以", "现在", "你们", "我们",
    # 疑问句里那些"问的是她"的虚词组合：留着只会让 LIKE 兜底捞回一堆无关段落。
    "你住", "住在", "在哪", "哪儿", "哪里", "什么", "做过", "会做", "你是谁", "就是",
})


def iter_chunks(corpus_dir: Path, *, stats: dict | None = None):
    """遍历语料，产出 `(source, title, content, lines)`。

    - 只收 `INCLUDED_DIRS` 里的文件；
    - 按 markdown 标题切段，段太长再按空行切成 `MAX_CHUNK_CHARS` 以内的块；
    - 保留 `lines`（"起始-结束行"）便于事后核对到原文；
    - `stats` 非空时累计"排掉了几段人设笔记 / 剔掉了几行批注"，供建索引时报出来。
    """

    corpus = Path(corpus_dir)
    for directory in INCLUDED_DIRS:
        base = corpus / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if any(pattern.search(path.name) for pattern in EXCLUDED_NAME_PATTERNS):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("knowledge_read_failed file=%s category=%s", path.name, type(exc).__name__)
                continue
            source = str(path.relative_to(corpus)).replace("\\", "/")
            yield from _chunks_of(source, text, stats=stats)


def _chunks_of(source: str, text: str, *, stats: dict | None = None):
    """把一份文件切成块：

    - 站外噪声（署名、"编辑于 …"、"收录于文集"、纯链接）直接丢掉，它们不是世界观；
    - **人设构建类段落整段丢**，其余段落的批注逐行剔（见 `_PERSONA_NOTE_RE`）；
    - 按 markdown 标题分段，段内累积到 `MAX_CHUNK_CHARS` 就断开；
    - 太短的碎片（< `MIN_CHUNK_CHARS`）不建索引。
    """

    chunks: list[tuple[str, str, str, str]] = []
    heading_stack: list[str] = []
    buffer: list[str] = []
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal buffer, start_line
        raw_lines = list(buffer)
        buffer = []
        first_line = start_line
        start_line = end_line + 1
        # 人设构建类段落**整段丢**：活下来的也都是"应体现…"式的指令。
        if any(_PERSONA_NOTE_RE.search(segment) for segment in heading_stack):
            if stats is not None:
                stats["meta_sections"] = stats.get("meta_sections", 0) + 1
            return
        # 其余段落**逐行**剔掉带机制词的批注，标题里的批注片段也剔掉。
        kept = [line for line in raw_lines if not _META_BODY_RE.search(line)]
        if stats is not None and len(kept) != len(raw_lines):
            stats["meta_lines"] = stats.get("meta_lines", 0) + (len(raw_lines) - len(kept))
        body = "\n".join(kept).strip()
        if len(body) < MIN_CHUNK_CHARS:
            return
        cleaned_heading = [_clean_heading(segment) for segment in heading_stack]
        title = " > ".join(segment for segment in cleaned_heading if segment).strip() or source
        title = sanitize_chat_text(title, max_length=300).strip()
        chunks.append((source, title, body, f"{first_line}-{end_line}"))

    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped and (NOISE_RE.match(stripped) or
                         (not buffer and AUTHOR_LINE_RE.match(stripped))):
            continue  # 导出页留下的署名/时间戳，不属于内容
        heading = HEADING_RE.match(line)
        if heading:
            flush(index - 1)
            level = len(heading.group(1))
            heading_stack[:] = heading_stack[: level - 1]
            heading_stack.append(heading.group(2).strip())
            buffer.append(heading.group(2).strip())
            continue
        buffer.append(line)
        if sum(len(item) + 1 for item in buffer) >= MAX_CHUNK_CHARS:
            flush(index)
    flush(len(text.splitlines()))
    yield from chunks
