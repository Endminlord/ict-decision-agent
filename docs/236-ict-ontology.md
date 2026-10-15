# 236 · 本体层（L3 契约）从设计稿到可跑：半自动构建 + 出处回查 + 换行业零改码

**日期**：2026-10-13 · **产物目录**：`research/ontology/`
**对应**：`03-场景无关契约与迁移设计.md` 的 L3 本体 schema 契约（截至 2026-10-14 **三层均已实现**：规则层 + 推理层 + 模型判定层）、赛题三任务要求②「低资源领域知识图谱本体半自动化构建」

## 1. 为什么做这一步

`03-场景无关契约与迁移设计.md` 把「场景无关」拆成三层：L1 MCP 工具契约（已实现）、L2 Skill 模板契约（已实现）、**L3 本体 schema 契约（本步完成规则层；推理层与模型判定层见 `237` / `238` / `239`）**。
L3 的验收判据在文档里已写好，本步就是把它跑出来：

| 判据 | 内容 | 实测 |
|---|---|---|
| **K1** | `build.py` 输出的每条三元组都带得出处 | 60,000 块 → **255,451 条**，`provenance_failed = 0` |
| **K2** | 换行业时 `schema.json` 与 `build.py` 改动行数 = 0 | 换制造词表后两文件 sha256 前后一致（见 §5） |
| **K3** | 对齐映射表能反向查回原文 | 抽样 100 条 occurrence 回源验证 **100/100** |

## 2. 分层：行业知识全在词表里

| 文件 | 职责 | 换行业时 |
|---|---|---|
| `schema.json` | 类型系统（5 个实体类型 / 8 个关系类型）、出处字段要求、置信度分级、人工确认闸门 | **不得改** |
| `terms.<行业>.zh.json` | 文档后缀（强/弱）、关系动词、机关后缀与坏后缀、时间句式、层级线索、概念种子 | **只改这里** |
| `build.py` | 抽取引擎：正则编译、分句、三元组构造、出处校验 | **不得改** |
| `align.py` | 规范名归并（去书名号/空白/全角、文号归一）+ 反向查回原文 | **不得改** |

`build.py` 里没有任何行业词——它只做「从词表编译正则 → 在句内匹配动词与实体 → 造三元组 → 校验出处」。

## 3. 抽取规则与置信度

| 关系 | 触发 | 置信度 |
|---|---|---|
| `cites` / `amends` / `repeals` / `implements` | **同一句内**出现关系动词 + 目标文档名 | `high` |
| 同上 | 同一块内共现但无关系动词 | `medium`（命中文档后缀）/ `low`（未命中） |
| `issued_by` | 标题或正文里的机关名 | 正文出现 = `high`；只在标题 = `medium` |
| `effective_from` | 「自YYYY年M月D日起施行」 | `high` |
| `mentions` | 概念种子命中 | `low` |

`schema.json` 的 `review_gate` 写死了人工闸门：**只有 `high` 可自动入库，`medium` / `low` 必须人工确认**。
即「半自动」的落点：机器给候选 + 出处，人给确认。

## 4. 判据咬出的 5 个真 bug（本步最有价值的部分）

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | 首跑 `provenance_failed = 47,394`（34.6%） | `issued_by` 用 **title** 当 evidence，而 title 不在块文本里 ⇒ 出处判据必然失败 | 证据只取正文；机关只在标题出现时降级 `medium` 并标 `evidence_source: title_only` |
| 2 | 修完仍 9,908 条空 evidence | 句内偏移用 `split` + `find` 递推，遇连续分隔符产生的空句时被污染 ⇒ 切片越界 | 改用 `finditer` 拿每句真实 span |
| 3 | 多目标句仍产出空 evidence | 目标在动词**之前**时，切片区间 `[vm.start()-10 : b]` 起点大于终点 | 区间改为同时覆盖动词与目标：`[min(vm.start(), a)-10 : max(vm.end(), b)+10]` |
| 4 | `cites/high` 榜首是「按照规定」「制定本办法」 | 书名号里不只是文档名；`any(suffix in name)` 让「规定」这种弱后缀命中普通短语 | 强/弱后缀分组 + 长度门槛 + 硬/软坏前缀 + 专有标记（软前缀只在无专有标记时判假阳） |
| 5 | 「合法权益」被判为文档名、「部署」被判为机关 | 单字后缀（「法」「署」）用 `in` 匹配 | 单字后缀必须 `endswith`；机关另加坏后缀表（部署/决策/安排…） |

