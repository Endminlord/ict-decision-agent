#!/usr/bin/env python3
"""从语料子集生成自包含数据文件。

用法：
    python3 scripts/build_data.py                    # 用默认源与默认输出
    python3 scripts/build_data.py --limit 2000       # 只取前 2000 块（快速验证）
    python3 scripts/build_data.py --self-test        # 跑自测

源格式：JSONL，每行至少含 chunk_id / doc_id / char_offset / title / date / source / text
输出格式：gzip 压缩的 JSONL，字段同上（多余的字段丢弃，保证包体积可控）
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
DEFAULT_SRC = "/home/endmin/dsh/research/ict-corpus-subset/subset-env.jsonl"
DEFAULT_OUT = os.path.join(PKG, "src", "ict_track2_mcp", "data", "chunks.jsonl.gz")

FIELDS = ("chunk_id", "doc_id", "char_offset", "title", "date", "source", "text")


def build(src: str, out: str, limit: int | None = None) -> dict:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    n = 0
    skipped = 0
    chars = 0
    with open(src, encoding="utf-8") as fi, gzip.open(out, "wt", encoding="utf-8") as fo:
        for line in fi:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if not d.get("chunk_id") or not d.get("text"):
                skipped += 1
                continue
            rec = {k: d.get(k, "") for k in FIELDS}
            rec["char_offset"] = int(rec.get("char_offset") or 0)
            fo.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
            chars += len(rec["text"])
            if limit and n >= limit:
                break
    size = os.path.getsize(out)
    return {"chunks": n, "skipped": skipped, "chars": chars,
            "out": out, "bytes": size, "mb": round(size / 1024 / 1024, 2)}


def self_test() -> int:
    ok = 0
    total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name} {detail}")

    tmp = tempfile.mkdtemp(prefix="build_data_selftest_")
    src = os.path.join(tmp, "src.jsonl")
    out = os.path.join(tmp, "out.jsonl.gz")
    with open(src, "w", encoding="utf-8") as f:
        f.write(json.dumps({"chunk_id": "a-0001", "doc_id": "a", "char_offset": 0,
                            "title": "T1", "date": "2020-01-01", "source": "S1",
                            "text": "第一块正文", "extra": "应被丢弃"},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"chunk_id": "a-0002", "doc_id": "a", "char_offset": 100,
                            "title": "T1", "date": "2020-01-01", "source": "S1",
                            "text": "第二块正文"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"chunk_id": "", "text": "缺 id"}, ensure_ascii=False) + "\n")
        f.write("\n")

    r = build(src, out)
    check("B1 只计入合法块（跳过空 id）", r["chunks"] == 2 and r["skipped"] == 1,
          f"got chunks={r['chunks']} skipped={r['skipped']}")

    with gzip.open(out, "rt", encoding="utf-8") as f:
        rows = [json.loads(x) for x in f if x.strip()]
    check("B2 输出行数 == 合法块数", len(rows) == 2)
    check("B3 只保留 7 个约定字段", set(rows[0].keys()) == set(FIELDS),
          f"got {sorted(rows[0].keys())}")
    check("B4 中文字段原样保留", rows[0]["text"] == "第一块正文")
    check("B5 char_offset 转为整数", rows[1]["char_offset"] == 100)

    r2 = build(src, os.path.join(tmp, "out2.jsonl.gz"), limit=1)
    check("B6 limit 生效", r2["chunks"] == 1)

    print(f"\n[build_data] 自测 {ok}/{total}")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="生成自包含语料数据文件")
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not os.path.isfile(a.src):
        print(f"[build_data] 源文件不存在：{a.src}", file=sys.stderr)
        return 1
    r = build(a.src, a.out, a.limit)
    print(f"[build_data] {r['chunks']} 块（跳过 {r['skipped']}）· "
          f"{r['chars']:,} 字符 → {r['out']}")
    print(f"[build_data] 输出 {r['mb']} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
