---
name: ontology-migrate
allowed-tools:
  - bash
description: |
  换行业迁移工作流：把一个已经在政务语料上跑通的本体抽取链路，迁移到另一个行业（制造、医疗、金融等）。
  核心约束是「换行业只换词表，代码零改动」—— 本技能给出验证这条约束的命令序列与判据，
  而不是靠人读代码判断「应该没改吧」。
  适用于行业智能体的冷启动、跨领域复用、以及向评委证明「方法不绑行业」的场合。
tags:
  - ontology
  - migration
  - cross-domain
  - verification
---

# 换行业迁移工作流

## 目标

用**可执行的判据**证明：换一个行业，抽取代码一行没改，效果来自词表。

## 依赖

材料包根目录下执行，需要 `ontology/` 与 `tools/`：

```bash
cd ict-material-20261010
```

## 三个文件的分工

| 文件 | 是否随行业变 | 说明 |
|---|---|---|
| `ontology/schema.json` | **不变** | 类型系统：5 类实体 + 8 个关系 |
| `ontology/build.py` | **不变** | 抽取引擎 |
| `ontology/terms.<行业>.zh.json` | **变** | 词表：后缀、动词、机关名、地域白名单 |

## 执行步骤（必须按序）

1. **记录基线哈希**（迁移前）：

   ```bash
   sha256sum ontology/schema.json ontology/build.py ontology/align.py ontology/reason.py
   ```

2. **准备新行业词表**：复制 `ontology/terms.governance.zh.json` → `terms.<新行业>.zh.json`，
   逐项替换：`doc_suffixes` / `doc_suffixes_weak` / `org_suffixes` / `org_bad_infix` /
   `cite_verbs` / `implement_verbs` / `amend_verbs` / `repeal_verbs` / `region_names` / `concept_seeds`。

3. **跑抽取**（用新词表）：

   ```bash
   python3 ontology/build.py --src <新行业语料.jsonl> --limit 2000 \
     --terms ontology/terms.<新行业>.zh.json \
     --out /tmp/new.jsonl --loose-out /tmp/new-loose.jsonl
   ```

4. **跑迁移验证脚本**（一键，7 项检查）：

   ```bash
   bash tools/verify-migration.sh
   ```

   它检查：合成语料生成 · 制造词表抽取 · provenance 失败 0 条 · 实体归并 ·
   引用链 self-test · 词表对照（新行业词表产出的实体数必须**显著多于**政务词表在同样语料上的产出）·
   零改码基线 sha256 打印。

5. **确认零改码**：再跑一次第 1 步的 `sha256sum`，**四个文件必须逐字节相同**。

## 判据

| 判据 | 通过标准 |
|---|---|
| 零改码 | 四个核心文件的 sha256 与迁移前**逐字节相同** |
| 词表有效 | 新行业词表产出的三元组数 > 政务词表在同一语料上的产出（实测约 2.3×） |
| 出处完整 | `provenance_failed == 0` |
| 链路可用 | `reason.py --self-test` 全过 |

## 注意

- **「零改码」的判据是 sha256 不变，不是「我觉得没改」**。每次迁移都要重新记录、重新比对。
- **词表不是越长越好**：`org_suffixes` 里放单字后缀（部/局/厅/院）会让「大局」「格局」「政治局」
  全部被当成机关 —— 政务词表踩过这个坑，靠 `org_bad_infix`（55 项虚词）才压住。
- **地域用白名单不用后缀正则**：`region_suffixes` 猜地名会抽出「印发南宁市」「资者对上市」这类碎片，
  改成 `region_names` 白名单（78 项）后精度 100%，代价是召回低。
- **合成语料只能验证机制，不能验证效果** —— 迁移验证用的是 `gen-mfg-corpus.py` 生成的 25 块合成语料，
  它证明「链路能跑通」，不证明「在真实制造业语料上效果好」。
