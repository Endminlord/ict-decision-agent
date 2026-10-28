#!/usr/bin/env python3
"""graph-retrieval.py — 图谱检索（GraphRAG）：实体链接 → 1-hop 扩展 → graph_ring 生成。

设计稿：docs/design-graphrag-retrieval.md（本体从治理资产升格为检索资产）。
零外部依赖（仅标准库）。主链路入口 augment_rings()：无图谱/无命中时原样返回，永不炸链。

离线自检：python3 tools/graph-retrieval.py --selftest
"""
import argparse
import json
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 模块可能从 research/tools（BASE=research）或 ict-open-source/tools 加载；
# 三元组在 <repo>/evidence/ 下，逐根探测。
_ROOTS = [BASE, BASE / "ict-open-source"]
DEFAULT_SOURCES = [
    "evidence/manufacturing-triples-loose.jsonl",
    "evidence/finance-triples-loose.jsonl",
    "evidence/yitu-triples-v2-loose.jsonl",
    "evidence/finance-triples-strict.jsonl",
]

PREDICATE_W = {"cites": 0.90, "implements": 0.85, "supersedes": 0.88,
               "references": 0.85, "mentions": 0.70}
CONF_W = {"high": 0.85, "medium": 0.72, "low": 0.60}
GRAPH_CAP = 0.90          # 图分上限（设计稿 §4：防图谱噪声压过稠密语义）
MAX_GRAPH_RINGS = 4       # 每次查询最多注入的图环数


def _norm(s: str) -> str:
    return "".join((s or "").split())


def load_edges(sources=None):
    """装载三元组；返回 (edges_by_subject, edges_by_object, stats)。
    两个索引都按归一化实体名建倒排；坏行跳过不抛。"""
    edges_by_subj, edges_by_obj = {}, {}
    n_ok = n_bad = 0
    rels = (sources or DEFAULT_SOURCES)
    files = []
    for rel in rels:
        for root in _ROOTS:
            f = root / rel
            if f.exists():
                files.append(f)
                break
    for f in files:
        if not f.exists():
            continue
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
                s, o, pr = _norm(e["subject"]), _norm(e["object"]), e["predicate"]
                if not s or not o:
                    n_bad += 1
                    continue
                edges_by_subj.setdefault(s, []).append(e)
                edges_by_obj.setdefault(o, []).append(e)
                n_ok += 1
            except (KeyError, ValueError):
                n_bad += 1
    return edges_by_subj, edges_by_obj, {"edges": n_ok, "bad_lines": n_bad,
                                          "entities": len(set(edges_by_subj) | set(edges_by_obj))}


def link_entities(query: str, edges_by_subj, edges_by_obj):
    """实体链接：长实体优先的子串匹配（起步版：精确包含，见设计稿 §6 误链护栏）。"""
    qn = _norm(query)
    ents = set(edges_by_subj) | set(edges_by_obj)
    hits = [e for e in ents if len(e) >= 3 and e in qn]
    hits.sort(key=len, reverse=True)
    return hits[:5]


def expand_1hop(entities, edges_by_subj, edges_by_obj, max_edges=12):
    """1-hop 扩展：任一端命中即取边；按（谓词权重×置信权重）降序截断。"""
    seen, picked = set(), []
    ents = set(entities)
    for idx in (edges_by_subj, edges_by_obj):
        for e in ents:
            for edge in idx.get(e, []):
                k = id(edge)
                if k in seen:
                    continue
                seen.add(k)
                w = PREDICATE_W.get(edge.get("predicate", ""), 0.75)
                c = CONF_W.get(edge.get("confidence", "low"), 0.60)
                picked.append((w * c, edge))
    picked.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in picked[:max_edges]]


