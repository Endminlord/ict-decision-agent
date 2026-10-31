#!/bin/bash
# replay-full.sh v2 — A1 满库复跑包（五层 R/M/X/C/G 一键全量）
# 【红线】仅当主库导入完成后执行；未满库跑此脚本违反既定口径
# 用法：bash tools/replay-full.sh   （预计 10-20 分钟）
set -e
cd /home/endmin/dsh/research/ict-open-source
TS=$(date +%Y%m%d-%H%M)

COUNT=$(bash tools/es-count.sh)
echo "[0] ES total docs = ${COUNT:-unknown} (need >= 757810)"
if [ -z "$COUNT" ] || [ "$COUNT" -lt 757810 ]; then
  echo "!! import not complete -- ABORT (红线：满库前不复跑)"; exit 1
fi

for L in R M X C G; do
  echo "[$L] running..."
  timeout 900 python3 tools/eval-stratified.py --layers "$L" --report "evidence/full-$L-$TS.json" 2>&1 | tail -1
done

echo "=== replay summary ($TS) ==="
python3 - "$TS" <<'PY'
import json, glob, sys
ts = sys.argv[1]
for L in "RMXCG":
    f = f"evidence/full-{L}-{ts}.json"
    d = json.load(open(f))
    for k, v in d.items():
        if isinstance(v, dict) and "pass_rate" in v:
            print(L, f'{v["passed"]}/{v["n"]}', v["pass_rate"])
PY

echo "=== A2 分数级正式 A/B（BASE=冻结 hybrid 基线）==="
echo "  python3 tools/gate-wilcoxon.py --from-reports evidence/eval-stratified-20261027-hybrid.json evidence/full-M-$TS.json --layer M --action promote"
echo "  python3 tools/gate-wilcoxon.py --from-reports evidence/eval-stratified-20261027-hybrid.json evidence/full-X-$TS.json --layer X --action promote"
echo "（G 层不计门控；key-numbers ◈ 快照随后刷新）"
