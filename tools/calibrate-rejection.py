#!/usr/bin/env python3
"""拒答校准：split-conformal 单侧校准 —— 用域内分数分布的数据驱动分位取代拍脑袋阈值。

方法：τ_α = 域内 max_score 的有限样本 (1-α) 分位（k=ceil((n+1)(1-α))），
保证域内新题被答覆盖率 ≥ 1-α；再实测该 τ 下域外拒答率，与现行 0.75 对照。
用法：python3 tools/calibrate-rejection.py [--n-ind 25] [--skip-ood 0]
输出：evidence/rejection-calibration.md + evidence/rejection-curve.svg
"""
import argparse
import importlib.util
import json
import math
import statistics
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, str(BASE / rel))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def collect(v, questions, label):
    rows = []
    for i, it in enumerate(questions, 1):
        q = it["q"]
        rec = {"q": q[:36], "label": label, "score": None, "refused": None}
        for a in range(3):
            try:
                d = v.mcp_call("retrieval_cross_doc", {"query": q, "hops": 2, "top_k": 5})
                rec["score"] = round(float(d.get("max_score", 0.0)), 4)
                rec["refused"] = bool(d.get("insufficient_evidence"))
                break
            except Exception:
                time.sleep(6 * (a + 1))
        rows.append(rec)
        print("  [%s %d/%d] %.4f %s" % (label, i, len(questions), rec["score"] or 0, "OK" if rec["score"] is not None else "MISS"))
        time.sleep(0.4)
    return rows


def quantile_tau(sorted_scores, alpha):
    # 覆盖率保证在低尾：tau 取域内分数的第 ceil((n+1)*alpha) 小值，
    # 则可交换性给出 P(新域内题 < tau) <= alpha，即被答覆盖率 >= 1-alpha。
    n = len(sorted_scores)
    k = max(1, min(n, math.ceil((n + 1) * alpha)))
    return sorted_scores[k - 1]


