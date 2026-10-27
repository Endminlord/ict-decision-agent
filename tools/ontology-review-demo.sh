#!/usr/bin/env bash
# ④ 本体确认闸门 demo（非交互、可复跑、不碰生产文件）
# 三层漏斗：规则层候选 → 模型双侧判定 → 人工闸门批量确认
# 产物全部落在 evidence/ontology-review-demo/，生产 ontology 文件只读
set -euo pipefail
ONTO=/home/endmin/dsh/research/ontology
REPO=/home/endmin/dsh/research/ict-open-source
EV=$REPO/evidence/ontology-review-demo
mkdir -p "$EV"
cd "$ONTO"

echo "══ 步骤1 候选层全貌统计（规则层 loose = pending）══"
python3 review.py --pending triples-governance-loose.jsonl --stats \
  | tee "$EV/step1-stats-loose.txt"

echo
echo "══ 步骤2 人工抽样：最可疑的 5 条先看（脚本化交互，逐条 s 跳过）══"
printf 's\ns\ns\ns\ns\nq\n' | python3 review.py \
  --pending triples-governance-loose.jsonl --sort suspicious --limit 5 \
  --confirmed "$EV/demo-sample-confirmed.jsonl" \
  --rejected "$EV/demo-sample-rejected.jsonl" \
  | tee "$EV/step2-interactive-sample.txt"

echo
echo "══ 步骤3a 模型双侧判定后的引用边（prune.py 产物 doc2doc）统计 ══"
python3 review.py --pending triples-doc2doc.jsonl --stats \
  | tee "$EV/step3a-stats-doc2doc.txt"

echo
echo "══ 步骤3b 人工闸门批量确认：接受 medium，low 留待下轮 ══"
python3 review.py --pending triples-doc2doc.jsonl --accept medium \
  --confirmed "$EV/triples-doc2doc-confirmed.jsonl" \
  --rejected "$EV/triples-doc2doc-rejected.jsonl" \
  --defer "$EV/triples-doc2doc-pending-low.jsonl" \
  | tee "$EV/step3b-batch-confirm.txt"

echo
echo "══ 步骤4 漏斗总账 ══"
python3 - "$EV" <<'PYEOF'
import json, sys
from pathlib import Path

ev = Path(sys.argv[1])


def wc(p):
    f = ev / p
    return sum(1 for _ in open(f, encoding="utf-8")) if f.exists() else 0


def judged_stats(name):
    f = Path("/home/endmin/dsh/research/ontology") / name
    if not f.exists():
        return {"candidates": 0, "is_doc_true": 0}
    t = n = 0
    for line in open(f, encoding="utf-8"):
        if not line.strip():
            continue
        n += 1
        if json.loads(line).get("is_doc") is True:
            t += 1
    return {"candidates": n, "is_doc_true": t}


strict = sum(1 for _ in open("/home/endmin/dsh/research/ontology/triples-governance.jsonl", encoding="utf-8"))
loose = sum(1 for _ in open("/home/endmin/dsh/research/ontology/triples-governance-loose.jsonl", encoding="utf-8"))
doc2doc = wc("step3a-stats-doc2doc.txt") and sum(
    1 for _ in open("/home/endmin/dsh/research/ontology/triples-doc2doc.jsonl", encoding="utf-8"))
confirmed = wc("triples-doc2doc-confirmed.jsonl")
low_pending = wc("triples-doc2doc-pending-low.jsonl")
subj = judged_stats("judged-subject-full.jsonl")
obj = judged_stats("judged-object-full.jsonl")

summary = {
    "date": "2026-10-27",
    "funnel": {
        "rule_layer_strict_auto": strict,
        "rule_layer_loose_pending": loose,
        "model_both_sides_doc2doc": doc2doc,
        "human_gate_confirmed_medium": confirmed,
        "human_gate_low_still_pending": low_pending,
    },
    "model_judge": {"subject_side": subj, "object_side": obj},
    "auto_ratio": {
        "before_gate": round(strict / (strict + loose), 4),
        "after_demo_gate": round((strict + confirmed) / (strict + loose), 4),
    },
}
(out := ev / "summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
print("summary ->", out)
PYEOF
echo "demo 完成"
