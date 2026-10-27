# 250 · 先进制造真实语料打通（附加分第 1 项）

- 时间：2026-10-21

## 2026-10-27 更新

本通道已由 `8018fda` 扩量至 **22 篇 / 119,981 字符 / 178 块**。本文保留 **9 篇 / 46 块**作为首通基线记录，涉及最新数字以本更新节为准。
- 目标：解决 `待补充清单.md` #28（通道断链）与 #33（迁移只有合成语料）
- 结论：**真实语料通道打通** —— 9 篇 / 29,812 字，来自中国政府网国务院政策文件库；全链路判据 PASS

---

## 一、通道选择（为什么不是 ModelScope）

`tools/probe-industry-corpus.py search 制造业` 返回的候选逐个验证：

| 数据集 | 文件树 | 判定 |
|---|---|---|
| `keimzhao/DiscreteManu-HQDataset` | README + `torque_resistance_model.zip` | 数据表，非文档 |
| `wuxiansanjin/industrial-process-operations-data-expert` | 仅 README（406 B） | 空壳 |
| `inizhu/EquipMfg_GenKnow` | 接口 403 | 不可达 |
| `youzhikongqi/ElecInforCorpus-public-V1.0` | 接口 403 | 不可达 |

⇒ ModelScope 的制造类数据集**要么是数据表、要么不可达**，事实复核了 #28 的判断。

**改用官方政策文件库**（与医疗/金融同源范式）：

| 站点 | 状态 | 说明 |
|---|---|---|
| `https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json` | ✅ 266 KB / 1,106 条 | 结构化 JSON（TITLE / URL / DOCRELPUBTIME），**无需渲染** |
| 工信部 `miit.gov.cn` | ✗ | 正文由 `AuthorizedRead/unitbuild.js` 动态注入，POST 接口返回「不支持的HTTP方法」 |
| 国家标准委 `sac.gov.cn` | 部分 | 可抓列表，正文页需另行定位 |

采用国务院 JSON：**1106 条政策里筛出 9 篇先进制造强相关**（制造/装备/工业/产业链/人工智能+/标准化/新型工业化/智能工厂/机器人/数控/两化/质量强国）。

## 二、抓取与切片

| 步骤 | 命令 | 结果 |
|---|---|---|
| 抓取 | `curl -A <UA> ZUIXINZHENGCE.json` → 按标题关键词筛选 → 逐篇取 `id="UCAP-CONTENT"` 正文 | **9 篇 / 29,812 字** |
| 切片 | `python3 tools/corpus-chunk.py /tmp/mfg-clean.jsonl --out /tmp/mfg-chunked` | **46 块**，块长 min 214 / median 819 / p90 866 / max 892 |
| 判据 | D1 输出块数>0 · D2 块长在界内 · D3 抽样出处回查 · D3b 全量自检 | **PASS（抽样 46 块，一致 46，不一致 0）** |

**踩坑（值得记）**：`corpus-chunk.py` 读输入用 `o.get("id")`，不是 `doc_id`。首轮我按 `doc_id` 写 jsonl，导致 46 块全部 `doc_id` 为空、D3 抽样回查 4/46 一致 → FAIL。改成 `id=mfg-000…` 后 46/46 一致。**判据 D3 抓出的正是这类输入契约错误，不是切片算法错误。**

## 三、本体抽取（换词表，零改码）

首次直接跑制造词表：`triples_out=0`。原因是**词表后缀与语料文体不匹配** ——

| 词表 | doc_suffixes | 该语料实际文体 |
|---|---|---|
| 制造（原） | 标准 / 规范 / 手册 / 规程 / 图样 / 工艺卡 | 规定 / 法 / 纲要 / 意见 |
| 医疗 | 条例 / 纲要 / 清单 / 规划 / 通知 / 细则 | — |
| 金融 | 法 / 条例 / 办法 / 规定 / 规则 / 准则 | — |

按医疗/金融口径扩展制造词表（保留工艺术语，补入政策法规后缀 28 项，`doc_name_min_len` 6→4，`cite_verbs` 补 5 项），`schema.json` 与 `build.py` **改动 0 行**：

