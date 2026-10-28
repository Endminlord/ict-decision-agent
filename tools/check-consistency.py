#!/usr/bin/env python3
"""check-consistency.py — D10 一致性复核：文档声称 vs 仓库实际。

核对项（每项 PASS/FAIL，输出 evidence/consistency-<date>.md）：
  1. 图谱规模：模块 --stats 实测 vs key-numbers.md 声称
  2. 免疫账本：哈希链 verify 通过；观测台快照与账本笔数一致
  3. 门控：7 用例自检退出码 0
  4. 宪法版本：constitution.json vs FEASIBILITY_MATRIX.md 标注一致
  5. 校准判据：evidence/rejection-calibration.md 存在且含 τ=0.8505
  6. 分层题库：X 层 25 题常量自洽（无 ES 依赖）
  7. 治理三件套文件在位（RIGOR/STRESS/EXPERIMENT）
用法：python3 tools/check-consistency.py --out evidence/consistency-20261028.md
"""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def graph_stats():
    spec = importlib.util.spec_from_file_location("gr", str(BASE / "tools" / "graph_retrieval.py"))
    gr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gr)
    return gr._get_store()["stats"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    checks = []

    def add(name, ok, detail):
        checks.append((name, bool(ok), detail))

    # 1 图谱规模
    try:
        st = graph_stats()
        kn = (BASE / "docs" / "key-numbers.md").read_text(encoding="utf-8")
        want = (st["edges"], st["entities"])
        claimed = ("2,013" in kn.replace(",", ","), "1,865" in kn.replace(",", ","))
        add("图谱规模=账面声称", want == (2013, 1865) and all(claimed),
            "实测 %s 边/%s 实体；key-numbers 声称 2,013/1,865" % (format(st['edges'], ','), format(st['entities'], ',')))
    except Exception as e:
        add("图谱规模=账面声称", False, repr(e)[:80])

    # 2 账本
    try:
        spec = importlib.util.spec_from_file_location("il", str(BASE / "governance" / "immunity_log.py"))
        il = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(il)
        v = il.verify(str(BASE / "governance" / "immunity-log.jsonl"))
        add("免疫账本哈希链 verify", v.get("ok") is True, json.dumps(v, ensure_ascii=False))
        console = BASE / "ui" / "governance-console.html"
        if console.exists():
            html = console.read_text(encoding="utf-8")
            add("观测台快照=账本笔数", ("%d 笔" % v["records"]) in html,
                "账本 %d 笔；console 含『%d 笔』标记" % (v["records"], v["records"]))
        else:
            add("观测台快照=账本笔数", False, "ui/governance-console.html 不存在")
    except Exception as e:
        add("免疫账本哈希链 verify", False, repr(e)[:80])

    # 3 门控自检
    r = subprocess.run([sys.executable, str(BASE / "tools" / "gate-wilcoxon.py"), "--selftest"],
                       capture_output=True, text=True, timeout=60)
    add("门控 7 用例自检", r.returncode == 0 and "selftest OK" in r.stdout, r.stdout.strip()[:90])

    # 4 宪法版本一致
    try:
        ver = json.loads((BASE / "governance" / "constitution.json").read_text(encoding="utf-8"))["constitution"]["version"]
        fm = (BASE / "FEASIBILITY_MATRIX.md").read_text(encoding="utf-8")
        add("宪法版本口径一致", ("v%s" % ver) in fm, "constitution v%s" % ver)
    except Exception as e:
        add("宪法版本口径一致", False, repr(e)[:80])

    # 5 校准判据
    cal = BASE / "evidence" / "rejection-calibration.md"
    add("共形校准判据在位(τ=0.8505)", cal.exists() and "0.8505" in cal.read_text(encoding="utf-8"),
        str(cal.relative_to(BASE)))

    # 6 X 层题库常量
    try:
        spec = importlib.util.spec_from_file_location("es", str(BASE / "tools" / "eval-stratified.py"))
        es = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(es)
        xq = es.build_x()
        add("X 层拒答题库=25", len(xq) == 25, "build_x()=%d 题" % len(xq))
    except Exception as e:
        add("X 层拒答题库=25", False, repr(e)[:80])

    # 7 治理三件套
    for f in ("docs/RIGOR_MATRIX.md", "STRESS_TEST.md", "EXPERIMENT_RESULTS.md"):
        add("文件在位 %s" % f, (BASE / f).exists(), "" if (BASE / f).exists() else "缺失")

    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    n_ok = sum(1 for _, ok, _ in checks if ok)
    lines = ["# 一致性复核（D10 · %s）" % ts.split("T")[0], "",
             "**%d/%d PASS** ｜ 复核器：tools/check-consistency.py（声称 vs 实际）" % (n_ok, len(checks)), "",
             "| 核对项 | 结果 | 明细 |", "|---|---|---|"]
    for name, ok, detail in checks:
        lines.append("| %s | %s | %s |" % (name, "✅ PASS" if ok else "❌ FAIL", detail.replace("|", "\\|")))
    blob = "\n".join(lines) + "\n"
    print(blob)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(blob, encoding="utf-8")
    return 0 if n_ok == len(checks) else 2


if __name__ == "__main__":
    sys.exit(main())
