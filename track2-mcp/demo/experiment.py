#!/usr/bin/env python3
"""赛题 2 · 核验价值的对照实验

**问题**：`verify_quote` 到底有没有用？

上一版实验站不住：提示词里写了「quote 必须逐字来自片段」，等于把答案预设好了，
模型当然不编，核验自然 0 驳回 —— 用 0 驳回去证明核验有用，是循环论证。

本实验把提示词分成两组，两组都开核验：

  strict —— 要求逐字引用（旧版提示词）
  loose  —— 只要求「给出依据」（模拟真实场景：用户提问时不会预先告诉模型「不许编」）

**loose 组的驳回率才是核验环节的真实价值**：模型在自然状态下会编多少，
核验就能拦下多少。如果 loose 组也 0 驳回，那结论就是「这个模型本身够可靠，
核验是保险而不是主力」——那也是有效结论，不是失败。

用法：

    python3 demo/experiment.py --llm-key <key> --n 20 --out experiment.json
    python3 demo/experiment.py --llm-key <key> --mode loose --n 20   # 只跑一组
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ask as A  # noqa: E402

# 20 个问题：12 个领域内（语料应能支撑）、5 个领域外（应拒答）、3 个边界
QUESTIONS: list[tuple[str, str]] = [
    # 领域内 —— 政务 / 营商环境
    ("in", "国家层面的《优化营商环境条例》是哪一年出台的？"),
    ("in", "《优化营商环境条例》对中小投资者保护是怎么规定的？"),
    ("in", "安徽省在优化营商环境方面出台了哪些文件？"),
    ("in", "2024年优化营商环境有哪些新政策？"),
    ("in", "世界银行营商环境评价体系包含哪些指标？"),
    ("in", "放管服改革的主要举措有哪些？"),
    ("in", "优化营商环境的政策目标是什么？"),
    ("in", "市场主体登记管理有哪些规定？"),
    ("in", "各地在简化企业开办流程方面做了什么？"),
    ("in", "营商环境评价的指标体系是怎么设计的？"),
    ("in", "政务服务一网通办有哪些政策依据？"),
    ("in", "国家在减轻企业负担方面出台了哪些文件？"),
    # 领域外 —— 语料里不应有
    ("out", "如何做红烧肉？"),
    ("out", "量子纠缠的退相干时间是多少？"),
    ("out", "Rust 语言的所有权模型是怎么实现的？"),
    ("out", "Kubernetes 的 Pod 调度策略有哪些？"),
    ("out", "太阳系八大行星的公转周期各是多少？"),
    # 边界 —— 部分相关，考验判断
    ("edge", "北京市的营商环境怎么样？"),
    ("edge", "中小企业的融资难问题解决了吗？"),
    ("edge", "外商在华投资的准入限制有哪些变化？"),
]


def run_one(q: str, system: str, top_k: int = 6) -> dict:
    """跑一条链路，返回读数（不打印过程）。"""
    t0 = time.time()
    try:
        hits = A.knn(A.embed(q), top_k)
        chunks = [{
            "chunk_id": h["_source"].get("path_or_url", "").replace(".txt", ""),
            "title": h["_source"].get("title", ""),
            "date": h["_source"].get("date", ""),
            "score": round(h["_score"], 4),
            "text": (h["_source"].get("content") or "")[:1200],
        } for h in hits]
    except Exception as e:
        return {"ok": False, "error": f"检索失败: {e}"}

    ctx = "\n\n".join(
        f"[{i+1}] chunk_id={c['chunk_id']}\n标题：{c['title']}\n"
        f"日期：{c['date']}\n正文：{c['text']}"
        for i, c in enumerate(chunks)
    )
    try:
        raw = A.chat(system, f"问题：{q}\n\n检索片段：\n{ctx}")
    except Exception as e:
        return {"ok": False, "error": f"模型失败: {e}"}

    draft = A._extract_json(raw)
    if draft is None:
        return {"ok": False, "error": "输出里找不到 JSON", "raw": raw[:200]}

    cites = draft.get("citations") or []
    answer = (draft.get("answer") or "").strip()

    mcp = A.Mcp()
    try:
        detail, rejected = [], []
        for c in cites:
            r = mcp.tool("verify_quote", {"quote": c.get("quote", ""), "max_hits": 3})
            ok = bool(r.get("verified"))
            vb = bool(r.get("verbatim"))
            detail.append({"chunk_id": c.get("chunk_id", ""),
                           "quote": (c.get("quote") or "")[:200],
                           "verified": ok,
                           "verbatim": vb,
                           "match_kinds": r.get("match_kinds") or [],
                           "reason": "" if ok else r.get("reason", "")})
            if not ok:
                rejected.append({"quote": (c.get("quote") or "")[:60],
                                 "reason": r.get("reason", "")})
    finally:
        mcp.close()

    return {
        "ok": True,
        "top_score": chunks[0]["score"] if chunks else None,
        "answer_len": len(answer),
        "answer": answer[:800],          # 留原文，供人工核对模型到底写了什么
        "refused": "不足以支撑" in answer or len(answer) < 15,
        "citations": len(cites),
        "citations_detail": detail,      # 每条引用的原文 + 是否通过核验
        "verbatim": sum(1 for d in detail if d["verbatim"]),          # 逐字可回查
        "omitted": sum(1 for d in detail if d["verified"] and not d["verbatim"]),  # 省略
        "verified": sum(1 for d in detail if d["verified"]),
        "rejected": len(rejected),                                    # 语料里查不到
        "rejected_detail": rejected,
        "seconds": round(time.time() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="核验价值对照实验")
    ap.add_argument("--llm-key", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--n", type=int, default=20, help="跑前 N 个问题（默认 20）")
    ap.add_argument("--mode", choices=["both", "strict", "loose"], default="both")
    ap.add_argument("--top-k", type=int, default=6)
    ap.add_argument("--out", default="experiment.json")
    a = ap.parse_args()

    if a.llm_key:
        A.LLM_KEY = a.llm_key
    if a.model:
        A.LLM_MODEL = a.model
    if not A.LLM_KEY:
        print("✗ 缺少模型 key：--llm-key 或 $LLM_KEY", file=sys.stderr)
        return 2

    modes = {"strict": A.ASK_SYS, "loose": A.LOOSE_SYS}
    if a.mode != "both":
        modes = {a.mode: modes[a.mode]}

    qs = QUESTIONS[:a.n]
    print(f"模型 {A.LLM_MODEL} · 问题 {len(qs)} 个 · 提示词 {list(modes)}")
    print(f"预计 {len(qs) * len(modes) * 8 / 60:.0f} 分钟\n")

    results: dict[str, list[dict]] = {m: [] for m in modes}
    for mode, system in modes.items():
        print(f"── {mode} ──")
        for i, (kind, q) in enumerate(qs, 1):
            r = run_one(q, system, a.top_k)
            r.update({"kind": kind, "question": q, "mode": mode})
            results[mode].append(r)
            if r.get("ok"):
                flag = f"驳回 {r['rejected']}/{r['citations']}" if r["rejected"] else \
                       f"引用 {r['citations']} 全过"
                print(f"  [{i:2}/{len(qs)}] {kind:4} {r['seconds']:5.1f}s  "
                      f"{flag:16} {q[:30]}")
            else:
                print(f"  [{i:2}/{len(qs)}] {kind:4} 失败：{r.get('error')}  {q[:30]}")
        print()

    # 汇总
    print("=" * 72)
    summary = {}
    for mode, rows in results.items():
        ok = [r for r in rows if r.get("ok")]
        cites = sum(r["citations"] for r in ok)
        rej = sum(r["rejected"] for r in ok)
        vb = sum(r.get("verbatim", 0) for r in ok)
        om = sum(r.get("omitted", 0) for r in ok)
        summary[mode] = {
            "runs": len(rows),
            "ok": len(ok),
            "failed": len(rows) - len(ok),
            "questions_with_citations": sum(1 for r in ok if r["citations"] > 0),
            "questions_refused": sum(1 for r in ok if r["refused"]),
            "total_citations": cites,
            "total_verbatim": vb,
            "total_omitted": om,
            "total_rejected": rej,
            "verbatim_rate": round(vb / cites, 4) if cites else None,
            "reject_rate": round(rej / cites, 4) if cites else None,
            "questions_with_rejection": sum(1 for r in ok if r["rejected"] > 0),
            "avg_seconds": round(sum(r["seconds"] for r in ok) / len(ok), 1) if ok else None,
        }
        s = summary[mode]
        print(f"[{mode}] 跑 {s['runs']} 问（成功 {s['ok']}）· "
              f"有引用 {s['questions_with_citations']} 问 · 拒答 {s['questions_refused']} 问")
        print(f"        引用合计 {s['total_citations']} 条："
              f"逐字可回查 {s['total_verbatim']}（{s['verbatim_rate']}）· "
              f"省略号写法 {s['total_omitted']} · "
              f"语料里查不到 {s['total_rejected']}（{s['reject_rate']}）")
        print(f"        出现查不到的问题 {s['questions_with_rejection']} 个 · "
              f"平均耗时 {s['avg_seconds']}s")

    # 按问题类型拆（只看 loose，因为那是关键组）
    for mode in modes:
        print(f"\n[{mode}] 按问题类型：")
        for kind in ("in", "out", "edge"):
            rows = [r for r in results[mode] if r.get("kind") == kind and r.get("ok")]
            if not rows:
                continue
            c = sum(r["citations"] for r in rows)
            j = sum(r["rejected"] for r in rows)
            print(f"  {kind:4} {len(rows):2} 问 · 引用 {c:3} · 驳回 {j:3} · "
                  f"驳回率 {round(j/c, 3) if c else '—'}")

    Path(a.out).write_text(json.dumps(
        {"model": A.LLM_MODEL, "summary": summary, "results": results},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
