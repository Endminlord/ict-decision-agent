#!/usr/bin/env python3
"""按主题 / 来源 / 年份从切分语料里筛子集。

设计要点：**换主题只改关键词**，筛选器本身不动 —— 这样「迁移到另一个行业」的
改动行数为 0（对应材料里的场景无关契约判据）。

用法：
  python3 tools/select-subset.py --src ict-corpus-chunks/chunks.jsonl \\
      --out ict-corpus-subset/subset.jsonl --kws 营商环境 --limit 15000 \\
      --per-doc 3 --years 2006-2024 --seed 42 --report ict-corpus-subset/report.json
  python3 tools/select-subset.py --self-test
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path


def parse_years(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    lo, _, hi = spec.partition("-")
    return int(lo), int(hi or lo)


def year_of(rec: dict) -> int | None:
    d = (rec.get("date") or "")[:4]
    return int(d) if d.isdigit() else None


def matched(rec: dict, kws: list[str]) -> bool:
    """标题或正文命中任一关键词。"""
    t = rec.get("title") or ""
    body = rec.get("text") or ""
    return any(w in t or w in body for w in kws)


def select(records, kws, years, per_doc, limit, seed, sources=None,
           top_sources=None, per_source_cap=None):
    """分层（来源 × 年份）均衡抽样；每文档最多 per_doc 块。"""
    rnd = random.Random(seed)
    buckets: dict[tuple[str, int | None], list[dict]] = collections.defaultdict(list)
    per_doc_count: collections.Counter = collections.Counter()
    kept_docs: set[str] = set()
    for rec in records:
        if not matched(rec, kws):
            continue
        y = year_of(rec)
        if years and (y is None or not (years[0] <= y <= years[1])):
            continue
        src = rec.get("source") or "?"
        if sources and src not in sources:
            continue
        did = rec.get("doc_id") or ""
        if per_doc_count[did] >= per_doc:
            continue
        per_doc_count[did] += 1
        kept_docs.add(did)
        buckets[(src, y)].append(rec)
    # 每个分层内部打乱后轮转取，保证来源与年份均衡
    for b in buckets.values():
        rnd.shuffle(b)
    order = sorted(buckets, key=lambda k: (k[0], -(k[1] or 0)))
    picked: list[dict] = []
    cursor = {k: 0 for k in order}
    per_src: collections.Counter = collections.Counter()
    while len(picked) < limit:
        progressed = False
        for k in order:
            if len(picked) >= limit:
                break
            if per_source_cap and per_src[k[0]] >= per_source_cap:
                continue
            i = cursor[k]
            if i < len(buckets[k]):
                picked.append(buckets[k][i])
                cursor[k] = i + 1
                per_src[k[0]] += 1
                progressed = True
        if not progressed:
            break
    picked.sort(key=lambda r: (r.get("date") or "", r.get("chunk_id") or ""))
    picked_docs = {r.get("doc_id") for r in picked}
    return picked, picked_docs, {k: len(v) for k, v in buckets.items()}


def run(args) -> int:
    kws = [w for w in (args.kws or "").split(",") if w]
    if not kws:
        print("需要 --kws", file=sys.stderr)
        return 2
    years = parse_years(args.years)
    sources = set(s for s in (args.sources or "").split(",") if s) or None
    src = Path(args.src)
    if not src.exists():
        print(f"源文件不存在：{src}", file=sys.stderr)
        return 2

    def stream():
        with src.open(encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except Exception:
                    continue

    # 两遍：第一遍统计候选来源命中量（决定 --top-sources），第二遍按分层轮转取
    cand_src: collections.Counter = collections.Counter()
    for rec in stream():
        if matched(rec, kws):
            y = year_of(rec)
            if years and (y is None or not (years[0] <= y <= years[1])):
                continue
            cand_src[rec.get("source") or "?"] += 1
    if args.top_sources:
        top = {s for s, _ in cand_src.most_common(args.top_sources)}
        sources = top if sources is None else (sources & top)
        print(f"  来源收敛到命中量 Top{args.top_sources}（候选 {len(cand_src)} 个来源）")

    picked, docs, bucket_sizes = select(stream(), kws, years, args.per_doc, args.limit, args.seed, sources,
                                        per_source_cap=args.per_source_cap)
    if not picked:
        print("✗ 无命中：检查关键词/年份/来源（这是负对照应触发的分支）", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in picked:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    src_c = collections.Counter(r.get("source") or "?" for r in picked)
    yr_c = collections.Counter(year_of(r) for r in picked)
    report = {
        "src": str(src), "out": str(out), "kws": kws, "years": args.years,
        "per_doc": args.per_doc, "limit": args.limit, "seed": args.seed,
        "selected_chunks": len(picked), "selected_docs": len(docs),
        "candidate_sources": len(cand_src),
        "sources": dict(src_c.most_common()),
        "years": {str(k): v for k, v in sorted(yr_c.items(), key=lambda kv: (kv[0] is None, kv[0]))},
        "buckets": len(bucket_sizes),
    }
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 选中 {len(picked)} 块 / {len(docs)} 篇文档（候选来源 {len(cand_src)} 个，分层 {len(bucket_sizes)} 个）")
    print(f"  来源 {len(src_c)} 个： " + " · ".join(f"{s}({v})" for s, v in src_c.most_common(6)))
    ys = [y for y in yr_c if y]
    if ys:
        print(f"  年份 {min(ys)}→{max(ys)}，覆盖 {len(ys)} 个年份")
    print(f"  输出 {out}")
    return 0


def self_test() -> int:
    """S1 关键词命中 / S2 per-doc 上限 / S3 年份过滤 / S4 负对照 / S5 分层均衡。"""
    recs = []
    for i in range(30):
        recs.append({"chunk_id": f"d{i}-0", "doc_id": f"d{i}", "date": "2020-01-01",
                     "source": "甲报" if i % 2 else "乙网", "title": "营商环境优化举措",
                     "text": "内容" * 10})
    for i in range(10):  # 同一文档多块，用来测 per-doc
        recs.append({"chunk_id": f"long-{i}", "doc_id": "long", "date": "2020-01-01",
                     "source": "甲报", "title": "营商环境", "text": "x" * 20})
    recs.append({"chunk_id": "old-0", "doc_id": "old", "date": "1999-01-01",
                 "source": "甲报", "title": "营商环境", "text": "y"})
    recs.append({"chunk_id": "no-0", "doc_id": "no", "date": "2020-01-01",
                 "source": "甲报", "title": "无关", "text": "无关"})

    fails = []
    picked, docs, buckets = select(recs, ["营商环境"], (2000, 2024), 3, 100, 1)
    if len(picked) != 33:  # 30 + 3(long 上限) + 0(old 被年份滤掉)
        fails.append(f"S1/S2 期望 33，得 {len(picked)}")
    if sum(1 for r in picked if r["doc_id"] == "long") != 3:
        fails.append("S2 per-doc 上限未生效")
    if any(r["doc_id"] == "old" for r in picked):
        fails.append("S3 年份过滤未生效")
    if any(r["doc_id"] == "no" for r in picked):
        fails.append("S1 关键词过滤未生效")
    srcs = {r["source"] for r in picked}
    if srcs != {"甲报", "乙网"}:
        fails.append(f"S5 分层不均衡：{srcs}")
    # S4 负对照：无命中必须返回空且 run() 报错
    empty, _, _ = select(recs, ["不存在的词"], None, 3, 100, 1)
    if empty:
        fails.append("S4 负对照失败：不该有命中")

    if fails:
        for f in fails:
            print("  ✗ " + f)
        print(f"自测：{5 - len(fails)}/5 通过")
        return 1
    print("自测：5/5 通过（S1 关键词 / S2 per-doc / S3 年份 / S4 负对照 / S5 分层均衡）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src")
    ap.add_argument("--out")
    ap.add_argument("--kws", help="逗号分隔关键词；标题或正文命中即选中")
    ap.add_argument("--sources", help="来源白名单（逗号分隔，可选）")
    ap.add_argument("--years", help="年份区间如 2006-2024")
    ap.add_argument("--per-doc", type=int, default=3)
    ap.add_argument("--limit", type=int, default=15000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top-sources", type=int, help="只在命中量最大的 N 个来源里选（避免长尾摊薄）")
    ap.add_argument("--per-source-cap", type=int, help="单一来源最多选多少块")
    ap.add_argument("--report")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not (args.src and args.out):
        ap.print_help()
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
