#!/usr/bin/env python3
"""关键数字自检：把「文档里写的数」和「代码实测的数」放在一起比。

为什么需要这个：2026-10-18 的分层改动之后，材料包里有 16 处文档还在写旧阶梯
（`6,793 / 31,210` 那两档），而代码已经产不出那些数了 —— 没有任何测试会发现这件事，
因为测试只查「代码对不对」，不查「文档说的和代码产的是不是同一个数」。

用法：
  python3 tools/check-numbers.py            # 全部检查
  python3 tools/check-numbers.py --quiet    # 只输出汇总行（给 verify-all.sh 用）

退出码：0 全部一致 / 1 有不一致或无法检查。

**不是所有数字都能自检**：`757,810 条`、`4.18 GB`、`63 分钟`、`387 条/秒` 这些
依赖外部语料或一次性测量，脚本会列为「不可自检」而不是判失败。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OK, BAD, SKIP = [], [], []
QUIET = False


def ok(name, got, want):
    OK.append(name) if got == want else BAD.append((name, got, want))
    if not QUIET:
        mark = "✓" if got == want else "✗"
        print(f"  {mark} {name}: {got}" + ("" if got == want else f"（文档写 {want}）"))


def skip(name, why):
    SKIP.append((name, why))
    if not QUIET:
        print(f"  – {name}: 跳过（{why}）")


def read_jsonl_count(path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def main():
    global QUIET
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    QUIET = a.quiet

    print("[1/6] 材料包文件数")
    mf = HERE / "MANIFEST.sha256"
    n = sum(1 for line in open(mf, encoding="utf-8") if line.strip())
    # 不硬编码总数（会随每次加文件变），只校验「README 声称的」与「清单实际的」一致
    if not QUIET:
        print(f"  · MANIFEST 条数: {n}（不设期望值，只与 README 对齐）")
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    claimed = re.search(r"wc -l MANIFEST\.sha256` = (\d+)", readme)
    ok("README 声称的文件数", int(claimed.group(1)) if claimed else -1, n)

    print("[2/6] 三元组分层")
    strict = read_jsonl_count(HERE / "ontology/triples-governance.jsonl")
    ok("强证据层条数", strict, 12594)
    loose_path = HERE / "ontology/triples-governance-loose.jsonl"
    if loose_path.exists():
        ok("候选层条数", read_jsonl_count(loose_path), 226710)
    else:
        skip("候选层条数", "不随包，可由 build.py --loose-out 重建")

    print("[3/6] 引用图三档")
    # strict 档：直接用包内 reason.py 跑
    tmp = "/tmp/_check_numbers_reason.json"
    r = subprocess.run(
        [sys.executable, str(HERE / "ontology/reason.py"),
         "--src", str(HERE / "ontology/triples-governance.jsonl"),
         "--out", tmp, "--chains", "/tmp/_check_numbers_chains.jsonl"],
        capture_output=True, text=True, cwd=str(HERE))
    if r.returncode == 0 and Path(tmp).exists():
        d = json.load(open(tmp, encoding="utf-8"))
        ok("strict 节点", d["nodes"], 2827)
        ok("strict 边", d["edges"], 7009)
        ok("strict 闭包对", d["closure_pairs"], 7117)
    else:
        BAD.append(("strict 图规模", "reason.py 失败", "2827/7009/7117"))
    skip("全量档 14,035 / 148,005", "需合并候选层，不随包")
    skip("medium+ 档 12,718 / 109,240", "需合并候选层，不随包")

    print("[4/6] track2 判据数")
    tests = HERE / "track2-mcp/tests"
    counts = {}
    for name, want in (("test_lookup.py", 55), ("test_mcp_protocol.py", 28), ("test_domains.py", 10)):
        p = tests / name
        if not p.exists():
            skip(f"{name} 条数", "文件不存在")
            continue
        r = subprocess.run([sys.executable, str(p)], capture_output=True, text=True, cwd=str(HERE / "track2-mcp"))
        m = re.search(r"(\d+)/(\d+)", r.stdout + r.stderr)
        got = int(m.group(2)) if m else -1
        counts[name] = got
        ok(f"{name} 条数", got, want)
    if len(counts) == 3:
        ok("四层合计（含 build 6 条）", 6 + sum(counts.values()), 99)

    print("[5/6] 文档里不该出现的旧数字")
    stale = ["6,793", "31,210", "32,163", "13,026", "118,452", "136,395", "15,828", "2,115", "255,451"]
    hits = []
    for p in HERE.rglob("*.md"):
        if ".venv" in p.parts or "node_modules" in p.parts:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for s in stale:
            if s in txt:
                hits.append(f"{p.relative_to(HERE)}: {s}")
    if hits:
        for h in hits:
            BAD.append(("旧数字残留", h, "0 处"))
    else:
        ok("旧阶梯数字残留", 0, 0)

    print("[6/7] 演示卡四条命令的分数")
    demos = [
        ("Q1 跨层级", "国家层面的优化营商环境条例是哪一年出台的？", {}, 0.8816),
        ("Q2 跨语言", "HTTP/2 的流量控制怎么工作？", {"ES_INDEX": "rfc-1024"}, 0.8168),
        ("Q2b 跨时间", "营商环境政策这几年有什么变化？", {}, 0.8633),
    ]
    # Q3 是拒答，不能只比分数 —— 判输出里有没有「证据不足」
    r3 = subprocess.run([sys.executable, str(HERE / "tools/cross-doc-demo.py"), "--query", "量子计算的退相干时间如何测量？"],
                        capture_output=True, text=True, cwd=str(HERE), timeout=120)
    ok("演示卡 Q3 拒答", "证据不足" in r3.stdout, True)

    for label, q, env, want in demos:
        e = dict(os.environ); e.update(env)
        r = subprocess.run([sys.executable, str(HERE / "tools/cross-doc-demo.py"), "--query", q],
                           capture_output=True, text=True, cwd=str(HERE), env=e, timeout=120)
        m = re.search(r"最高分 ([0-9.]+)", r.stdout)
        ok(f"演示卡 {label}", float(m.group(1)) if m else -1.0, want)

    print("[7/7] 不可自检的数字（依赖外部语料或一次性测量）")
    for name, why in (
        ("757,810 条 / 4.18 GB", "原始语料不随包"),
        ("1,864,451 块", "全量块化产物不随包"),
        ("15,494 块（检索入库）", "需 ES 在跑"),
        ("63 分钟（全量判定）", "一次性测量"),
        ("387 条/秒（embedding）", "需 GPU 环境"),
    ):
        skip(name, why)

    print()
    if BAD:
        print(f"汇总  一致 {len(OK)} · 不一致 {len(BAD)} · 跳过 {len(SKIP)}")
        for name, got, want in BAD:
            print(f"  ✗ {name}: 实测 {got} · 期望 {want}")
        return 1
    print(f"汇总  一致 {len(OK)} · 不一致 0 · 跳过 {len(SKIP)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
