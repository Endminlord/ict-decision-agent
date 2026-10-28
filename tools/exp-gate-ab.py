#!/usr/bin/env python3
"""exp-gate-ab.py — D7 对照实验：GraphRAG on/off 逐题 A/B（M/X 层，n=25 配对）。

设计：
- 题库 = eval-stratified 冻结构建器（build_m(25,2026) / build_x()），两臂同题；
- OFF 臂 = er._cross_doc（生产核心函数、注入前代码路径 = 消融组）；
- ON  臂 = 生产 MCP retrieval_cross_doc（含 GRAPH_BOOST 注入 + ES 标题反查，实服 8793）；
- M 判据照抄 run_m：span(levels≥2 或 dates≥2) ∧ grounded≥1 ∧ 非 insufficient；
- X 层 = 安全列：图谱"永不影响拒答"的独立再证（期望 25/25 全一致）；
- 产出：evidence/ab-gate-20261028.json（逐题）+ evidence/gate-ab-M-pairs.json（门控输入）。

用法：python3 tools/exp-gate-ab.py --report evidence/ab-gate-20261028.json
"""

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


es = _load("eval_stratified", HERE / "eval-stratified.py")
vr = _load("verify_ritual", HERE / "verify-ritual.py")
er = es.er


def pass_m(ch):
    span = (len(ch["levels"]) >= 2) or (len(ch["dates"]) >= 2)
    return bool(span and ch["grounded_rings"] >= 1 and not ch["insufficient_evidence"])


def mcp_safe(q):
    last = None
    for attempt in range(3):
        try:
            d = vr.mcp_call("retrieval_cross_doc", {"query": q, "hops": 2, "top_k": 5})
            if isinstance(d, dict) and "rings" in d:
                if "dates" not in d and "date_range" in d:
                    d["dates"] = d["date_range"] or []
                return d
            last = d
        except Exception as e:
            last = e
        time.sleep(3)
    raise SystemExit("MCP 三连失败：%r" % (last,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="evidence/ab-gate-20261028.json")
    a = ap.parse_args()
    t0 = time.time()
    print("warmup MCP …", flush=True)
    mcp_safe("先进制造业和现代服务业融合发展的政策要求是什么？")
    mq = es.build_m(25, 2026)
    xq = es.build_x()
    print("题库就绪 M=%d X=%d (%.1fs)" % (len(mq), len(xq), time.time() - t0), flush=True)

    rows_m = []
    for i, it in enumerate(mq, 1):
        off = er._cross_doc(it["q"], hops=2, top_k=5)
        on = mcp_safe(it["q"])
        rows_m.append({
            "q": it["q"][:60], "src": it["src"],
            "off": pass_m(off), "on": pass_m(on),
            "off_levels": len(off["levels"]), "on_levels": len(on["levels"]),
            "off_grounded": off["grounded_rings"], "on_grounded": on["grounded_rings"],
            "off_insuff": off["insufficient_evidence"], "on_insuff": on["insufficient_evidence"],
            "on_added": on.get("graph_rings_added"), "on_err": on.get("graph_err"),
        })
        print("M %2d/25 off=%s on=%s added=%s" % (i, rows_m[-1]["off"], rows_m[-1]["on"],
                                                  rows_m[-1]["on_added"]), flush=True)

    rows_x = []
    for i, it in enumerate(xq, 1):
        off = er._cross_doc(it["q"], hops=2, top_k=5)
        on = mcp_safe(it["q"])
        rows_x.append({
            "q": it["q"][:60],
            "off_refused": bool(off["insufficient_evidence"]),
            "on_refused": bool(on["insufficient_evidence"]),
            "off_max": off["max_score"], "on_max": on["max_score"],
            "max_score_identical": abs(off["max_score"] - on["max_score"]) < 1e-9,
        })
        print("X %2d/25 off=%s on=%s max_identical=%s" % (
            i, rows_x[-1]["off_refused"], rows_x[-1]["on_refused"],
            rows_x[-1]["max_score_identical"]), flush=True)

    m_wins = sum(1 for r in rows_m if r["on"] and not r["off"])
    m_reg = sum(1 for r in rows_m if r["off"] and not r["on"])
    m_ties = 25 - m_wins - m_reg
    x_mismatch = sum(1 for r in rows_x if r["off_refused"] != r["on_refused"])
    x_max_bad = sum(1 for r in rows_x if not r["max_score_identical"])
    out = {
        "experiment": "GraphRAG on/off A/B（D7）",
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "seed": 2026, "n": 25,
        "arms": {"off": "eval-retrieval._cross_doc（注入前路径）",
                 "on": "retrieval_cross_doc via MCP 8793（GRAPH_BOOST=on，实服）"},
        "m_layer": {"wins_on_better": m_wins, "regressions": m_reg, "ties": m_ties},
        "x_layer": {"refusal_mismatch": x_mismatch, "max_score_mismatch": x_max_bad},
        "rows_m": rows_m, "rows_x": rows_x,
        "wall_seconds": round(time.time() - t0, 1),
    }
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    pairs = {"pairs": [{"item": r["q"], "base": 1.0 if r["off"] else 0.0,
                        "cand": 1.0 if r["on"] else 0.0} for r in rows_m]}
    pair_path = Path(a.report).with_name("gate-ab-M-pairs.json")
    pair_path.write_text(json.dumps(pairs, ensure_ascii=False, indent=1), encoding="utf-8")
    print("SUMMARY M: wins=%d reg=%d ties=%d | X: refusal_mismatch=%d max_mismatch=%d | %.0fs" % (
        m_wins, m_reg, m_ties, x_mismatch, x_max_bad, time.time() - t0), flush=True)
    print("report:", a.report, "| pairs:", pair_path, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