| 指标 | 扩展前 | 扩展后 |
|---|---|---|
| strict（强证据层） | 0 | **10** |
| loose（候选层） | 410 | **498** |
| auto_ratio | 0.0 | **1.0** |
| provenance_failed | 0 | **0** |

抽出的 10 条（全部 `high`，可自动入库）：

```
国务院关于产业链供应链安全的规定 --cites--> 中华人民共和国国家安全法
                              --cites--> 中华人民共和国对外关系法
                              --cites--> 中华人民共和国反外国制裁法
                              --cites--> 中华人民共和国对外贸易法
                              --implements--> 同上 4 部
中办国办关于调整工业和信息化部职责机构编制的通知 --cites--> 党和国家机构改革方案
                                          --cites--> 党中央、国务院议事协调机构优化调整方案
```

本体对齐：`canonical_entities=6`，回查 `sampled=10 / found=2 / verified=2 / failed=0`。

## 四、词表对照实验（回答「迁移是不是真迁移」）

**同一份语料、同一套代码，只换词表**：

| 语料 | 制造词表 | 政务词表 | 金融词表 |
|---|---|---|---|
| 真实制造政策（9 篇 / 46 块） | strict 10 / loose **498** | strict 10 / loose **534** | strict 10 / loose **117** |
| 合成制造工艺语料（9 块） | strict 0 / loose **11** | strict 0 / loose **4** | strict 0 / loose **4** |

**诚实读法**：
1. 真实政策语料上制造与政务词表 strict 都是 10 —— 因为**书名号引用路径与文书后缀高度重合**，政策文本的强证据靠「书名号 + 引用动词」，两家词表都覆盖。差距体现在 **loose 候选层：498 vs 534 vs 117**（金融词表最窄，因为它的后缀表不含「纲要/规划/方案」）。
2. 合成工艺语料上制造词表 loose **11 vs 4**（2.75×）—— 这才是「词表决定召回」的干净证据，因为该语料没有任何政策文书后缀，只有工艺术语。
3. **不能声称的**：在真实政策语料上制造词表严格层优于政务词表。数据不支持这个结论，如实写在这里。

## 五、落地清单

| 文件 | 内容 |
|---|---|
| `evidence/manufacturing-docs.jsonl` | 9 篇原文（id / title / date / source / url / text） |
| `evidence/manufacturing-chunks.jsonl` | 46 块，带 `doc_id + char_offset` 出处锚 |
| `evidence/manufacturing-triples-strict.jsonl` | 强证据层 10 条 |
| `evidence/manufacturing-triples-loose.jsonl` | 候选层 498 条 |
| `evidence/manufacturing-align.json` | 对齐结果（6 个规范实体 + 回查 10/2/2/0） |
| `ontology/terms.manufacturing.zh.json` | 词表扩展（工艺 + 政策双覆盖） |

复跑：

```bash
python3 tools/corpus-chunk.py evidence/manufacturing-docs.jsonl --out /tmp/mfg-chunked
python3 ontology/build.py --src /tmp/mfg-chunked/chunks.jsonl --out /tmp/t.jsonl \
  --terms ontology/terms.manufacturing.zh.json --loose-out /tmp/tl.jsonl
python3 ontology/align.py --triples /tmp/t.jsonl --terms ontology/terms.manufacturing.zh.json \
  --out /tmp/al.json --verify-chunks /tmp/mfg-chunked/chunks.jsonl --sample 20
```

## 六、仍未解决（诚实边界）

1. **语料规模**：9 篇 / 46 块，比医疗（26 篇）与金融（15 篇）还小。根因是 `ZUIXINZHENGCE.json` 只含「最新政策」1106 条，工信部正文页动态注入抓不到。
2. **需补的通道**：工信部司局文件页的正文由 `AuthorizedRead/unitbuild.js` 注入，需走浏览器渲染（同 #23 医保局列表页的解法：bsk + Chrome 渲染）。
3. **strict 只有 10 条**：政策文体能产出的强证据本来就窄（书名号引用），要提升得靠工艺术语型真实语料（GB/T 标准全文受版权限制，未取得）。
