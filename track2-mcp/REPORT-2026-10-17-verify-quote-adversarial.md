# verify_quote 对抗修复报告（2026-10-17）

目标：`verify_quote` 是反幻觉闸门，判据是 `verified: bool`。对抗测试找到 3 条独立路径、6 个实例
「不该通过但判通过」。本报告记录改了什么、每项验收的实测输出、`min_len` 的判断依据、以及没覆盖到的残余风险。

---

## 一、根因（修改前实测确认）

| 位置 | 事实 |
|---|---|
| `src/ict_track2_mcp/lookup.py:220-226`（改前） | `gap_max` / `span_ratio` **只计算、只 append 进 hit dict，从不参与是否命中的判断** —— 死数据 |
| `src/ict_track2_mcp/lookup.py:205-206`（改前） | 省略号只按 `…+\|\.{3,}\|⋯+` 切分、只看每段 `len >= min_len`，**不看被省略的是什么** |
| `src/ict_track2_mcp/lookup.py:212`（改前） | 分段用 `cn.find(s)` 无游标，`any(pos[i] >= pos[i+1])` 只判**起点**顺序，没判区间重叠 |
| `src/ict_track2_mcp/server.py:130-141`（改前） | `verified = bool(hits)`，`segments` 命中一律进 True |

三条路径之所以独立：路径 1 靠「跨度」，路径 2 是 `exact`（**没有 gap 字段**），路径 3 是 `gap_max=0`
（**拼得太近**）。所以任何单一 gap/span 阈值都不可能同时覆盖三者。

---

## 二、改了哪些文件、哪些行

### `src/ict_track2_mcp/lookup.py`

| 行 | 改动 |
|---|---|
| `29-34` | 新增阈值常量：`CONTEXT_CHARS=3`、`LONG_OMISSION_CHARS=40`、`OMITTED_KEEP=40`、`GENERIC_MIN_BLOCKS=20`、`GENERIC_RATIO=0.02`、`FRAGMENT_CHARS=12` |
| `38-56` | 新增 `_NEG_WORDS`（不得/不应/不准/不许/不能/不可/不宜/不再/不予/不搞/不免/不失/严禁/禁止/杜绝/切勿/毋/勿/未经/未按/未依/未予/未依法/未及时/否则/但是/然而/而非/反而/反之/非法/非经/非正常）、`_NEG_SINGLE=("不","未","但","勿")`、`_NEG_SKIP`（不断/不同/不仅/不但/不少/不久/不论/不管/不外/不妨/不由/不禁/不愧/不时/不啻、未来/未必/未曾/未尝、不但/但凡）、`_NEG_RE`（按长度倒序，保证「不得」先于「不」被匹配） |
| `60-85` | 新增 `_negation_marks(text) -> list[str]`：正则取标记，单字标记用 `_NEG_SKIP` 双向排除假否定，去重保序 |
| `91` / `94-112` | 新增 `_SENT_SPLIT = re.compile(r"[。；！？;!?\n]")` 与 `_sentence_of(cn, lo, hi, limit=80)`：取命中所在整句（超长以命中为中心截 80 字） |
| `115-157` | 新增 `_risk_fields(cn, spans, qn) -> dict`：`omitted`(≤40 字)/`omitted_len`/`long_omission`(任一 gap >40)/`omitted_negation`/`negation_words`/`negation_in_quote`/`context_before`/`context_after`/`context_sentence`/`sentence_negation`。原文侧取 `before + "\|" + gaps + "\|" + after`（用「\|」隔开防止跨边界拼出不存在的词） |
| `299-384` | 重写 `verify()`：三级命中都补 `_risk_fields`；exact/normalized 不再提前 `return`（specificity 需要真实命中块数）；分段改用游标 `j = cn.find(s, start)`、`start = j + len(s)`（**第 366 行**），删掉原 `any(pos[i] >= pos[i+1])` 死检查（游标已保证区间不重叠） |
| `387-407` | 新增 `_finish(hits, matched, total, qlen)`：给每条 hit 补 `block_hits`/`specificity`(matched/total)/`generic`(≥20 块且 ≥2%)/`fragment`(qlen < 12) |