def curve_svg(s_in, s_ood, taus):
    W, H, L, B = 560, 300, 56, 40
    lo, hi = 0.55, 1.0
    xs = lambda t: L + (t - lo) / (hi - lo) * (W - L - 20)
    ys = lambda p: B + (1 - p) * (H - B - 30)
    def poly(scores, fn):
        pts = []
        for t in taus:
            p = fn(scores, t)
            pts.append("%d,%d" % (xs(t), ys(p)))
        return " ".join(pts)
    cov = lambda s, t: sum(1 for x in s if x >= t) / len(s)
    rej = lambda s, t: sum(1 for x in s if x < t) / len(s)
    g = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" font-family="sans-serif">' % (W, H)]
    g.append('<rect width="%d" height="%d" fill="#fafbfd"/>' % (W, H))
    for p in (0, 0.25, 0.5, 0.75, 1):
        g.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#e3e6ee"/>'
                 % (L, ys(p), W - 20, ys(p)))
        g.append('<text x="%d" y="%d" font-size="10" fill="#889" text-anchor="end">%d%%</text>'
                 % (L - 6, ys(p) + 3, p * 100))
    for t in (0.6, 0.7, 0.75, 0.8, 0.9, 1.0):
        g.append('<text x="%d" y="%d" font-size="10" fill="#889" text-anchor="middle">%.2f</text>'
                 % (xs(t), H - 20, t))
    g.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#3a6ea5" stroke-width="2"/>'
             % (xs(0.75), B, xs(0.75), H - 30))
    g.append('<text x="%d" y="%d" font-size="10" fill="#3a6ea5">现行0.75</text>' % (xs(0.75) - 44, B + 8))
    g.append('<polyline points="%s" fill="none" stroke="#2e8b57" stroke-width="2"/>'
             % poly(s_in, cov))
    g.append('<polyline points="%s" fill="none" stroke="#c2452d" stroke-width="2" stroke-dasharray="5,3"/>'
             % poly(s_ood, rej))
    g.append('<text x="%d" y="16" font-size="11" fill="#2e8b57">— 域内覆盖率（被答）</text>' % (L + 8))
    g.append('<text x="%d" y="30" font-size="11" fill="#c2452d">-- 域外拒答率</text>' % (L + 8))
    g.append('<text x="%d" y="%d" font-size="10" fill="#667">阈值 τ</text>' % (W - 60, H - 6))
    g.append('</svg>')
    return "\n".join(g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-ind", type=int, default=25)
    ap.add_argument("--alpha-list", default="0.20,0.10,0.05")
    a = ap.parse_args()
    ev = load("ev", "tools/eval-stratified.py")
    v = load("v", "tools/verify-ritual.py")

    def es_retry(fn, tries=4):
        for i in range(tries):
            try:
                return fn()
            except Exception as e:
                print("  es build retry %d/%d: %s" % (i + 1, tries, type(e).__name__))
                time.sleep(12 * (i + 1))
        return fn()
    ind = es_retry(lambda: ev.build_m(a.n_ind, 2026))
    ood = ev.build_x()
    print("采样：域内 %d（build_m seed2026）+ 域外 %d（build_x 固定），走生产 MCP" % (len(ind), len(ood)))
    rows = collect(v, ind, "in") + collect(v, ood, "ood")

    s_in = sorted(r["score"] for r in rows if r["label"] == "in" and r["score"] is not None)
    s_ood = sorted(r["score"] for r in rows if r["label"] == "ood" and r["score"] is not None)
    n = len(s_in)
    if n < 10:
        raise SystemExit("域内有效样本不足（%d），中止" % n)

    alphas = [float(x) for x in a.alpha_list.split(",")]
    taus = [round(0.55 + i * 0.01, 2) for i in range(46)]
    lines = ["# 拒答校准报告（split-conformal · 数据驱动阈值）", "",
             "- 日期：2026-10-27 · 通道：生产 MCP retrieval_cross_doc（hops=2, top_k=5）",
             "- 样本：域内 %d 题（build_m seed2026）/ 域外 %d 题（build_x 固定集）" % (n, len(s_ood)),
             "- 保证：τ_α 取域内分数有限样本 (1-α) 分位 ⇒ 域内新题被答覆盖率 ≥ 1-α（共形覆盖保证）", ""]
    med_in, med_ood = statistics.median(s_in), statistics.median(s_ood)
    lines.append("域内分布：min=%.4f 中位=%.4f max=%.4f ｜ 域外分布：min=%.4f 中位=%.4f max=%.4f ｜ 中位差=%.3f"
                 % (s_in[0], med_in, s_in[-1], s_ood[0], med_ood, s_ood[-1], med_in - med_ood))
    lines.append("")
    lines.append("| 阈值 τ_α | 域内覆盖率（实测） | 域外拒答率（实测） | 性质 |")
    lines.append("|---|---|---|---|")
    for alpha in alphas:
        tau = quantile_tau(s_in, alpha)
        cov = sum(1 for s in s_in if s >= tau) / n
        rej = sum(1 for s in s_ood if s < tau) / max(1, len(s_ood))
        lines.append("| %.4f（α=%d%%） | %.0f%% | %.0f%% | 共形校准 |" % (tau, alpha * 100, cov * 100, rej * 100))
    cov75 = sum(1 for s in s_in if s >= 0.75) / n
    rej75 = sum(1 for s in s_ood if s < 0.75) / max(1, len(s_ood))
    lines.append("| 0.7500（现行） | %.0f%% | %.0f%% | 人工设定 |" % (cov75 * 100, rej75 * 100))
    tau10 = quantile_tau(s_in, 0.10)
    rej10 = sum(1 for s in s_ood if s < tau10) / max(1, len(s_ood))
    lines.append("")
    if rej10 >= rej75:
        lines.append("**结论**：α=10%% 共形阈值 %.4f 的域外拒答率（%.0f%%）不低于现行 0.75（%.0f%%），"
                     "且覆盖率有形式化保证 —— 现行阈值位于数据驱动合理区间，可平滑切换到校准口径。"
                     % (tau10, rej10 * 100, rej75 * 100))
    else:
        lines.append("**结论**：α=10%% 共形阈值 %.4f 的域外拒答率（%.0f%%）低于现行 0.75（%.0f%%），"
                     "建议以两者之间折中，或提高 α 重新校准；迁移前需在满库复跑中复核。"
                     % (tau10, rej10 * 100, rej75 * 100))
    out_md = BASE / "evidence" / "rejection-calibration.md"
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_svg = BASE / "evidence" / "rejection-curve.svg"
    out_svg.write_text(curve_svg(s_in, s_ood, taus), encoding="utf-8")
    (BASE / "evidence" / "rejection-calibration-raw.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写：", out_md, out_svg)
    print("\n".join(lines[7:]))


if __name__ == "__main__":
    main()
