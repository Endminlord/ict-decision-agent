# 一致性复核（D10 · 2026-10-28）

**10/10 PASS** ｜ 复核器：tools/check-consistency.py（声称 vs 实际）

| 核对项 | 结果 | 明细 |
|---|---|---|
| 图谱规模=账面声称 | ✅ PASS | 实测 2,013 边/1,865 实体；key-numbers 声称 2,013/1,865 |
| 免疫账本哈希链 verify | ✅ PASS | {"ok": true, "records": 6, "head": "52cd29d6c416fd11"} |
| 观测台快照=账本笔数 | ✅ PASS | 账本 6 笔；console 含『6 笔』标记 |
| 门控 7 用例自检 | ✅ PASS | selftest OK：A 连续PASS / B n下限BLOCKED / C 永久回归FAIL / D 遗忘PASS / E McNemar全胜PASS / F 二元回退FAIL |
| 宪法版本口径一致 | ✅ PASS | constitution v1.0.0 |
| 共形校准判据在位(τ=0.8505) | ✅ PASS | evidence/rejection-calibration.md |
| X 层拒答题库=25 | ✅ PASS | build_x()=25 题 |
| 文件在位 docs/RIGOR_MATRIX.md | ✅ PASS |  |
| 文件在位 STRESS_TEST.md | ✅ PASS |  |
| 文件在位 EXPERIMENT_RESULTS.md | ✅ PASS |  |
