# 华为 ICT 大赛 · 赛题二与赛题三

> 第十一届华为 ICT 大赛 · 中国创新赛
> **赛题 2**（基于华为云 AgentArts 打造行业智能体）+ **赛题 3**（基于 ModelEngine Nexent 打造可进化决策智能体）

同一个问题的两个交付面。**赛题三**把「检索 → 推理 → 判定」做成跑在 Nexent 上的完整智能体；**赛题二**把其中最硬的一环 —— **引用核验** —— 抽成零依赖的 MCP 服务，任何 Agent 平台都能接。

---

## 两个赛题的关系

| | 赛题三 | 赛题二 |
|---|---|---|
| **平台** | ModelEngine Nexent（本地部署） | 华为云 AgentArts |
| **交付** | 检索链 + 本体三层 + 判定层 | [`track2-mcp/`](track2-mcp/)（零依赖 UVX 包，3 工具） |
| **核心问题** | 跨层级政策链条能不能一次答出、每环可查 | 模型引用的那句原话到底在不在语料里 |
| **判据** | 检索四组 + 本体三层 + 迁移 0 改码 | **96 条**（6 数据 + 52 查询层 + 28 协议 + 10 领域无关） |
| **状态** | 已交付 | MCP 已交付（实测可跑），平台部署待 AgentArts 权限 |

两个赛题共用同一套语料与同一套「先有证据、再下结论」的方法：赛题三证明**这套方法能撑起一个完整智能体**，赛题二证明**其中最关键的闸门可以脱离本项目单独复用**。

---

## 赛题三：可回查出处的决策智能体

> 把 4.18 GB 政务公开语料做成一条「检索 → 推理 → 判定」的依据链，让**每条结论都能回查原文出处**。

---

## 它解决什么

政务/行业决策的依据分散在数十万份文件里。真正的难点不是「检索不到」，而是检索到之后**无法确认依据是否成立**：

- 哪一条是**上位文件**，哪一条只是正文里**提到了文件名**？
- 哪一条**已经被废止**？
- 跨层级链条（省级条例 → 国办函 → 市级决定）能不能**一次答出**，且每环都可核对？

本项目的答案是三件事：**逐条给出可回查的出处锚（`chunk_id`）**、**证据不足时明确拒答**、**换行业零改码**。

---

## 架构

```
用户提问
   │
   ▼
Nexent Agent（智能体运行时）
   │  调用自建 Skill（编排流程 + 拒答闸门）
   ├──────────────► 检索 MCP（宿主 :8793）
   │                  ├─ retrieval_cross_doc    跨文档检索
   │                  ├─ retrieval_trace_source 逐环回查出处
   │                  └─ retrieval_corpus_stats 语料统计
   ├──────────────► 工作台 MCP（宿主 :8793）
   │                  ├─ workbench_task_status
   │                  ├─ workbench_recent_commits
   │                  └─ workbench_run_demo
   ▼
Nexent 知识库（Elasticsearch :9220 · 1024 维向量）
   ▲
   │ 入库
embedding 服务（宿主 :8792 · bge-m3 ONNX）
```

本体三层（`ontology/`）提供**离线**的引用关系推理能力：

| 层 | 做什么 | 实测 |
|---|---|---|
| **规则层** `build.py` + `align.py` | 行业词表驱动的三元组抽取 + 规范名归并 | 60,000 块 → **255,451** 条三元组，出处失败 **0** |
| **推理层** `reason.py` | 引用类谓词连成有向图 + 有界传递闭包 | 14,035 节点 / 148,005 边 / 闭包 171,552 对 |
| **模型判定层** `judge.py` + `prune.py` | LLM 逐条判定候选是否为真文件，双侧过滤 | subject 1,762 真文件 · object 6,890 · 双侧都清 **15,870** 条文件→文件边 |

---

## 快速开始

### 前置

- Python 3.10+
- Docker（跑 Nexent + Elasticsearch）
- 一个 Nexent 实例（本仓库的 MCP 服务注册到它的 MCP 网关）
- 一个 OpenAI 兼容的模型端点（本体判定层用；不跑判定层可跳过）

### 1. 起 embedding 服务

