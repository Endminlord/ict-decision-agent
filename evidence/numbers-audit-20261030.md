# 数字对表审计（2026-10-30）

基准：`evidence/key-numbers-20261030.md`。只读核对 `README.md`、`docs/`、`BENCHMARK*` 与 `evidence/eval-stratified-*.json`；未修改现有文件。

判定规则：明确标注日期的历史实验数值、不同语料/索引/评测口径不直接判错；仍被写成当前状态、当前规模或当前材料规格的旧数字判为过期。下列“基准值”均来自基准卡相应行。

## 一、规模与入库状态（20 条）

1. `README.md:30`：当前仍写“ES 已入 **18 万+ 块**、导入进行中”；基准 `evidence/key-numbers-20261030.md:7` 为 **满库，ES 1,874,596 / chunks 源 1,864,451**。状态及现量过期。
2. `README.md:80`：当前知识库写 **134,894+ 块**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块**。现量过期。
3. `README.md:169`：知识库写 **134,894+ 块**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块**。现量过期。
4. `README.md:196`：当前效果评测写 **15,494 块子集、全量导入进行中、完成后应复跑**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块满库**，`evidence/key-numbers-20261030.md:19` 已有满库复跑。规模与状态过期。
5. `docs/02-Nexent平台智能体设计说明.md:357`：2026-10-29 时点仍写已入 **378,894+ 块、导入中**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块、满库**。作为当前知识库规格已过期。
6. `docs/02-Nexent平台智能体设计说明.md:357`：写“政务语料 **757,810 块**”；基准 `evidence/key-numbers-20261030.md:6` 为 **757,810 条**，`evidence/key-numbers-20261030.md:7` 的块源为 **1,864,451 块**。条/块口径错误。
7. `docs/key-numbers.md:7`：旧关键数字卡写 **378,894+ 块、约 50%、导入中**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块、满库**。旧卡未同步。
8. `docs/design-graphrag-retrieval.md:16`：块索引写 **207k→190 万（导入中）**；基准 `evidence/key-numbers-20261030.md:7` 为 **chunks 源 1,864,451 / ES 1,874,596、满库**。状态与约数口径过期。
9. `docs/待补充清单.md:32`：知识库仍写 **15,494 块**，并称“仍缺全量导入”；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块、满库**。规模与缺口状态过期。
10. `docs/待补充清单.md:65`：全量规模策略仍写 **15,494 块子集已导完**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块、满库**。当前结论过期。
11. `docs/242-ict-rules-compliance.md:29`：知识库已入库写 **15,494 块**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块**。现量过期。
12. `docs/244-ict-finals-gap.md:61`：政务语料效果评测写“语料已在库 **15,494 块**”；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块**。现量过期。
13. `docs/244-ict-finals-gap.md:106`：仍称效果评测只覆盖 **15,494 / 1,864,451 = 0.83%**；基准 `evidence/key-numbers-20261030.md:19` 已为 **1.87M 块满库复跑**，R/M/X/C/G 为 **100 题同分层集**。覆盖状态过期。
14. `docs/答辩稿.md:308`：答辩问答写 ES 索引 **15,494 块**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块**。答辩口径过期。
15. `docs/问答速查卡.md:39`：仍称检索入库子集 **15,494 块 / 0.83%**、“只是入库按子集做演示”；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块、满库**。当前覆盖口径过期。
16. `docs/数字口径表.md:13`：“检索入库”当前行写 **15,494 块**；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块**，其中 **1,864,451** 是 chunks 源而非 ES 现量。当前行过期且需区分两个分母。
17. `docs/评分对照-20261029.md:16`：写“全量导入中 **378,894+**”；基准 `evidence/key-numbers-20261030.md:7` 为 **1,874,596 块、满库**。状态与现量过期。
18. `docs/评分对照-20261029.md:16`：写“政务 **757,810 块**”；基准 `evidence/key-numbers-20261030.md:6` 为 **757,810 条**。条/块口径错误。
19. `docs/评分对照-20261029.md:16`：先进制造写 **22 篇 / 46 块**；基准 `evidence/key-numbers-20261030.md:8` 为 **22 篇 / 178 块**（另有 **119,981 字符**）。把图文 46 块混入先进制造政策语料，数值与口径均不符。
20. `docs/答辩稿.md:264`：写“全量 = 政务语料 **757,810 块**”，又称 **1,864,451 块**仅属本体侧；基准 `evidence/key-numbers-20261030.md:6` 为 **757,810 条原始语料**，`evidence/key-numbers-20261030.md:7` 为 **1,864,451 chunks 源 / 1,874,596 ES 现量**。条/块及满库分母均错误。

## 二、满库复跑与 A2 状态（9 条）

