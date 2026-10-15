---
name: governance-policy-trace
allowed-tools:
  - retrieval_cross_doc
  - retrieval_trace_source
  - retrieval_corpus_stats
description: |
  面向政务/政策法规语料的「检索-推理-溯源」工作流：先多跳检索定位相关文件，再按出处锚回查原文，
  逐条核对后才成文；证据不足时必须明确拒答，不得用模型记忆补答。
  适用于制度沿革梳理、政策依据查找、跨层级文件关系梳理等需要可溯源结论的场合。
tags:
  - retrieval
  - governance
  - traceability
  - mcp
---

# 政策法规检索与溯源工作流

## 目标

用**语料库中真实存在的原文片段**回答问题，并给出可回查的出处；证据不足时明确拒答。

## 可用工具

| 工具 | 用途 |
|---|---|
| `retrieval_cross_doc` | 多跳检索：语义召回 + 法规名/发文机关/年份抽取 + 实体二次检索，返回带出处的链条 |
| `retrieval_trace_source` | 按 `chunk_id` 回查原文片段（决策溯源的最后一环） |
| `retrieval_corpus_stats` | 语料与索引统计（已入库块数 / 清洗后文档数 / embedding 状态） |

## 执行步骤（必须按序）

1. **检索**：调用 `retrieval_cross_doc(query, hops=3, top_k=5)`。
2. **判定证据充分性**：若 `insufficient == true`，或 `max_score < 0.75`，或命中全部 `grounded == false`
   ⇒ **停止**，直接回答「语料库中没有找到足够依据」，并说明可改问的方向；**不得**用模型记忆补答。
3. **回查**：对准备引用的每一环调用 `retrieval_trace_source(chunk_id)` 取回原文；
   回查失败（`found == false`）的环**不得**写进结论。
4. **成文**：按下方模板输出，末尾必须附引用块清单。

## 成文模板

```
结论：<一句话>
依据（按时间排序）：
- <date> <title>（<发文机关 / 层级>）：<该文件在链条中的作用>  [chunk_id]
制度沿革：<上位文件> → <地方落实文件> → …
引用块清单：
- chunk_id / title / date
数据来源：<retrieval_corpus_stats 的 indexed_docs / total_chunks>
```

## 注意

- **检索分数不等于相关性**：高相似度但词面不落地（`grounded=false`）的命中不得当依据。
- `indexed_docs` 是索引内文档数，与「清洗后文档数」不是一个口径，引用时必须写明。
- 制度沿革按时间排序，并区分「国家 / 省级 / 市级 / 媒体」层级。
- 换行业时**只改**本文件的字段文案与 `query` 的领域词，不改步骤、不改工具名。
