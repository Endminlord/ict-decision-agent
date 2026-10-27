# 设计稿：图谱检索接线（GraphRAG Retrieval）—— 把本体从"治理资产"升格为"检索资产"

> 状态：设计定稿待实现 · 2026-10-27 · 作者口径：与现有 rings/chunk_id 契约严格兼容，纯增量零破坏

## 1. 动机（为什么这是全仓库含金量最高的一步）

- 现状：本体图谱（边 15,870 → medium 5,269，每条带 chunk_id 出处）只服务**治理与出处展示**（④闸门、③三重出处），**检索通路完全没用它**。
- 断点：多跳问题（"A 政策引用的 B 文件对 C 领域有什么要求"）依赖稠密+BM25 的 RRF——跨文档的**关系跳转**是词面/向量都覆盖不到的结构化盲区；M 层 24/25 的那 1 失，正是这类。
- 升级叙事：接上之后，本作品从"RAG + 图谱副产品"变成**真 GraphRAG**——检索证据环里既有语义命中也有图谱路径，2026 年的评委一看便知段位。

## 2. 数据资产（已就绪，零新建）

| 资产 | 位置 | 规模 | 出处 |
|---|---|---|---|
| 三元组边 | merge-triples 产物 | medium 5,269（strict+loose 过滤后） | 每边 subject/predicate/object + chunk_id + evidence |
| 块索引 | ES 主索引 | 207k→190 万（导入中） | chunk_id ↔ ES _source.id |
| 词面锚 | path_or_url/title | 已在 rings | 实体链接的锚文本源 |

## 3. 接口设计（retrieve_graph → 合流进 rings）

```
retrieve_graph(query, hops=1, per_q=8):
  1) 实体链接：query 分词 n-gram ∩ {subject, object 别名表}（可先精确匹配起步）
  2) 图谱扩展：命中实体 1-hop 邻边（predicate 类型加权：cites/implements/overrides > mentions）
  3) 产物：每边两端 chunk_id → 生成 graph_ring（hop=2、score=图分、path="A -[predicate]-> B"）
```

合流规则（进 `retrieval_cross_doc` 出口前）：
- rings = top5 稠密/词面环 ∪ graph_ring（去重按 chunk_id，图分 ≤ 稠密分，防图谱噪声压过语义）
- `grounded` 判定不变（词面落地仍必须）；图环只是**多给了评委可回查的路径证据**
- 兜底：实体链接空手 → 完全走原通路（行为不变，118 测试与仪式全绿即回归证明）

## 4. 评分与解释

- 图分 = edge_confidence × predicate_weight × log(1+path_len)，压到 [0, 0.9] 区间
- ring 新增字段 `via: "graph"` + `path_or_url` 照填——信任面板/trace --deep 无需改动即可回查图环

## 5. 实施三步（估时）

1. **读边**（2h）：ES/triples 文件双通道读 medium 边 + 别名表构建（内存 ~5k 边，进程内 dict 足够）
2. **接线**（3h）：`retrieval_cross_doc` 出口合流 + `via` 字段；配置开关 `GRAPH_BOOST=on/off`
3. **验收**（2h）：M 层 25 题复跑（目标 25/25）+ 新 G 层 25 图谱推理题入库 eval-stratified + 仪式 full 全绿

## 6. 风险与护栏

| 风险 | 护栏 |
|---|---|
| 图谱噪声污染 rings | 图分上限 0.9 + grounded 词面闸不变 + GRAPH_BOOST 可一键关 |
| 实体链接误命中 | 别名表起步用精确匹配；误链在 118 测试回归中暴露 |
| 满库后边规模膨胀 | 边读取按 medium 分级过滤；超过 5 万边再考虑倒排索引 |

## 7. 排期建议

**必须在导入完成（10-28 晚）前上线** —— 否则 P0-① 满库复跑的基线不含 GraphRAG，新技术等于白做一遍复跑。
实现人：调度官亲自写（触碰生产 MCP 核心）；CC 可并行做 G 层 25 题的题库起草。