21. `docs/方案兑现对账表.md:28`：E6 状态仍为“**等满库冻结索引**、导入后执行”；基准 `evidence/key-numbers-20261030.md:28` 已完成正式 A/B，`evidence/key-numbers-20261030.md:29` 为 **M FAIL p=1.0 / X FAIL p=1.0**。状态过期。
22. `docs/方案兑现对账表.md:45`：仍列“**等语料满库+索引冻结、满库复跑夜执行**”；基准 `evidence/key-numbers-20261030.md:19` 已有 **20261030-1118 满库复跑**，`evidence/key-numbers-20261030.md:29` 已有 A2 双 FAIL。状态过期。
23. `docs/答辩稿.md:263`：仍写“**满库后 replay** 一键刷新”；基准 `evidence/key-numbers-20261030.md:19` 已完成 **20261030-1118** 满库复跑，MRR **0.8494→0.8711**、X **19/25**。状态过期。
24. `docs/答辩当天流程.md:62`：“后续计划”仍列满库复跑；基准 `evidence/key-numbers-20261030.md:19` 已完成复跑，R **hit@10 1.00 / MRR 0.8711**、M **25/25**、X **19/25**、C/G **25/25**。状态过期。
25. `docs/答辩当天流程.md:75`：待办仍写“导入完成后满库复跑 + 全文档数字换血”；基准 `evidence/key-numbers-20261030.md:7` 已满库，`evidence/key-numbers-20261030.md:19` 已完成复跑。状态过期。
26. `docs/复核命令.md:751`：A2 标准流程仍标“**满库后**”；基准 `evidence/key-numbers-20261030.md:28` 已有 **20261030-1119 正式 A/B**。若作为当前操作状态则过期。
27. `docs/复核命令.md:771`：仍写“**夜场满库后**照上方命令直接跑”；基准 `evidence/key-numbers-20261030.md:29` 已跑出 **M/X 均 FAIL、p=1.0**。状态过期。
28. `docs/复核命令.md:773`：仍写“满库夜标准流程……据此拍板”；基准 `evidence/key-numbers-20261030.md:29` 已决定 **冠军维持混合冻结基线，τ=0.8505 不动**。状态过期。
29. `docs/评分对照-20261029.md:27`：阈值写“**τ=0.8505 待拍板**”；基准 `evidence/key-numbers-20261030.md:29` 为 **τ=0.8505 不动**。数值相同但决策状态过期。

## 三、PPT 页数（15 条）

30. `README.md:38`：**12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
31. `README.md:134`：**12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
32. `README.md:197`：**12 页时间码**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
33. `docs/数字口径表.md:70`：**12 页 / 15 分钟**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
34. `docs/答辩稿.md:3`：配套 PPT **12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
35. `docs/242-ict-rules-compliance.md:35`：**12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
36. `docs/01-开发设计文档.md:616`：**12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
37. `docs/244-ict-finals-gap.md:37`：**12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
38. `docs/244-ict-finals-gap.md:41`：**12 页**逐页稿；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
39. `docs/244-ict-finals-gap.md:43`：论证仍按 **12 页 / 900 秒**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
40. `docs/246-ict-track2-design.md:221`：复用基础写 **12 页 PPT**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
41. `docs/待补充清单.md:23`：**12 页**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
42. `docs/待补充清单.md:79`：仍按 **12 页**试讲；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
43. `docs/使用说明书.md:15`：**12 页 PPT**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。
44. `docs/使用说明书.md:244`：**12 页 PPT**；基准 `evidence/key-numbers-20261030.md:60`：**13 页**。

## 四、仪式幕数（4 条）

45. `README.md:23`：当前亮点写 **五幕验证仪式**；基准 `evidence/key-numbers-20261030.md:54` 为 **四幕仪式 90 秒 + 对抗三连 3/3**。主仪式幕数不一致。
46. `docs/scoring-alignment-20261028.md:24`：写 `verify-ritual` **五幕**；基准 `evidence/key-numbers-20261030.md:54` 为 **四幕**。幕数不一致。
47. `docs/scoring-alignment-20261028.md:26`：写“**五幕仪式**”且称每幕均调生产 MCP；基准 `evidence/key-numbers-20261030.md:54` 为 **四幕仪式 + 独立对抗三连**。幕数口径不一致。
48. `docs/architecture.svg:85`：图内写“verify-ritual **五幕仪式**（90 秒）”；基准 `evidence/key-numbers-20261030.md:54` 为 **四幕仪式 90 秒**。图内数字过期。

## 五、赛事状态（1 条）

49. `README.md:197`：仍写“华为云算力**未申请（无账号）**”；基准 `evidence/key-numbers-20261030.md:58` 为 **华为 7,998 核时已用**。状态过期。

## JSON 与未列项说明

- `evidence/eval-stratified-20261027.json:14` / `:15` 的 **hit@10=1.00 / MRR=0.8494**、`:20` 的 **M=24/25**、`:327` 的 **X refusal_rate=0.92（23/25）**、`:483` 的 **C=25/25**，与基准卡冻结评测段一致。
- `evidence/eval-stratified-20261027-hybrid.json:11` 的 **M=25/25**、`:324` 的 **X refusal_rate=0.96（24/25）**、`:393`-`:395` 的咖啡豆 **refused=false / max_score=0.9321**，与基准卡生产接线段一致。JSON 未含满库 **MRR=0.8711 / X=19/25**，不能拿两份 20261027 快照否证 20261030 满库段。
- `BENCHMARK.md:24`、`:65` 的 **15,494 块**均明确限定为 2026-10-21 历史实验子集，不作为当前满库数字判错。其他明确的历史快照、不同 RFC 索引或不同评测脚本数字同理。

**不一致/过期条数：49。**
