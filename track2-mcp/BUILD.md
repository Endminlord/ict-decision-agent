# 重建说明（track2-mcp）

## 0. 已随包提供的产物（2026-10-17 实测）

`dist/ict_track2_mcp-0.1.0-py3-none-any.whl`（11.7 MB）**已放进材料包**，数据（15,000 块）已打进 wheel，**自包含、零第三方依赖**。

一条命令验证（等价于 AgentArts 的 UVX 接入）：

```bash
uvx --from track2-mcp/dist/ict_track2_mcp-0.1.0-py3-none-any.whl ict-track2-mcp
# → [ict-track2-mcp] 已加载 15000 块 / 12250 篇 · 来源 40 个 · 年份 2006-2024
# 然后按 stdio 送 JSON-RPC：initialize → tools/list → tools/call
```

**实测记录（2026-10-17）**：`initialize` 返回 `protocolVersion 2024-11-05` + `serverInfo ict-track2-mcp/0.1.0`；`tools/list` 返回 3 个工具；`tools/call verify_quote {"quote":"市场准入负面清单","max_hits":2}` → `verified: true / verbatim: true / match_kinds: ["exact"] / hit_count: 192 / returned_hits: 2`（前两条命中《国务院办公厅关于同意在上海等9个城市开展国内贸易流通体制改革发展综合试点的复函》与《国务院关于实行市场准入负面清单制度的意见》）。

五层自测（**121 条判据**，重建后实跑）：数据生成 **6/6** · 查询层 **52/52** · MCP 协议往返 **28/28** · 领域无关 **10/10** · HTTP 传输 **25/25**。

> 判据数在 2026-10-17 第二轮对抗修复后由 74 增至 80（查询层 +4：L33–L36；协议层 +2：L37–L38）。新增六条锁的是第二轮绕过里已关闭的三个根因：否定词作用域对齐（R2）、`无需/无须/毋须` 词表（R3）、边界窗口与两个结构性风险字段（R1）。
>
> **第三轮（同日，限定语覆盖比对 R4）再由 80 增至 89**（查询层 +9：L39–L47）。攻击脚本 `demo/adversarial-round2.py` 的 28 条从 **绕过 9 → 0**。根因：第二轮留下的 9 条全部是「范围/条件/例外/强度」限定被丢弃，而引用是原文的**逐字子串**——扫 span 内部一条都抓不到，限定语贴在子串**边界外侧**。新判据锚在边界：紧贴边界且同句的限定语（例外前置 `除…外，`、例外后置 `…除外`、条件前置 `对…，`、限定强度 `原则上/一般/基本/通常`、主体范围 `各级/县级以上/…`）若引用未覆盖，报 `dropped_qualifier` 并回传被丢掉的原文，`verified=false`。反向（引用加强、加了原文没有的限定语）**不报**：逐字子串不可能凭空多字，能「加」的只有省略号拼接，已由 `omitted_negation`/`long_omission` 兜住。

> **同日追加（R2 假阴性 + 比例闸门）再由 89 增至 92，随后英文/繁体词表再加 2 条至 94**（查询层 +3：L48–L50）。L48/L49 锁 R2 修复的假阴性方向 —— 边界窗口里的「不」必须属于**紧邻的**那个词（`不利于`/`不符合`/`驰而不息`/`不遗余力`/`坚定不移`/`但`/新谓语），不能把「不利于优化营商环境」读成「不优化营商环境」；同时反方向加 L49 保证真否定算子（`不得`/`严禁`/`未经`）仍然认得，不用假阴性换假阳性。L50 把 `context_negation` 从**存在性**改成**比例**：实测「优化营商环境条例」379 块里 18 块（4.75%）、「一网通办」387 块里 42 块（10.85%）、「市场准入负面清单」192 块里 32 块（16.67%）邻句带否定，全是少数派；而条款片段的命中块几乎全带否定（≈1.0）。阈值 0.5 把两者分开 —— 修前这三条合法引用全被误伤。

---

## 以下为从源码重建（可选）

本目录是**赛题 2 的引用回查 MCP 服务**源码。**wheel 已放进本包**（见 `dist/`，`MANIFEST.sha256` 收录）；**数据文件（`src/ict_track2_mcp/data/chunks.jsonl.gz`，11.7 MB）在包内但不入 `MANIFEST.sha256`** —— 理由是体积大、且可由下面第一步 `scripts/build_data.py` 从语料子集逐字节重建；**wheel 里也已打进一份**，所以只装 wheel（不建数据）也能跑。

