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


def run_g(questions: list[dict]) -> dict:
    passed = 0
    rows = []
    store = graph_retrieval._get_store()
    for it in questions:
        # 保留显式实体链接调用；扩展边取生产入口实际返回的 graph rings。
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
        ok = not missing
        passed += ok
        rows.append({"q": it["q"][:40], "pass": ok,
                     "expected_hit": expected_hit, "missing": missing})
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
