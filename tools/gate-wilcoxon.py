#!/usr/bin/env python3
"""gate-wilcoxon.py — D3 统计门控 + 回归守卫（宪法 C1/C5/C6 的执行器）。

判定权外置的最后一环：任何技能晋升/降级/废止声明，必须通过本门控并留痕。

方法（依据 SAGE, arXiv 2609.36043 的失败模式分析设计）：
- 逐项配对 ledger：wins / regressions / ties 全部落账，不看聚合分数；
- 连续分层（逐题打分）：单侧 Wilcoxon 符号秩检验（配对、tie 校正正态近似，非零配对 n≥15）；
- 二元分层（逐题 0/1，如 M 链路通过/X 拒答）：不一致对上的 McNemar 精确二项检验；
- 回归守卫：任何单题回退超过容忍度 → 直接 FAIL（二元层任何回退即 1.0，必拦）
  —— 防 SAGE 失败模式之一"聚合分数门控放行永久回归"；
- 不对称效用：U = wins − λ·regressions（λ≥1，默认 1.5），U≤0 → FAIL；
- Optimizer's Curse 防线：门控只约束**新的**进化声明（宪法 C4 不追溯）。

输入（二选一）：
  --pairs pairs.json        {"pairs":[{"item":..,"base":0.8,"cand":0.9},...]}（或裸数组）
  --from-reports base.json cand.json --layer M|X   （吃 eval-stratified.py 报告的逐题行）

用法：
  python3 tools/gate-wilcoxon.py --pairs p.json --action promote --out gate-result.json
  python3 tools/gate-wilcoxon.py --selftest

退出码：0=PASS，2=FAIL/BLOCKED，3=用法/输入错误。
PASS 后把 --out 结果文件喂给免疫账本：
  governance/immunity_log.py append --action promote --target <skill> --gate-file gate-result.json
"""

import argparse
import json
import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MIN_N = 15          # 宪法下限：配对样本量（冻结集规模），不可通过参数覆盖
ALPHA = 0.05        # 单侧显著性水平（写死默认，可从严不可从宽）
LAM = 1.5           # 不对称罚 λ≥1（SAGE：回归必须比同额进步更疼）
TOLERANCE = 0.05    # 单题回退容忍度（浮点噪声豁免线；二元层任何回退=1.0 必拦）
FROZEN_SET = "eval-stratified R/M/X/C seed2026（四层各 25 题冻结集）"


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _binom_sf_ge(k, n):
    """P(X ≥ k | X~Bin(n, .5))，X 为方向性不一致对中"偏胜"侧的计数。"""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i)
    return total / (2.0 ** n)


def _load_pairs(path):
    d = json.load(open(path, encoding="utf-8"))
    pairs = d["pairs"] if isinstance(d, dict) and "pairs" in d else d
    if not isinstance(pairs, list) or not pairs:
        raise SystemExit("pairs 文件格式：[{item, base, cand}, ...] 非空数组")
    out = []
    for p in pairs:
        try:
            out.append((str(p["item"]), float(p["base"]), float(p["cand"])))
        except (KeyError, TypeError, ValueError):
            raise SystemExit("坏 pair（缺 item/base/cand）：%s" % json.dumps(p, ensure_ascii=False)[:80])
    return out


def _pairs_from_reports(base_path, cand_path, layer):
    key = {"M": "pass", "X": "refused"}.get(layer)
    if key is None:
        raise SystemExit("--layer 目前支持 M|X（逐题 0/1 判定行）；R/C 层请先在报告里补 rows 后扩展本适配器")
    def rows_of(path):
        d = json.load(open(path, encoding="utf-8"))
        rows = (d.get(layer) or {}).get("rows")
        if rows is None:
            raise SystemExit("%s 缺 %s 层逐题 rows" % (path, layer))
        return {r["q"]: (1.0 if r.get(key) else 0.0) for r in rows}
    b, c = rows_of(base_path), rows_of(cand_path)
    common = sorted(set(b) & set(c))
    if len(common) < MIN_N:
        raise SystemExit("两报告逐题键交集 %d < %d，冻结集不对齐（必须同 seed 同层）" % (len(common), MIN_N))
    return [(q, b[q], c[q]) for q in common]


def wilcoxon_one_sided(diffs, action):
    """连续层：返回 (n_used, Wp, Wm, mu, sigma, z, p)。非零差值参与秩。"""
    d = [x for x in diffs if x != 0.0]
    n = len(d)
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    tie_groups = {}
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        t = j - i + 1
        tie_groups[t] = tie_groups.get(t, 0) + 1
        i = j + 1
    wp = sum(ranks[k] for k in range(n) if d[k] > 0)
    wm = sum(ranks[k] for k in range(n) if d[k] < 0)
    mu = n * (n + 1) / 4.0
    var = n * (n + 1) * (2 * n + 1) / 24.0 - sum(t ** 3 - t for t in tie_groups.values()) / 48.0
    if var <= 0:
        return n, wp, wm, mu, 0.0, 0.0, 1.0
    sd = math.sqrt(var)
    if action == "promote":          # H1: cand>base → W+ 偏大
        z = (wp - mu) / sd
        p = 1.0 - _phi(z)
    else:                            # demote/retire：H1: cand<base → W− 偏大
        z = (wm - mu) / sd
        p = 1.0 - _phi(z)
    return n, wp, wm, mu, sd, z, p


