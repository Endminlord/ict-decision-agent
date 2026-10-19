---
name: citation-verify
allowed-tools:
  - verify_quote
  - lookup_chunk
  - list_sources
description: |
  引用回查工作流：拿到一段「据说出自某文件」的引文后，先逐字核对它是否真的在语料里，
  再检查引用者是否悄悄丢掉了原文的否定词、限定语、例外条款 —— 这三类丢失会让引用语义反转。
  适用于报告成稿前的引用核验、竞品材料的事实核查、以及任何「这句话真有出处吗」的场合。
tags:
  - verification
  - citation
  - anti-hallucination
  - mcp
---

# 引用回查工作流

## 目标

回答一个问题：**这段引文是否忠于原文** —— 不是「有没有提到」，是「有没有被改写」。

## 依赖

本技能调用 `track2-mcp` 服务（零第三方依赖，随材料包提供）：

```bash
uvx --from track2-mcp/dist/ict_track2_mcp-0.1.0-py3-none-any.whl ict-track2-mcp
```

在 MCP 客户端里注册为 stdio server 后，三个工具即可用。

## 可用工具

| 工具 | 用途 |
|---|---|
| `verify_quote(quote, max_hits=10, min_len=4)` | 核对引文：是否逐字命中、有无省略、有无丢掉否定词或限定语 |
| `lookup_chunk(chunk_id)` | 按 `chunk_id` 取回原文块（回查失败则不得引用） |
| `list_sources(topic, top=10)` | 按主题看语料覆盖：命中多少块、来自哪些来源与年份 |

## 执行步骤（必须按序）

1. **先核对是否逐字命中**：`verify_quote(quote)`。
   - `verified == true` → 进入第 2 步
   - `verified == false` 且 `partial == true` → **停下来**，看 `risk_flags`
   - `verified == false` 且 `hit_count == 0` → 语料里没有这句话，**直接判为查不到**

2. **逐条读 `risk_flags`**，四种情况处理方式不同：

   | flag | 含义 | 处理 |
   |---|---|---|
   | `omitted_negation` | 引用丢掉了原文的否定词（「**不得**滥用…」引成「滥用…」） | **判为语义反转**，必须引全 |
   | `dropped_qualifier` | 引用丢掉了限定语（「**除银团贷款外**不得…」引成「不得…」） | **判为范围放大**，必须补上 |
   | `long_omission` | 省略号跨越了过长区间（拼接两段不相邻原文） | **判为拼接**，需分段引用 |
   | `sentence_negation` | 命中的块所在句含否定词（比例 ≥50% 才报） | 看 `sentence_example`，人工判断 |

3. **回查原文**：对每条准备引用的命中调 `lookup_chunk(chunk_id)`，`found == false` 的不得引用。

4. **报告**：按下方模板输出。

## 报告模板

```
引用：<原引文>
结论：<忠于原文 / 语义反转 / 范围放大 / 拼接 / 查不到>
命中：<hit_count> 块，逐字命中 <verbatim>
风险：<risk_flags 与回传的原文片段>
原文（回查）：<lookup_chunk 取回的那一段>
建议：<可以直接用 / 改成什么 / 不要用>
```

## 注意

- **`verified=false` 不等于「语料里没有」** —— 它可能是「有，但你引错了」。
- **短引文天然容易误报**：`len(quote) < 12` 时限定语判据不生效（会误伤合法短引），
  此时应看 `fragment` 字段，并补一句上下文再核。
- **否定词表是中文政务词表 + 英文常用否定词**；换行业（如医疗、金融）需重新抽词表。
- 本技能只回答「这句话在不在语料里、有没有被改写」，**不回答「这句话能不能推出那个结论」** ——
  那是逻辑推理，超出引用核验的范围。