修完的效果（同一 60,000 块）：`cites/high` 榜首变成《中共中央关于进一步全面深化改革、推进中国式现代化的决定》《中国共产党纪律处分条例》《中华人民共和国土地管理法》等真法规名；机关榜变成国务院、财政部、交通运输部、广州市人民政府等真机构。

**教训**：这五个 bug 全是「出处判据 + 产出分布」逼出来的，没有一个是靠肉眼看代码发现的。判据必须校验**能回查**，而不只是校验**有值**。

## 5. K2 实测：换行业零改码

```bash
sha256sum ontology/schema.json ontology/build.py   # 换词表前
python3 ontology/build.py --src ict-corpus-chunks/chunks.jsonl --limit 20000 \
        --terms ontology/terms.manufacturing.zh.json --out ontology/triples-manufacturing.jsonl
sha256sum ontology/schema.json ontology/build.py   # 换词表后
```

| 文件 | sha256（前后一致） |
|---|---|
| `ontology/schema.json` | `17a0455d47a877b660260c52515f4b1659cf1e642b8948ebec7a7a5780cb87b1` |
| `ontology/build.py` | `02c47bca51f03453d7f8158cad0fcb9a1c5af40864d6020a7361bf4efb211e5b` |

两文件逐字节未变 ⇒ **改动行数 0**。同一批语料在两套词表下产出结构明显不同（`mentions` 从 9,470 降到 80、机关 top 从「国务院」变成「自然资源部 / 农业农村部 / 交通运输部」），证明行业差异确实由词表驱动。

## 6. 规模与产物

| 产物 | 规模 |
|---|---|
| `ontology/triples-governance.jsonl` | 60,000 块 → **255,451 条**三元组，`provenance_failed = 0` |
| `ontology/alias-index.json` | **127,766 个**规范实体，各带 3 条 occurrence 样本（含 chunk_id + doc_char_offset + evidence） |
| 谓词分布 | `cites` 178,099 · `issued_by` 42,717 · `implements` 14,963 · `mentions` 9,470 · `amends` 8,273 · `repeals` 1,218 · `effective_from` 711 |

## 7. 诚实边界

- **规则抽取，不是 NER**：没有模型，全靠词表 + 正则；召回率未测（无法测——没有标注集）。
- **只到「文档级」关系**：抽的是「本文档引用了《X》」，不是「第 5 条第 2 款引用了《X》第 7 条」。
- **`medium` / `low` 未入库**：按 `review_gate`，它们只是候选，需人工确认；本轮没有做人工确认，所以**没有一条三元组被写进知识库**。
- **未接 LLM 补召回**：`build.py` 的 docstring 里留了 `--prompt-candidates` 的钩子，但本轮没实现。
- **未做本体推理**：`schema.json` 标了 `cites` 可传递，但没有跑闭包（A 引用 B、B 引用 C ⇒ A 引用 C）。
- **语料侧仍是子集**：本轮用前 60,000 块（全量 1,864,451 块）。

## 8. 复跑

```bash
cd /home/endmin/dsh/research
python3 ontology/build.py --self-test                                   # 12/12
python3 ontology/align.py --self-test                                   # 4/4
python3 ontology/build.py --src ict-corpus-chunks/chunks.jsonl --limit 60000 \
        --terms ontology/terms.governance.zh.json --out ontology/triples-governance.jsonl
python3 ontology/align.py --triples ontology/triples-governance.jsonl \
        --terms ontology/terms.governance.zh.json --out ontology/alias-index.json \
        --verify-chunks ict-corpus-chunks/chunks.jsonl --sample 100
```

**判据说明**：`--self-test` 只证明规则有效；要证明「产出真的能回查」，必须跑带 `--verify-chunks` 的那条——它会把 occurrence 拿回源块按偏移复算。