### `src/ict_track2_mcp/server.py`

| 行 | 改动 |
|---|---|
| `66-76` | `verify_quote` 的 `description` 增补 `partial` / `fragment` / `context_sentence` / `sentence_negation` / `omitted` 的含义与用法 |
| `142-149` | `risk_flags` 收集 `omitted_negation` / `long_omission`；`partial = bool(risk_flags)`；`verified = bool(hits) and not partial` |
| `150-173` | `message` 分五种：无命中 / 否定反转（带 `negation_words[:5]`）/ 跨度过大（带 `gap_max`）/ 逐字 / 省略非逐字；`generic` 时追加「命中 N 块（占语料 x%），区分度低」 |
| `174-186` | `fragment` 分支：短片段 + 原句含否定 ⇒ message 追加「仅 N 字，是原句的一截；所在原句含否定/转折（原句摘录），单独引用可能把禁止性表述读成事实陈述」；否则追加「只能证明这些字出现过」 |
| `188-200` | 返回体新增 `partial` / `risk_flags` / `specificity` / `generic` / `fragment` |

### `README.md`

- 第 23 行工具表：`verify_quote` 输出列改为 `verified` / `partial` / `verbatim` / `risk_flags` / `fragment` / `specificity` + 命中位置（含 `omitted`）。
- 「三类判定」表扩成四类，新增 `partial` 行与 `fragment` 行，并说明 `partial` 与 `verified` 互斥、为什么要分两个字段。
- 新增「### 对抗修复（2026-10-17）」一节：三条路径表、修法、阈值位置、`min_len` 判断、残余风险清单。

---

## 三、五项验收的实测命令与输出

环境：`/home/endmin/dsh/research/ict-material-20261010/track2-mcp/.venv`（`uv venv --python 3.10` + `uv pip install -e .`）。
对抗语料：`/tmp/t2-adv/chunks.jsonl.gz`（2000 块，原始 15,000 块语料的抽样）。
验收脚本：`/tmp/t2-adv/accept.py`、`/tmp/t2-adv/probe.py`（均只读调用 `server._call_tool`，不改仓库）。

### 验收 1 — 路径 3 最小复现必须被拦（`gap_max=0` 那条）

```bash
cd /tmp/t2-adv && /home/endmin/dsh/research/ict-material-20261010/track2-mcp/.venv/bin/python accept.py
```

```
--- 验收1 路径3 省略号吃掉否定词（期望 verified=False 或 partial=True）
  quote      : 借乡村振兴之名……违法违规变相举债
  verified   : False   partial=True   verbatim=False   kinds=['segments']
  risk_flags : ['omitted_negation']   specificity=0.0005   generic=False
  message    : 引用内容在语料中存在，但省略或边界处含否定/转折词（原文侧：不得），引用把否定框架去掉了，语义可能反转——不能当原文引用。
  hit b7ea46e8cbad6f7a-0007 match=segments segs=2 gap_max=0 span=0.019
      omitted           = ''
      omitted_len       = 0
      long_omission     = False
      omitted_negation  = True
      negation_words    = ['不得']
      context_before    = ',不得'
      context_after     = '.支持'
      context_sentence  = '券,支持符合条件,有一定收益的乡村公益性项目建设.规范地方政府举债融资行为,不得借乡村振兴之名违法违规变相举债.支持符合条件的农业企业上市融资.(人民银行,财政'
      sentence_negation = True
```

**判定 PASS**。关键点：这条 `gap_max=0`、`omitted=''` —— 它根本不是「省略太多」，而是「引用前面紧贴着
一个『不得』」。抓它的是 `context_before`（命中区间前 3 字）而不是省略区。这验证了「exact 命中也要回看
边界」这个设计：只做 gap 判定的话，这条永远漏。

### 验收 2 — 路径 2，`exact` 命中但否定词在边界外

```bash
# 同一条命令，第二段输出
```

