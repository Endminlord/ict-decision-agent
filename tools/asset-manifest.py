#!/usr/bin/env python3
"""资产清单：从三元组反查每个数据资产（文档级）贡献了什么。

`asset_id` 是文档级标识（= sha256(doc_id)[:16]），`chunk_id` 是块级 ——
两级粒度都能回查。这个工具给的是资产级视图，用来回答三个问题：
  1. 评审抽查：这个资产贡献了哪些事实？
  2. 增量合并：哪些资产是新的？（merge-triples 只报到实体级）
  3. 分发核对：清单承诺的资产都进来了吗？

用法：
  python3 tools/asset-manifest.py                          # 人读摘要
  python3 tools/asset-manifest.py --json                    # 机器读
  python3 tools/asset-manifest.py --asset-id a3f2...        # 单个资产明细
  python3 tools/asset-manifest.py --top 20                  # 按三元组数排前 20
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_SRC = Path(__file__).resolve().parent.parent / "ontology" / "triples-governance.jsonl"


def load(src):
    rows = []
    with open(src, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{src}:{i} JSON 解析失败：{e}")
    return rows


def build(rows):
    assets = defaultdict(lambda: {
        "doc_id": None, "title": None, "source": None, "date": None,
        "asset_type": None, "chunks": set(), "triples": 0,
        "predicates": Counter(),
    })
    for r in rows:
        p = r.get("provenance") or {}
        aid = p.get("asset_id")
        if not aid:
            continue
        a = assets[aid]
        a["doc_id"] = a["doc_id"] or p.get("doc_id")
        a["title"] = a["title"] or p.get("title")
        a["source"] = a["source"] or p.get("source")
        a["date"] = a["date"] or p.get("date")
        a["asset_type"] = a["asset_type"] or p.get("asset_type")
        if p.get("chunk_id"):
            a["chunks"].add(p["chunk_id"])
        a["triples"] += 1
        a["predicates"][r.get("predicate")] += 1
    return assets


def main():
    ap = argparse.ArgumentParser(description="从三元组反查资产级清单")
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--asset-id", help="看单个资产的明细")
    ap.add_argument("--top", type=int, default=0, help="按三元组数排前 N")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.exists():
        sys.exit(f"找不到 {src}")
    rows = load(src)
    assets = build(rows)

    if a.asset_id:
        x = assets.get(a.asset_id)
        if not x:
            sys.exit(f"没有 asset_id = {a.asset_id}")
        out = dict(x, chunks=sorted(x["chunks"]), predicates=dict(x["predicates"]))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    items = sorted(assets.items(), key=lambda kv: -kv[1]["triples"])
    if a.top:
        items = items[: a.top]

    if a.json:
        out = [dict(asset_id=k, chunks=len(v["chunks"]), **{kk: vv for kk, vv in v.items() if kk != "chunks"},
                    predicates=dict(v["predicates"])) for k, v in items]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    total_triples = sum(v["triples"] for v in assets.values())
    types = Counter(v["asset_type"] for v in assets.values())
    print(f"资产数（按 asset_id 去重）：{len(assets)}")
    print(f"覆盖三元组：{total_triples} 条（占输入 {len(rows)} 条的 {total_triples/max(len(rows),1)*100:.1f}%）")
    print(f"资产类型：{dict(types)}")
    avg = sum(len(v['chunks']) for v in assets.values()) / max(len(assets), 1)
    print(f"平均每资产块数：{avg:.2f}")
    print()
    print(f"{'asset_id':18s} {'块':>4s} {'三元组':>6s}  标题")
    for k, v in items:
        t = (v["title"] or "")[:44]
        print(f"{k:18s} {len(v['chunks']):4d} {v['triples']:6d}  {t}")


if __name__ == "__main__":
    main()
