# RIGOR_MATRIX — 统计门控与回归守卫（D3 · 2026-10-28）

> 执行器：`tools/gate-wilcoxon.py`（纯 stdlib；`--selftest` 7 用例自证）。
> 本文件是宪法 C1/C5/C6 的操作手册；红线：**门控不追溯（C4）**、**n=25 功效限制如实标注**。

## 1. 门控矩阵：什么声明过什么门

| 声明类型 | 必需判据 | 判定层 | 决策规则 | 落账动作 |
|---|---|---|---|---|
| observed → verified（晋升） | 冻结集复跑逐题分（base vs cand） | 连续层=Wilcoxon 单侧；二元层=McNemar 精确 | p≤0.05 **且** 零单题回退**且** U=wins−1.5·reg>0 | `immunity_log append --action promote --gate-file <result.json>` |
| verified → observed（降级） | 同上 | 同上 | 劣化侧 p≤0.05（遗忘同权，C6） | `append --action demote --gate-file …` |
| 技能废止 | 同降级 | 同上 | 同降级 | `append --action retire --gate-file …` |
| 检索/算法变更上线 | 冻结集四层全复跑 | R/M/X/C 全绿 | 任一层 promote 判据 FAIL 即不上线 | 变更记录 + gate 结果归档 evidence/ |
| 宪法/契约修订 | 人工确认 | — | 版本递增 + 本文件同步 | `append --action amend --version <新版本>` |

## 2. 写死的参数（宪法值，脚本拒绝放宽）

| 参数 | 值 | 依据 |
|---|---|---|
| MIN_N | 15（连续层=非零配对数；二元层=冻结集 25 全对齐） | Wilcoxon 正态近似可靠性下限 + 功效要求 |
| α（单侧） | 0.05 | 惯例；脚本拒绝 α>0.05（只能从严） |
| λ（不对称罚） | 1.5，下限 1.0 | SAGE：回归必须比同额进步更疼 |
| 单题回退容忍度 | 0.05（仅连续层） | 浮点噪声豁免；二元层任何翻坏=1.0 必拦 |

## 3. 判定层自动路由

- 逐题分全 ∈ {0,1}（如 M 链路通过、X 拒答）→ **McNemar 精确二项**（不一致对上单侧检验）。并列对不携带方向信息，但"冻结集 25 题全对齐"由 MIN_N 保证。注意：不一致对少时必须近乎全胜才显著——这是检验力的诚实，不是缺陷。
- 存在非 0/1 差值（连续/分级打分）→ **Wilcoxon 符号秩**（tie 校正正态近似）。

## 4. 对 SAGE 两大失败模式的防线（arXiv 2609.36043）

| SAGE 失败模式 | 本门控防线 |
|---|---|
| 聚合分数门控放行永久回归 | 逐项 ledger + 单题回退一票否决（连续层 >0.05、二元层 >0） |
| Optimizer's Curse（挑到虚高） | 单侧配对检验显著性门槛 + C4 不追溯（现有数字不受新门控约束） |
| 高使用率误判为高效用（SkillFlow, arXiv 2604.17308：Kimi K2.5 使用率 +0.60pp 而成功率降） | 命名空间只记录不判定（governance/namespace-contract.json），使用频率从不作为晋升依据 |

## 5. 功效限制（如实声明，写进任何引用本门控的材料）

n=25 时单侧 Wilcoxon（α=0.05）对中低效应量检验力不足（dz≈0.5 约 78%、dz≈0.3 约 39%）；二元层不一致对 4/4 全胜时 p=0.0625 恰好不显著（5/5 才到 0.031）。**阴性结果 ≠ 无差异**。扩样或改 α 需修订宪法并同步本文件。

## 6. 标准流程（晋升示例）

```bash
# 1) 冻结集两次复跑（变更前/后，同 seed 同层）
python3 tools/eval-stratified.py --layers M,X --report eval-base.json
#    …变更生效后…
python3 tools/eval-stratified.py --layers M,X --report eval-cand.json
# 2) 过门控（M 层示例）
python3 tools/gate-wilcoxon.py --from-reports eval-base.json eval-cand.json \
    --layer M --action promote --out evidence/gate-<skill>-M.json
# 3) PASS → 免疫账本（gate.ref 自动进入防篡改链）
python3 governance/immunity_log.py append --action promote --target <skill> \
    --namespace verified --gate-file evidence/gate-<skill>-M.json \
    --evidence evidence/gate-<skill>-M.json
```

`--selftest` 覆盖：连续 PASS / n 下限 BLOCKED / SAGE 永久回归 FAIL / 遗忘 PASS / McNemar 全胜 PASS / 二元回退 FAIL / 报告适配器。
