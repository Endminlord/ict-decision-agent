# 赛题 2（AgentArts）智能体设计方案

> 🗄 **归档说明（2026-10-22）**：本届参赛**只报赛题 3**，赛题 2（AgentArts）不在参赛范围内。
> 本文档保留作历史记录，**不再维护**、不计入任何评分项。文中「待平台权限」等条目已不再是待办。
> 其中仍然有效的部分是 `track2-mcp/` 这个**通用件**（引擎与领域无关，任何 MCP 宿主可挂），见 `README.md` §7。
> 面向第十一届华为 ICT 大赛中国创新赛 · 赛题 2「基于华为云 AgentArts 打造行业智能体」
> 编写：2026-10-16 · 平台：华为云智果（AgentArts）智能体平台（华北-北京四）
> 状态：**设计方案 ✅ · 引用回查 MCP ✅（wheel 已随包，实测可跑）· 平台部署待 AgentArts 权限（公测审核中）**

---

## 1. 赛题 2 要求 vs 本方案

| 赛题 2 要求（规则 §三 / §五） | 本方案的对应 | 状态 |
|---|---|---|
| 必用技术：华为云智果（AgentArts） | Agent 建在 AgentArts 上（单智能体 + 工作流） | 待平台权限 |
| 初赛：作品信息收集表（团队介绍 + **Agent 设计方案** + Demo 视频） | 本文档即设计方案；Demo 视频待平台可用后录制 | 设计方案 ✅ |
| 决赛：答辩 PPT + **可在线访问的 Demo 页面** | AgentArts 应用发布后自带在线体验页 | 待平台权限 |
| 决赛：**自定义 Skill 或 MCP 服务说明** | 自建「引用回查 MCP」（UVX 包，自包含）+ AgentArts 工作流 | **✅ 已完成并实测**（`track2-mcp/dist/*.whl`，`uvx` 握手通过，**121 条判据全过**；另有 Streamable HTTP 传输层，**已在华为云 AgentArts MCP 网关实连成功**） |
| 决赛：社区分享帖截图 | 待平台可用后发布 | 待平台权限 |

---

## 2. Agent 设计

### 2.1 做什么

**「政务决策依据查询智能体」**——给定一个政策问题，返回**可回查原文出处的依据链**，并在依据不足时**明确拒答**。

**为什么是这个场景**：与赛题 3 同场景，但这不是偷懒——**同一个场景在换平台后重新实现，本身就是对「方案是否平台无关」的最强验证**。而且这个场景的核心难点（依据必须可回查、不足必须拒答）在 AgentArts 上同样存在，不会因为换平台而消失。

### 2.2 给谁用

需要「写材料时能引到原文」的人：政策研究人员、企业合规岗、地方政府工作人员。

**痛点不是找不到，是找不到之后不敢用**——检索出来的东西无法确认是不是真的来自某份文件，于是不敢写进材料。

### 2.3 核心能力（三条，与赛题 3 一致）

1. **可回查**：每条依据带 `chunk_id` + 文件名 + 原文片段
2. **可拒答**：依据不足时返回 `insufficient_evidence=true`，不编造
3. **可迁移**：换行业只换词表与语料，不改代码

---

## 3. 架构（AgentArts 上怎么搭）

```
用户在 AgentArts 应用页提问
        │
        ▼
┌─────────────────────────────────────┐
│ 单智能体（AgentArts）                │
│  · 模型：平台预置 MaaS 模型          │
│  · 提示词：规定「先检索、再作答、    │
│    每条依据必须带出处、不足则拒答」  │
│  · 记忆：用户偏好（行业/地区）       │
│                                     │
│  技能（AgentArts 的 Skill = 插件 +   │
│        工作流 + 聊天记忆）           │
│   ├─ 知识库：政务语料子集            │
│   ├─ 工作流：检索 → 判定 → 生成      │
│   └─ MCP：引用回查服务（自建）       │
└─────────────────────────────────────┘
        │
        ▼
返回：结论 + 依据链（每条带 chunk_id）+ 拒答标记
```

### 3.1 三个组件各自负责什么

| 组件 | 在 AgentArts 上的形态 | 负责 |
|---|---|---|
| **知识库** | AgentArts 知识库（上传语料） | 语义检索：找到相关段落 |
| **工作流** | AgentArts 工作流（节点编排） | 编排：意图识别 → 检索 → 证据判定 → 生成 |
| **MCP** | 自建 MCP（UVX 包） | 引用回查：给定 `chunk_id` 返回原文与文件名 |

**为什么引用回查要单独做 MCP**：AgentArts 知识库的检索结果**不保证带稳定可回查的 ID**。回查能力必须由我们控制，这是「可回查」承诺的技术基础。

