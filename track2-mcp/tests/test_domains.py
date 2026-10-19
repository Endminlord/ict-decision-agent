#!/usr/bin/env python3
"""领域无关性测试：同一套判据跑在任意行业的语料上。

**这个测试存在的意义**：证明「换语料不改代码」。它不假设语料内容——
所有测试数据都从语料自身提取（第一块的正文、标题），所以对政务、医疗、
软件工程、金融风控、或任何其它领域的语料都适用。

用法：
    python3 tests/test_domains.py                          # 用包内默认语料
    python3 tests/test_domains.py --data /path/to/x.jsonl.gz
    ICT_TRACK2_DATA=/path/to/x.jsonl.gz python3 tests/test_domains.py
    python3 tests/test_domains.py --all /path/to/dir       # 跑目录下所有 .jsonl.gz
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ict_track2_mcp.lookup import DATA_FILE, Corpus  # noqa: E402

# 负例：任何语料里都不该出现的文本 / 不该匹配的主题词。
# 中英各一——只用中文负例时，英文语料（如 RFC）上是「送分题」，
# 因为中文字符本来就不可能出现在英文正文里，测不出引擎的匹配行为。
FABRICATED = (
    "根据第七七四十九条之规定，本段文字系测试编造，任何语料库中均不存在。",
    "Per Section 7749 of the imaginary specification, this fabricated sentence "
    "exists in no corpus whatsoever and must never be found by any query.",
)
OFF_TOPIC = (
    "量子纠缠退相干时间与黎曼猜想证明的联合推导xyz",
    "quantum entanglement decoherence riemann hypothesis zyzzyva proof",
)


def run_checks(c: Corpus, label: str) -> tuple[int, int]:
    ok = total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        mark = "✓" if cond else "✗"
        extra = f" — {detail}" if detail and not cond else ""
        print(f"  {mark} [{label}] {name}{extra}")

    # D1 加载
    check("D1 语料可加载且非空", len(c.chunks) > 0, f"{len(c.chunks)} 块")
    if not c.chunks:
        return ok, total

    # D2 stats 字段齐全（不假设领域）
    st = c.stats()
    need = {"chunks", "documents", "sources", "data_file", "data_path",
            "year_range", "top_sources"}
    check("D2 stats 字段齐全", need <= set(st), f"缺 {need - set(st)}")

    # D3 lookup 回查（用真实 chunk_id）
    first = next(iter(c.chunks.values()))
    got = c.lookup(first.chunk_id)
    check("D3 lookup 命中真实 chunk_id",
          got is not None and got.chunk_id == first.chunk_id,
          f"got {got.chunk_id if got else None}")

    # D4 verify 能逐字回查语料里真实存在的一句
    sample = first.text[:24]
    if len(sample) >= 6:
        hits = c.verify(sample)
        check("D4 verify 能回查真实文本",
              bool(hits) and hits[0]["match"] in ("exact", "normalized"),
              f"got {[(h['match'], h['chunk_id']) for h in hits[:2]]}")

    # D5 verify 拦住编造文本（中英各一）
    for t in FABRICATED:
        fake = c.verify(t)
        check(f"D5 verify 拦住编造文本（{t[:10]}…）", not fake, f"got {len(fake)} hits")

    # D6 sources 用真实标题做主题词能命中
    topic = first.title[:8]
    if len(topic) >= 3:
        src = c.sources(topic)
        check("D6 sources 用真实标题能命中", src["matched_chunks"] > 0,
              f"topic={topic!r} got {src['matched_chunks']}")

    # D7 sources 对无关词不假匹配且给出 hint（中英各一）
    for t in OFF_TOPIC:
        off = c.sources(t)
        check(f"D7 sources 对无关词给 hint 不假匹配（{t[:10]}…）",
              off["matched_chunks"] == 0 and bool(off.get("hint")),
              f"got {off['matched_chunks']} hint={off.get('hint')!r}")

    # D8 缓存生效后 verify 足够快（领域无关的性能底线）
    c.verify(sample)          # 预热缓存
    t0 = time.time()
    for _ in range(5):
        c.verify(sample)
    dt = (time.time() - t0) / 5 * 1000
    check("D8 缓存后 verify < 100 ms", dt < 100, f"{dt:.0f} ms/次")

    return ok, total


def main() -> int:
    ap = argparse.ArgumentParser(description="领域无关性测试")
    ap.add_argument("--data", default=None, help="单个语料文件")
    ap.add_argument("--all", default=None, help="目录，跑其中所有 .jsonl.gz")
    a = ap.parse_args()

    targets: list[tuple[str, str]] = []
    if a.all:
        for p in sorted(glob.glob(os.path.join(a.all, "*.jsonl.gz"))):
            targets.append((os.path.basename(p).split(".")[0], p))
    else:
        p = a.data or os.environ.get("ICT_TRACK2_DATA") or DATA_FILE
        targets.append((os.path.basename(p).split(".")[0], p))

    ok_all = total_all = 0
    for label, path in targets:
        print(f"\n── {label} · {path}")
        try:
            c = Corpus(path)
        except Exception as e:
            print(f"  ✗ 加载失败：{type(e).__name__}: {e}")
            total_all += 1
            continue
        ok, total = run_checks(c, label)
        ok_all += ok
        total_all += total

    print(f"\n[test_domains] {ok_all}/{total_all}"
          f"（{len(targets)} 个领域）")
    return 0 if ok_all == total_all else 1


if __name__ == "__main__":
    sys.exit(main())
