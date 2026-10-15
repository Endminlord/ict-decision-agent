---
name: workbench-status-report
allowed-tools:
  - workbench_task_status
  - workbench_recent_commits
  - workbench_run_demo
description: |
  生成「个人 AI 工作台」状态简报：调用工作台 MCP 工具取真实数据（任务计划对账计数、最近提交、
  离线演示链健康度），交叉校验数字与工具原始输出一致后再输出简报。适用于需要引用工作台真实
  工程状态、而不是凭模型记忆作答的场合。
tags:
  - workbench
  - mcp
  - report
---

# 工作台状态简报工作流

## 目标

用**工具返回的原始数据**（不是模型记忆）生成一段可复核的工作台状态简报，并显式标注数据来源。

## 可用工具

| 工具 | 用途 |
|---|---|
| `workbench_task_status` | 读 `TASK_PLAN_STATUS.md`，返回 done / partial / todo 计数与文件字节数、mtime |
| `workbench_recent_commits` | 返回仓库最近 N 条提交（`git log --oneline`） |
| `workbench_run_demo` | 跑一次离线演示链，返回 rc / 耗时 / model_calls / domains |

## 执行步骤（必须按序）

1. **取数**：依次调用 `workbench_task_status`、`workbench_recent_commits(limit=3)`、`workbench_run_demo`。
2. **校验**：把三个工具的返回值逐字段与要写进简报的数字对照；任一字段缺失或与工具输出不符时，
   **不得写入简报**，改为报告「该字段未取到」。
3. **成文**：按下方模板输出；最后一行必须写数据来源（工具名 + 文件路径/字节数）。

## 简报模板

```
工作台状态简报
- 任务对账：done=<n> partial=<n> todo=<n>（来源 TASK_PLAN_STATUS.md，<bytes> 字节，mtime <mtime>）
- 最近提交：<commit1>；<commit2>；<commit3>
- 演示链：rc=<rc> 耗时=<seconds>s model_calls=<n> domains=<...>
- 校验：以上数字均来自工具返回，未做任何推测
```

## 注意

- `workbench_task_status` 的计数是**文档中状态标记的出现次数**（含图例与说明文字），不等于「项数」；
  引用时必须写明口径。
- 工具返回 `parse_error` 或 `rc != 0` 时，如实报告失败，不要补全。
