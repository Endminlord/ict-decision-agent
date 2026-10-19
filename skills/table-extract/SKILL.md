---
name: table-extract
allowed-tools:
  - bash
description: |
  表格抽取工作流：把政务/行业文档里的表格从「看起来像一段话」还原成有行列的结构。
  分两条路：PDF 原件走矢量线检测（精确），已清洗的纯文本语料走「序号 + 表头」结构识别（保守）。
  适用于政策文件里的清单、指标表、责任分工表 —— 这些内容按纯文本读会丢掉行列对应关系。
tags:
  - multimodal
  - table
  - extraction
  - corpus
---

# 表格抽取工作流

## 目标

把表格还原成**有行列的结构**，而不是一段被空格撑开的文字。

## 两条路，别混用

| 输入 | 工具 | 原理 | 精度 |
|---|---|---|---|
| PDF 原件 | `tools/extract-tables.py` | pymupdf `find_tables()` 检测矢量线 | 高（线在表就在） |
| 已清洗的纯文本块 | `tools/detect-tables.py` | 「序号」开头 + 表头词 ≥3 + 紧跟数字 | 保守（宁少判不造假） |

## 执行步骤

### A. 从 PDF 抽

```bash
python3 tools/extract-tables.py --pdf <文件.pdf> --out /tmp/tables.md
python3 tools/extract-tables.py --pdf <文件.pdf> --strategy text   # 无矢量线时用文本流推断
```

输出 markdown 表格 + 线性化文本（后者用于喂 embedding）。

**自测**：`python3 tools/extract-tables.py --self-test` → **10/10**。

### B. 在已清洗语料里识别

```bash
python3 tools/detect-tables.py --src <chunks.jsonl> --out /tmp/tagged.jsonl
```

**自测**：`python3 tools/detect-tables.py --self-test` → **9/9**。

## 判据（收紧过一轮，记录理由）

第一版靠「如下表/附件」+ 表头词 ≥2，在 15,000 块上命中 1,205 块（8.0%），但**抽检精度只有约 20%** ——
「附件」后面接的是正文方案，「项目/标准」在叙述句里也常出现。

收紧后只认两种**结构证据**：

1. 「序号」出现在前 120 字（政务表格几乎都以序号起头）
2. 表头词 ≥3 **且**同段紧跟数字

结果：命中 **42 块（0.3%）**，抽检 **5/8 为真**，能还原 3–7 列。

**这个取舍要主动说**：宁可少判，不造假结构。把一段散文硬拆成表格，比不抽更糟 ——
下游会把它当结构化事实用。

## 注意

- **表头词表是领域相关的**：政务用「序号/项目/内容/责任单位/完成时限」，换行业要换表。
- **线性化文本要保留行列标记**：`| 序号 | 项目 | 责任单位 |` 比 `序号 项目 责任单位` 更容易让模型理解对应关系。
- **合并单元格会丢**：pymupdf 能检测到合并，但线性化时只保留左上角的值，跨行跨列语义会损失。
