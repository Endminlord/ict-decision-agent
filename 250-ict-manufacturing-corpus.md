# 先进制造真实语料（扩充版）

## 语料规模

| 项 | 值 |
|---|---|
| 篇数 | 22 |
| 正文字数 | 119,981 |
| 切片块数 | 178 |
| 块长 | min 114 / median 821 / p90 859 / max 1,011 |
| 出处回查 | 抽样 178 块全部一致（D1–D3b 全 PASS） |

## 来源与通道

中国政府网政策文件库 `https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json`（1,106 条结构化政策，
字段 TITLE / SUB_TITLE / URL / DOCRELPUBTIME），正文按 `id="UCAP-CONTENT"` 抽取。

**关键发现（此前 9 篇的原因）**：同站检索接口 `sousuo.www.gov.cn/search-gov/data` 的
`searchfield` 参数决定范围——`searchfield=title` 只搜标题（「智能制造」仅 2 条），留空则全文检索
（681 条）。此前取 9 篇正是踩了标题检索。全文检索虽召回高，但会带进「体育赛事」「全民健身」
这类正文偶然提及的无关政策，故仍以 ZUIXINZHENGCE 的主题显式字段筛，再按标题词表分流。

工信部站内通道实测不可用：`/zwgk/zcwj/wjfb/index.html` 返回 `window.location.href` 跳转脚本，
目标 `/search/wjfb.html?tpl=14` 的列表由 JS 动态注入，6 个候选接口（`/api`、`/search/wjfb`、
`api-gateway/jcms-search/api/search` 等）全部 404。

## 三元组抽取

| 项 | 值 |
|---|---|
| 严格层 | 30 条 |
| 候选层 | 1,226 条 |
| auto_ratio | 1.0 |
| provenance_failed | 0 |
| 本体对齐 | 14 个规范实体 / 抽样 27 命中 8 verified 8 failed 0 |

## 词表口径说明

`ontology/terms.manufacturing.zh.json` 的 `doc_suffixes` 覆盖两类文档：工艺术语
（标准/规范/工艺卡/作业指导书）与产业政策法规（法/条例/规定/意见/纲要）。此前只有工艺术语，
对政策公文体产出 0 条三元组；补齐政策后缀后 strict 0→30。schema.json 与 build.py 改动 0 行。
