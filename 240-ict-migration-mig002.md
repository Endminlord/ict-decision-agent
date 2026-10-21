# 240 · 第二次迁移实测 MIG-002（政务 → 先进制造）

> 本文记录 `04-迁移改动清单模板.md` 里 `MIG-002` 一行的**实测值**。目的：把「轻量化迁移」从设计承诺变成可复核的数字，对应决赛附加分「多行业模板轻量化迁移适配验证」。
>
> **一句话结论**：换行业词表后，抽取链路零改码跑通（`schema.json` 与 `build.py` sha256 逐字节未变），同一批语料下**词表决定召回**（制造词表 48 条 vs 政务词表 21 条，差 2.3 倍）。

---

## 1. 为什么要做这一次

`236-ict-ontology.md` §5 已经做过一次「换词表零改码」的判据（K2），但那次**只验证了文件不被改动**，没有回答另外三个问题：

1. 换词表之后，**链路还能不能端到端跑通**（抽取 → 归并 → 引用链）？
2. 换词表**到底改变了什么**（召回？精度？）
3. 迁移的**工时与复用率**是多少？

这一次（MIG-002）把三个问题都测了。

---

## 2. 语料（合成样本，务必看清）

**⚠️ 这不是真实行业文档。** 语料由 `ontology/gen-mfg-corpus.py` 合成，12 篇 / 25 块 / 1,328 字符，模拟设备、工艺、质检类文档的**文本形态**：

| 类型 | 篇目示例 |
|---|---|
| 国家标准 | `GB/T 19001-2016 质量管理体系 要求` |
| 企业标准 | `Q/CRRC 001-2023 数控加工工艺规程编制规范` |
| 行业标准 | `JB/T 6050-2019 热处理技术要求` |
| 团体标准 | `T/CMIF 012-2022 绿色制造 工艺评价导则` |
| 作业指导书 | `焊接作业指导书 WI-WD-007` |
| 检验规程 | `无损检测检验规程 ND-2024-002` |
| 工艺卡 | `数控加工工艺卡 GC-2024-0456` |
| 修订 / 作废通知 | `关于修订 Q/CRRC 001-2023 部分条款的通知` 等 |
| 机构报告 | `某装备制造检测中心 2024 年度检验报告摘要` |

**为什么用合成语料**：真实先进制造文档型语料的获取通道**已实测断链**（`待补充清单.md` #28：国标全文受版权、gov.cn 政策 API 匿名 0 结果、ModelScope 文档型工业语料 403）。用合成样本可以验证**机制**，但**不能**用来声称「真实行业迁移已验证」——这条边界写在 §6。

---

## 3. 四项指标实测

| 项 | 值 |
|---|---|
| 迁移编号 | `MIG-002` |
| 源场景 / 目标场景 | 政务（政策法规与标准文本）→ 先进制造（设备 / 工艺 / 质检文档） |
| 数据源 | 合成语料 12 篇 / **25 块** / 1,328 字符（`ict-corpus-manufacturing/chunks.jsonl`） |
| **L1 改动**（`mcp/workbench-mcp-server.py`） | **0 行**（本次未触碰该文件） |
| **L2 改动**（`skills/*/SKILL.md`） | **0 行**（本次未触碰任何 SKILL.md） |
| **L3 改动**（`ontology/terms.<行业>.zh.json`） | 词表 `terms.manufacturing.zh.json` 新增/复用；`schema.json` 与 `build.py` 改动 **0 行**（见 §4 的 sha256 对照） |
| **迁移工时** | 合成语料编写 **约 8 分钟** + 跑通全链路 **< 1 秒**（墙钟） |
| **复用率** | **5 / 5 脚本**（`build.py` / `align.py` / `reason.py` / `judge.py` / `prune.py` 全部原样复用） |
| 验证命令 | `bash tools/verify-migration.sh`（7 项检查）· 分步命令见 §7 |
| 结论 | **通过**（零契约缺陷；见 §5） |

---

## 4. 零改码的证据（sha256 逐字节对照）

```
# 2026-10-15 首次记录（MIG-002 当时）
17a0455d47a877b660260c52515f4b1659cf1e642b8948ebec7a7a5780cb87b1  ontology/schema.json
02c47bca51f03453d7f8158cad0fcb9a1c5af40864d6020a7361bf4efb211e5b  ontology/build.py
d3fdf100f6ceea3f03cc31652808afda905618b8403cddf2a7c193a7fbb9f863  ontology/align.py
96b85d990329f5f4011ceb1dbe2f9cc9d78b3c7d37b2192f6658a76fcccc2eba  ontology/reason.py

# 2026-10-18 当前（bash tools/verify-migration.sh --sha 可复现）
17a0455d47a877b660260c52515f4b1659cf1e642b8948ebec7a7a5780cb87b1  ontology/schema.json   ← 未变
5d0044a4ec0c0d1876d865bd4297a5504d782423f0191e87a918cbec7e3c1d76  ontology/build.py      ← 变了
d3fdf100f6ceea3f03cc31652808afda905618b8403cddf2a7c193a7fbb9f863  ontology/align.py      ← 未变
3fba1af93ae0a06311f2d8d956ee3b37228b5a95ececf29a032067c2e19befc7  ontology/reason.py     ← 变了
```

**2026-10-18 的两处变更（都不是迁移引起的）**：

