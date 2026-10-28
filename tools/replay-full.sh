#!/bin/bash
# replay-full.sh — A1 满库复跑包（v1，2026-10-29 预写）
# 【红线】仅当主库导入完成后执行；未满库跑此脚本无意义且违反既定口径
# 用法：bash tools/replay-full.sh   （预计 ~3-5 分钟）
set -e
cd /home/endmin/dsh/research/ict-open-source
TS=$(date +%Y%m%d-%H%M)

# 0) 满库前置检查：主库须 >= 757,810
COUNT=$(timeout 20 curl -s "localhost:9200/_cat/indices?h=index,docs.count" | awk '{s+=$2} END {print s}')
echo "[0] ES total docs = ${COUNT:-unknown} (need >= 757810)"
if [ -z "$COUNT" ] || [ "$COUNT" -lt 757810 ]; then
  echo "!! import not complete -- ABORT (红线：满库前不复跑)"; exit 1
fi

# 1) M 层重裁（D7 曾 FAIL p=1.0 @半库；满库后重裁为正式结论）
python3 tools/eval-stratified.py --layers M --report "evidence/m-gate-full-$TS.json"
echo "[1] M gate re-run done -> evidence/m-gate-full-$TS.json"

# 2) X 层漂移确认（半库 25/25；满库应仍 25/25）
python3 tools/eval-stratified.py --layers X --report "evidence/x-full-$TS.json"
echo "[2] X layer done -> evidence/x-full-$TS.json"

# 3) G 层确认（v3/v4 结果满库后应不回退）
python3 tools/eval-stratified.py --layers G --report "evidence/g-full-$TS.json"
echo "[3] G layer done -> evidence/g-full-$TS.json"

# 4) 汇总
echo "=== replay summary ($TS) ==="
python3 - <<'PY'
import json, glob
for pat in ["evidence/m-gate-full-*.json", "evidence/x-full-*.json", "evidence/g-full-*.json"]:
    f = sorted(glob.glob(pat))[-1]
    d = json.load(open(f))
    rows = d.get("rows", [])
    hit = sum(1 for r in rows if r.get("hit") or r.get("pass"))
    print(f, f"{hit}/{len(rows)}")
PY
echo "next: 分数级正式 A/B 工具（--from-reports）待满库后另单编写；本包只覆盖复跑。"
