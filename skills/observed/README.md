# skills/observed/ — 观察区（只记录，不判定）

本目录是技能沉淀的 **observed 命名空间**。契约要点（全文见 `governance/namespace-contract.json`，规则见 `governance/constitution.json` C1/C2/C6）：

1. **只记录不判定**：observed 技能不被主链自动调用，不计入任何对外能力声明与评测数字。
2. **先落观察区**：任务中新捕获的工作流，先以 `<skill-name>/SKILL.md` 落入本目录，附 source（哪次任务/哪个 trace）、成功与失败样本链接。
3. **晋升唯一通道**：observed → verified（即上移到 `skills/`）必须通过 D3 统计门控（Wilcoxon 逐项配对，n≥15）+ 冻结集回归守卫复跑（eval-stratified R/M/X/C）。
4. **遗忘同权**：verified 技能废止/降级同样走门控并留免疫记录（parent_skill_id 谱系，D2）。

> 依据：SkillFlow 实证「高使用率 ≠ 高效用」；SAGE 实证「聚合分数门控会产生永久回归」。
> 本目录的存在就是为了让系统**敢记、不敢吹**。
