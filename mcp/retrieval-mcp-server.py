#!/usr/bin/env python3
"""检索-推理 MCP 服务（retrieval-mcp-server.py）

把「跨文档推理 + 决策溯源」能力以标准 MCP（streamable-http）暴露给 Nexent 智能体。
三个工具都查真实知识库（ES + 本机 bge-m3），不返回编造数据：
  1. retrieval_cross_doc(query, hops, top_k)  多跳检索，返回链条与每环出处
  2. retrieval_trace_source(chunk_id)         按 chunk_id 回查原文（决策溯源）
  3. retrieval_corpus_stats()                 知识库与语料统计

核心逻辑复用 tools/cross-doc-demo.py（importlib 加载，避免两处实现漂移）。

用法: PORT=8793 python retrieval-mcp-server.py
"""
import importlib.util
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from fastmcp import FastMCP

TOOLS_DIR = Path(os.environ.get("TOOLS_DIR", "/home/endmin/dsh/research/tools"))
PORT = int(os.environ.get("PORT", "8793"))
CORPUS = Path(os.environ.get("CORPUS_DIR", "/home/endmin/dsh/research"))

# 复用已验证的检索核心（embed / knn / extract / keyword_grounded / level_of）
_spec = importlib.util.spec_from_file_location("cross_doc_core", TOOLS_DIR / "cross-doc-demo.py")
core = importlib.util.module_from_spec(_spec)
sys.modules["cross_doc_core"] = core
_spec.loader.exec_module(core)

mcp = FastMCP("retrieval")


@mcp.tool
def retrieval_cross_doc(query: str, hops: int = 2, top_k: int = 5) -> dict:
    """跨文档多跳检索：返回链条每一环的标题、日期、层级与出处锚（chunk_id），
    并在证据不足时明确返回 insufficient_evidence=true，不编造答案。
    query 为自然语言问题；hops 1-3；top_k 1-10。"""
    hops = max(1, min(int(hops), 3))
    top_k = max(1, min(int(top_k), 10))
    index = core.DEFAULT_INDEX
    rings, seen = [], set()
    q = query
    for h in range(1, hops + 1):
        hits = core.knn(core.embed(q), index, top_k)
        ents = {}
        for hit in hits:
            src = hit["_source"]
            cid = hit["_id"]
            if cid in seen:
                continue
            seen.add(cid)
            body = src.get("content") or ""
            e = core.extract(body + " " + (src.get("title") or ""))
            for k, v in e["doc_names"].items():
                ents[k] = ents.get(k, 0) + v
            rings.append({
                "hop": h, "chunk_id": cid, "score": round(hit["_score"], 4),
                "title": src.get("title"), "date": (src.get("date") or "")[:10],
                "level": core.level_of(src),
                "source": src.get("process_source"),
                "path_or_url": src.get("path_or_url"),
                "excerpt": body[:120],
                "grounded": core.keyword_grounded(query, body + " " + (src.get("title") or "")),
            })
        top = [k for k, _ in sorted(ents.items(), key=lambda kv: -kv[1]) if len(k) >= 6][:4]
        if h < hops and top:
            q = f"{top[0]} {query}"
        else:
            break
    max_score = max((r["score"] for r in rings), default=0.0)
    grounded = [r for r in rings if r["grounded"]]
    insufficient = max_score < 0.75 or not grounded
    levels = sorted({r["level"] for r in rings})
    dates = sorted(r["date"] for r in rings if r["date"])
    return {
        "query": query,
        "insufficient_evidence": insufficient,
        "note": "证据不足：知识库未覆盖该主题，不做推断" if insufficient else None,
        "max_score": max_score,
        "grounded_rings": len(grounded),
        "levels": levels,
        "date_range": [dates[0], dates[-1]] if dates else [],
        "rings": rings,
    }


@mcp.tool
def retrieval_trace_source(chunk_id: str) -> dict:
    """按 chunk_id 回查该块在知识库中的原文与元数据（标题、日期、来源、正文全文）。
    用于决策溯源：任何结论都能回查到具体文本。"""
    req = urllib.request.Request(
        f"{core.ES}/{core.DEFAULT_INDEX}/_doc/{chunk_id}",
        headers={"Authorization": "ApiKey " + core.ES_KEY},
    )
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if not d.get("found"):
        return {"found": False, "chunk_id": chunk_id}
    s = d["_source"]
    return {
        "found": True, "chunk_id": chunk_id,
        "title": s.get("title"), "date": (s.get("date") or "")[:10],
        "source": s.get("process_source"), "path_or_url": s.get("path_or_url"),
        "file_size": s.get("file_size"),
        "content_chars": len(s.get("content") or ""),
        "content": s.get("content"),
    }


@mcp.tool
def retrieval_corpus_stats() -> dict:
    """返回知识库与语料统计：ES 文档数、清洗后语料块数、embedding 服务健康状态。"""
    out = {"index": core.DEFAULT_INDEX}
    req = urllib.request.Request(f"{core.ES}/{core.DEFAULT_INDEX}/_count",
                                 headers={"Authorization": "ApiKey " + core.ES_KEY})
    out["indexed_docs"] = json.load(urllib.request.urlopen(req, timeout=30)).get("count")
    clean = CORPUS / "ict-corpus-clean/clean.jsonl"
    if clean.exists():
        n = 0
        with clean.open(encoding="utf-8") as f:
            for _ in f:
                n += 1
        out["cleaned_docs"] = n
    chunks = CORPUS / "ict-corpus-chunks/chunks.jsonl"
    if chunks.exists():
        n = 0
        with chunks.open(encoding="utf-8") as f:
            for _ in f:
                n += 1
        out["total_chunks"] = n
    try:
        h = json.load(urllib.request.urlopen("http://127.0.0.1:8792/health", timeout=10))
        out["embedding_service"] = {"status": h.get("status"), "model": h.get("model"), "dim": h.get("dim")}
    except Exception as exc:
        out["embedding_service"] = {"status": "unreachable", "error": str(exc)}
    out["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=PORT, path="/mcp")