```
--- 验收2 路径2 exact 边界外否定（期望标出风险）
  quote      : 滥用行政权力制定含有排除、限定竞争内容的规定
  verified   : False   partial=True   verbatim=True   kinds=['exact']
  risk_flags : ['omitted_negation']   specificity=0.0005   generic=False
  message    : 引用内容在语料中存在，但省略或边界处含否定/转折词（原文侧：不得），引用把否定框架去掉了，语义可能反转——不能当原文引用。
  hit 7acfbda868979510-0004 match=exact segs=None gap_max=None span=None
      omitted           = ''
      omitted_len       = 0
      long_omission     = False
      omitted_negation  = True
      negation_words    = ['不得']
      context_before    = '，不得'
      context_sentence  = '着力破除各类市场壁垒，不得滥用行政权力制定含有排除、限定竞争内容的规定，不得限定或者变相限定单位或者个人经营、购买、使用行政机关指定的经营者提供的商品，取消针对'
```

**判定 PASS**。`verbatim=True`（确实逐字）但 `verified=False` + `partial=True` —— 正是要的语义：
「字在，但不能当原文引用」。

**设计依据（为什么是前后各 3 字）**：中文里否定词作用于其后动词短语，最紧的绑定距离就是
「不得/严禁/禁止 + 谓词」，即 2 字否定 + 0 间隔。取 3 字是为了同时覆盖「**不得**」这类双字否定和
「均**不得**」这类带副词的写法，又不足以跨过一个分句（实测误伤率见下）。窗口不能更宽：
放宽到整句后，「优化营商环境」这种高频短语会因为所在句子别处有「不得」而被大面积误判
（该引用在 2000 块里命中 834 块，任何一处「不得」都会污染它）。

### 验收 3 — 路径 1，`gap_max=1051` 的大间隙拼接

```bash
# 同一条命令，第三段输出（quote = c1.text[0:18] + "……" + c1.text[-58:-40]）
```

```
--- 验收3 路径1 gap_max=1051（期望 long_omission 或 partial）
  quote      : 015〕19号 45 廊坊市人民政府……016-2020年）的通知 〔201
  verified   : False   partial=True   verbatim=False   kinds=['segments']
  risk_flags : ['long_omission']   specificity=0.0005   generic=False
  message    : 引用内容在语料中存在，但被省略的原文跨度达 1051 字（阈值 40），两段原文相距过远，属拼接而非省略——不能当原文引用。
  hit f20fbc992be807c4-0005 match=segments segs=2 gap_max=1051 span=0.967
      omitted           = '关于推进全市开发区深化改革创新发展的实施意见廊政〔2015〕83号46廊坊市人民'
      omitted_len       = 1051
      long_omission     = True
```

**判定 PASS**。`omitted` 直接把被吞掉的 1051 字开头回传，人一眼能看出是两份公文的标题被拼在一起。

### 验收 4 — 原有 48 条判据

```bash
cd /home/endmin/dsh/research/ict-material-20261010/track2-mcp
export ICT_TRACK2_DATA=/home/endmin/dsh/research/ict-track2-mcp/src/ict_track2_mcp/data/chunks.jsonl.gz
.venv/bin/python tests/test_lookup.py && .venv/bin/python tests/test_mcp_protocol.py \
  && .venv/bin/python tests/test_domains.py && .venv/bin/python scripts/build_data.py --self-test
```

```
[test_lookup] 21/21
[test_mcp_protocol] 11/11
[test_domains] 10/10（1 个领域）
[build_data] 自测 6/6
```

**判定 PASS（48/48）**。其中 `M8 verify_quote 命中真实引用` 用 `优化营商环境`（6 字、834 块）仍
`verified=True` —— 说明「不改 min_len、不把高频短引用判死」的选择与既有判据一致；`L17`（省略号识别为
segments）与 `L20`（`gap_max`/`span_ratio` 仍存在）也都保持原语义。

### 验收 5 — 合法省略号不得误伤

```bash
# 同一条命令，第四段输出；素材由脚本在语料中挑：整句 44-60 字、无否定标记、省略区 <40 字
```