def run_gate(pairs, action, alpha=ALPHA, lam=LAM, tolerance=TOLERANCE, ref="stdout"):
    if action not in ("promote", "demote", "retire"):
        raise SystemExit("action 必须是 promote|demote|retire")
    diffs = [c - b for _, b, c in pairs]
    max_drop = max((b - c for _, b, c in pairs), default=0.0)
    wins = sum(1 for x in diffs if x > 0)
    reg = sum(1 for x in diffs if x < 0)
    ties = sum(1 for x in diffs if x == 0)

    binary = all(x in (0.0, 1.0, -1.0) for x in diffs)
    result = {
        "action": action,
        "n_pairs": len(pairs),
        "wins": wins,
        "regressions": reg,
        "ties": ties,
        "utility": wins - lam * reg,
        "max_drop": round(max_drop, 4),
        "tolerance": tolerance,
        "alpha": alpha,
        "lam": lam,
        "frozen_set": FROZEN_SET,
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "ref": ref,
        "reasons": [],
    }

    if binary:
        n_d = wins + reg
        result["method"] = "McNemar exact binomial on discordant pairs (binary 0/1 layer)"
        result["n_discordant"] = n_d
        if action == "promote":
            p = _binom_sf_ge(wins, n_d)
        else:  # demote/retire：劣化方向偏胜 = 回退侧
            p = _binom_sf_ge(reg, n_d)
        result["p_one_sided"] = round(p, 6)
        if len(pairs) < MIN_N:
            result["decision"] = "BLOCKED"
            result["reasons"].append("配对样本 n=%d < 宪法下限 %d（冻结集规模，不可覆盖）" % (len(pairs), MIN_N))
        elif action == "promote":
            if p > alpha:
                result["reasons"].append("p=%.4f > α=%.2f（不一致对 %d/%d 偏胜不足）" % (p, alpha, wins, n_d))
            if reg > 0:
                result["reasons"].append("二元层存在 %d 题回退（永久回归防线：任何单题翻坏即拦）" % reg)
            result["decision"] = "FAIL" if result["reasons"] else "PASS"
        else:
            if p > alpha:
                result["reasons"].append("p=%.4f > α=%.2f：劣化不显著，不足以支撑 %s" % (p, alpha, action))
            result["decision"] = "FAIL" if result["reasons"] else "PASS"
        return result

    # 连续层：Wilcoxon
    result["method"] = "Wilcoxon signed-rank, one-sided paired, tie-corrected normal approx"
    n, wp, wm, mu, sd, z, p = wilcoxon_one_sided(diffs, action)
    result.update({"n_used": n, "W_plus": round(wp, 2), "W_minus": round(wm, 2),
                   "mu": round(mu, 2), "sigma": round(sd, 4), "z": round(z, 4),
                   "p_one_sided": round(p, 6)})
    if n < MIN_N:
        result["decision"] = "BLOCKED"
        result["reasons"].append("逐题非零配对 n=%d < 宪法下限 %d（不可覆盖）" % (n, MIN_N))
    elif action == "promote":
        if p > alpha:
            result["reasons"].append("p=%.4f > α=%.2f：改进不显著（Optimizer's Curse 防线）" % (p, alpha))
        if max_drop > tolerance:
            result["reasons"].append("单题最大回退 %.4f > 容忍度 %.2f（SAGE 永久回归防线）" % (max_drop, tolerance))
        if wins - lam * reg <= 0:
            result["reasons"].append("不对称效用 U=%g≤0（λ=%.1f）" % (wins - lam * reg, lam))
        result["decision"] = "FAIL" if result["reasons"] else "PASS"
    else:
        if p > alpha:
            result["reasons"].append("p=%.4f > α=%.2f：劣化不显著，不足以支撑 %s" % (p, alpha, action))
        result["decision"] = "FAIL" if result["reasons"] else "PASS"
    return result


_POWER_NOTE = ("功效限制（如实声明）：连续层 n=25 单侧 Wilcoxon（α=0.05）对中低效应量检验力不足"
               "（dz≈0.5 约 78%，dz≈0.3 约 39%），阴性结果 ≠ 无差异；二元层走 McNemar 精确检验，"
               "不一致对少时必须近乎全胜才显著。样本量下限 15 为宪法值，扩样需修订宪法"
               "（版本递增+人工确认）。")