```bash
# 模型：bge-m3 ONNX（约 2.3 GB，需自行下载到 ~/.cache/bge-m3-onnx/）
python3 tools/embed-server.py --port 8792 --model-dir ~/.cache/bge-m3-onnx
curl -s http://localhost:8792/health   # 应返回 ok
```

> GPU 加速（可选）：`onnxruntime-gpu` + `nvidia-cudnn-cu12` + `nvidia-cublas-cu12`，运行前把 wheel 里的库目录加进 `LD_LIBRARY_PATH`。实测 RTX 5070 上 **13 ms / batch 8（635 条/秒）** vs CPU **556 ms（14.4 条/秒）**，约 44 倍。详见 `docs/241-ict-gpu-embedding.md`。

### 2. 起两套 MCP 服务

```bash
python3 mcp/workbench-mcp-server.py &      # 工作台工具
python3 mcp/retrieval-mcp-server.py &      # 检索工具
```

两者都支持环境变量覆盖路径（`WORKBENCH_DIR` / `TOOLS_DIR` / `CORPUS_DIR`）。

### 3. 在 Nexent 里注册 MCP

在 Nexent 的 MCP 配置里加两条 stdio 传输：

```json
{
  "serverName": "workbench",
  "transport": "stdio",
  "command": "python3",
  "args": ["/absolute/path/to/mcp/workbench-mcp-server.py"]
}
```

```json
{
  "serverName": "retrieval",
  "transport": "stdio",
  "command": "python3",
  "args": ["/absolute/path/to/mcp/retrieval-mcp-server.py"]
}
```

### 4. 装 Skill

把 `skills/` 下两个目录拷进 Nexent 的技能目录（或在 Nexent 技能管理页导入）：

- `workbench-status-report` —— 生成工作台状态简报
- `governance-policy-trace` —— 政策溯源：检索 → 拒答闸门 → 逐环回查 → 成文

### 5. 验证

```bash
python3 mcp/retrieval-mcp-server.py --self-test
python3 mcp/workbench-mcp-server.py --self-test
python3 tools/check-contract-doc.py --self-test   # 契约负对照 6/6
```

---

## 复现实测数字

本体三层与语料链的每个数字都能用单条命令复现（详见 `docs/复核命令.md`）：

```bash
# 规则层：抽三元组（需先准备 chunks.jsonl）
python3 ontology/build.py --src <语料> --limit 60000 \
    --terms ontology/terms.governance.zh.json --out triples.jsonl
# 预期：255,451 条三元组，provenance_failed = 0

# 推理层：引用图 + 传递闭包
python3 ontology/reason.py --src triples.jsonl --out reason.json --chains chains.jsonl
# 预期：14,035 节点 / 148,005 边 / 闭包 171,552 对

# 双侧过滤（文件 → 文件）
python3 ontology/prune.py --src triples.jsonl \
    --subject-judge <judge 产出> --object-judge <judge 产出> --out doc2doc.jsonl
# 预期：双侧都清 15,870 条边 / 8,532 个节点

# 各层自测
python3 ontology/build.py --self-test    # 12/12
python3 ontology/align.py --self-test    # 4/4
python3 ontology/prune.py --self-test    # 6/6
python3 ontology/judge.py --self-test    # 6/6
```

**零改码迁移**（`corpus-manufacturing/` 是自造的合成语料，用于验证机制）：

```bash
python3 ontology/build.py --src corpus-manufacturing/chunks.jsonl \
    --terms ontology/terms.manufacturing.zh.json --out mfg-triples.jsonl
# 预期：50 条三元组
# 对照：同一批语料换 terms.governance.zh.json → 只抽 22 条（词表决定召回）
```

---

## 目录结构

```
├── mcp/                   2 套 MCP 服务（6 个工具）
├── skills/                2 个自建 Skill（编排流程 + 拒答闸门）
├── ontology/              本体三层（schema + 行业词表 + 四个脚本）
├── tools/                 语料链（清洗/切分/选子集/导入）+ UI 截图/录屏 + 契约判据
├── track2-mcp/            【赛题二】引用回查 MCP（零依赖 UVX 包 · 3 工具 · 96 条判据）
├── workbench/             工作台 30 个 agent 定义 + 8 个工作流（l1 10 / l2 8 / l3 12）
├── docs/                  设计文档与实测记录（01–04 主文档 + 235–249 分项记录 + 复核命令 + 数字口径表 + 使用说明书）
├── corpus-manufacturing/  合成语料（12 篇 / 25 块，用于零改码迁移验证）
├── figures/               智能体调用关系图（PNG + 可交互 HTML）
├── deck/                  参赛演示 PPT（12 页 · PPTX + PDF + 生成脚本）
├── evidence/              UI 实拍截图与关键文本（8 张）
└── videos/                演示录屏（2 段）
```