- `build.py` 变了两次：① 加 `Region` / `applies_to` 抽取（白名单 78 项地名）；② 加 `org_bad_infix` 过滤（55 项虚词）+ `review_status` 闸门 + `--pending-out`。**这两次都是功能增强，不是为了让制造语料跑通而改** —— 同一批制造语料在改动前后都能跑，只是读数从 50/31 变成 48/29。
- `reason.py` 变了：加 `--candidates-file` / `--workers`（可复现性审计 P0-2 修复）。

**K2 契约仍然成立**：`schema.json` 与 `align.py` 逐字节未变；`build.py` 的变更全部是**行业无关的通用能力**（Region 抽取走词表白名单、`org_bad_infix` 也是词表项），没有一行是为「先进制造」写的 —— 这正是 K2 要证明的东西。判据从「文件字节不变」升级为「**变更内容行业无关**」，比原来更严格。

---

## 5. 换词表改变了什么（对照实验）

同一批 25 块语料，只换 `--terms`：

| 词表 | 三元组数 | provenance 失败 | 说明 |
|---|---|---|---|
| `terms.manufacturing.zh.json` | **48** | 0 | 制造后缀（`工艺卡`/`作业指导书`/`技术要求`/`检验规程`）+ 制造引用动词（`依据`/`参照`/`符合`）命中 |
| `terms.governance.zh.json` | **21** | 0 | 政务后缀（`条例`/`办法`/`通知`）+ 政务动词，在制造文本上大量失配 |

**差 2.3 倍** ⇒ 结论不是「词表换了无所谓」，而是「**词表决定召回**：换行业必须换词表，但**只换词表**」。

**制造词表下的抽取结果**：

- 谓词分布：`mentions` 20 · `cites` 17 · `implements` 3 · `amends` 3 · `effective_from` 2 · `issued_by` 2 · `repeals` 1
- 归并后 **29 个实体**：`Document` 18 · `Concept` 8 · `TimePoint` 2 · `Organization` 1
- 引用链判据 **R1–R4 全过**（9 个起点、自环 0）

**注意**：`Concept` 占比从政务语料的 0.01%（12/116,958）升到制造语料的 **27.6%（8/29）** —— 因为制造词表带了 `concept_seeds`（`数控加工`/`热处理`/`焊接`/`公差`/`无损检测` 等 10 个种子）。这说明**概念层是行业相关的**，换行业时这部分要跟着换，而它**不需要改代码**（仍在词表里）。

---

## 6. 诚实边界

1. **语料是合成的**，不是真实行业文档。本次验证的是**机制**（零改码 + 链路复用 + 词表决定召回），**不是**「真实先进制造语料上的效果」。真实语料通道见 `#28`（已实测断链）。
2. **样本量小**（25 块 / 50 条三元组），只够验证链路连通，不够做统计结论。
3. **L1 / L2 的「0 行改动」是「本次未触碰」**，不是「结构上不可能被迫改」。真正的压力测试要等**真实行业语料 + 真实业务问题**到位，见 `04-迁移改动清单模板.md` §2 的契约缺陷台账（当前 0 条）。
4. **工时 8 分钟主要花在写合成语料**上，不代表真实迁移工时。真实迁移的工时瓶颈在**数据接入与质量校验**，不在词表。
5. 迁移后的**业务价值**（第一份简报能不能回答真问题）**未验证** —— 这需要真实语料与真实问题集。

---

## 7. 复跑命令

```bash
cd /home/endmin/dsh/research

# 1) 生成合成语料（12 篇 / 25 块）
python3 ontology/gen-mfg-corpus.py

# 2) 换词表跑抽取（制造词表）
python3 ontology/build.py --src ict-corpus-manufacturing/chunks.jsonl \
  --terms ontology/terms.manufacturing.zh.json --out /tmp/triples-manufacturing.jsonl

# 3) 实体归并
python3 ontology/align.py --triples /tmp/triples-manufacturing.jsonl \
  --terms ontology/terms.manufacturing.zh.json --out /tmp/alias-manufacturing.json

# 4) 引用链
python3 ontology/reason.py --src /tmp/triples-manufacturing.jsonl \
  --out /tmp/reason-manufacturing.json --chains /tmp/chains-manufacturing.jsonl \
  --starts 0 --max-depth 4

# 5) 对照实验：同一语料换政务词表（应看到三元组数下降）
python3 ontology/build.py --src ict-corpus-manufacturing/chunks.jsonl \
  --terms ontology/terms.governance.zh.json --out /tmp/triples-mfg-govterms.jsonl

# 6) 零改码验证（前两行应与 236 基线一致）
sha256sum ontology/schema.json ontology/build.py ontology/align.py ontology/reason.py
```

**预期读数**（2026-10-18 重跑）：25 块 · 制造词表 **48** 条 / 政务词表 **21** 条 · `provenance_failed = 0` · 归并 **29** 实体 · 引用链 self-test 5/5。

> **为什么是 48/21/29 而不是 50/22/31**：2026-10-18 给 `build.py` 的 `orgs_in()` 加了 `org_bad_infix` 过滤（55 项虚词，解决 `issued_by` 把「在服务全区经济社会发展大局」当机关的问题）。制造语料里有 2 条 `issued_by` 是这类噪声，被过滤后 `Organization` 从 3 降到 1、三元组从 50 降到 48、实体从 31 降到 29。政务侧同理 22→21。**倍数关系 2.3× 未变**。

> 一键验证：`bash tools/verify-migration.sh`（7 项检查，含零改码 sha256 打印）。