---

## 3.1 已实连：华为云 AgentArts MCP 网关（2026-10-20）

**这不是设计稿，是已经连通的链路**：

```
华为云 AgentArts 网关  →  Cloudflare 隧道  →  WSL 里的 track2-mcp HTTP 服务
gateway-ta3du9nj          trycloudflare        127.0.0.1:8765/mcp
```

| 环节 | 实测 |
|---|---|
| 传输层 | 新增 `src/ict_track2_mcp/http_server.py`（667 行，纯标准库，零第三方依赖）；`server.py` / `lookup.py` 逐字节未动（sha256 对照） |
| 判据 | 新增 `tests/test_http_transport.py` **25 条**；五层合计 **121 条**全过 |
| 协议版本 | `PROTOCOL_VERSION = "2025-03-26"`（华为云网关要求的版本） |
| 网关侧 | 创建网关 `gateway-ta3du9nj`（MCP 2025-03-26、公网访问、会话保持）→ 创建 Target `target-b1rsnahx`（协议 MCP、状态「就绪可用」） |
| 连接测试 | **2954 ms**，日志 `initialize` → `会话初始化成功` → `tools/list` → **成功获取工具列表，共 3 个** |
| 工具调试 | 选中 `target-b1rsnahx_lookup_chunk`、参数 `4041c674716e1dc4-0000` → 返回 MCP JSON 响应（含 `id`） |

**三个工具在网关上的实际名字**：`target-b1rsnahx_lookup_chunk` / `target-b1rsnahx_verify_quote` / `target-b1rsnahx_list_sources`（网关按「Target 名_工具名」重命名）。

**边界（必须写清）**：

1. **隧道地址是临时的** —— `trycloudflare.com` 免费隧道每次重启换域名，换完要在 Target 里改地址。要稳定得上 ECS + 固定域名 + HTTPS 反代。
2. **会话粘性是硬约束** —— `Mcp-Session-Id` 是进程内内存态。多副本部署时若 `initialize` 与 `tools/call` 落到不同副本，全部 404。网关侧要开会话保持。
3. **入站认证走华为云 IAM** —— 网关侧配置为 IAM 认证，本机服务本身无认证（只在 127.0.0.1 语义下安全，经隧道暴露期间靠隧道域名不可枚举）。

## 4. MCP 方案（关键设计决策）

### 4.1 平台约束（官方文档 `agentarts_05_0021.html`）

| 项 | 限制 |
|---|---|
| 单应用 MCP 数量 | **≤ 5 个** |
| 预置 MCP 安装方式 | SSE / streamableHttp（**只支持 HTTP/HTTPS、标准 URL、IP 默认不应为内网**） |
| **创建 MCP 安装方式** | **NPX**（Node.js）· **UVX**（Python）· SSE · streamableHttp |

### 4.2 选型：UVX（不是 SSE）

**SSE/streamableHttp 需要公网可访问的服务器**——我们的服务跑在 WSL 内网，要暴露就得买 ECS 或开隧道，引入额外依赖和故障点。

**UVX 不需要**：AgentArts 在自己的沙箱里执行 `uvx <包名>`，**包发布到 PyPI 即可**。

**代价**：包必须**自包含**——不能依赖本机的 ES（:9220）和 embedding 服务（:8792）。

### 4.3 自包含怎么做到

把「引用回查」需要的**最小数据**打包进 wheel：

| 数据 | 大小估算 | 做法 |
|---|---|---|
| `chunk_id → (doc_id, title, date, source, char_offset)` 索引 | ~2 MB（15,000 块） | 打包进 wheel |
| 原文片段（每块前 500 字） | ~7 MB（15,000 块） | 打包进 wheel |
| 全文 | 4.18 GB | **不打包**（超限）；MCP 返回片段 + 定位信息，全文由知识库提供 |

**设计取舍**：MCP 返回「**片段 + 精确定位**」而不是全文——这恰恰是「可回查」的定义（能定位到原文的哪一块哪一偏移），而不是「把全文吐出来」。

### 4.4 工具清单（3 个）

| 工具 | 输入 | 输出 |
|---|---|---|
| `lookup_chunk` | `chunk_id` | 该块的原文片段 + 文档元信息 + 字符偏移 |
| `verify_quote` | `quote`（引用文本） | 是否能在语料中找到该引用 + 命中位置列表 |
| `list_sources` | `topic`（可选） | 语料覆盖的文档清单（按来源/年份统计） |

`verify_quote` 是**反幻觉的关键**：模型生成的引用必须能被这个工具验证，验证不通过就不能写进答案。

---

### 4.5 UVX 包已实现（2026-10-16）

**位置**：`/home/endmin/dsh/research/ict-track2-mcp/`（源码 + wheel）

