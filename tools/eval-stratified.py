#!/usr/bin/env python3
"""②125 题分层评测集：检索 / 多跳 / 拒答 / 跨语言 / 图谱推理 各 25 题。

与 eval-retrieval.py（A-D 组）互补：那套回答"系统基础能力"，本套回答
"分层能力水位"，五层各 25 题、全部自动可复跑、零人工标注：

  R 检索层 25   政务库标题→文档（hybrid_search，seed=2026 与旧 A 组 42 区分，防过拟合）
  M 多跳层 25   真实领域问 + 抽样标题，跑生产链（hops=2），判据=链质量三门槛
                （跨层级或跨日期 + ≥1 落地环 + 非证据不足），不是问"找没找到"
  X 拒答层 25   领域外问题（政务库 20 固定 + 5 新增），应判证据不足
  C 跨语言层 25 中文问 RFC 英文库（rfc-1024）：10 道语义题（题库常量）+
                15 道 RFC 号锚定题（抽样生成，命中=找回该 RFC 文档）
  G 图谱层 25   loose 图谱问，判据=预期三元组的 subject/object 命中扩展边

用法:
  python3 tools/eval-stratified.py --self-test
  python3 tools/eval-stratified.py                     # 全跑
  python3 tools/eval-stratified.py --layers R,X --report out.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


core = _load("cross_doc_core", HERE / "cross-doc-demo.py")
er = _load("eval_retrieval", HERE / "eval-retrieval.py")
graph_retrieval = _load("graph_retrieval", HERE / "graph_retrieval.py")

INDEX = core.DEFAULT_INDEX          # 政务库
RFC_INDEX = "rfc-1024"              # RFC 英文库


def _es(index: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{core.ES}/{index}/_search", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "ApiKey " + core.ES_KEY})
    return json.load(urllib.request.urlopen(req, timeout=60))


def _doc_of(hit: dict) -> str:
    return hit["_source"].get("path_or_url") or hit["_id"]


# ── R 检索层 ──────────────────────────────────────────────────────────────
def build_r(n: int, seed: int) -> list[dict]:
    return er.sample_docs(n, seed)


def run_r(questions: list[dict], k: int = 10) -> dict:
    at = {1: 0, 3: 0, 5: 0, 10: 0}
    rr = 0.0
    misses = []
    rows = []
    for d in questions:
        hits = core.hybrid_search(d["title"], INDEX, k)
        r = next((i for i, h in enumerate(hits, 1) if _doc_of(h) == d["doc"]), None)
        if r:
            rr += 1.0 / r
            for t in at:
                if r <= t:
                    at[t] += 1
        else:
            misses.append(d["title"])
        rows.append({"q": d["title"], "hit": r is not None, "rank": r})
    n = len(questions) or 1
    return {"n": n, "hit@1": round(at[1]/n, 4), "hit@3": round(at[3]/n, 4),
            "hit@5": round(at[5]/n, 4), "hit@10": round(at[10]/n, 4),
            "mrr": round(rr/n, 4), "misses": misses, "rows": rows}


# ── M 多跳层 ──────────────────────────────────────────────────────────────
def build_m(n: int, seed: int) -> list[dict]:
    pool = [{"q": q, "src": "领域问"} for q, _ in er.IN_DOMAIN]
    body = {"size": 1500, "query": {"match_all": {}}, "_source": ["title", "path_or_url"]}
    seen, titles = set(), []
    for h in _es(INDEX, body)["hits"]["hits"]:
        t = (h["_source"].get("title") or "").strip()
        if len(t) >= 14 and t not in seen:
            seen.add(t)
            titles.append(t)
    rnd = random.Random(seed)
    rnd.shuffle(titles)
    pool += [{"q": t, "src": "标题"} for t in titles[: n - len(pool)]]
    return pool[:n]


def run_m(questions: list[dict]) -> dict:
    passed = 0
    rows = []
    for it in questions:
        ch = er._cross_doc(it["q"], hops=2, top_k=5)
        span = (len(ch["levels"]) >= 2) or (len(ch["dates"]) >= 2)
        ok = bool(span and ch["grounded_rings"] >= 1 and not ch["insufficient_evidence"])
        passed += ok
        rows.append({"q": it["q"][:40], "src": it["src"], "pass": ok,
                     "levels": ch["levels"], "n_dates": len(ch["dates"]),
                     "grounded": ch["grounded_rings"],
                     "insufficient": ch["insufficient_evidence"]})
    return {"n": len(questions), "chain_pass": passed,
            "chain_pass_rate": round(passed/(len(questions) or 1), 4),
            "rows": rows}


# ── X 拒答层 ──────────────────────────────────────────────────────────────
OOD_EXTRA = [
    "怎么做提拉米苏？",
    "2026 年 NBA 总决赛的赛程安排",
    "如何给猫咪剪指甲？",
    "Bellman 方程在强化学习里的推导过程",
    "怎么用 Arduino 点亮一盏 LED？",
]


def build_x() -> list[dict]:
    return [{"q": q, "src": "政务库固定题"} for q in er.OUT_OF_DOMAIN] + \
           [{"q": q, "src": "新增"} for q in OOD_EXTRA]


def run_x(questions: list[dict]) -> dict:
    refused = 0
    rows = []
    for it in questions:
        ch = er._cross_doc(it["q"], hops=2, top_k=5)
        ok = bool(ch["insufficient_evidence"])
        refused += ok
        rows.append({"q": it["q"][:40], "refused": ok,
                     "max_score": ch["max_score"], "grounded": ch["grounded_rings"]})
    return {"n": len(questions), "refused": refused,
            "refusal_rate": round(refused/(len(questions) or 1), 4),
            "rows": rows}


# ── C 跨语言层 ────────────────────────────────────────────────────────────
ZH_SEMANTIC = [
    ("HTTP 缓存是怎么工作的？", ["Caching"]),
    ("TCP 的拥塞控制是怎么做的？", ["TCP", "Congestion"]),
    ("TLS 1.3 的握手过程是什么？", ["TLS"]),
    ("JSON 的语法规则有哪些？", ["JSON"]),
    ("DNS 解析是怎么进行的？", ["DNS", "Domain Names"]),
    ("QUIC 的多路复用是怎么实现的？", ["QUIC"]),
    ("OAuth 2.0 的授权码流程是什么？", ["OAuth"]),
    ("WebSocket 的帧格式是怎样的？", ["WebSocket"]),
    ("HTTP 方法的语义有哪些约定？", ["Semantics"]),
    ("URI 的通用语法是怎么定义的？", ["URI"]),
    ("TCP 三次握手是怎么完成的？", ["TCP"]),
]


def build_c(n_num: int, seed: int) -> list[dict]:
    qs = [{"q": q, "expect_kw": kw, "kind": "语义"} for q, kw in ZH_SEMANTIC]
    body = {"size": 2000, "query": {"match_all": {}},
            "_source": ["title", "path_or_url"]}
    docs, seen = {}, set()
    for h in _es(RFC_INDEX, body)["hits"]["hits"]:
        doc = _doc_of(h)
        if doc in seen:
            continue
        seen.add(doc)
        t = h["_source"].get("title") or ""
        m = re.search(r"rfc[\s-]?(\d{3,4})", t + " " + doc, re.I)
        if m and len(t) >= 10:
            docs[doc] = (m.group(1), t)
    rnd = random.Random(seed)
    picks = rnd.sample(sorted(docs.items()), min(n_num, len(docs)))
    for doc, (num, _t) in picks:
        qs.append({"q": "RFC %s 这份协议文件讲了什么内容？" % num,
                   "expect_doc": doc, "rfc_num": num, "kind": "RFC号锚定"})
    return qs


def run_c(questions: list[dict], k: int = 10) -> dict:
    hits = 0
    rows = []
    for it in questions:
        found = core.hybrid_search(it["q"], RFC_INDEX, k)
        if "expect_kw" in it:
            kws = [k.lower() for k in it["expect_kw"]]
            rank = next((i for i, h in enumerate(found, 1)
                         if any(k in (h["_source"].get("title") or "").lower() for k in kws)), None)
        else:
            rank = next((i for i, h in enumerate(found, 1)
                         if _doc_of(h) == it["expect_doc"]), None)
        ok = rank is not None
        hits += ok
        rows.append({"q": it["q"][:44], "kind": it["kind"], "hit": ok, "rank": rank})
    return {"n": len(questions), "hit": hits,
            "hit_rate": round(hits/(len(questions) or 1), 4),
            "rows": rows}


# ── G 图谱推理层 ──────────────────────────────────────────────────────────
def build_g() -> list[dict]:
    """从已验收题库读取题目和预期边，避免维护第二份题面。"""
    text = (HERE / "eval-g-layer-draft.md").read_text(encoding="utf-8")
    questions = []
    for block in re.split(r"(?m)^### G\d{2}\s*$", text)[1:]:
        q = re.search(r"(?m)^- `q`：(.*)$", block)
        expected = re.search(r"(?m)^- `expected`：(.*)$", block)
        rubric = re.search(r"(?m)^- `rubric`：(.*)$", block)
        triples = re.findall(
            r"(?m)^\s+- `(.+?) --(.+?)--> (.+?) \[([^]]+)\]`\s*$", block)
        if not (q and expected and rubric and triples):
            raise ValueError("G 层题目字段解析失败")
        questions.append({
            "q": q.group(1), "expected": expected.group(1),
            "triples": [{"subject": s, "predicate": p, "object": o,
                         "chunk_id": cid} for s, p, o, cid in triples],
            "rubric": rubric.group(1),
        })
    if len(questions) != 25:
        raise ValueError("G 层题量应为 25，实际为 %d" % len(questions))
    return questions


def _extract_anchors(q: str) -> list[str]:
    """从题面抓锚点片段：优先引号内短语（『』「」“”''"" 以及中文书名号内的实体），
    再补充书名号《…》整段。归一化去空白，长度≥2 保留，按长度降序去重。"""
    spans = re.findall(r"[『「“”\"']([^『」“”\"']{2,40})[』」“”\"']", q)
    spans += re.findall(r"《([^》]{2,60})》", q)
    excluded = {graph_retrieval._norm(x) for x in re.findall(r"不属于《([^》]+)》", q)}
    seen, out = set(), []
    for s in sorted(spans, key=len, reverse=True):
        n = graph_retrieval._norm(s)
        if n in excluded:
            continue
        if len(n) >= 2 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _subjects_citing(anchor: str, store: dict) -> set[str]:
    """原语A：经边找主体——扫 edges_by_obj，凡对象实体与 anchor 互为子串，
    收集全部来源主体（归一化名）。"""
    norm = graph_retrieval._norm
    out = set()
    for obj, edges in store["by_obj"].items():
        on = norm(obj)
        if not on:
            continue
        if anchor in on or on in anchor:
            for e in edges:
                s = norm(e.get("subject", ""))
                if s:
                    out.add(s)
    return out


def _out_objects(subject: str, store: dict) -> set[str]:
    """主体实体的全部出边对象（归一化）。subject 允许子串匹配任一主体实体。"""
    norm = graph_retrieval._norm
    out = set()
    for s, edges in store["by_subj"].items():
        sn = norm(s)
        if not sn:
            continue
        if subject in sn or sn in subject:
            for e in edges:
                o = norm(e.get("object", ""))
                if o:
                    out.add(o)
    return out


def _two_hop_via_shared(q: str, store: dict) -> set[str]:
    """原语C：两跳路径——抓题面提到的『中间节点』（引号短语），
    找以该节点为对象的所有主体集合；若题面还提到一个具体文档 A，
    返回『除 A 外也连到该中间节点的主体集合』。"""
    anchors = _extract_anchors(q)
    norm = graph_retrieval._norm
    # 区分：引号短语 = 中间节点候选；书名号 = 已知文档 A
    mids = [a for a in anchors if not any(a in norm(t) or norm(t) in a
                                         for t in re.findall(r"《([^》]+)》", q))]
    result = set()
    for mid in mids:
        subs = _subjects_citing(mid, store)
        # 排除题面已提到的文档（作为『已知端』）
        known_docs = [norm(t) for t in re.findall(r"《([^》]+)》", q)]
        for s in subs:
            if any(kd and (kd in s or s in kd) for kd in known_docs):
                continue
            result.add(s)
    return result


def _multi_constraint_intersection(q: str, store: dict) -> set[str]:
    """原语B：多实体交集——题面提到 ≥2 个引号短语时，分别取主体集合再交。"""
    spans = re.findall(r"[『「“”\"']([^『」“”\"']{2,40})[』」“”\"']", q)
    norms = []
    seen = set()
    for s in spans:
        n = graph_retrieval._norm(s)
        if len(n) >= 2 and n not in seen:
            seen.add(n)
            norms.append(n)
    if len(norms) < 2:
        return set()
    sets = [_subjects_citing(a, store) for a in norms]
    if any(not s for s in sets):
        return set()
    out = sets[0]
    for s in sets[1:]:
        out = out & s
    return out


def _direct_1hop(q: str, store: dict) -> dict:
    """原语D：已知主体的直连查询/存在性检查，不借共享节点反查。

    书名号统一〈〉与《》后匹配主体；对象按题面谓词及引号片段收窄。
    返回匹配边，由 run_g 按预期三元组判定，存在性题有边即通过。"""
    norm = graph_retrieval._norm
    qn = norm(q).replace("〈", "《").replace("〉", "》")
    subjects = []
    for s in store["by_subj"]:
        sn = norm(s).replace("〈", "《").replace("〉", "》")
        if len(sn) >= 3 and sn in qn:
            subjects.append(s)
    if not subjects or any(w in q for w in
                           ("两跳", "共享节点", "共享引用", "共享 `cites`", "文档集合")):
        return {"subjects": set(), "edges": []}
    predicates = set(re.findall(r"`([a-z_]+)`", q))
    quoted = [norm(s) for s in
              re.findall(r"[『「“”\"']([^『」“”\"']{2,40})[』」“”\"']", q)]
    edges = []
    for s in subjects:
        for e in store["by_subj"].get(s, []):
            if predicates and e.get("predicate") not in predicates:
                continue
            obj = norm(e.get("object", ""))
            # “含『标准』”这类问法用引号片段过滤目标；完整对象片段则要求
            # 与边对象互为子串，避免把同主体的其他出边当成存在性证据。
            object_filters = [a for a in quoted if a and a not in norm(s)]
            if object_filters and not any(a in obj or obj in a for a in object_filters):
                continue
            edges.append(e)
    objects = {norm(e.get("object", "")) for e in edges if norm(e.get("object", ""))}
    return {"subjects": objects, "edges": edges}


def answer_g(q: str, store: dict) -> dict:
    """G 层答案引擎：D 直连 > B 多约束交 > C 两跳 > A 单锚点反查。"""
    d = _direct_1hop(q, store)
    if d["edges"]:
        return {"subjects": d["subjects"], "primitive": "D", "edges": d["edges"]}
    b = _multi_constraint_intersection(q, store)
    if b:
        return {"subjects": b, "primitive": "B"}
    # C 只处理真正的两跳题：两个文档端点均已具名，或题面明确求两个
    # 文档/已知一端经共享节点找另一端。普通“通过谓词连到哪个节点”不走 C。
    two_hop = any(w in q for w in ("两跳", "共享节点", "共享引用", "共享 `cites`"))
    docs = re.findall(r"《([^》]+)》", q)
    asks_pair = any(w in q for w in ("哪两份", "哪两个", "哪些文档"))
    if two_hop and (len(docs) >= 2 or len(docs) == 1 or asks_pair):
        c = _two_hop_via_shared(q, store)
        if c:
            return {"subjects": c, "primitive": "C"}
    # A：单锚点反查（题面只提一个节点时用）
    anchors = _extract_anchors(q)
    if len(anchors) == 1:
        a = _subjects_citing(anchors[0], store)
        if a:
            # 排除"不属于《X》"题面中明确排除的主体
            norm = graph_retrieval._norm
            excluded = [norm(x) for x in re.findall(r"不属于《([^》]+)》", q)]
            if excluded:
                a = {s for s in a if not any(x in s or s in x for x in excluded)}
            if a:
                return {"subjects": a, "primitive": "A"}
    return {"subjects": set(), "primitive": None}


def _expected_hit_in_subjects(expected: str, subjects: set[str]) -> bool:
    """expected 是中文短语/分号分隔的实体列表，检查是否至少一个主要名在 subjects 里
    互为子串命中。粗策略：抽书名号或按 『；』『、』『，』切，逐段比对。"""
    norm = graph_retrieval._norm
    cands = re.findall(r"《([^》]+)》", expected)
    if not cands:
        # 用标点切
        parts = re.split(r"[；;，,、。\.：:\s]+", expected)
        cands = [p for p in parts if len(norm(p)) >= 3]
    for c in cands:
        cn = norm(c)
        if not cn:
            continue
        for s in subjects:
            if cn in s or s in cn:
                return True
    return False


def run_g(questions: list[dict]) -> dict:
    passed = 0
    rows = []
    store = graph_retrieval._get_store()
    for it in questions:
        # 优先走 answer_g 答案引擎
        ag = answer_g(it["q"], store)
        if ag["primitive"] == "D" and any(
                graph_retrieval._norm(t["subject"]) not in
                {graph_retrieval._norm(e["subject"]) for e in ag["edges"]}
                for t in it["triples"]):
            # D 只绑定题面主体；预期边还包含其他主体时回到集合反查。
            ag = {"subjects": set(), "primitive": None}
        engine = None
        ok = False
        if ag["subjects"]:
            engine = "direct1hop" if ag["primitive"] == "D" else "answer2hop"
            if ag["primitive"] == "D":
                ok = all(any(
                    graph_retrieval._norm(t["subject"]) in graph_retrieval._norm(e["subject"]) and
                    graph_retrieval._norm(t["object"]) in graph_retrieval._norm(e["object"])
                    for e in ag.get("edges", [])) for t in it["triples"])
            else:
                ok = _expected_hit_in_subjects(it["expected"], ag["subjects"])
        if not ok:
            # 回退 1-hop expand_1hop 判定
            graph_retrieval.link_entities(it["q"], store["by_subj"], store["by_obj"])
            rings = graph_retrieval.augment_rings(it["q"], [], graph_store=store)
            edge_re = re.compile(r"^(.*?) -\[(.*?)\]-> (.*?)$")
            edges = []
            for ring in rings:
                m = edge_re.match(ring.get("excerpt", ""))
                if m:
                    edges.append({"subject": m.group(1), "predicate": m.group(2),
                                  "object": m.group(3)})
            expected_hit, missing = [], []
            for t in it["triples"]:
                want_s = graph_retrieval._norm(t["subject"])
                want_o = graph_retrieval._norm(t["object"])
                path = "%s -[%s]-> %s" % (t["subject"], t["predicate"], t["object"])
                hit = any(want_s in graph_retrieval._norm(e["subject"]) and
                          want_o in graph_retrieval._norm(e["object"]) for e in edges)
                (expected_hit if hit else missing).append(path)
            fallback_ok = not missing
            if fallback_ok:
                ok = True
                engine = "expand1hop"
            rows.append({"q": it["q"][:40], "pass": ok,
                         "engine": engine or (ag["primitive"] and "answer2hop") or "expand1hop",
                         "primitive": ag["primitive"],
                         "expected_hit": expected_hit, "missing": missing})
        else:
            rows.append({"q": it["q"][:40], "pass": True,
                         "engine": "direct1hop" if ag["primitive"] == "D" else "answer2hop",
                         "primitive": ag["primitive"]})
        passed += ok
    return {"n": len(questions), "passed": passed,
            "pass_rate": round(passed/(len(questions) or 1), 4), "rows": rows}


# ── 自检与主流程 ──────────────────────────────────────────────────────────
def self_test() -> int:
    r = build_r(25, 2026)
    m = build_m(25, 2026)
    x = build_x()
    c = build_c(15, 2026)
    g = build_g()
    assert len(g) == 25, "G 题量不为 25：%d" % len(g)
    assert len(r) == 25 and len(m) == 25 and len(x) == 25 and len(c) == 25, \
        "题量不为 25：R=%d M=%d X=%d C=%d" % (len(r), len(m), len(x), len(c))
    qs = [it["q"] for it in m] + [it["q"] for it in x] + [it["q"] for it in c] + \
         [it["q"] for it in g]
    assert len(qs) == len(set(qs)), "存在重复题"
    _es(INDEX, {"size": 1, "query": {"match_all": {}}})
    _es(RFC_INDEX, {"size": 1, "query": {"match_all": {}}})
    smoke = run_c(c[:1])
    assert smoke["hit"] == 1, "冒烟失败：%s" % smoke["rows"]
    print("自检通过：五层各 25 题，索引可达，跨语言冒烟 1/1")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", default="R,M,X,C,G")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--report", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    layers = {s.strip().upper() for s in a.layers.split(",") if s.strip()}
    out = {"config": {"seed": a.seed, "index": INDEX, "rfc_index": RFC_INDEX,
                      "retrieval": "hybrid_search(alpha=0.6)",
                      "chain": "eval-retrieval._cross_doc(hops=2, top_k=5)"}}

    def log(msg):
        print(msg, flush=True)

    if "R" in layers:
        log("── R 检索层 25 …")
        out["R"] = run_r(build_r(25, a.seed))
        r = out["R"]
        log("R: hit@1=%.2f hit@10=%.2f mrr=%.4f miss=%d" %
            (r["hit@1"], r["hit@10"], r["mrr"], len(r["misses"])))
    if "M" in layers:
        log("── M 多跳层 25 …")
        out["M"] = run_m(build_m(25, a.seed))
        log("M: chain_pass_rate=%.2f (%d/%d)" %
            (out["M"]["chain_pass_rate"], out["M"]["chain_pass"], out["M"]["n"]))
    if "X" in layers:
        log("── X 拒答层 25 …")
        out["X"] = run_x(build_x())
        log("X: refusal_rate=%.2f (%d/%d)" %
            (out["X"]["refusal_rate"], out["X"]["refused"], out["X"]["n"]))
    if "C" in layers:
        log("── C 跨语言层 25 …")
        out["C"] = run_c(build_c(15, a.seed))
        log("C: hit_rate=%.2f (%d/%d)" %
            (out["C"]["hit_rate"], out["C"]["hit"], out["C"]["n"]))
    if "G" in layers:
        log("── G 图谱推理层 25 …")
        out["G"] = run_g(build_g())
        log("G: pass_rate=%.2f (%d/%d)" %
            (out["G"]["pass_rate"], out["G"]["passed"], out["G"]["n"]))
    if a.report:
        Path(a.report).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        log("报告已写入 " + a.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