def _selftest():
    mk = lambda rows: [("i%d" % i, b, c) for i, (b, c) in enumerate(rows)]
    # A：连续层清晰改进（17 胜 2 退 1 平）→ promote PASS
    r = run_gate(mk([(.8, .9)] * 17 + [(.8, .78)] * 2 + [(.8, .8)]), "promote", ref="selftest")
    assert r["decision"] == "PASS" and r["p_one_sided"] < 0.001 and r["utility"] == 14, r
    # B：连续层 n=10 非零配对 → 宪法下限 BLOCKED
    r = run_gate(mk([(.5, 1.0)] * 10), "promote", ref="selftest")
    assert r["decision"] == "BLOCKED" and r["n_used"] == 10, r
    # C：SAGE 永久回归场景——聚合涨但一题崩 0.5 → regression guard FAIL
    r = run_gate(mk([(.7, .75)] * 19 + [(.9, .4)]), "promote", ref="selftest")
    assert r["decision"] == "FAIL" and any("回退" in s for s in r["reasons"]), r
    # D：连续层明确劣化 → demote PASS（遗忘同权）
    r = run_gate(mk([(.9, .8)] * 18 + [(.8, .82)] * 2), "demote", ref="selftest")
    assert r["decision"] == "PASS" and r["p_one_sided"] < 0.001, r
    # E：二元层 6/6 不一致对全胜 → McNemar p=1/64 PASS
    r = run_gate(mk([(0, 1)] * 6 + [(1, 1)] * 19), "promote", ref="selftest")
    assert r["decision"] == "PASS" and r["n_discordant"] == 6 and r["p_one_sided"] == 0.015625, r
    # F：二元层 5 胜 1 退（n_d=6, p=11/64>0.05 且有回退）→ FAIL 双理由
    r = run_gate(mk([(0, 1)] * 5 + [(1, 0)] * 1 + [(1, 1)] * 19), "promote", ref="selftest")
    assert r["decision"] == "FAIL" and r["regressions"] == 1, r
    # G：from-reports M 层适配器（20→24 通过：wins=4 n_d=4 → p=1/16>0.05 → FAIL，检验力如实）
    with tempfile.TemporaryDirectory() as td:
        mk_rep = lambda p, vals: Path(p).write_text(json.dumps(
            {"M": {"rows": [{"q": "q%d" % i, "pass": v} for i, v in enumerate(vals)]}}), encoding="utf-8")
        pa, pb = Path(td) / "b.json", Path(td) / "c.json"
        mk_rep(pa, [True] * 20 + [False] * 5)
        mk_rep(pb, [True] * 24 + [False] * 1)
        pairs = _pairs_from_reports(str(pa), str(pb), "M")
        assert len(pairs) == 25
        r = run_gate(pairs, "promote", ref="selftest")
        assert r["decision"] == "FAIL" and r["wins"] == 4 and r["n_discordant"] == 4, r
    print("selftest OK：A 连续PASS / B n下限BLOCKED / C 永久回归FAIL / D 遗忘PASS / "
          "E McNemar全胜PASS / F 二元回退FAIL / G 报告适配器")


def main():
    ap = argparse.ArgumentParser(description="D3 统计门控：逐项配对（Wilcoxon/McNemar）+ 回归守卫")
    ap.add_argument("--pairs")
    ap.add_argument("--from-reports", nargs=2, metavar=("BASE", "CAND"))
    ap.add_argument("--layer", choices=["M", "X"])
    ap.add_argument("--action", choices=["promote", "demote", "retire"])
    ap.add_argument("--alpha", type=float, default=ALPHA)
    ap.add_argument("--lam", type=float, default=LAM)
    ap.add_argument("--tolerance", type=float, default=TOLERANCE)
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return 0
    if a.from_reports:
        if not a.layer:
            ap.error("--from-reports 需要 --layer M|X")
        pairs = _pairs_from_reports(a.from_reports[0], a.from_reports[1], a.layer)
    elif a.pairs:
        pairs = _load_pairs(a.pairs)
    else:
        ap.error("需要 --pairs 或 --from-reports（或 --selftest）")
    if not a.action:
        ap.error("需要 --action promote|demote|retire")
    if a.alpha > ALPHA:
        ap.error("α=%.2f 比宪法默认更宽，拒绝（宪法 C1：只能从严）" % a.alpha)
    if a.lam < 1.0:
        ap.error("λ 必须 ≥1（不对称罚下限）")
    r = run_gate(pairs, a.action, alpha=a.alpha, lam=a.lam, tolerance=a.tolerance,
                 ref=a.out or "stdout")
    r["power_note"] = _POWER_NOTE
    blob = json.dumps(r, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(blob + "\n", encoding="utf-8")
    print(blob)
    return 0 if r["decision"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