def make_graph_rings(edges, cap=GRAPH_CAP):
    """边 → graph_ring（hop=2；grounded=False 留给主链词面落地判定）。"""
    rings = []
    now = time.strftime("%Y-%m-%d")
    seen_cids = set()
    for e in edges:
        prov = e.get("provenance") or {}
        cid = prov.get("chunk_id", "")
        if cid:
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
        w = PREDICATE_W.get(e.get("predicate", ""), 0.75)
        c = CONF_W.get(e.get("confidence", "low"), 0.60)
        path = "%s -[%s]-> %s" % (e.get("subject", "")[:40], e.get("predicate", "?"),
                                  e.get("object", "")[:40])
        rings.append({
            "hop": 2,
            "chunk_id": prov.get("chunk_id", ""),
            "title": (prov.get("title") or e.get("subject", ""))[:60],
            "date": prov.get("date", "") or now,
            "level": "graph",
            "source": "graph-retrieval",
            "via": "graph",
            "path_or_url": prov.get("doc_id", ""),
            "excerpt": path,
            "score": round(min(cap, w * c + 0.05), 4),
            "grounded": False,
            "cross_lingual": False,
        })
    return rings


def augment_rings(query: str, rings: list, graph_store=None, max_new: int = MAX_GRAPH_RINGS):
    """主链路入口：稠密/词面 rings ∪ graph_rings（按 chunk_id 去重）。
    graph_store 为 None 时惰性装载并缓存；装载失败/零命中 → 原样返回。"""
    if graph_store is None:
        graph_store = _get_store()
    if not graph_store.get("ok"):
        return rings
    idx = graph_store
    ents = link_entities(query, idx["by_subj"], idx["by_obj"])
    if not ents:
        return rings
    edges = expand_1hop(ents, idx["by_subj"], idx["by_obj"])
    if not edges:
        return rings
    existing = {r.get("chunk_id") for r in rings if r.get("chunk_id")}
    new = [r for r in make_graph_rings(edges) if r["chunk_id"] not in existing]
    return list(rings) + new[:max_new]


_STORE = None


def _get_store():
    global _STORE
    if _STORE is None:
        try:
            by_s, by_o, st = load_edges()
            _STORE = {"ok": st["edges"] > 0, "by_subj": by_s, "by_obj": by_o, "stats": st}
        except Exception as e:
            _STORE = {"ok": False, "err": str(e)}
    return _STORE


def _selftest():
    fake = {"by_subj": {}, "by_obj": {}, "ok": True}
    e1 = {"subject": "甲文件", "subject_type": "Document", "predicate": "cites",
          "object": "乙办法", "object_type": "Document", "confidence": "medium",
          "provenance": {"chunk_id": "c-1", "title": "甲文件", "doc_id": "d1"}}
    e2 = {"subject": "乙办法", "subject_type": "Document", "predicate": "implements",
          "object": "十四五规划", "object_type": "Policy", "confidence": "high",
          "provenance": {"chunk_id": "c-2", "title": "乙办法", "doc_id": "d2"}}
    for e in (e1, e2):
        fake["by_subj"].setdefault(_norm(e["subject"]), []).append(e)
        fake["by_obj"].setdefault(_norm(e["object"]), []).append(e)
    # 链接：问句含"乙办法" → 命中
    ents = link_entities("乙办法对制造业有什么要求", fake["by_subj"], fake["by_obj"])
    assert ents == [_norm("乙办法")], ents
    # 1-hop：乙办法作为 subject 的 implements 边 + 作为 object 的 cites 边
    edges = expand_1hop(ents, fake["by_subj"], fake["by_obj"])
    assert len(edges) == 2, edges
    # 分数：high implements (0.85*0.85) > medium cites (0.9*0.72)
    rings = make_graph_rings(edges)
    assert rings[0]["score"] >= rings[1]["score"]
    assert all(r["via"] == "graph" and r["hop"] == 2 for r in rings)
    assert rings[0]["score"] <= GRAPH_CAP
    # 去重合流：已有 c-2 的稠密环 → 只注入 c-1
    dense = [{"chunk_id": "c-2", "score": 0.83, "title": "dense-hit"}]
    merged = augment_rings("乙办法对制造业有什么要求", dense, graph_store=fake)
    assert len(merged) == 2 and merged[1]["chunk_id"] == "c-1", merged
    assert merged[0]["title"] == "dense-hit"          # 稠密环在前，图环只做增量
    # 零命中：原样返回
    assert augment_rings("完全无关的问题", dense, graph_store=fake) == dense
    print("selftest-ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--stats", action="store_true", help="装载真实图谱并打印规模")
    a = ap.parse_args()
    if a.stats:
        s = _get_store()
        print(json.dumps(s.get("stats", s), ensure_ascii=False))
    else:
        _selftest()
