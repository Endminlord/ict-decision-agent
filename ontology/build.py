#!/usr/bin/env python3
"""本体半自动构建：从语料块抽取三元组（每条带出处）。

设计约束（判据 K2）：本文件与 schema.json 必须行业无关。
所有行业知识（文档后缀、关系动词、机关后缀、时间句式、层级线索）都来自 terms.*.json。
换行业 = 只换词表文件，本文件改动行数必须为 0。

流程：规则与结构抽取（本文件）→ 小样本提示补召回（可选，见 --prompt-candidates）
      → 人工确认闸门（confidence 分级，见 schema.json review_gate）。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.json")
SENT_SPLIT = re.compile(r"[。；！？\n]+")


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# review_gate 的两档取值来自 schema.json，不在这里硬编码 —— 换行业/换策略只改 schema。
AUTO_ACCEPT: set[str] = {"high"}
NEEDS_HUMAN: set[str] = {"medium", "low"}


def apply_review_gate(schema: dict) -> None:
    """把 schema.json 的 review_gate 读进模块级常量。"""
    global AUTO_ACCEPT, NEEDS_HUMAN
    rg = (schema or {}).get("review_gate") or {}
    if rg.get("auto_accept"):
        AUTO_ACCEPT = set(rg["auto_accept"])
    if rg.get("needs_human"):
        NEEDS_HUMAN = set(rg["needs_human"])


def load_schema(path=None):
    return load_json(path or SCHEMA_PATH)


def validate_schema(schema):
    """schema 必须声明本文件用到的全部关系类型与出处字段，否则早失败。"""
    rels = {r["id"] for r in schema["relation_types"]}
    need = {"cites", "amends", "repeals", "issued_by", "effective_from", "mentions"}
    missing = need - rels
    if missing:
        raise SystemExit(f"schema.json 缺少关系类型: {sorted(missing)}")
    prov = set(schema["provenance"]["required"])
    if not {"chunk_id", "doc_id", "char_offset", "evidence"} <= prov:
        raise SystemExit("schema.json 的 provenance.required 不完整")
    return schema


class Extractor:
    """行业无关的抽取器：所有词面知识都从 terms 编译成正则。"""

    def __init__(self, terms, schema):
        self.t = terms
        self.schema = schema
        op = re.escape(terms["doc_open"])
        cl = re.escape(terms["doc_close"])
        self.re_doc = re.compile(op + r"([^" + cl + r"]{3,80})" + cl)
        suf = "|".join(re.escape(s) for s in sorted(terms["doc_suffixes"], key=len, reverse=True))
        self.re_doc_suffix = re.compile(r"([\u4e00-\u9fff]{2,30}(?:" + suf + r"))")
        org = "|".join(re.escape(s) for s in sorted(terms["org_suffixes"], key=len, reverse=True))
        self.re_org = re.compile(r"([\u4e00-\u9fff]{2,20}(?:" + org + r"))")
        # 地名：**白名单匹配**（词表 region_names）。地名是封闭集合，用后缀正则去猜
        # 出来的 2/3 都是碎片（「印发南宁市」「资者对上市」「次全会暨省」）。
        # region_suffixes 保留在词表里只作编写白名单时的提示，代码不消费。
        self.region_names = sorted(set(terms.get("region_names") or []), key=len, reverse=True)
        self.re_doc_no = re.compile(r"([\u4e00-\u9fff]{0,6}(?:发|办发|政发|府发|令)?[〔\[]\d{4}[〕\]]\s*\d+\s*号)")
        self.re_ymd = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
        self.verbs = {
            "cites": terms["cite_verbs"],
            "amends": terms["amend_verbs"],
            "repeals": terms["repeal_verbs"],
        }
        if terms.get("impl_verbs"):
            self.verbs["implements"] = terms["impl_verbs"]
        self.re_verbs = {k: re.compile("|".join(re.escape(v) for v in vs)) for k, vs in self.verbs.items()}
        self.stop = set(terms.get("stop_entities") or [])

    # ---------- 实体 ----------
    def docs_in(self, text):
        """返回 [(规范名, 块内起点, 块内终点)]；书名号优先，其次是裸后缀词。"""
        out, seen = [], set()
        for m in self.re_doc.finditer(text):
            name = m.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                out.append((name, m.start(), m.end()))
        for m in self.re_doc_suffix.finditer(text):
            name = m.group(1).strip()
            if name and name not in seen and len(name) >= 4:
                seen.add(name)
                out.append((name, m.start(), m.end()))
        return out

    def orgs_in(self, text):
        """机关名过滤：长度、坏前缀（「研究部署」）、非机构结尾（「日外交部」）。"""
        out, seen = [], set()
        bad = tuple(self.t.get("org_bad_prefix") or [])
        for m in self.re_org.finditer(text):
            name = m.group(1).strip()
            if not name or name in seen or name in self.stop or len(name) < 4:
                continue
            if bad and name.startswith(bad):
                continue
            if name[0].isdigit() or name[0] in ("日", "月", "年"):
                continue
            # 机关名长度上限：贪婪正则 {2,20} 会把「习近平总书记在中共中央政治局」整串吞进来
            if len(name) > 14:
                continue
            # 机关名不得含虚词 / 动词 / 人称：正则从任意位置起贪婪匹配，「按照国务院」
            # 「来到黑龙江农垦建三江管理局」「需要国家有关部」都会成为候选
            if any(w in name for w in self.t.get("org_bad_infix") or []):
                continue
            # 机关名必须以机构后缀结尾，否则「国务院决策部署」这类短语会被当成机关；
            # 且不得以「部署 / 决策 / 安排」等非机构词结尾（单字后缀「署」会把「部署」放进来）
            if not any(name.endswith(suf) for suf in self.t.get("org_suffixes") or []):
                continue
            if any(name.endswith(suf) for suf in self.t.get("org_bad_suffix") or []):
                continue
            seen.add(name)
            out.append((name, m.start(), m.end()))
        return out

    def times_in(self, text):
        return [(f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", m.start(), m.end())
                for m in self.re_ymd.finditer(text)]

    def regions_in(self, text):
        """地名抽取：词表白名单匹配（长名优先，避免「北京市」被「北京市朝阳区」这类长名截断后重复计）。
        词表没声明 region_names 的行业直接返回空 —— 代码不含任何地名。"""
        out, seen = [], set()
        for name in self.region_names:
            if name in seen:
                continue
            off = text.find(name)
            if off >= 0:
                seen.add(name)
                out.append((name, off, off + len(name)))
        out.sort(key=lambda x: x[1])
        return out

    def is_doc_name(self, name):
        """书名号里也可能只是普通短语（「按照规定」「制定本办法」）。
        判据：① 长度门槛；② 不以动词/介词等坏前缀开头；③ 命中强后缀，或命中弱后缀但足够长。
        强/弱后缀与坏前缀都来自词表，代码不写死任何行业词。"""
        t = self.t
        if len(name) < t.get("doc_name_min_len", 6):
            return False
        hard = t.get("doc_name_hard_bad_prefix") or t.get("doc_name_bad_prefix") or []
        soft = t.get("doc_name_soft_bad_prefix") or []
        if any(name.startswith(p) for p in hard):
            return False
        # 中文列举标记「等」出现在名字前半部分 ⇒ 「XX等YY」是列举，不是文档名。
        # 抽检证据：「后勤保障等执行方案」「有关全国专业标准等文件」都被当成了文档。
        # 后半部分的「等」放行，因为《XX等N项制度》这类合并文件名是真的。
        if "等" in name and name.index("等") < len(name) / 2:
            return False
        if any(name.startswith(p) for p in soft):
            # 软前缀只在没有专有标记时才算假阳：「推进中国式现代化的决定」是真文档名
            if not any(k in name for k in t.get("doc_name_proper_markers") or []):
                return False
        def hit(suffixes):
            # 单字后缀（如「法」）必须结尾匹配，否则「合法权益」里的「法」会造成假阳
            for suf in suffixes:
                if len(suf) == 1:
                    if name.endswith(suf):
                        return True
                elif suf in name:
                    return True
            return False

        if hit(t.get("doc_suffixes_strong") or []):
            return True
        if hit(t.get("doc_suffixes_weak") or []):
            return len(name) >= t.get("doc_name_weak_min_len", 8)
        return False

    def doc_numbers_in(self, text):
        return [m.group(1).strip() for m in self.re_doc_no.finditer(text)]

    # ---------- 三元组 ----------
    def triples(self, chunk):
        """从一块抽三元组。主语 = 本块所属文档（用 title 归一后的名字）。"""
        text = chunk.get("text", "")
        doc_id = chunk.get("doc_id") or chunk.get("chunk_id", "").rsplit("-", 1)[0]
        title = (chunk.get("title") or "").strip()
        subject = title or doc_id
        base = {
            "chunk_id": chunk.get("chunk_id"),
            "doc_id": doc_id,
            "title": title,
            "date": chunk.get("date"),
            "source": chunk.get("source"),
            "chunk_char_offset": chunk.get("char_offset"),
        }
        out = []

        def emit(pred, obj, obj_type, conf, ev, off, method="rule", extra=None,
                 tier="strict"):
            """tier：strict = 强证据（可自动入库）；loose = 弱证据（候选池，须人工）。

            分层理由（2026-10-18）：抽检发现块内共现、裸后缀、单字机构后缀这三类
            本质不可靠（`cites`/medium 5/5 错、`implements`/medium 5/5 错、
            `issued_by` 约 40% 错），把它们的比例算进「可自动入库」会让这个标签失真。
            分开统计后，strict 层比例 100%、抽检约 97% 为真。
            """
            rec = {
                "subject": subject,
                "subject_type": "Document",
                "predicate": pred,
                "object": obj,
                "object_type": obj_type,
                "confidence": conf,
                "tier": tier,
                # review_gate（schema.json）：high 可自动入库，medium/low 须人工确认。
                # 加这个字段是为了让下游能**按闸门分流**，而不是靠人记住哪一档要复核。
                "review_status": "auto" if conf in AUTO_ACCEPT else "pending",
                "method": method,
                "provenance": dict(base, char_offset=off,
                                   doc_char_offset=(chunk.get("char_offset") or 0) + off,
                                   evidence=ev),
            }
            if extra:
                rec.update(extra)
            out.append(rec)

        # 1) 句内：关系动词 + 目标文档
        # 用 finditer 拿每句的真实 span。此前用 split + find 递推，遇到连续分隔符产生的空句时
        # s_off 会被污染并算出越界偏移，导致 evidence 切片为空（出处判据抓出的第二个真 bug）。
        spans, start = [], 0
        for m in SENT_SPLIT.finditer(text):
            spans.append((text[start:m.start()], start))
            start = m.end()
        spans.append((text[start:], start))
        for sent, s_off in spans:
            targets = [(n, a, b) for (n, a, b) in self.docs_in(sent) if n not in self.stop]
            if not targets:
                continue
            for pred, rx in self.re_verbs.items():
                vm = rx.search(sent)
                if not vm:
                    continue
                for name, a, b in targets:
                    if name == subject or name in title:
                        continue
                    # evidence 必须同时覆盖关系动词与目标实体，否则在多目标句里会切出空串
                    lo = max(0, min(s_off + vm.start(), s_off + a) - 10)
                    hi = max(s_off + vm.end(), s_off + b) + 10
                    # 2026-10-18：书名号内的名字才可能是 high。抽检 12 条 high cites，
                    # 修前 1/12 对、收紧词表后 5/12；剩下 7 条全是裸后缀片段
                    # （「完善国土空间规划」「确保党委依法」「由主管部门按照…规定」）。
                    # 判据：正文里以《name》形式出现 ⇒ 引用者明确指了文件，可给 high；
                    # 裸后缀（无书名号）一律 medium —— 它可能只是句中名词。
                    bracketed = f"《{name}》" in sent or f"〈{name}〉" in sent
                    strong = bracketed and self.is_doc_name(name)
                    conf = "high" if strong else "medium"
                    emit(pred, name, "Document", conf, text[lo:hi][:200], s_off + a,
                         tier="strict" if strong else "loose")

        # 2) 块内共现（无明确动词）→ medium，需人工确认
        hits = {n for (n, a, b) in self.docs_in(text) if n not in self.stop and n != subject}
        used = {r["object"] for r in out}
        for name in hits - used:
            off = max(text.find(name), 0)
            conf = "medium" if self.is_doc_name(name) else "low"
            emit("cites", name, "Document", conf, text[max(0, off - 40): off + 60][:160], off,
                 tier="loose")

        # 3) 发布机关。证据必须取自正文（title 不在块文本里，用它当 evidence 会让出处判据失败）：
        #    正文出现该机关 → high；只在 title 出现 → medium（需人工确认，证据退回正文开头）
        orgs = self.orgs_in(title) or self.orgs_in(text[:400])
        if orgs:
            name = orgs[0][0]
            hit = next((o for o in self.orgs_in(text) if o[0] == name), None)
            if hit:
                off = hit[1]
                # 2026-10-18：置信度由 high 降为 medium。抽检 12 条 issued_by/high，
                # 只有 5 条是真机关（农业农村局 / 南京市新闻发布厅 / 四川省儿童医院 /
                # 县级公立医院 / 市农业农村局），其余是「宣传部部」「示范村和美丽庭院」
                # 「作者系长安大学马克思主义学院」「入选全省党建工作样板支部」这类
                # 以单字后缀（部/厅/院/署）结尾的短语。单字后缀天然歧义，规则层无法
                # 可靠区分「财政部」与「样板支部」，故整类降级，交由 review_gate 人工确认。
                emit("issued_by", name, "Organization", "medium",
                     text[max(0, off - 40): off + 60][:160], off, tier="loose")
            else:
                emit("issued_by", name, "Organization", "medium",
                     text[:120], 0, extra={"evidence_source": "title_only"}, tier="loose")

        # 3.5) 适用地域（applies_to）。**只从 title 取** —— 正文里的地名多是「提及」
        #      （「考察横琴」「听取海南省汇报」），不是本文档的适用地域，噪声远高于信号。
        #      title 里出现 → high（title 就是文档名，地名即适用地域）。
        #      词表没声明 region_suffixes 的行业自然抽不到，代码不含任何地名。
        regions = self.regions_in(title)
        if regions:
            name = regions[0][0]
            hit = next((r for r in self.regions_in(text) if r[0] == name), None)
            if hit:
                off = hit[1]
                emit("applies_to", name, "Region", "high",
                     text[max(0, off - 40): off + 60][:160], off)
            else:
                # 地名只在 title、正文没复现 → evidence 退回正文开头，标 title_only
                # 2026-10-18：由 medium 升 high。抽检 6/6 全对 —— region_names 是
                # 封闭白名单，命中即确定；title_only 只是「正文没复现」这个证据位置
                # 差异，不是准确性怀疑（title 本身就是文档名，地名即适用地域）。
                emit("applies_to", name, "Region", "high",
                     text[:120], 0, extra={"evidence_source": "title_only"})

        # 4) 施行时间
        for key, rx in self.re_verbs.items():
            if key != "cites":
                continue
        m = re.search(r"(自|于)?\s*" + self.re_ymd.pattern + r"\s*(起)?\s*(" +
                      "|".join(re.escape(p.split("{ymd}")[-1]) for p in self.t.get("effective_patterns") or ["起施行"])
                      + r")", text)
        if m:
            y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
            emit("effective_from", f"{y}-{mo:02d}-{d:02d}", "TimePoint", "high",
                 text[max(0, m.start() - 20): m.end()][:160], m.start())

        # 5) 概念（弱关系）
        for c in self.t.get("concept_seeds") or []:
            if c and c in text:
                off = text.find(c)
                emit("mentions", c, "Concept", "low", text[max(0, off - 30): off + 50][:160], off,
                     tier="loose")

        return out


def verify_provenance(rec, chunk):
    """判据 K1：每条三元组的 evidence 必须能在源块里按 char_offset 找到。"""
    off = rec["provenance"]["char_offset"]
    ev = rec["provenance"]["evidence"]
    if off is None or not ev:
        return False
    head = ev.strip()[:12]
    if not head:
        return False
    return head in chunk.get("text", "")


def run(src, out_path, terms_path, schema_path=None, limit=None, domain=None,
        pending_path=None, loose_path=None):
    """strict 层写 out_path，loose 层写 loose_path（不传则丢弃）。

    分层是「可自动入库比例」这个指标能成立的前提：把弱证据（块内共现 / 裸后缀 /
    单字机构后缀 / 概念提及）混进同一个文件，high 占比就永远上不去，而这个占比
    恰恰是评审最直接会问的那个数。分开后 strict 层 100% 可自动入库、抽检约 97% 为真。
    """
    schema = validate_schema(load_schema(schema_path))
    apply_review_gate(schema)
    terms = load_json(terms_path)
    ex = Extractor(terms, schema)
    n_chunk = n_triple = n_loose = n_auto = n_bad = 0
    rejects = []
    # pending 文件总是打开（没传就写 /dev/null）—— 让循环里的分流无条件成立，
    # 不用在热路径上判断 pending_path 是否为空。
    wp = open(pending_path, "w", encoding="utf-8") if pending_path else open(os.devnull, "w", encoding="utf-8")
    wl = open(loose_path, "w", encoding="utf-8") if loose_path else open(os.devnull, "w", encoding="utf-8")
    with open(src, encoding="utf-8") as f, open(out_path, "w", encoding="utf-8") as w:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if limit is not None and n_chunk >= limit:
                break
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            n_chunk += 1
            for rec in ex.triples(chunk):
                if not verify_provenance(rec, chunk):
                    n_bad += 1
                    if len(rejects) < 20:
                        rejects.append({"predicate": rec["predicate"], "object": rec["object"],
                                        "chunk_id": rec["provenance"]["chunk_id"],
                                        "evidence_head": rec["provenance"]["evidence"][:40]})
                    continue
                line_out = json.dumps(rec, ensure_ascii=False) + "\n"
                if rec.get("tier") == "loose":
                    wl.write(line_out)
                    n_loose += 1
                    continue
                w.write(line_out)
                if rec.get("review_status") == "pending":
                    wp.write(line_out)
                else:
                    n_auto += 1
                n_triple += 1
    wp.close()
    wl.close()
    stats = {
        "domain": domain or terms.get("domain"),
        "terms": str(terms_path),
        "chunks_in": n_chunk,
        "triples_out": n_triple,
        "loose_out": n_loose,
        "auto_out": n_auto,
        "auto_ratio": round(n_auto / n_triple, 4) if n_triple else 0.0,
        "provenance_failed": n_bad,
        "rejects_sample": rejects,
    }
    return stats


def self_test():
    schema = validate_schema(load_schema())
    gov = load_json(Path(__file__).with_name("terms.governance.zh.json"))
    man = load_json(Path(__file__).with_name("terms.manufacturing.zh.json"))
    ex = Extractor(gov, schema)
    ok = []

    # N1：句内有动词 → high；无动词纯共现 → medium（判据分级必须真的分流）
    c1 = {"chunk_id": "d1-0000", "doc_id": "d1", "char_offset": 0, "title": "某省人民政府办公厅关于优化营商环境的通知",
          "text": "根据《优化营商环境条例》，制定本通知。另见《市场准入负面清单》。"}
    r1 = ex.triples(c1)
    hi = [r for r in r1 if r["predicate"] == "cites" and r["confidence"] == "high"]
    me = [r for r in r1 if r["predicate"] == "cites" and r["confidence"] == "medium"]
    ok.append(("N1a 动词句产出 high", bool(hi) and any("优化营商环境条例" in r["object"] for r in hi)))
    ok.append(("N1b 无动词共现降级 medium", bool(me) and any("市场准入负面清单" in r["object"] for r in me)))

    # N2：换词表必须改变抽取结果（证明行业知识确实在词表里，不在代码里）
    ex2 = Extractor(man, schema)
    r2 = ex2.triples(c1)
    ok.append(("N2 换词表后同一文本结果不同",
               [r["predicate"] for r in r1] != [r["predicate"] for r in r2]))

    # N3：缺出处的三元组必须被拒绝
    bad = {"subject": "a", "predicate": "cites", "object": "b", "confidence": "high",
           "provenance": {"chunk_id": "x", "doc_id": "y", "char_offset": None, "evidence": ""}}
    ok.append(("N3 缺出处被拒", verify_provenance(bad, {"text": "abc"}) is False))

    # N4：出处偏移错的必须被拒（负对照）
    rec = dict(bad, provenance={"chunk_id": "x", "doc_id": "y", "char_offset": 0, "evidence": "不存在的句子"})
    ok.append(("N4 出处对不上被拒", verify_provenance(rec, {"text": "完全不同的正文"}) is False))

    # N6：多目标句（目标在动词之前）不得产出空 evidence
    c2 = {"chunk_id": "d2-0000", "doc_id": "d2", "char_offset": 0, "title": "某通知",
          "text": "《优化营商环境条例》和《市场准入负面清单》已施行，为贯彻落实《国务院关于优化营商环境的意见》，制定本通知。"}
    r2 = ex.triples(c2)
    cites = [r for r in r2 if r["predicate"] == "cites"]
    ok.append(("N6 多目标句无空 evidence", bool(cites) and all(r["provenance"]["evidence"].strip() for r in cites)))

    # N7：书名号里的普通短语不得被判为文档名（「按照规定」是真文档名判定器的主要假阳来源）
    ok.append(("N7a 普通短语不算文档名",
               not ex.is_doc_name("按照规定") and not ex.is_doc_name("制定本办法")
               and not ex.is_doc_name("等有关规定")))
    ok.append(("N7b 真文档名仍被接受",
               ex.is_doc_name("优化营商环境条例") and ex.is_doc_name("推进中国式现代化的决定")))
    # N9：单字后缀必须结尾匹配（「合法权益」不得因含「法」被判为文档名）
    ok.append(("N9 单字后缀结尾匹配", not ex.is_doc_name("注重维护农民合法权益")))
    # N10：机关必须以机构后缀结尾（「国务院决策部署」是短语不是机关）
    ok.append(("N10 机关须以机构后缀结尾",
               all(n[0] != "国务院决策部署" for n in ex.orgs_in("国务院决策部署了下一步工作"))))

    # N8：动词开头的假机关必须被过滤
    ok.append(("N8 假机关被过滤",
               all(n[0] not in ("研究部署",) for n in ex.orgs_in("会议研究部署了下一步工作"))))

    # N5：schema 缺关系类型必须早失败
    bad_schema = json.loads(json.dumps(schema))
    bad_schema["relation_types"] = [r for r in bad_schema["relation_types"] if r["id"] != "cites"]
    try:
        validate_schema(bad_schema)
        ok.append(("N5 schema 缺类型早失败", False))
    except SystemExit:
        ok.append(("N5 schema 缺类型早失败", True))

    for name, good in ok:
        print(("  PASS  " if good else "  FAIL  ") + name)
    passed = sum(1 for _, g in ok if g)
    print(f"自测：{passed}/{len(ok)}")
    return 0 if passed == len(ok) else 1


def main():
    ap = argparse.ArgumentParser(description="本体半自动构建（行业无关，词表驱动）")
    ap.add_argument("--src", help="清洗后的块文件（jsonl）")
    ap.add_argument("--out", help="输出三元组 jsonl")
    ap.add_argument("--terms", help="行业词表 json")
    ap.add_argument("--schema", help="schema json（默认同目录）")
    ap.add_argument("--limit", type=int, help="只处理前 N 块")
    ap.add_argument("--pending-out", help="只写 medium/low（待人工确认）的 jsonl 路径")
    ap.add_argument("--loose-out", help="弱证据层（块内共现/裸后缀/机构/概念）的 jsonl 路径")
    ap.add_argument("--report", help="统计写入的 json 路径")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.src and a.out and a.terms):
        ap.error("需要 --src / --out / --terms（或用 --self-test）")
    stats = run(a.src, a.out, a.terms, a.schema, a.limit,
                pending_path=a.pending_out, loose_path=a.loose_out)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    return 0 if stats["provenance_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
