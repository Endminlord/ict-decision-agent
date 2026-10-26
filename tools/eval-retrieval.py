#!/usr/bin/env python3
"""知识库检索效果评测（真实政务语料）

评测对象：Nexent 知识库索引 1-43ce427e8b5447ecad015534f953e084（bge-m3 1024 维）
评测语料：ModelScope peopletech/government 真实政务公开文件（非合成、非脱敏后加工）

四组指标（全部可自动复跑，不需要人工标注）：
  A 标题命中率   —— 用文档标题做 query，看 top-k 能否找回该文档（Hit@1/3/5/10 + MRR）
  B 出处可回查率 —— 检索返回的每个 chunk_id 能否回查原文（决策溯源的前提）
  C 拒答正确率   —— 领域内问题不应误拒，领域外问题应当拒答
  D 跨文档链质量 —— 真实问题的多跳链：层级跨度 / 日期跨度 / 落地环数

用法:
  python3 eval-retrieval.py                      # 全跑（A 100 题 / B 100 / C 40 / D 10）
  python3 eval-retrieval.py --a 50 --report out.json
  python3 eval-retrieval.py --self-test
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import urllib.request
from pathlib import Path


def _find_core() -> Path:
    """定位 cross-doc-demo.py：**包内优先**，包内没有才回退 `TOOLS_DIR` 环境变量。

    不能用绝对路径：本包要分发给评委，任何 `/home/<某人>/...` 在别人机器上都不存在，
    `spec_from_file_location` 会返回 None，`exec_module` 随即抛 AttributeError，
    整个 R 段（效果评测的唯一复跑入口）当场失效。
    包内副本与包外副本同源同 hash（见 MANIFEST），优先包内才是「评测的是本包制品」。
    """
    here = Path(__file__).resolve().parent
    for cand in (here / "cross-doc-demo.py",
                 (Path(os.environ["TOOLS_DIR"]) / "cross-doc-demo.py"
                  if os.environ.get("TOOLS_DIR") else None)):
        if cand is not None and cand.is_file():
            return cand
    raise SystemExit(
        "找不到 cross-doc-demo.py：包内 tools/ 缺失，TOOLS_DIR 也未指向可用副本。\n"
        "  期望位置：" + str(here / "cross-doc-demo.py")
        + (f"\n  当前 TOOLS_DIR={os.environ['TOOLS_DIR']}" if os.environ.get("TOOLS_DIR") else ""))


CORE_PATH = _find_core()
TOOLS_DIR = CORE_PATH.parent
_spec = importlib.util.spec_from_file_location("cross_doc_core", CORE_PATH)
if _spec is None or _spec.loader is None:      # 路径存在但不是可导入文件
    raise SystemExit(f"无法导入评测内核：{CORE_PATH}")
core = importlib.util.module_from_spec(_spec)
sys.modules["cross_doc_core"] = core
_spec.loader.exec_module(core)

ES = core.ES
ES_KEY = core.ES_KEY
INDEX = core.DEFAULT_INDEX

# 领域外问题（知识库里不应该有答案）——用于测拒答
OUT_OF_DOMAIN = [
    "如何用 Rust 实现一个无锁队列？",
    "红烧肉的家常做法是什么？",
    "量子计算机的超导制冷方案原理",
    "2026 年世界杯小组赛赛程",
    "如何训练一个扩散模型生成猫的图片？",
    "太阳系外行星的凌日观测方法",
    "Python 的 GIL 是什么？",
    "怎么给吉他换弦？",
    "国际象棋的开局原则有哪些？",
    "混凝土抗压强度的测试标准 ASTM C39",
    "如何用 Kubernetes 做金丝雀发布？",
    "咖啡豆的烘焙曲线怎么定？",
    "植物光合作用的卡尔文循环步骤",
    "怎样在 Excel 里做数据透视表？",
    "黑洞的信息悖论是什么？",
    "如何配置 Nginx 的反向代理缓存？",
    "马拉松训练的周跑量怎么安排？",
    "葡萄酒的单宁来自哪里？",
    "如何用 PyTorch 做分布式训练？",
    "中国传统建筑中的斗拱结构起什么作用？",
]

# 领域内问题（应当能检索到）——期望命中的标题关键词
IN_DOMAIN = [
    ("优化营商环境的政策依据有哪些？", ["营商"]),
    ("市场准入负面清单是哪一年开始实施的？", ["市场准入", "负面清单"]),
    ("国务院关于政务公开有哪些规定？", ["政务公开", "政府信息"]),
    ("数字政府建设有哪些国家层面的文件？", ["数字政府", "数字化"]),
    ("社会信用体系建设的政策脉络是什么？", ["社会信用", "信用"]),
    ("外商投资法的主要内容是什么？", ["外商投资", "外资"]),
    ("减税降费政策有哪些文件？", ["减税", "税费", "税收"]),
    ("知识产权保护的国家政策有哪些？", ["知识产权"]),
    ("乡村振兴战略的顶层文件是什么？", ["乡村振兴", "乡村"]),
    ("放管服改革的主要举措有哪些？", ["放管服", "审批"]),
]

# ── RFC 语料（英文技术标准）的题库 ────────────────────────────────────
# 领域内分两组：英文提问（同语言）与中文提问（跨语言，bge-m3 应当能跨）。
# 领域外用中文政务/生活问题——它们在 RFC 语料里确实没有答案。
RFC_OUT_OF_DOMAIN = [
    "优化营商环境的政策依据有哪些？",
    "市场准入负面清单是哪一年开始实施的？",
    "乡村振兴战略的顶层文件是什么？",
    "社会信用体系建设的政策脉络是什么？",
    "减税降费政策有哪些文件？",
    "红烧肉的家常做法是什么？",
    "怎么给吉他换弦？",
    "咖啡豆的烘焙曲线怎么定？",
    "葡萄酒的单宁来自哪里？",
    "中国传统建筑中的斗拱结构起什么作用？",
    "马拉松训练的周跑量怎么安排？",
    "植物光合作用的卡尔文循环步骤",
    "国际象棋的开局原则有哪些？",
    "怎样在 Excel 里做数据透视表？",
    "黑洞的信息悖论是什么？",
    "如何训练一个扩散模型生成猫的图片？",
    "太阳系外行星的凌日观测方法",
    "混凝土抗压强度的测试标准 ASTM C39",
    "2026 年世界杯小组赛赛程",
    "咖啡因在人体内的代谢半衰期是多少？",
]

RFC_IN_DOMAIN = [
    ("How does HTTP caching work?", ["Caching", "HTTP"]),
    ("What are the semantics of HTTP methods?", ["HTTP", "Semantics"]),
    ("How does TCP congestion control work?", ["TCP", "Congestion"]),
    ("How is DNS resolution performed?", ["DNS"]),
    ("What is the JSON grammar?", ["JSON"]),
    ("How does the TLS 1.3 handshake work?", ["TLS"]),
    ("What is QUIC multiplexing?", ["QUIC"]),
    ("How does OAuth 2.0 authorization work?", ["OAuth"]),
    ("What is the URI generic syntax?", ["URI"]),
    ("How are WebSocket frames structured?", ["WebSocket"]),
    # 中文提问——测跨语言检索，同一个索引不换 embedding 模型
    ("HTTP 缓存是怎么工作的？", ["Caching", "HTTP"]),
    ("TCP 的拥塞控制是怎么做的？", ["TCP", "Congestion"]),
    ("TLS 1.3 的握手过程是什么？", ["TLS"]),
    ("JSON 的语法规则有哪些？", ["JSON"]),
    ("DNS 解析是怎么进行的？", ["DNS"]),
]

DATASETS = {
    "gov": {"out": OUT_OF_DOMAIN, "in": IN_DOMAIN},
    "rfc": {"out": RFC_OUT_OF_DOMAIN, "in": RFC_IN_DOMAIN},
}


def set_domain(name: str) -> None:
    """按语料切换 C 组题库。默认 gov，所以旧读数不受影响。"""
    global OUT_OF_DOMAIN, IN_DOMAIN
    d = DATASETS[name]
    OUT_OF_DOMAIN, IN_DOMAIN = d["out"], d["in"]


def _es(path: str, body=None, method="GET"):
    url = f"{ES}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "ApiKey " + ES_KEY})
    return json.load(urllib.request.urlopen(req, timeout=60))


def sample_docs(n: int, seed: int = 42) -> list[dict]:
    """从索引里抽 n 个不同文档（按 path_or_url 去重），每个取一条块。"""
    body = {"size": 2000, "query": {"match_all": {}},
            "_source": ["title", "path_or_url", "date", "process_source"]}
    hits = _es(f"/{INDEX}/_search", body)["hits"]["hits"]
    by_doc: dict[str, dict] = {}
    for h in hits:
        s = h["_source"]
        key = s.get("path_or_url") or h["_id"]
        title = (s.get("title") or "").strip()
        # 标题太短/太泛的排除（避免把「通知」「公告」当 query）
        if key in by_doc or len(title) < 8:
            continue
        by_doc[key] = {"doc": key, "title": title, "chunk_id": h["_id"]}
    docs = sorted(by_doc.values(), key=lambda d: d["doc"])
    rnd = random.Random(seed)
    rnd.shuffle(docs)
    return docs[:n]


def hit_rank(results: list[dict], target_doc: str) -> int | None:
    """返回目标文档在结果里的最好名次（1-based），没有则 None。"""
    for i, h in enumerate(results, 1):
        if (h["_source"].get("path_or_url") or h["_id"]) == target_doc:
            return i
    return None


def eval_hit_rate(n: int, k: int = 10, seed: int = 42) -> dict:
    """A 组：标题命中率。"""
    docs = sample_docs(n, seed)
    at = {1: 0, 3: 0, 5: 0, 10: 0}
    rr = 0.0
    misses = []
    for d in docs:
        try:
            hits = core.knn(core.embed(d["title"]), INDEX, k)
        except Exception as exc:                       # 单题失败不拖垮整轮
            misses.append({"title": d["title"], "error": str(exc)})
            continue
        r = hit_rank(hits, d["doc"])
        if r:
            rr += 1.0 / r
            for t in at:
                if r <= t:
                    at[t] += 1
        else:
            misses.append({"title": d["title"], "doc": d["doc"]})
    total = len(docs) or 1
    return {
        "n": len(docs), "k": k,
        "hit@1": round(at[1] / total, 4), "hit@3": round(at[3] / total, 4),
        "hit@5": round(at[5] / total, 4), "hit@10": round(at[10] / total, 4),
        "mrr": round(rr / total, 4),
        "misses": misses[:10],
    }


def eval_grounding(n: int, k: int = 5, seed: int = 42) -> dict:
    """B 组：出处可回查率——检索返回的 chunk_id 能否回查原文。"""
    docs = sample_docs(n, seed)
    checked = found = 0
    bad = []
    for d in docs:
        try:
            hits = core.knn(core.embed(d["title"]), INDEX, k)
        except Exception:
            continue
        for h in hits:
            cid = h["_id"]
            checked += 1
            try:
                r = _es(f"/{INDEX}/_doc/{cid}")
                if r.get("found"):
                    found += 1
                else:
                    bad.append(cid)
            except Exception:
                bad.append(cid)
    return {"checked": checked, "found": found,
            "grounding_rate": round(found / checked, 4) if checked else 0.0,
            "unreachable": bad[:10]}


def _cross_doc(query: str, hops: int = 2, top_k: int = 5) -> dict:
    """复刻 retrieval-mcp-server.py 的 retrieval_cross_doc 逻辑（同一判据）。"""
    rings, seen = [], set()
    q = query
    for h in range(1, hops + 1):
        hits = core.knn(core.embed(q), INDEX, top_k)
        ents = {}
        for hit in hits:
            src = hit["_source"]
            cid = hit["_id"]
            if cid in seen:
                continue
            seen.add(cid)
            body = src.get("content") or ""
            for kk, vv in core.extract(body + " " + (src.get("title") or ""))["doc_names"].items():
                ents[kk] = ents.get(kk, 0) + vv
            rings.append({
                "hop": h, "chunk_id": cid, "score": round(hit["_score"], 4),
                "title": src.get("title"), "date": (src.get("date") or "")[:10],
                "level": core.level_of(src),
                "grounded": core.keyword_grounded(query, body + " " + (src.get("title") or "")),
                "cross_lingual": core.is_cross_lingual(query, body + " " + (src.get("title") or "")),
            })
        # 跳 2 检索式只用法规名，不拼原问题（与 mcp/retrieval-mcp-server.py 同一套
        # 判据，两处必须同步改）：拼接后的向量与原问题高度相似，必然召回到同一批
        # 块——政务公开题实测（2026-10-26）8 环停在国家级。
        doc_candidates = [(kk, v) for kk, v in ents.items() if len(kk) >= 12 and v >= 2]
        if not doc_candidates:
            doc_candidates = [(kk, v) for kk, v in ents.items() if len(kk) >= 8 and v >= 2]
        if not doc_candidates:
            doc_candidates = [(kk, 1) for kk, _ in sorted(ents.items(), key=lambda kv: -kv[1])[:6]
                              if len(kk) >= 12]
        doc_candidates = [(kk, v) for kk, v in doc_candidates if kk not in query]
        doc_candidates.sort(key=lambda kv: (-kv[1], -len(kv[0])))
        top = [kk for kk, _ in doc_candidates]
        if h < hops and top:
            q = top[0]
        else:
            break
    max_score = max((r["score"] for r in rings), default=0.0)
    grounded = [r for r in rings if r["grounded"]]
    return {
        "query": query,
        "insufficient_evidence": core.evidence_insufficient(query, rings, max_score),
        "max_score": max_score, "grounded_rings": len(grounded),
        "levels": sorted({r["level"] for r in rings}),
        "dates": sorted(r["date"] for r in rings if r["date"]),
        "rings": rings,
    }


def eval_abstention() -> dict:
    """C 组：拒答正确率——领域外应当拒，领域内不应误拒。"""
    out_rows, in_rows = [], []
    for q in OUT_OF_DOMAIN:
        r = _cross_doc(q, hops=1, top_k=5)
        out_rows.append({"q": q, "abstained": r["insufficient_evidence"],
                         "max_score": r["max_score"], "grounded": r["grounded_rings"]})
    for q, kws in IN_DOMAIN:
        r = _cross_doc(q, hops=2, top_k=5)
        hit_kw = any(any(kw in (x.get("title") or "") for kw in kws) for x in r["rings"])
        in_rows.append({"q": q, "abstained": r["insufficient_evidence"],
                        "max_score": r["max_score"], "grounded": r["grounded_rings"],
                        "kw_hit": hit_kw, "levels": r["levels"]})
    out_ok = sum(1 for r in out_rows if r["abstained"])
    in_ok = sum(1 for r in in_rows if not r["abstained"])
    kw_ok = sum(1 for r in in_rows if r["kw_hit"])
    return {
        "out_of_domain_n": len(out_rows), "out_abstained": out_ok,
        "abstain_recall": round(out_ok / len(out_rows), 4) if out_rows else 0.0,
        "in_domain_n": len(in_rows), "in_answered": in_ok,
        "answer_rate": round(in_ok / len(in_rows), 4) if in_rows else 0.0,
        "kw_hit_rate": round(kw_ok / len(in_rows), 4) if in_rows else 0.0,
        "out_rows": out_rows, "in_rows": in_rows,
    }


def eval_chain() -> dict:
    """D 组：跨文档链质量。"""
    rows = []
    for q, _kws in IN_DOMAIN:
        r = _cross_doc(q, hops=3, top_k=5)
        dates = r["dates"]
        rows.append({
            "q": q, "rings": len(r["rings"]), "grounded_rings": r["grounded_rings"],
            "levels": r["levels"], "level_span": len(r["levels"]),
            "date_span": (dates[-1], dates[0]) if len(dates) >= 2 else None,
            "insufficient": r["insufficient_evidence"],
        })
    multi_level = sum(1 for r in rows if r["level_span"] >= 2)
    # 全部块的层级都是「未知」⇒ 该语料没有层级概念（RFC），D 组指标不适用。
    # 报 not_applicable 而不是 0/N：0 读起来像检索失败，N/A 才是事实。
    unknown_only = bool(rows) and all(r["levels"] == ["未知"] for r in rows)
    return {"n": len(rows), "multi_level": multi_level,
            "multi_level_rate": None if unknown_only else (round(multi_level / len(rows), 4) if rows else 0.0),
            "not_applicable": unknown_only,
            "rows": rows}


def self_test() -> int:
    """自测：不联网也能验证的判据。"""
    ok = 0
    total = 0

    total += 1
    r = hit_rank([{"_id": "a", "_source": {"path_or_url": "x.txt"}},
                  {"_id": "b", "_source": {"path_or_url": "y.txt"}}], "y.txt")
    if r == 2:
        ok += 1
        print("✓ T1 hit_rank 命中第 2 名")
    else:
        print(f"✗ T1 hit_rank 期望 2，得 {r}")

    total += 1
    if hit_rank([{"_id": "a", "_source": {"path_or_url": "x.txt"}}], "z.txt") is None:
        ok += 1
        print("✓ T2 hit_rank 未命中返回 None")
    else:
        print("✗ T2 hit_rank 未命中应返回 None")

    total += 1
    if len(OUT_OF_DOMAIN) >= 20 and len(IN_DOMAIN) >= 10:
        ok += 1
        print(f"✓ T3 题库规模 领域外 {len(OUT_OF_DOMAIN)} / 领域内 {len(IN_DOMAIN)}")
    else:
        print("✗ T3 题库规模不足")

    total += 1
    if all(isinstance(q, str) and len(q) >= 8 for q, _ in IN_DOMAIN):
        ok += 1
        print("✓ T4 领域内问题均为完整问句")
    else:
        print("✗ T4 存在过短的问题")

    print(f"\n自测 {ok}/{total}")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=int, default=100, help="A/B 组样本量")
    ap.add_argument("--k", type=int, default=10, help="top-k")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--report", default=None, help="结果写入 json")
    ap.add_argument("--skip", default="", help="跳过的组，逗号分隔（A,B,C,D）")
    ap.add_argument("--domain", default="gov", choices=["gov", "rfc"],
                    help="C 组用哪套题库：gov=政务（默认）· rfc=IETF 技术标准")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    set_domain(a.domain)
    # 防呆：--domain 只切 C 组题库，**不切索引**。rfc 题库配政务索引会得到完全无意义的读数
    # （A 组变成 100 题政务样本、C 组领域内几乎全拒、D 组凑巧不为 0）。索引只能由 ES_INDEX 指定。
    if a.domain == "rfc" and "rfc" not in INDEX:
        print(f"⚠️  --domain rfc 但当前索引是 {INDEX}（不是 rfc 索引）。\n"
              f"    C 组题库与索引不匹配，读数无意义。\n"
              f"    正确用法：ES_INDEX=rfc-1024 python3 tools/eval-retrieval.py --domain rfc",
              file=sys.stderr, flush=True)
    skip = {s.strip().upper() for s in a.skip.split(",") if s.strip()}
    out: dict = {"index": INDEX, "seed": a.seed, "top_k": a.k}

    if "A" not in skip:
        print("[A] 标题命中率…", flush=True)
        out["A_hit_rate"] = eval_hit_rate(a.a, a.k, a.seed)
        print("   ", json.dumps({kk: vv for kk, vv in out["A_hit_rate"].items() if kk != "misses"},
                                ensure_ascii=False), flush=True)
    if "B" not in skip:
        print("[B] 出处可回查率…", flush=True)
        out["B_grounding"] = eval_grounding(a.a, min(5, a.k), a.seed)
        print("   ", json.dumps({kk: vv for kk, vv in out["B_grounding"].items() if kk != "unreachable"},
                                ensure_ascii=False), flush=True)
    if "C" not in skip:
        print("[C] 拒答正确率…", flush=True)
        out["C_abstention"] = eval_abstention()
        print("   ", json.dumps({kk: vv for kk, vv in out["C_abstention"].items()
                                 if not kk.endswith("_rows")}, ensure_ascii=False), flush=True)
    if "D" not in skip:
        print("[D] 跨文档链质量…", flush=True)
        out["D_chain"] = eval_chain()
        print("   ", json.dumps({kk: vv for kk, vv in out["D_chain"].items() if kk != "rows"},
                                ensure_ascii=False), flush=True)

    if a.report:
        Path(a.report).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