```
验收5 素材: be1e65625142f499-0003  原句 48 字: 内地向香港开放了27个服务贸易领域，特别是在金融、会计、法律等领域，提供了更加便利的市场准入条件
        省略区 24 字: 务贸易领域，特别是在金融、会计、法律等领域，提供  含否定=False
--- 验收5 合法省略号（期望 verified=True 且 partial=False）
  quote      : 内地向香港开放了27个服……了更加便利的市场准入条件
  verified   : True   partial=False   verbatim=False   kinds=['segments']
  risk_flags : []   specificity=0.0005   generic=False
  message    : 引用内容可在语料中核实，但为省略写法（含「……」），非逐字——如需精确回查请让模型给出原文。
  hit be1e65625142f499-0003 match=segments segs=2 gap_max=24 span=0.06
      omitted           = '务贸易领域,特别是在金融,会计,法律等领域,提供'
      omitted_len       = 24
      long_omission     = False
      omitted_negation  = False
```

**判定 PASS**。省略 24 字、含正常内容、无否定 ⇒ `verified=True` 且 `partial=False`，没有被误伤。

### 附：11 例对抗总表（修复后）

```bash
cd /tmp/t2-adv && ICT_TRACK2_DATA=/tmp/t2-adv/chunks.jsonl.gz \
  /home/endmin/dsh/research/ict-material-20261010/track2-mcp/.venv/bin/python probe.py
```

| 用例 | 修复前 | 修复后 | 拦法 |
|---|---|---|---|
| C1 超长间隙正序（gap 1051） | True | **False** | `long_omission` |
| C2 否定词剥离（exact） | True | **False** | `omitted_negation`（`context_before='，不得'`） |
| C3 省略号删否定框架（gap 0） | True | **False** | `omitted_negation`（`context_before=',不得'`） |
| C4 三段省略号跨段落（span 0.951） | True | **False** | `long_omission` |
| C5 高频短语拼接（gap 142） | True | **False** | `long_omission` |
| C6a 6 字「破坏营商环境」 | True | True（`fragment=True` + message 警告） | **未硬拦**，见残余风险 |
| C6b 5 字对照 | False | False | `min_len` |
| C7 全角半角混排 | True | True（合法） | — |
| C8 繁简混排 | False | False | — |
| C9 零宽字符 | False | False | — |
| C10 跨文档拼接 | False | False | — |
| C11 同段重复 5 次（gap 104） | True | **False** | `long_omission` |

C6a 修复后的实际返回：

```
破坏营商环境: verified=True partial=False fragment=True generic=False spec=0.0015 hits=3
   引用逐字可在语料中核实。 警告：该引用仅 6 字，是原句的一截；所在原句含否定/转折（「恪守职业道德，依法施检、公正施检，不得出现无故推诿、拖延、刁难以及违反廉洁纪律、…」），单独引用可能把禁止性表述读成事实陈述，请回到原句核对。
```

---

## 四、`min_len` 的判断：保持 6，不改 8

**理由**：长度不是区分「编造」与「真实」的正确维度，它是**区分度**的维度，而区分度已经可以精确测量。

1. **提到 8 会误杀合法短专名**：`放管服改革`(5)、`一网通办`(4)、`优化营商环境`(6) 都是政策文本里
   正常可引的短名；`min_len=8` 会把它们全部判成「查不到」——这比「太泛」更糟，因为它把
   **真的**说成**假的**。而 `verified=false` 的语义是「不能当依据」，两者不该混。
2. **6 字泛滥是「区分度」问题，可以用数字说清**：`优化营商环境` 在 2000 块样本里命中 834 块
   （41.7%）。这个事实比一个二值闸门更有用——调用方可以自己决定「41.7% 的命中率算不算依据」，
   而阈值化的 `min_len` 只是把决定权藏进一个魔数。
3. **所以按你的第二个选项做**：保留 `min_len=6`，在每条 hit 里加
   `block_hits`（命中块数）/ `specificity`（命中块数 ÷ 语料总块数）/ `generic`（≥20 块且 ≥2%），
   顶层返回 `specificity`（取最大）与 `generic`，并在 `message` 里追加一句
   「该引用命中 N 块（占语料 x%），区分度低，不宜单独作为依据」。

**注意一个反直觉的实测结果**：`破坏营商环境` 只有 3/2000 = 0.15%，**一点都不泛**——所以
`specificity`/`generic` 完全抓不到它。它是「短 + 落在否定性列举里」，不是「太泛」。
这条正是下面残余风险的第一条。