| 项 | 实测值 |
|---|---|
| wheel | `ict_track2_mcp-0.1.0-py3-none-any.whl` · **11.7 MB**（含数据） |
| 语料 | 15,000 块 / 12,250 篇 / 1,112 万字 |
| **冷启动到首次响应** | **0.34 秒**（含语料加载） |
| **第三方依赖** | **0 个** |
| 自测 | 数据生成 **6/6** · 查询层 **52/52** · MCP 协议往返 **28/28** · **HTTP 传输 25/25**（五层共 **121 条**）· **领域通用性 110 条**（11 领域 × 10）· 语料生成器 **34/34** |

**两个关键工程决策**：

1. **手写 JSON-RPC 而不用 `mcp` SDK**。原因：SDK 在 1.x→2.x 之间把 `FastMCP` 改名成 `MCPServer`（实测：装到的 2.x 里 `Server` 对象没有 `list_tools` 方法，直接 `AttributeError` 启动失败）。手写让包**零依赖**——沙箱里 `uvx` 装包更快，也不会因为 SDK 版本不匹配而起不来。协议正确性由真实 stdio 往返验证。
2. **验证「零依赖」的方式**：建一个全新 venv，只装本 wheel，确认里面没有 `mcp` SDK（`find_spec('mcp') is False`），再跑完整协议往返——`initialize` 与 `verify_quote` 均正常。

**踩到的两个坑**（都记在这里，避免重犯）：
- `str.maketrans` 的源与目标**必须等长**：19 个全角标点配 19 个半角字符，少一个就 `ValueError`。
- hatchling 的 `packages` 已包含整个包目录（含数据文件），再写 `force-include` 会**重复添加同一路径**导致 wheel 构建失败（`A second file is being added to the wheel archive at the same path`）。

**⇒ 结论**：包侧已就绪，等 AgentArts 权限即可接入。**唯一未验证的是**：AgentArts 沙箱能否真的执行 UVX 装包（官方文档写明支持，但沙箱网络策略、Python 版本、包体积上限均未知）。

### 4.6 引擎与领域无关（2026-10-16）

**要求**：「不要只做一个领域，要全领域通用」。原包只跑过政务语料，耦合点藏在数据路径、工具描述、docstring 举例里。

**改造**：`DATA_FILE` 支持 `ICT_TRACK2_DATA` 环境变量 · `corpus(path=None)` 支持显式路径 · `main(argv)` 加 `--data` / `--version` · `stats()` 加 `data_path` / `year_range` / `top_sources` · docstring / hint / 工具描述全部改为领域中性表述。

**验证**：`tests/test_domains.py` 用**同一套 10 条判据**跑任意语料，判据本身不含领域假设（测试数据从语料自身提取）。

| 领域 | 语料 | 判据 |
|---|---|---|
| 政务 | 15,000 块真实语料（中文） | **10/10** |
| RFC 技术标准 | 1,739 块真实语料（IETF 14 篇，**英文**） | **10/10** |
| 先进制造 | 25 块真实语料 | **10/10** |
| 医疗 · 软件工程 · 金融 · 法律 · 制造标准 · 环保 · 教育 · 能源 | 各 4–6 篇合成语料（`scripts/gen-domains.py`） | **80/80** |

合计 **110/110**。

**抓到的两个真缺陷**：

1. **固定阈值在大语料上失效**（政务，15,000 块）：无关问句「量子纠缠退相干时间与黎曼猜想证明的联合推导xyz」**命中 23 块**——`_best_substring` 的 `min_hits=5` 是固定阈值，大语料里任意 3 字组合都能撞上。修法：`min_hits = max(5, 块数 // 200)`（15,000 → 75）+ 最短子串 3 字 → 4 字。

2. **字符滑窗在英文语料上切出「词的碎片」**（RFC，1,739 块）：无关英文句命中 556 块，而命中的是滑窗从 `entanglement` 切出的 `lement`（`element` 的碎片）。中文没有词边界，字符滑窗成立；英文必须按词滑。修法：`_best_word_span`——ASCII 占比过半的长句改走词序列滑窗，绝不切开一个词。

**只做一个领域不会发现这两个问题**：一个要「多规模」，一个要「多语言」。

**⇒ 对赛题 2 的含义**：AgentArts 上换任何行业语料（医疗、制造、金融……）都只需换数据文件，代码不动；**换语言也不用改**（RFC 英文语料 10/10）。详见 `track2-mcp/README.md` 的「全领域通用」段与 `249-ict-track2-verify-experiment.md` §10。

---

## 5. Skill 方案

**AgentArts 的「Skill」是平台内概念**（技能 = 插件 + 工作流 + 聊天记忆），与 DSH 的 `SKILL.md` 形态不同。赛题 2 决赛要的「自定义 Skill 说明」有两种交法：