---

## 本体三层为什么这样分

**规则层召回、模型层判定、人工闸门兜底** —— 三层分工的理由：

- 规则层用词表做**高召回**抽取（政务语料里 `cites` 一类谓词能抽到 17.8 万条），代价是**误召回**：正文里提到一个文件名，不等于这份文件真的引用了它。
- 于是需要模型层**逐条判定**「这是真文件吗」。实测 subject 侧候选里只有 12.5% 是真文件，object 侧 21.6% —— 噪音占比很高，不判定就全是假的引用边。
- 判定结果仍不直接入库：`schema.json` 的 `review_gate` 规定只有 `high` 置信度可自动入库，`medium`/`low` 必须人工确认。

**一个诚实的修正**：早期我们用「各 400 个候选」抽样得出「双侧都清 0 条边」，并据此推断「政务文件之间的引用链很稀疏」。全量判定后发现那是**抽样错在层** —— 400 个 subject 候选里只有 6 个是真文件，从噪音占 90% 的池子里随机抽，自然抽不到边。正确做法是**先从已确认为文件的节点出发再往外走**，真实数字是 15,870 条。

---

## 诚实边界

1. **语料是公开数据**（ModelScope `peopletech/government`，Apache-2.0），不等于企业内部的沉睡资产；本仓库不包含该语料本体，只包含处理脚本与子集选择策略。
2. **先进制造语料通道断链**：国标全文受版权、gov.cn 政策 API 匿名查询 0 结果、ModelScope 文档型工业语料 403。因此零改码迁移用的是**合成语料** —— 验证的是**机制**（改动 0 行、脚本复用 5/5），不是真实行业效果。
3. **判定层用外部模型**：换模型结论可能变；`review_gate` 之后目前 0 条人工确认入库。
4. **GPU 化未落到生产**：44 倍加速在独立环境实测，尚未切换正在跑的服务。
5. **知识库是子集**：全量 1,864,451 块按 0.8 块/秒约需 27 天，不可行；采用 15,000 块分层子集策略。
6. **规则抽取不是 NER**：召回率未测；推理层深度天花板 3 跳（第 3 跳开始掺正文短语，这正是需要模型判定层的原因）。

---

## 赛题二：引用回查 MCP（`track2-mcp/`）

一个零依赖的 UVX 包，把「模型说『原文这么写』，到底写没写」做成可调用的三个工具：

| 工具 | 作用 |
|---|---|
| `verify_quote` | 给一句引用，判断它在 15,000 块语料里是逐字命中、规范化命中还是查不到；**风险扫描与展示解耦** —— 展示 10 条也扫全库 5,598 条 |
| `lookup_chunk` | 给 `chunk_id`，返回原文与元信息 |
| `list_sources` | 给主题词，返回命中块数、文档数、来源与年份分布 |

**它能拦住的**（三轮对抗测试 28 条攻击用例，修了 23 条绕过路径）：正序大间隙拼接、否定词落在引用边界外、省略号吃掉否定词、限定语被丢弃（范围/条件/例外/强度四类）、零宽字符切断、展示参数关闸门、跨块拼接。

```bash
cd track2-mcp && python3 scripts/build_data.py   # 15,000 块 / 11.7 MB
python3 tests/test_lookup.py                     # 52/52
uvx --from dist/*.whl ict-track2-mcp             # 握手 → 3 工具
```

完整说明见 [`track2-mcp/BUILD.md`](track2-mcp/BUILD.md) 与 [`track2-mcp/README.md`](track2-mcp/README.md)。

---

---

## 协议

代码采用 **Apache License 2.0**（见 `LICENSE`）。

文档与图表采用 **CC BY 4.0**。

合成语料（`corpus-manufacturing/`）为项目自造，同 Apache-2.0。
