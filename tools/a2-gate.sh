#!/bin/bash
# a2-gate.sh — A2 分数级正式 A/B 一键门控（满库 replay 之后跑）
# 用法：bash tools/a2-gate.sh [TS]   （TS 缺省=自动找最新 full-*-*.json）
set -e
cd /home/endmin/dsh/research/ict-open-source
BASE_M="evidence/eval-stratified-20261027-hybrid.json"
BASE_X="evidence/eval-stratified-20261027-hybrid.json"
if [ -n "$1" ]; then
  CAND_M="evidence/full-M-$1.json"; CAND_X="evidence/full-X-$1.json"
else
  CAND_M=$(ls -t evidence/full-M-*.json | head -1)
  CAND_X=$(ls -t evidence/full-X-*.json | head -1)
fi
echo "[A2] BASE=$BASE_M"
echo "[A2] CAND_M=$CAND_M  CAND_X=$CAND_X"
TS=$(date +%Y%m%d-%H%M)
echo "=== M 层门控（重裁）==="
python3 tools/gate-wilcoxon.py --from-reports "$BASE_M" "$CAND_M" --layer M --action promote --out "evidence/a2-gate-M-$TS.json" 2>&1 | tail -4
echo "=== X 层门控 ==="
python3 tools/gate-wilcoxon.py --from-reports "$BASE_X" "$CAND_X" --layer X --action promote --out "evidence/a2-gate-X-$TS.json" 2>&1 | tail -4
echo "=== 决定摘要 ==="
python3 - "$TS" <<'PY'
import json, sys
ts = sys.argv[1]
for L in "MX":
    d = json.load(open(f"evidence/a2-gate-{L}-{ts}.json"))
    print(L, "->", d.get("decision"), "| p =", d.get("p_one_sided"), "| U =", d.get("u_stat", d.get("U", "?")))
PY
echo "[A2] 报告: evidence/a2-gate-{M,X}-$TS.json —— 据此拍板 τ/A3/A4"
