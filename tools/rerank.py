#!/usr/bin/env python3
"""rerank.py — cross-encoder 重排（bge-reranker-v2-m3）。

定位：RRF 混合召回 top-N 后的精排，主链路可选（RERANK=on 时 retrieval_cross_doc 出口前调用）。
降级路径：依赖/模型缺失时 rerank() 原样返回输入——主链路永不因精排挂掉。
用法：
  python3 tools/rerank.py --selftest            # 纯逻辑自检（不加载模型）
  python3 tools/rerank.py --probe               # 加载模型实测一对打分（可行性验证）
"""
import argparse
import json
import os
from pathlib import Path

MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
BASE = Path(__file__).resolve().parent.parent


class Reranker:
    """惰性加载的 cross-encoder；加载失败置 available=False 走恒等降级。"""

    def __init__(self, model=MODEL):
        self.model_name = model
        self.available = False
        self._ce = None
        try:
            from sentence_transformers import CrossEncoder
            self._ce = CrossEncoder(model, max_length=512)
            self.available = True
        except Exception as e:
            self.err = "%s: %s" % (type(e).__name__, e)

    def score(self, query: str, docs: list[str]) -> list[float]:
        if not self.available or not docs:
            return []
        return [float(x) for x in self._ce.predict([(query, d) for d in docs])]


def fuse(rings: list[dict], scores: list[float], top_k: int = 5, cap: float = 0.98) -> list[dict]:
    """重排分与原 RRF 分线性融合（召回不丢，序重排；cap 防精排分压过拒答护栏语义）。

    fused = 0.6 * norm(rerank) + 0.4 * norm(rrf)；norm 用批次内 min-max。
    返回按 fused 降序的前 top_k，ring 增补 via:"rerank" 与 rrf_score 留痕。
    """
    if not rings:
        return []
    if len(scores) != len(rings) or not scores:
        return rings[:top_k]
    lo_s, hi_s = min(scores), max(scores)
    rng_s = (hi_s - lo_s) or 1.0
    rrf = [float(r.get("score", 0.0)) for r in rings]
    lo_r, hi_r = min(rrf), max(rrf)
    rng_r = (hi_r - lo_r) or 1.0
    fused = []
    for r, s in zip(rings, scores):
        ns = (s - lo_s) / rng_s
        nr = (r.get("score", 0.0) - lo_r) / rng_r
        out = dict(r)
        out["rrf_score"] = r.get("score")
        out["score"] = round(min(cap, 0.6 * ns + 0.4 * nr), 4)
        out["via"] = "rerank"
        fused.append(out)
    fused.sort(key=lambda x: x["score"], reverse=True)
    return fused[:top_k]


def rerank(rings: list[dict], query: str, top_k: int = 5) -> list[dict]:
    """主链路入口：可用则精排，不可用恒等返回——调用方零负担。"""
    if not rings:
        return rings
    r = _get()
    if not r.available:
        return rings[:top_k]
    docs = ["%s\n%s" % (x.get("title") or "", x.get("excerpt") or "") for x in rings]
    return fuse(rings, r.score(query, docs), top_k)


_R = None


def _get():
    global _R
    if _R is None:
        _R = Reranker()
    return _R


def _selftest():
    rings = [{"score": 0.83, "chunk_id": "a"}, {"score": 0.91, "chunk_id": "b"},
             {"score": 0.75, "chunk_id": "c"}]
    # 精排把 c 提到第一（语义更贴），融合后顺序应翻转且分不过 cap
    out = fuse(rings, [0.2, 0.3, 0.95], top_k=3)
    assert out[0]["chunk_id"] == "c", out
    assert all(x["score"] <= 0.98 for x in out)
    assert all("via" in x and "rrf_score" in x for x in out)
    # 分数长度不齐 → 恒等截断
    assert fuse(rings, [0.1], top_k=2)[0]["chunk_id"] == "a"
    # 降级：空 rings
    assert fuse([], []) == []
    print("selftest-ok")


def _probe():
    r = Reranker()
    if not r.available:
        print("rerank-unavailable:", getattr(r, "err", "?"))
        return 1
    q = "先进制造业和现代服务业融合发展的政策要求"
    docs = ["深化先进制造业与现代服务业融合发展，推动两业融合试点。",
            "咖啡豆烘焙温度一般控制在 190-220 摄氏度。",
            "加强新型工业化布局，推进智能制造装备产业链协同。"]
    s = r.score(q, docs)
    print("probe-scores:", [round(x, 4) for x in s], "model:", r.model_name)
    print("ranking-ok" if s[0] > s[1] else "ranking-unexpected")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if a.probe:
        raise SystemExit(_probe())
    _selftest()