## 三步重建

```bash
# 1. 生成自包含数据（从语料子集抽取 15,000 块的片段与定位信息）
python3 scripts/build_data.py
#    默认源：/home/endmin/dsh/research/ict-corpus-subset/subset-env.jsonl
#    输出：src/ict_track2_mcp/data/chunks.jsonl.gz（约 11 MB）
#    也可 --limit 2000 快速验证

# 2. 建环境并装包
uv venv .venv --python 3.10
uv pip install --python .venv/bin/python -e .

# 3. 打包
uv build
#    → dist/ict_track2_mcp-0.1.0-py3-none-any.whl
```

## 自测（五层，共 121 条判据）

```bash
python3 scripts/build_data.py --self-test          # 数据生成 6/6
python3 tests/test_lookup.py                       # 查询层 52/52
python3 tests/test_mcp_protocol.py                 # MCP 协议往返 28/28
python3 tests/test_domains.py                      # 领域无关 10/10
```

## 构建产物（已实测，供核对）

| 项 | 值 |
|---|---|
| wheel | `ict_track2_mcp-0.1.0-py3-none-any.whl` |
| 大小 | **11,765,431 B**（11.7 MB，含数据） |
| sha256 | `3213f0ecefa694cb6d6e22c0965593d7227af29a5016e28f1e6bd0174196caf3` |
| sdist | `ict_track2_mcp-0.1.0.tar.gz`（`uv build` 一并产出；**本包未收录**） |
| 冷启动到首次响应 | **0.34 秒**（含 15,000 块语料加载） |
| 第三方依赖 | **0 个**（MCP stdio 协议手写实现） |

## 为什么是零依赖

`mcp` SDK 在 1.x→2.x 之间把 `FastMCP` 改名成 `MCPServer`——实测装到的 2.x 里 `Server` 对象没有 `list_tools` 方法，服务直接 `AttributeError` 起不来。本包手写 JSON-RPC over stdio（`initialize` / `tools/list` / `tools/call` 三个方法），因此：

- AgentArts 沙箱里 `uvx` 装包更快（不用拉 SDK 依赖树）
- 不会因为 SDK 版本不匹配而启动失败
- 协议正确性由 `tests/test_mcp_protocol.py` 的真实 stdio 往返验证

## 零依赖的验证方式

建一个全新 venv，**只装本 wheel**，确认里面没有 `mcp` SDK，再跑完整协议往返：

```bash
uv venv /tmp/wheel-test --python 3.10
uv pip install --python /tmp/wheel-test/bin/python dist/ict_track2_mcp-0.1.0-py3-none-any.whl
/tmp/wheel-test/bin/python -c "import importlib.util as u; print('mcp SDK:', u.find_spec('mcp') is not None)"   # → False
printf '%s\n%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"verify_quote","arguments":{"quote":"优化营商环境"}}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"verify_quote","arguments":{"quote":"优化营商环境条例","max_hits":2}}}' \
  | /tmp/wheel-test/bin/ict-track2-mcp 2>/dev/null
# 期望：initialize 返回 serverInfo（ict-track2-mcp/0.1.0）；两条 verify_quote 都是
#       verified=true / verbatim=true / partial=false / risk_flags=[] ——
#         「优化营商环境」hit_count=5598, returned_hits=10
#         「优化营商环境条例」hit_count=379, returned_hits=2
#       —— 全量扫描下这两条引用逐字命中，边界窗口里的「不利于优化营商环境」
#          「驰而不息抓好优化营商环境」「不符合…要求」「不遗余力」「坚定不移」
#          都是**别的词里的「不」**，不修饰被引用片段，不构成风险（2026-10-17 修）。
```

> 反方向也要能看见：把同一句**少引**前置例外 ——
> `{"quote":"不得向小微企业收取贷款承诺费、资金管理费"}` ⇒ `verified=false` /
> `risk_flags=["dropped_qualifier"]`（回传被丢掉的 `除银团贷款外`）。
> 引全（带上「除银团贷款外，」）则 `verified=true`。
```

## 未验证的部分

**AgentArts 沙箱能否真的执行 UVX 装包**——官方文档写明支持（`agentarts_05_0021.html`：「UVX：当 MCP 基于 Python 开发时选择 UVX 方式」），但沙箱的网络策略、Python 版本、包体积上限均未知。等公测权限到手后实测。