| 交法 | 内容 | 评价 |
|---|---|---|
| **A. 工作流即 Skill** | 把「检索 → 证据判定 → 生成」做成 AgentArts 工作流，写说明文档 | 平台原生，评委好理解 |
| **B. 复用我们的 SKILL.md** | 提交赛题 3 的两个 Skill 说明（`governance-policy-trace` / `workbench-status-report`） | 与平台无关，但评委可能觉得「没用到 AgentArts 的 Skill」 |

**选 A 为主、B 为辅**：主交 AgentArts 工作流（证明会用平台），附我们的 SKILL.md 作为「可迁移性证据」（证明同一套能力能跨平台）。

---

## 6. 从赛题 3 资产里的迁移清单

| 资产 | 怎么用 | 改动量 |
|---|---|---|
| 政务语料（15,000 块子集） | 导入 AgentArts 知识库 | 转换格式（`import-corpus.py` 改 endpoint） |
| `retrieval-mcp-server.py`（3 工具） | **改造**为 UVX 包 + 自包含数据 | 重写（stdio → 自包含），约 300 行 |
| `workbench-mcp-server.py`（3 工具） | **不用**（依赖本地工作台，与赛题 2 场景无关） | — |
| 本体三层（188,423 条 = 强证据层 12,594 + 候选层 175,829） | **不在 AgentArts 上重跑**；作为「同一方案的另一实现」在答辩中说明 | 0 |
| 2 个 SKILL.md | 作为可迁移性附件 | 0 |
| 12 页 PPT | 骨架复用，内容重写（平台换成 AgentArts） | 改 60% |
| 2 段录屏 | 重录（内容是 Nexent 的） | 重录 |
| 效果评测脚本（`eval-retrieval.py`） | **复用**，改成对 AgentArts 知识库评测 | 改 endpoint |

**能零改动复用的**：评测方法论、拒答判据（`max_score < 0.75 or grounded_rings == 0`）、诚实边界的写法。

---

## 7. 缺口与计划

| # | 缺口 | 谁能解 | 计划 |
|---|---|---|---|
| 1 | AgentArts 公测权限 | 用户（**已提交申请，审核中，约 5 个工作日**） | 等待；期间做 #2 #3 #4 |
| 2 | UVX 包（引用回查 MCP） | **我** | ✅ **已完成**（见 §4.5） |
| 3 | AgentArts 工作流设计文档 | **我** | 现在做——按官方文档的节点模型写 |
| 4 | Demo 视频脚本 | **我** | 现在做——等平台可用后按脚本录 |
| 5 | 可在线访问的 Demo 页面 | 平台（发布后自带） | 等权限 |
| 6 | 社区分享帖截图 | 平台账号 | 等权限 |
| 7 | 团队 3 学生 + 1 教师 | 用户 | 与赛题 3 同一缺口 |

---

## 8. 诚实边界

1. **本方案尚未在 AgentArts 上跑通**——公测审核中，所有平台侧描述来自官方文档，不是实测。
2. **UVX 包能否在 AgentArts 沙箱里跑通未验证**——官方文档写明支持 UVX，但沙箱的网络策略、Python 版本、包体积限制都未知。
3. **知识库导入规模受限**——基础版 1 GB OBS 存储，15,000 块约 131 MB（ES 侧实测）可放得下，但 AgentArts 知识库的分块策略与我们不同，实际块数会变。
4. **赛题 2 与赛题 3 是两套平台**——本方案不是「把赛题 3 搬过去」，是「同一场景在另一平台重新实现」。答辩时必须说清这一点，不能让人以为是同一份工作交两次。
5. **同一支队伍能否同时报两个赛题未确认**——规则未明写，报名前须向官方确认。

---

## 9. 复跑（核实本文的文档来源）

```bash
# 平台约束（官方文档，curl 被 EdgeOne 拦，用 web_fetch 或浏览器）
#   添加 MCP     https://support.huaweicloud.com/usermanual-agentarts0/agentarts_05_0021.html
#   开通服务     https://support.huaweicloud.com/qs-agentarts0/agentarts_04_0000.html
#   使用流程     https://support.huaweicloud.com/usermanual-agentarts0/agentarts_05_0001.html
#   创建 MCP     https://support.huaweicloud.com/usermanual-agentarts0/agentarts_05_0319.html

# 本机已有资产（赛题 3 侧）
ls /home/endmin/dsh/research/ict-material-20261010/mcp/
ls /home/endmin/dsh/research/ict-material-20261010/skills/
wc -l /home/endmin/dsh/research/ict-material-20261010/tools/eval-retrieval.py
```
