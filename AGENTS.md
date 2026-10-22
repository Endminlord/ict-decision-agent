# AGENTS.md

本仓库的 agent 指令。

## Agent skills

### Issue tracker

Issues 与 spec 以 markdown 文件形式存放在 `.scratch/<feature>/`（本地 tracker，不依赖 `gh`）。见 `docs/agents/issue-tracker.md`。

### Triage labels

五个标准 triage 角色，label 字符串与角色同名：`needs-triage`、`needs-info`、`ready-for-agent`、`ready-for-human`、`wontfix`。见 `docs/agents/triage-labels.md`。

### Domain docs

single-context：根目录 `GLOSSARY.md` + `docs/adr/`。缺失时静默跳过，由 `/domain-modeling` 按需创建。见 `docs/agents/domain.md`。