---

## 五、残余风险（没覆盖到的，诚实清单）

1. **6 字片段落在否定性列举里 —— 没硬拦。** `破坏营商环境` 的原文是
   「…不得出现无故推诿、拖延、刁难以及违反廉洁纪律、破坏营商环境的行为」，否定框架在
   引用前 20 字外，3 字窗口看不到；`specificity` 只有 0.15%，`generic` 也不触发。
   现在的处置是**回传事实**：`sentence_negation=true` + `context_sentence`（整句）+
   `message` 里的警告。为什么不硬拦：把窗口放宽到整句后，`优化营商环境` 这类高频短语会因为
   句子别处有「不得」而被大面积误判，而它命中 834 块 —— 一刀切会把闸门变成噪声源。
   要做对，需要句法分析（判断否定词的作用域是否覆盖引用），超出零依赖的实现范围。
2. **否定词表是封闭集合。** 现在收的是「不得/严禁/禁止/勿/未经/否则/但是/然而…」及其单字形式。
   「难以」「不足以」「尚未」「免于」「杜绝」之外的变体、「未…」的少数搭配、以及
   **非否定式的语义反转**（反讽、条件句、双重否定）都抓不到。这是白名单式的必然缺口。
3. **`long_omission` 的 40 字阈值是拍的。** 实测只有两档样本（合法省略 24 字 / 拼接 104-1051 字），
   40 落在空档里。但**合法的长省略会被误判**：引用一整节的开头和结尾、中间跳读 50 字以上，
   会得到 `partial=true`。这是「宁可误报不可漏报」的取舍，代价是真的。
4. **`span_ratio` 仍然只是事实，没有参与判定。** 它和 `gap_max` 现在是「冗余证据」：真正拦人的是
   `long_omission`。之所以留着，是因为块长度差异很大（200 字 vs 1200 字），同一个 40 字间隙在
   短文里是灾难、在长文里可能正常 —— 但这一步判断仍然留给了调用方。
5. **`fragment` 的 12 字阈值也是拍的。** 它只作提示、不改 `verified`，所以误判的代价低；
   但它对「多短的引用算片段」给了一个武断的答案。
6. **零宽字符（C9）是假阴性**：`normalize()` 不去零宽字符，带 `\u200b` 的真引用会被判 `false`。
   这是**保守方向**的错误（把真的说成查不到），不属于本次「不该通过却通过」的范畴，故当时未修。
   > **后续（2026-10-17 14:45 已修）**：`lookup.py:154` 的 `_WS` 从 `\s+` 扩到
   > `[\s\u200b-\u200f\u2060\ufeff]+`，并新增回归用例 `L22`（`tests/test_lookup.py`）。
   > 实测 `降低准入门槛，优化营商\u200b环境` → `verified=True`、`match_kinds=['normalized']`。
7. **`context_before` / `context_after` 取的是规范化后的文本**（全角标点已转半角），
   所以回传的上下文里标点形状与原文不同。定位用的 `char_offset` 仍是原文偏移。

---

## 六、约束核对

- `pyproject.toml` 第 17 行 `dependencies = []` **未改**；`lookup.py` / `server.py` 只 import
  `json`/`os`/`re`/`sys`/`gzip`/`dataclasses`/`typing` + `from __future__`，**零第三方**。
- `lookup_chunk` / `list_sources` 两个工具**未改动**（`server.py:122-131` 与 `203-207` 原样）。
- `dist/` 下的 wheel **当时未动**（本报告写于 14:25，未重新打包）。
  > **后续（2026-10-17 14:45 已重建）**：因 `lookup.py` 的零宽字符修复需要进包，wheel 已重建 ——
  > `11,750,177 B` / sha256 `b574f5847805a585ad07afe39a35e6f4fd9024c7f89d9ba97e092f82d9a9a184`；
  > `uvx --from dist/*.whl ict-track2-mcp` 实测 `initialize` + `tools/list` + `verify_quote` 全通；
  > `BUILD.md:56-57` 已同步字节数与 sha256。
