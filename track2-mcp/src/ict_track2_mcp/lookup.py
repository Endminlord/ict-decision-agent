"""语料索引与查询：自包含，无外部依赖。

数据文件 `data/chunks.jsonl.gz` 每行一个 JSON 对象，字段：
  chunk_id / doc_id / char_offset / title / date / source / text
"""

from __future__ import annotations

import gzip
import json
import os
import re
from dataclasses import dataclass, field

DATA_FILE = os.environ.get(
    "ICT_TRACK2_DATA",
    os.path.join(os.path.dirname(__file__), "data", "chunks.jsonl.gz"),
)

# 返回片段的最大长度（字符）。全文由知识库提供，MCP 只给定位 + 片段。
SNIPPET_LIMIT = 600

# list_sources 的 `top` 上限。没有上限时 top=10**6 会把 12,250 篇文档全部
# 塞进一次响应（实测 1.48 MB / 5 ms）—— 单次调用就能吃满上下文（M4）。
TOP_MAX = 200
# topic 长度上限：`sources()` 的降级路径 `_best_substring` 是 O(长度 × 块数)，
# 10 KB 的 topic 实测要 180 秒（2026-10-18 压测发现）。超过这个长度直接截断，
# 语义上也不需要更长的主题词。
TOPIC_MAX = 200
# `_best_substring` 的滑窗总数上限：正常问句（<60 字）窗口数远低于此，
# 只有异常长输入会触发。超过就放弃降级匹配，返回 None（宁可不匹配）。
WINDOW_MAX = 600

# ---- 引用核验的语义风险阈值（2026-10-17 对抗修复）----
# 省略号能吞掉否定框架：「不得借乡村振兴之名违法违规变相举债」写成
# 「借乡村振兴之名……违法违规变相举债」——两段零间隙、逐段都在原文里，
# 但语义完全反转。这类「拼得太近」的伪造，gap/span 两条阈值都拦不住，
# 必须看**被省略的到底是什么**。
# R1/A1（第二轮对抗）：3 字窗口装不下「不得以任何方式」——「不得」被推到第
# 5 字，窗口里只剩「任何方式」，禁止条款整段消失（「为招标人指定招标代理机构」
# 于是读成陈述）。8 字覆盖「以任何方式」「以任何名义」这类插入语的常见跨度；
# 邻句噪声仍由 _clause_tail / _clause_head 挡（L27 不变）。
CONTEXT_CHARS = 8          # 命中区间前后各回看多少字（找边界外的否定词）
# R2：判断「边界窗口里的否定词有没有被引用保留」时，只看引用**同侧边界**
# 的这么多个字。看整条引用会被引用中间任意一个否定字洗白（K1：「不合理」
# 的「不」）。6 字装得下「不得」「严禁」「禁止」「未经」。
FRAME_CHARS = 6
LONG_OMISSION_CHARS = 40   # 单个省略区超过多少字算「跨度失控」（拼接而非省略）
OMITTED_KEEP = 40          # omitted 字段回传的省略区最大长度
GENERIC_MIN_BLOCKS = 20    # 「泛滥引用」的绝对下限（少于 20 块谈不上无区分度）
GENERIC_RATIO = 0.02       # 命中块数占语料 2% 以上 ⇒ 该引用没有区分度
FRAGMENT_CHARS = 12        # 短于此的引用只能证明「字符出现过」，证明不了整句语境
# 可核验的引用下限（规范化后的字符数）。原为 6，导致 4-5 字的真实专名
# （「放管服改革」21 块、「一网通办」387 块）被判「语料中未找到」——
# 把真的说成假的比漏报更糟。降到 4；区分度不足由 generic / fragment 事实兜住。
MIN_QUOTE_CHARS = 4
# 省略号分段里每一段的下限。分段放宽到 4 会让「…」两侧的零碎字词也参与
# 拼装，段数一多就容易凑出假命中，所以单独保留 6。
MIN_SEGMENT_CHARS = 6

# 否定 / 禁止 / 转折标记。只收高精度写法：裸「不」在「不断」「不同」「不仅」
# 里大量出现，单字无脑匹配会把正常引用判成风险（实测「持续优化营商环境……
# 加快建设现代化经济体系」的省略区就含「不断解放和发展社会生产力」）。
# 否定/禁止类：省略它会把「禁止做 X」读成「做 X」。这是最危险的一类 ——
# 政策原文「严禁破坏营商环境」，引用写成「破坏营商环境……」，语义完全反转。
_NEG_ONLY = (
    "不得", "不应", "不准", "不许", "不能", "不可", "不宜", "不再", "不予",
    "不搞", "不免", "不失", "严禁", "禁止", "杜绝", "切勿", "毋", "勿",
    "未经", "未按", "未依", "未予", "未依法", "未及时",
    "非法", "非经", "非正常",
    # R3（第二轮对抗 G1/G2）：义务的「零要求」形态。缺了它们，
    # 「无需履行备案手续」会被引成「履行备案手续」—— 义务方向整个反转，
    # 而工具连 `omitted='无需再'` 都打印出来了却报「无省略否定」。
    "无需", "无须", "毋须",
)
# 转折类：省略它会把「A，但是 B」读成「A」。危险程度低一档，但同样是
# 「省略即改义」。
# 英文否定词（2026-10-17）：此前整层失明 —— `No department may charge fees`
# 引成 `charge fees for such services`，`omitted_negation=False`。词表是中文的，
# 而 RFC 语料与中英混排引用都真实存在。
# 用 `(?<![A-Za-z])…(?![A-Za-z])` 而不是 `\b`：`\b` 在 `can't` / `mustn't` 上
# 会把撇号当边界，反而切成两段。
_NEG_EN = (
    "must not", "may not", "shall not", "should not", "will not", "cannot",
    "can not", "could not", "do not", "does not", "is not", "are not",
    "no", "not", "never", "without", "unless", "except", "excluding",
    "prohibited", "forbidden", "barred", "illegal", "unlawful",
)
_NEG_EN_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(sorted(_NEG_EN, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)
# 繁体否定词 → 简体（2026-10-17）：词表全是简体，繁简混排引用匹配不到。
# 只收否定/转折类，不做全文转换 —— 转换会改偏移量，而这里所有位置都用于
# 边界窗口计算，偏移一错全错。
_T2S_NEG = {
    "嚴禁": "严禁", "無需": "无需", "無須": "无须", "不應": "不应",
    "不準": "不准", "不許": "不许", "未經": "未经", "杜絕": "杜绝",
    "否則": "否则", "而非": "而非",
}
_CONJ_ONLY = ("否则", "但是", "然而", "而非", "反而", "反之")
_NEG_WORDS = _NEG_ONLY + _CONJ_ONLY
_NEG_SINGLE = ("不", "未", "勿", "但")
# 单字标记的「假否定」搭配：出现在这些词里的 不/未/但 不表否定。
#
# R2 重做（2026-10-17）：判据是「否定词必须是**独立语义单元**」。否定一旦被
# 吃进一个词里（词汇化），它就不再修饰后面的短语，不能当边界否定用：
#   「不利于 X」= 对 X 有害，**不是**「不优化 X」；
#   「驰而不息」「不遗余力」「坚定不移」「锲而不舍」= 持续/全力，与否定无关；
#   「不符合」「不满足」「不适应」「不一致」= 含否定的实义谓词（主谓结构里的谓语）。
# 把这些「不」当成否定算子，会把「不利于优化营商环境」读成「不优化营商环境」，
# 合法引用被判伪造（假阴性）。词表按**词根**收（「不利」覆盖「不利于/不利影响」），
# 免得为每个搭配单列一条。
_NEG_SKIP = {
    "不": (
        # 副词 / 连词性：不表否定
        "不断", "不同", "不仅", "不但", "不少", "不久", "不论", "不管",
        "不外", "不妨", "不由", "不禁", "不愧", "不时", "不啻", "不失",
        "不免", "不单", "不光", "不止", "不只", "不成", "不意",
        # 含否定的实义谓词（主谓结构里的谓语，不修饰后面的短语）
        "不利", "不符", "不适", "不满", "不履", "不一致", "不相适",
        "不属", "不亚", "不乏", "不外乎", "不在于", "不觉", "不惜",
        # 成语 / 固定短语：否定义已固化
        "不遗余力", "不折不扣", "不懈", "不倦", "不厌", "不辍", "不怠",
        "坚定不移", "坚持不懈", "坚韧不拔", "锲而不舍", "驰而不息",
        "生生不息", "经久不息", "川流不息", "自强不息", "奋斗不息",
        # 动词性固定搭配（「离不开 X」= 依赖 X，不是否定 X）
        "离不开", "少不了", "怪不得", "恨不得", "巴不得", "了不得",
        "动不动", "说不定", "保不齐", "不由得",
    ),
    "未": ("未来", "未必", "未曾", "未尝", "未始"),
    "但": ("不但", "但凡"),
}


def _split_marks(marks: list[str]) -> tuple[list[str], list[str]]:
    """把混合标记拆成（否定/禁止类, 转折类）。

    L10：两类必须分开判 —— 原文「严禁破坏营商环境，但是要依法监管」，
    引用「破坏营商环境……但是要依法监管」省略了「严禁」，但引用里带了
    「但是」。混在一起判（`bool(src_marks) and not q_marks`）会认为
    「引用里也有标记」，于是漏掉真正被省掉的否定词。
    """
    neg = [m for m in marks
           if m in _NEG_ONLY or m in ("不", "未", "勿") or m.lower() in _NEG_EN]
    conj = [m for m in marks if m in _CONJ_ONLY or m == "但"]
    return neg, conj
# 长的优先：「不得」必须先于「不」被匹配到，否则「不」会先吃掉第一个字
_NEG_RE = re.compile(
    "|".join(sorted(set(_NEG_WORDS) | set(_NEG_SINGLE), key=len, reverse=True))
)


def _mark_spans(text: str) -> list[tuple[str, int, int]]:
    """文本里出现的否定/转折标记及其位置（`(词, 起, 止)`，保序）。

    单字标记要排除「不断」「未来」「不但」这类不表否定的搭配——判定结果是
    `verified=False`，误报会把合法引用判成伪造，代价比漏报更高。

    R2 重做：返回位置而不只是词，是因为**边界窗口**要按「否定词紧不紧贴
    命中区间」再筛一遍（`_boundary_marks`）。同一套词表，两种作用域。
    """
    out: list[tuple[str, int, int]] = []
    if not text:
        return out
    # 英文否定词：独立扫描，词边界用 `(?<![A-Za-z])` 而非 `\b`。
    for m in _NEG_EN_RE.finditer(text):
        out.append((m.group(0).lower(), m.start(), m.end()))
    # 繁体否定词：按 `_T2S_NEG` 逐个 `find`，位置取原文偏移（不做全文转换）。
    for tw, sw in _T2S_NEG.items():
        start = 0
        while True:
            j = text.find(tw, start)
            if j < 0:
                break
            out.append((sw, j, j + len(tw)))
            start = j + 1
    for m in _NEG_RE.finditer(text):
        w = m.group(0)
        skip = _NEG_SKIP.get(w)
        if skip:
            dropped = False
            for s in skip:
                j = s.find(w)              # 假否定词里 w 的位置（可能在开头或结尾）
                if j < 0:
                    continue
                start = m.start() - j
                if start >= 0 and text.startswith(s, start):
                    dropped = True
                    break
            if dropped:
                continue
        out.append((w, m.start(), m.end()))
    out.sort(key=lambda t: t[1])
    return out


def _negation_marks(text: str) -> list[str]:
    """返回文本里出现的否定/转折标记（去重、保序）。"""
    out: list[str] = []
    for w, _s, _e in _mark_spans(text):
        if w not in out:
            out.append(w)
    return out


def _boundary_marks(text: str, side: str) -> list[str]:
    """边界窗口里的否定词：只认**独立语义单元**（R2 重做，2026-10-17）。

    旧实现把窗口里任意一个「不」都当成对命中区间的否定限定，于是
    「不利于优化营商环境」（11 块）「驰而不息优化营商环境」「不符合…规定」
    里的「不」把合法引用判死 —— 全库 `优化营商环境` 7918 次命中里 73 次触发。

    判据：否定的作用域是**紧邻的谓词/规则**，不是跨越主谓结构的成分。

      * 多字算子（不得/严禁/禁止/未经/无需/不予/不能…）自带谓词，命中即算；
      * 裸「不/未/但」在 **before** 侧只在**紧贴命中区间**时才算 —— 中间隔了
        字，说明「不」属于那个字所在的谓词（利于/符合/遗余力/断/懈…），
        修饰的是那个谓词，不是引用片段；
      * 裸「不/未/但」在 **after** 侧一律不认：引用片段是主语，其后的「不V」
        是**新的谓语**（「优化营商环境不止步」否定的是「止步」，不是
        「优化营商环境」；「…未作规定的」的主语是「本条例」）；
      * after 侧的多字算子也必须**紧贴引用结尾**（`start == 0`）——隔了字
        就是另一个谓词域（「优化营商环境要求政务服务**不能**仅专注于…」，
        「不能」的主语是「政务服务」）；
      * 裸「但」是**篇章连接词**，不承载否定义（「…合理增长。但优化营商环境
        方面存在一些问题」），边界两侧都不认。

    保守方向：宁可漏掉几个真否定（假阳性），也不把合法引用判死（假阴性）。
    """
    out: list[str] = []
    n = len(text)
    for w, s, e in _mark_spans(text):
        if len(w) == 1:
            # 单字：只有 before 侧紧贴命中区间的「不/未/勿」才算否定算子
            if side != "before" or e != n or w == "但":
                continue
        elif side == "after" and s != 0:
            # after 侧：只有紧贴引用结尾的多字算子才算
            continue
        if w not in out:
            out.append(w)
    return out


# 句界：中文句末标点 + 换行 + 半角句点。分号也算——政策文本常把并列项写成
# 一句，「不得出现 A、B、C 的行为；……」这种列举在分号处切开更贴近人的读法。
#
# 半角句点必须收进来：`normalize()` 把「。」转成「.」，normalized / segments
# 两条路径拿到的都是规范化文本，句界不含「.」时整段会被当成一句，
# `context_sentence` 于是横跨多句（否定词从邻句「泄漏」进来）。
# 但小数点不能切：「增长5.2%」被切开会让句子莫名断开，所以前后都是数字的
# 「.」不算句界；「...」（省略号）里每个点仍算——它本来就是断句。
_SENT_SPLIT = re.compile(r"[。；！？;!?\n]|(?<!\d)\.(?!\d)")


def _sentence_of(cn: str, lo: int, hi: int, limit: int = 80) -> str:
    """取命中区间所在的整句（以句末标点 / 换行切分），供人核对语境。

    为什么需要：6 字片段「破坏营商环境」在原文里是「……不得出现……违反廉洁
    纪律、破坏营商环境的行为」的一部分，否定框架在 20 字外，3 字邻域看不到。
    这类片段无法靠窗口判死（放宽窗口会把合法引用一起误伤），所以只把整句
    作为事实回传，由调用方判断。
    """
    start = 0
    for m in _SENT_SPLIT.finditer(cn, 0, lo):
        start = m.end()
    m = _SENT_SPLIT.search(cn, hi)
    end = m.start() if m else len(cn)
    raw = cn[start:end]
    # 前导空白会让 `lo - start` 偏离 strip 后的坐标，先把偏移量折算掉再截窗
    rel = max(0, lo - start - (len(raw) - len(raw.lstrip())))
    s = raw.strip()
    if len(s) > limit:
        left = max(0, rel - limit // 2)
        s = s[left:left + limit]
    return s


# 小句分隔符。命中区间与否定词之间若隔着这些字符，那个否定词属于邻句
# （`"一网通办",不动产` / `"一网通办",不断提升`），不修饰命中区间。
# 3 字窗口跨过逗号取词会把 4/387 块的邻句噪声放大成整条引用的判决。
_CLAUSE_SEP = re.compile(r"[\s，。；：！？、,.;:!?…\"'“”‘’「」『』（）()\[\]【】《》<>|]")


def _clause_tail(s: str) -> str:
    """before 窗口：只保留最后一个分隔符之后的部分（同一小句内）。"""
    last = None
    for last in _CLAUSE_SEP.finditer(s):
        pass
    return s[last.end():] if last else s


def _clause_head(s: str) -> str:
    """after 窗口：只保留第一个分隔符之前的部分（同一小句内）。"""
    m = _CLAUSE_SEP.search(s)
    return s[:m.start()] if m else s


# ---- 限定语（qualifier）覆盖比对（第三轮对抗 A3/A4/A8/H1/B3/O1/O2/B1/B2）----
# 9 条绕过的共同形态：引用是原文的**逐字子串**，被丢掉的限定语贴在子串边界
# **外侧**（原文「除银团贷款外，不得向小微企业收取…」→ 引用「不得向小微企业
# 收取…」）。所以「扫 span 内部」一条都抓不到 —— span 就是引用本身，内部
# 集合恒等于引用集合。判据必须锚在边界上：
#
#   只有**紧贴边界**、且与命中区间**同属一句**的限定语，才判定它修饰的是被
#   引用的那个小句；隔着逗号/句号的是别的小句，不报（这是精度的来源）。
#
# 方向：只报「原文有、引用无」= 引用把局部规则读成普遍规则（削弱/放宽）。
# 反向（引用加了原文没有的限定语）不报：逐字子串不可能凭空多出原文没有的
# 字；能「加」限定语的只有省略号拼接，那条路径已由 omitted_negation /
# long_omission 兜住，另起一套判据只会带来误报（见 BUILD.md）。
QUAL_WINDOW = 80           # 边界外侧回看多少字（够装「除…以外，」这类长例外）
_QUAL_STRENGTH = ("原则上", "一般", "基本", "通常")
_QUAL_SCOPE = ("县级以上", "省级以上", "市级以上", "设区的市",
               "各级", "县级", "省级", "市级", "国家级")
# 例外前置：「除…外，」「除…以外，」紧贴 span 起点
_EXC_PRE_RE = re.compile(r"除[^。；.\n]{1,60}?(?:以外|外)[，,]?$")
# 例外后置：「，另有含义的除外」「，法律另有规定的除外」紧贴 span 终点
_EXC_POST_RE = re.compile(r"^[，,]?\s*[^。；.,，\n]{0,20}?除外")
# 条件前置：「对未列入清单的证明事项，」——「对/对于」起首的小句 + 逗号，
# 紧贴 span 起点。`(?:^|[，,])` 保证「对」是小句首字（挡掉「针对」「绝对」）。
_COND_PRE_RE = re.compile(r"(?:^|[，,])\s*(?:对|对于)[^。；.,，\n]{2,40}[，,]$")
# 「对X，Y」在政策文本里多数是**并列举措**而非条件（「对各类市场主体一视同仁，
# 加快打造…」「对城区40余条道路…清淤，安装安全防坠网4100余套」）。条件只
# 修饰「规则」，所以被引用的 Y 必须是道义句（含不得/应当/必须…）才认；纯
# 举措句（加快/推进/安装/提升）一律不认 —— 这是本层唯一的精度闸门。
_DEONTIC = ("不得", "禁止", "严禁", "应当", "必须", "应予", "不予",
            "可以", "不得", "须", "应")


def _tail_sentence(s: str) -> str:
    """取最后一个句界之后的部分（限定语不跨句修饰）。"""
    last = 0
    for m in _SENT_SPLIT.finditer(s):
        last = m.end()
    return s[last:]


def _head_sentence(s: str) -> str:
    """取第一个句界之前的部分。"""
    m = _SENT_SPLIT.search(s)
    return s[:m.start()] if m else s


def _dropped_qualifiers(cn: str, spans: list[tuple[int, int]],
                        qn: str) -> list[dict]:
    """命中区间边界外侧、修饰本小句的限定语，若引用未覆盖 ⇒ 判定被丢弃。

    返回 `[{"text": 原文限定语, "kind": exception|condition|strength|scope,
    "side": before|after}]`。空列表 = 边界上没有被丢掉的限定语。

    覆盖判定用 `text in qn`：引用自带同一个限定语（省略号引用把「原则上」
    放在前一段）就算已覆盖 —— 宁漏报不误报。
    """
    start, end = spans[0][0], spans[-1][1]
    pre = _tail_sentence(cn[max(0, start - QUAL_WINDOW):start])
    post = _head_sentence(cn[end:end + 24])
    out: list[dict] = []
    seen: set[str] = set()

    def add(text: str, kind: str, side: str) -> None:
        t = text.strip(" \t，,")
        if not t or t in seen or t in qn:
            return
        seen.add(t)
        out.append({"text": t, "kind": kind, "side": side})

    # 1) 限定强度：原则上 / 一般 / 基本 / 通常 —— 紧贴 span 起点
    for w in _QUAL_STRENGTH:
        if pre.endswith(w):
            add(w, "strength", "before")
            break
    # 2) 主体范围：各级 / 县级以上 / 省级以上 … —— 紧贴 span 起点。
    # 这一层只收「行政层级」写法（本身无歧义）；「所有」「全部」不收 ——
    # 「所有权」「全部价款」里它们不是范围限定，误报代价高于漏报。
    for w in _QUAL_SCOPE:
        if pre.endswith(w):
            add(w, "scope", "before")
            break
    # 3) 例外前置：「除银团贷款外，」「除法律、行政法规…规定的以外，」
    # 例外把绝对禁止变成「只对一部分主体禁止」，丢掉它 = 范围无限扩大。
    if "除" in pre:
        m = _EXC_PRE_RE.search(pre)
        if m:
            add(m.group(0), "exception", "before")
    # 4) 例外后置：「…不得作为字号，另有含义的除外」
    if "除外" in post:
        m = _EXC_POST_RE.search(post)
        if m:
            add(m.group(0), "exception", "after")
    # 5) 条件前置：「对未列入清单的证明事项，各部门…不得…」
    # 前置状语把全称规则限缩到一个子集（清单外事项），丢掉它 = 清单外变全域。
    # 只认「对/对于」起首的小句：`(?:^|[，,])` 挡住「针对」「绝对」「应对」；
    # 再加道义句闸门，挡住「对X，<并列举措>」这类非条件并列（见 _DEONTIC）。
    if pre.endswith(("，", ",")) and any(w in qn for w in _DEONTIC):
        m = _COND_PRE_RE.search(pre)
        if m:
            add(m.group(0), "condition", "before")
    return out


# 悬空比较句：引用停在「不得少于」这类量词比较词上，而原文紧接着是数字。
# 数量是这句话的全部信息量 —— 截掉数字，模型可以自由补成「6 个月」「3 年」
# 而仍显示「逐字可核实」（A5：17 字，连 fragment 标记都不给）。
_QUANT_TAIL = (
    "不得少于", "不得低于", "不得高于", "不得多于", "不得超过", "不得短于",
    "不少于", "不超过", "不多于", "不低于", "不高于", "少于", "多于",
    "低于", "高于", "超过", "短于", "长达",
)
_DIGIT_HEAD = re.compile(r"^[0-9０-９一二三四五六七八九十百千万亿两]")


def _lead(s: str, n: int = FRAME_CHARS) -> str:
    """引用开头的 n 个字（跳过前导省略号/分隔符）。

    R2：边界窗口（命中区间之前）的否定词修饰的是整个命中区间，引用要保留
    它就必须在**开头**也出现同类标记。取「整条引用里有没有否定词」会被
    引用中间任意一个否定字洗白 —— K1 的「不合理」在第 5 字，而「严禁」
    在边界外紧邻，二者毫无关系。
    """
    i = 0
    while i < len(s) and _CLAUSE_SEP.match(s[i]):
        i += 1
    return s[i:i + n]


def _trail(s: str, n: int = FRAME_CHARS) -> str:
    """引用结尾的 n 个字（跳过尾部省略号/分隔符）。与 `_lead` 对称。"""
    j = len(s)
    while j > 0 and _CLAUSE_SEP.match(s[j - 1]):
        j -= 1
    return s[max(0, j - n):j]


def _risk_core(cn: str, spans: list[tuple[int, int]], qn: str,
               q_marks: list[str] | None = None) -> tuple[dict, bool]:
    """算一次命中的**廉价**风险事实，不算整句。

    spans 是引用各段在规范化原文里的 [起, 止) 区间。检查两个地方：
      1. 省略区（相邻两段之间的原文）——省略号吞掉的内容；
      2. 命中区间前后各 CONTEXT_CHARS 字——exact/normalized 命中时没有省略区，
         但边界外的否定词同样能反转语义（「不得」+「滥用行政权力……」）。

    只有「原文侧有否定/转折、引用侧没有」才算风险：引用自带否定说明否定
    没有被丢掉，此时再报风险就是误报。

    返回 `(fields, has_src_marks)`。`has_src_marks` 给全量扫描用：整句语境
    只在原文侧确有标记时才值得再算一次。`q_marks` 由调用方传入——全量扫描
    时它只跟引用有关，逐块重算是纯浪费。
    """
    if q_marks is None:
        q_marks = _negation_marks(qn)
    before = cn[max(0, spans[0][0] - CONTEXT_CHARS):spans[0][0]]
    after = cn[spans[-1][1]:spans[-1][1] + CONTEXT_CHARS]
    gaps = [cn[spans[i][1]:spans[i + 1][0]] for i in range(len(spans) - 1)]
    omitted = "…".join(g for g in gaps if g)
    # 窗口跨过小句分隔符时，那个否定词属于邻句（`"一网通办",不动产登记`），
    # 不修饰命中区间；只取同一小句内的部分去认标记。回传的 context_* 仍是原样窗口。
    scan_before, scan_after = _clause_tail(before), _clause_head(after)

    # ---- R2：否定词的作用域对齐（第二轮对抗 K1 / A6 / G2）----
    # 旧判定是集合存在性：`bool(src_neg) and not q_neg` —— 引用里出现**任意
    # 一个**否定词就算「否定框架被保留」，既不核对它是不是被省掉的那个，也
    # 不核对它修饰谁。于是「严禁 X」→「X」只要在引用里塞一个「不合理」的
    # 「不」就能过闸（K1，成本一个汉字）。
    #
    # 两处否定词必须分开判：
    #   1. 省略区（gap）里的否定词 —— gap 就是原文被吃掉的那一块，引用在别
    #      处补任何否定都补不回它 ⇒ **无条件**风险，与引用侧有无否定无关。
    #   2. 边界窗口里的否定词 —— 它修饰整个命中区间，引用要保留它就必须在
    #      **同侧边界**出现同类标记（before ↔ 引用开头，after ↔ 引用结尾）。
    #      看整条引用会被中间任意一个否定字洗白（K1）。
    gap_marks = _negation_marks("|".join(gaps))
    gap_neg, gap_conj = _split_marks(gap_marks)
    # R2 重做：边界窗口只认**独立语义单元**（见 `_boundary_marks`）——
    # 「不利于/不符合/驰而不息/不遗余力/坚定不移」里的「不」属于那个词，
    # 不修饰命中区间；after 侧的「不V」是新的谓语，否定的是 V 不是引用片段。
    before_marks = _boundary_marks(scan_before, "before")
    after_marks = _boundary_marks(scan_after, "after")
    # 英文否定词的窗口要比中文宽得多：`No department may charge fees` 里
    # 「No」距引用 18 字符，8 字窗口看不到；而中文窗口一旦放大到 48，就会把
    # 邻句的「不利于」「不符合」拉进来，误报率上升。所以英文单独扫宽窗口，
    # 且**只认英文词** —— 中文词仍走 8 字窗口。
    _EN_WIDE = 48
    for _w in _boundary_marks(cn[max(0, spans[0][0] - _EN_WIDE):spans[0][0]], "before"):
        if _w.lower() in _NEG_EN and _w not in before_marks:
            before_marks.append(_w)
    b_neg, b_conj = _split_marks(before_marks)
    a_neg, a_conj = _split_marks(after_marks)
    # L10 的两类分判保留：引用里带「但是」不能替原文的「严禁」顶包。
    qh_neg, qh_conj = _split_marks(_negation_marks(_lead(qn)))
    qt_neg, qt_conj = _split_marks(_negation_marks(_trail(qn)))
    risky: list[str] = []
    if gap_neg or gap_conj:
        risky += gap_marks
    if b_neg and not qh_neg:
        risky += b_neg
    if b_conj and not qh_conj:
        risky += b_conj
    if a_neg and not qt_neg:
        risky += a_neg
    if a_conj and not qt_conj:
        risky += a_conj
    marks: list[str] = []
    for m in risky:                     # 去重保序
        if m not in marks:
            marks.append(m)
    reversed_risk = bool(marks)
    # R1/A5：悬空比较句 —— 引用停在量词比较词上、原文紧接着数字。数量被截掉，
    # 引用就只剩「要求至少/至多」的骨架，模型可以任意补数而仍「逐字可核实」。
    tail_win = _trail(qn, n=8)
    dangling = (bool(after)
                and any(tail_win.endswith(w) for w in _QUANT_TAIL)
                and bool(_DIGIT_HEAD.match(after.lstrip())))
    # 限定语覆盖比对：短于 fragment 下限的引用是名词短语，边界外侧的
    # 「各级」「原则上」多属邻句噪声（与 context_negation 同一条精度闸门）。
    dq = _dropped_qualifiers(cn, spans, qn) if len(qn) >= FRAGMENT_CHARS else []
    fields = {
        "omitted": omitted[:OMITTED_KEEP],
        "omitted_len": sum(len(g) for g in gaps),
        "long_omission": any(len(g) > LONG_OMISSION_CHARS for g in gaps),
        "omitted_negation": reversed_risk,
        "dangling_comparison": dangling,
        "dropped_qualifiers": dq,
        "negation_words": marks,
        "negation_in_quote": bool(q_marks),
        "context_before": before,
        "context_after": after,
        "gap_max": max((len(g) for g in gaps), default=0),
    }
    return fields, bool(gap_marks or before_marks or after_marks)


def _risk_fields(cn: str, spans: list[tuple[int, int]], qn: str,
                 q_marks: list[str] | None = None) -> dict:
    """`_risk_core` + 整句语境（供返回给调用方的那几条命中用）。

    句级语境只作事实回传，不参与 verified：短片段（「破坏营商环境」）的否定
    框架可能远在 20 字外，但把窗口放宽到整句会把大量合法引用一起误伤，
    所以这里给原文、不给结论。
    """
    fields, _ = _risk_core(cn, spans, qn, q_marks)
    sentence = _sentence_of(cn, spans[0][0], spans[-1][1])
    fields["context_sentence"] = sentence
    # 与 _risk_core 同口径：两类分开判，引用里的转折词不算「否定已被覆盖」。
    qn_neg, qn_conj = _split_marks(q_marks or [])
    sent_neg, sent_conj = _split_marks(_negation_marks(sentence))
    fields["sentence_negation"] = ((bool(sent_neg) and not qn_neg)
                                   or (bool(sent_conj) and not qn_conj))
    return fields

# \s 在 Python re 里不含 Unicode Cf 类（零宽空格 U+200B、零宽连接符 U+200C/D、
# 词连接符 U+2060、BOM U+FEFF）。从 PDF / 网页复制的引用常带这些字符，
# 不剥除会让「看起来一模一样」的引用逐字匹配失败 —— 假阴性，演示现场更尴尬。
_WS = re.compile(r"[\s\u200b-\u200f\u2060\ufeff]+")
# 源与目标必须等长：19 个全角标点 → 19 个半角对应
_FULLWIDTH = str.maketrans(
    "，。；：！？（）【】《》“”‘’、—～",
    ",.;:!?()[]<>\"\"'',-~",
)


# 滑窗兜底时，以这些字开头或结尾的子串不作为关键词候选（虚词/疑问词，无检索价值）
_STOP_CHARS = set(
    "的了在和与有为对从到把被使让给向往往及或而之其此该等些个中上下方面里内外前后时"
    "哪些什么怎么如何为什么多少哪一年"
)


def _topic_terms(topic: str) -> list[str]:
    """把用户问句降级成可匹配的候选词。

    模型常把整句问句当 topic 传进来（如「《某某管理办法》是哪一年发布的？」），
    直接子串匹配会 0 命中。这里按命中概率从高到低给出候选：

      1. 书名号 / 括号 / 引号里的专名（《某某管理办法》）—— 用户显式指名的，
         高置信，无条件接受（只要求 >= 2 字）。
      2. 按标点 / 空白切分出的片段 —— 必须覆盖原句 >= 25% 且 >= 4 字符。
         否则英文长句会被按空格切成 `proof` / `decoherence` 这类通用词，
         在语料里撞上几块就被当成主题词（RFC 语料实测命中 4 块，
         见 tests/test_domains.py 的 D7）。
      3. 整串（保底，通常匹配不到，会落到 _best_substring 滑窗）。

    与领域无关：不假设书名号里装的是法规还是标准、论文还是合同。
    """
    out: list[str] = []
    n = len(topic.strip())

    def add(s: str, *, quoted: bool = False) -> None:
        s = s.strip()
        if not s or s in out:
            return
        if quoted:
            if len(s) >= 2:
                out.append(s)
            return
        if len(s) < 4:
            return
        if n and len(s) / n < 0.25:
            return
        out.append(s)

    for m in re.findall(r"[《【（(]([^》】）)]{2,40})[》】）)]", topic):
        add(m, quoted=True)
    for m in re.findall(r"[\"'『「]([^\"'』」]{2,40})[\"'』」]", topic):
        add(m, quoted=True)
    for part in re.split(r"[，。；：！？、,.!?;:\s]+", topic):
        add(part)
    add(topic)
    return out


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    char_offset: int
    title: str
    date: str
    source: str
    text: str

    def snippet(self, limit: int = SNIPPET_LIMIT) -> str:
        t = self.text.strip()
        return t if len(t) <= limit else t[:limit] + "…"

    def to_dict(self, limit: int = SNIPPET_LIMIT) -> dict:
        """`limit` 由 Corpus 传入它的 `snippet_limit`——构造参数要真的生效，
        否则 `Corpus(path, snippet_limit=100)` 是个静默无效的死参数（L1）。"""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "char_offset": self.char_offset,
            "title": self.title,
            "date": self.date,
            "source": self.source,
            "snippet": self.snippet(limit),
            "text_length": len(self.text),
        }


def normalize(s: str) -> str:
    """规范化：去所有空白 + 全角标点转半角。用于引用比对。"""
    return _WS.sub("", (s or "").translate(_FULLWIDTH))


@dataclass
class ScanResult:
    """一次引用核验的完整结论：返回的命中列表 + 对**全量命中**的风险扫描。

    B1 的根因就在这个分离：`max_hits` 是展示参数，风险事实必须对全部命中成立。
    否则同一条引用、同一份语料，`verified` 会取决于调用方传了 10 还是 500 ——
    风险命中排在第 11 条之后就被静默丢弃，闸门等于可被参数关掉。

    `hits` 之外的字段都是全量聚合结果，与 `max_hits` 无关。
    """
    quote: str
    qn: str
    qlen: int                 # 规范化后的长度（零宽/空白不计，见 H1）
    total: int
    matched: int = 0
    hits: list[dict] = field(default_factory=list)
    # ---- 全量风险扫描（与 max_hits 无关）----
    omitted_negation: bool = False
    long_omission: bool = False
    # R1/A5：引用停在「不得少于」这类量词比较词上、原文紧接着数字。
    dangling_comparison: bool = False
    negation_words: list[str] = field(default_factory=list)
    gap_max: int = 0
    sentence_negation: bool = False
    sentence_example: str = ""
    # 带否定的邻句块数。`sentence_negation` 是 any 语义（有一块就算），对
    # 高频短语必然误报：实测「优化营商环境条例」命中 379 块里只有 18 块
    # （4.75%）邻句带否定，「一网通办」387 块里 42 块（10.85%）—— 全是少数派。
    # 而 A2 要拦的「条款片段」场景，命中块几乎全带否定（比例接近 1）。
    # 所以判定该用比例，不该用存在性。
    sentence_neg_blocks: int = 0
    # R4（第三轮对抗）：边界外侧被引用丢掉的限定语（例外/条件/强度/主体范围）。
    # 非空 ⇒ 引用把局部规则读成了普遍规则。
    dropped_qualifiers: list[dict] = field(default_factory=list)
    # ---- 引用本身的属性 ----
    short: bool = False       # 短于可核验下限（≠ 语料里不存在）
    match_kinds: list[str] = field(default_factory=list)
    # 省略号分段里因过短而未参与匹配的段。非空 ⇒ 这条引用有「没核验的部分」，
    # 命中成立也不能当原文引用（M6）。
    dropped_segments: list[str] = field(default_factory=list)

    @property
    def specificity(self) -> float:
        return round(self.matched / max(1, self.total), 4)

    @property
    def generic(self) -> bool:
        return self.matched >= GENERIC_MIN_BLOCKS and self.specificity >= GENERIC_RATIO

    @property
    def fragment(self) -> bool:
        return self.qlen < FRAGMENT_CHARS

    @property
    def block_hits(self) -> int:
        return self.matched

    @property
    def sentence_neg_ratio(self) -> float:
        """带否定邻句的命中块占全部命中块的比例。

        用比例而不是存在性：条款片段（A2 要拦的）命中块几乎全带否定（≈1.0），
        高频短语（「优化营商环境条例」4.75%）只是邻句噪声。阈值 0.5 把两者分开。
        """
        return round(self.sentence_neg_blocks / max(1, self.matched), 4)

    @property
    def verbatim(self) -> bool:
        return any(k in ("exact", "normalized") for k in self.match_kinds)


def _absorb(rep: ScanResult, fields: dict) -> None:
    """把一次命中的风险事实并进整轮结论。只升不降（any 语义）。"""
    if fields.get("omitted_negation"):
        rep.omitted_negation = True
        for w in fields.get("negation_words") or ():
            if w not in rep.negation_words:
                rep.negation_words.append(w)
    if fields.get("long_omission"):
        rep.long_omission = True
    if fields.get("dangling_comparison"):
        rep.dangling_comparison = True
    for dq in fields.get("dropped_qualifiers") or ():
        if dq not in rep.dropped_qualifiers:
            rep.dropped_qualifiers.append(dq)
    g = fields.get("gap_max") or 0
    if g > rep.gap_max:
        rep.gap_max = g
    if fields.get("sentence_negation"):
        # 计数无条件累加；`sentence_negation` 保留 any 语义供展示，判定看 ratio。
        rep.sentence_neg_blocks += 1
        if not rep.sentence_negation:
            rep.sentence_negation = True
            rep.sentence_example = fields.get("context_sentence") or ""


class Corpus:
    """内存语料库。15,000 块量级下，加载约 1 秒、占用约 40 MB。"""

    def __init__(self, path: str = DATA_FILE, snippet_limit: int = SNIPPET_LIMIT):
        self.path = path
        self.snippet_limit = snippet_limit
        self.chunks: dict[str, Chunk] = {}
        self.docs: dict[str, dict] = {}
        self._norm_text: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            raise FileNotFoundError(
                f"语料数据不存在：{self.path}。"
                f"请先运行 scripts/build_data.py 生成。"
            )
        opener = gzip.open if self.path.endswith(".gz") else open
        with opener(self.path, "rt", encoding="utf-8") as f:
            # 带行号解析：15,000 行里坏一行，没有行号就只能二分定位。
            # 行号是**解压后**的逻辑行号（gzip 的 JSONDecodeError 报的是流内
            # 偏移，跟文件行号对不上，排障时没用）——M7。
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(
                        f"{self.path}: 第 {n} 行: JSON 解析失败（{e.msg}）"
                    ) from e
                try:
                    c = Chunk(
                        chunk_id=d["chunk_id"],
                        doc_id=d.get("doc_id", ""),
                        char_offset=int(d.get("char_offset", 0)),
                        title=d.get("title", ""),
                        date=d.get("date", ""),
                        source=d.get("source", ""),
                        text=d.get("text", ""),
                    )
                except (KeyError, TypeError, ValueError) as e:
                    raise ValueError(
                        f"{self.path}: 第 {n} 行: 字段非法（{type(e).__name__}: {e}）"
                    ) from e
                self.chunks[c.chunk_id] = c
                doc = self.docs.setdefault(
                    c.doc_id,
                    {"doc_id": c.doc_id, "title": c.title, "date": c.date,
                     "source": c.source, "chunks": []},
                )
                doc["chunks"].append(c.chunk_id)

    # ---- 工具 1：精确回查 ----
    def lookup(self, chunk_id: str) -> Chunk | None:
        # 非字符串输入（MCP 层已挡，但直接调库时仍可能传入）——
        # `(123 or "").strip()` 会抛 AttributeError，这里提前返回 None。
        if not isinstance(chunk_id, str):
            return None
        return self.chunks.get(chunk_id.strip())

    # ---- 工具 2：引用验证 ----
    def _norm(self, c: Chunk) -> str:
        """规范化文本，惰性缓存。

        verify 要遍历全部块；每次重算 normalize 是 679 ms/次的主因（实测），
        缓存后降到 5 ms/次。缓存按需构建，代价是首次调用稍慢。
        """
        n = self._norm_text.get(c.chunk_id)
        if n is None:
            n = normalize(c.text)
            self._norm_text[c.chunk_id] = n
        return n

    def verify_report(self, quote: str, max_hits: int = 10,
                      min_len: int = MIN_QUOTE_CHARS) -> ScanResult:
        """在语料中查找引用文本，返回**全量风险扫描** + 截断后的命中列表。

        三级：精确 → 规范化（去空白 + 全角转半角）→ 省略号分段。

        `max_hits` 只决定 `hits` 里有几条；风险事实（`omitted_negation` /
        `long_omission` / `sentence_negation`）对**该级全部命中**都算一遍，
        所以同一条引用不会因为调用方要了 10 条还是 500 条而改判。

        每条返回的命中除了定位信息，还带一组风险事实（`_risk_fields`）：
          - `omitted` / `omitted_len`：被省略号吞掉的原文（截断 40 字）与总字数
          - `omitted_negation`：原文侧（省略区或命中区间前后 3 字）有否定/转折词，
            而引用本身没有 ⇒ 语义可能被反转（「不得借乡村振兴之名……」→
            「借乡村振兴之名……」）
          - `long_omission`：单个省略区 > 40 字 ⇒ 跨度失控，是拼接不是省略
          - `context_before` / `context_after`：命中区间前后各 3 字，供人核对
          - `block_hits` / `specificity` / `generic`：该引用命中多少块、占语料多少、
            是否已无区分度（6 字高频短语在 2000 块样本里能命中 41.7%）

        分段命中要求各段在同一块里**按原文顺序、且互不重叠**出现：查找用游标
        推进（`find(s, start)`），下一段必须整体落在上一段之后。逆序或重叠说明是
        拼接而非省略，自然查不到。
        """
        q = (quote or "").strip()
        qn = normalize(q)
        qlen = len(qn)          # 零宽/空白不计入：H1 的判定基础
        max_hits = max(0, int(max_hits))
        rep = ScanResult(quote=q, qn=qn, qlen=qlen, total=len(self.chunks))
        if qlen < min_len:
            # 太短 ⇒ 不查。但「不查」不是「不存在」，由调用方按 short 措辞。
            rep.short = True
            return rep
        q_marks = _negation_marks(qn)
        # 整句语境原本只在短引用（fragment）时才按需补算。R1/A2 之后它要参与
        # `verified`（升为 context_negation），必须对**全量命中**都成立 —— 否则
        # 同一条引用会不会被拦，取决于风险命中排在第 1 条还是第 11 条（B1 的
        # 同一个坑）。引用自身带否定词时不需要：sentence_negation 的定义里就
        # 要求「引用侧没有同类标记」。
        probe_sentence = not q_marks

        # ---- 一级：精确 ----
        matched = 0
        for c in self.chunks.values():
            j = c.text.find(q)
            if j < 0:
                continue
            matched += 1
            spans = [(j, j + len(q))]
            if len(rep.hits) < max_hits:
                h = {**c.to_dict(self.snippet_limit), "match": "exact",
                     **_risk_fields(c.text, spans, qn, q_marks)}
                rep.hits.append(h)
                _absorb(rep, h)
            else:
                f, _ = _risk_core(c.text, spans, qn, q_marks)
                _absorb(rep, f)
                if probe_sentence:
                    s = _sentence_of(c.text, spans[0][0], spans[-1][1])
                    if _negation_marks(s):
                        rep.sentence_neg_blocks += 1
                        if not rep.sentence_negation:
                            rep.sentence_negation = True
                            rep.sentence_example = s
        if matched:
            rep.matched = matched
            return self._seal(rep)

        # ---- 二级：规范化 ----
        matched = 0
        for c in self.chunks.values():
            cn = self._norm(c)
            j = cn.find(qn)
            if j < 0:
                continue
            matched += 1
            spans = [(j, j + len(qn))]
            if len(rep.hits) < max_hits:
                h = {**c.to_dict(self.snippet_limit), "match": "normalized",
                     **_risk_fields(cn, spans, qn, q_marks)}
                rep.hits.append(h)
                _absorb(rep, h)
            else:
                f, _ = _risk_core(cn, spans, qn, q_marks)
                _absorb(rep, f)
                if probe_sentence and not rep.sentence_negation:
                    s = _sentence_of(cn, spans[0][0], spans[-1][1])
                    if _negation_marks(s):
                        rep.sentence_negation = True
                        rep.sentence_example = s
        if matched:
            rep.matched = matched
            return self._seal(rep)

        # ---- 三级：省略号分段 ----
        # 模型常用「……」省略中间内容，整句无法逐字匹配，但每一段都能在同一篇
        # 里找到 —— 这不是编造，是省略。段长下限单列（MIN_SEGMENT_CHARS）：
        # 跟着引用下限一起放宽，会让零碎字词也参与拼装，凑出假命中。
        raw_segs = [s.strip() for s in re.split(r"…+|\.{3,}|⋯+", qn)]
        segs = [s for s in raw_segs if len(s) >= MIN_SEGMENT_CHARS]
        # 短段不参与匹配约束，但不能因为它就把整条引用判成「查不到」：
        # 「优化营商环境……好」的短段只有 1 字，修复前被过滤后只剩 1 段 →
        # 整条判「未找到」，而前半段本身命中 5598 块（M6）。短段记进
        # dropped_segments 回传，由调用方知道「这几段没核验」。
        dropped = [s for s in raw_segs if s and len(s) < MIN_SEGMENT_CHARS]
        if len(raw_segs) < 2:
            return rep          # 没有省略号：二级已试过整串，不必再扫一遍
        if not segs:
            return rep          # 全是短段：没有任何可定位的长段
        matched = 0
        for c in self.chunks.values():
            cn = self._norm(c)
            pos: list[int] = []
            start = 0                      # 游标：下一段只能从上一段之后开始找
            for s in segs:
                j = cn.find(s, start)
                if j < 0:
                    break
                pos.append(j)
                start = j + len(s)         # 游标推进 ⇒ 起点递增且区间不重叠
            if len(pos) != len(segs):
                continue                   # 逆序 / 重叠 / 跨块 ⇒ 不是省略
            matched += 1
            spans = [(pos[i], pos[i] + len(segs[i])) for i in range(len(segs))]
            span = spans[-1][1] - spans[0][0]
            if len(rep.hits) < max_hits:
                h = {**c.to_dict(self.snippet_limit), "match": "segments",
                     "segments": len(segs),
                     "span_ratio": round(span / max(1, len(cn)), 3),
                     **_risk_fields(cn, spans, qn, q_marks)}
                rep.hits.append(h)
                _absorb(rep, h)
            else:
                f, _ = _risk_core(cn, spans, qn, q_marks)
                _absorb(rep, f)
                if probe_sentence and not rep.sentence_negation:
                    s = _sentence_of(cn, spans[0][0], spans[-1][1])
                    if _negation_marks(s):
                        rep.sentence_negation = True
                        rep.sentence_example = s
        rep.matched = matched
        rep.dropped_segments = dropped
        return self._seal(rep)

    def verify(self, quote: str, max_hits: int = 10,
               min_len: int = MIN_QUOTE_CHARS) -> list[dict]:
        """只看命中列表的便捷入口。风险扫描照样跑全量，见 `verify_report`。"""
        return self.verify_report(quote, max_hits=max_hits, min_len=min_len).hits

    @staticmethod
    def _seal(rep: ScanResult) -> ScanResult:
        """给返回的命中补上「这条引用有多少区分度」，并汇总命中类型。

        6 字短语「优化营商环境」在 2000 块样本里命中 834 块（41.7%），
        能通过一切长度闸门，但作为「依据」毫无区分度。是否够用交给调用方判断：
        这里只给事实，不改 `verified`。

        `fragment` 同理：短于 FRAGMENT_CHARS 的引用只能证明「这几个字出现过」，
        证明不了「这句话在语料里是这么说的」——「破坏营商环境」在原文里是
        「不得出现……违反廉洁纪律、破坏营商环境的行为」的一截，拿它当依据
        等于把禁止性列举当成事实陈述。6 字高频短语（「优化营商环境」）也算
        fragment，所以它只能作提示，不能直接改判。
        """
        rep.match_kinds = sorted({h.get("match", "") for h in rep.hits})
        spec, gen, frag, n = rep.specificity, rep.generic, rep.fragment, rep.matched
        for h in rep.hits:
            h["block_hits"] = n
            h["specificity"] = spec
            h["generic"] = gen
            h["fragment"] = frag
        return rep

    # ---- 工具 3：来源清单 ----
    def sources(self, topic: str | None = None, top: int = 30) -> dict:
        """按来源/年份/文档统计。`top` 截断 `by_source` 与 `documents` 两个明细表。

        `top=0` 是合法取值：只要统计数字（`matched_chunks` / `matched_docs` /
        `by_year`），不要任何明细 —— 调用方可以借此把响应压到几百字节。
        `by_year` 与 `matched_*` **不受 top 截断**：年份基数天然很小（十几年），
        截断它只会丢信息；明细表才需要上限（`TOP_MAX`，M4）。
        """
        top = min(max(0, int(top)), TOP_MAX)  # 上限见 TOP_MAX（M4）
        topic = (topic or "").strip()
        if len(topic) > TOPIC_MAX:
            # 见 TOPIC_MAX 的说明：超长 topic 会让 _best_substring 卡死
            topic = topic[:TOPIC_MAX]
        matched_by = None
        if topic:
            # 降级：模型常把整句问句当关键词传进来，直接子串匹配会 0 命中。
            # 依次试「书名号/引号内的专名」→「按标点切出的长片段」→「整串」；
            # 都不中时再滑窗找一个真正命中语料的子串。
            for term in _topic_terms(topic):
                if any(term in c.text or term in c.title for c in self.chunks.values()):
                    topic, matched_by = term, term
                    break
            else:
                found = self._best_substring(topic)
                if found:
                    topic, matched_by = found, found
        by_source: dict[str, int] = {}
        by_year: dict[str, int] = {}
        by_doc: dict[str, dict] = {}
        for c in self.chunks.values():
            if topic and topic not in c.text and topic not in c.title:
                continue
            by_source[c.source or "(未知)"] = by_source.get(c.source or "(未知)", 0) + 1
            y = (c.date or "")[:4] or "(未知)"
            by_year[y] = by_year.get(y, 0) + 1
            d = by_doc.setdefault(
                c.doc_id, {"doc_id": c.doc_id, "title": c.title,
                           "date": c.date, "source": c.source, "chunks": 0}
            )
            d["chunks"] += 1
        n = sum(by_source.values())
        out = {
            "topic": topic or None,
            "matched_by": matched_by,
            "matched_chunks": n,
            "matched_docs": len(by_doc),
            "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])[:top]),
            "by_year": dict(sorted(by_year.items())),
            "documents": sorted(by_doc.values(), key=lambda d: -d["chunks"])[:top],
        }
        if topic and n == 0:
            out["hint"] = ("没匹配到。topic 要传 2-6 字的专名或术语"
                           "（书名号、引号里的名词通常合适），不要传整句问句。")
        return out

    def _best_substring(self, topic: str, min_hits: int | None = None) -> str | None:
        """从长句里滑窗找一个真正命中语料的子串。

        只在前面的候选全部 0 命中时才调用（代价是 O(长度 × 块数)）。
        同一长度里选命中块数最多的；命中数不到 min_hits 就试更短的。
        全都不到 min_hits 则返回 None——宁可不匹配，也不给个无关词。

        阈值随语料规模走：15,000 块的语料里，任意 3 字组合都能撞上几块，
        固定 min_hits=5 会让无关问句也「命中」（实测命中 23 块）。取
        max(5, 块数 // 200)：15,000 块对应 75，小语料仍用 5。
        最短也从 3 字提到 4 字，进一步减少撞车。
        """
        if min_hits is None:
            min_hits = max(5, len(self.chunks) // 200)
        # 英文（ASCII 字母为主）改走词序列滑窗，见 _best_word_span 的说明
        if sum(1 for ch in topic if ch.isascii() and ch.isalpha()) * 2 > len(topic):
            return self._best_word_span(topic, min_hits)
        cap = max(50, min_hits)   # 数够这么多就不必再数
        best, best_n = None, 0
        windows = 0               # 见 WINDOW_MAX：防止超长输入把这里变成 O(n²)
        for size in (8, 6, 5, 4):
            if len(topic) < size:
                continue
            for i in range(len(topic) - size + 1):
                windows += 1
                if windows > WINDOW_MAX:
                    return best if best_n >= min_hits else None
                sub = topic[i:i + size]
                # 以虚词/疑问词开头或结尾的子串没有检索价值，跳过
                if not sub.strip() or sub[0] in _STOP_CHARS or sub[-1] in _STOP_CHARS:
                    continue
                n = 0
                for c in self.chunks.values():
                    if sub in c.text or sub in c.title:
                        n += 1
                        if n >= cap:
                            break
                if n > best_n:
                    best, best_n = sub, n
                    if n >= cap:
                        return best
            if best_n >= min_hits:
                return best
        return None

    def _best_word_span(self, topic: str, min_hits: int) -> str | None:
        """英文长句：只在完整词序列上滑，绝不切开一个词。

        字符级滑窗对英文是错的——它会把 `entanglement` 切成 `lement`，
        而 `lement` 是 `element` 的碎片、在 RFC 语料里命中 556 块（32%），
        于是一句无关英文被判成「有主题词」。中文没有词边界，字符滑窗成立；
        英文必须按词滑。

        先试 2–3 个词的组合（更专指），再退到单个词。
        """
        words = [w for w in re.findall(r"[A-Za-z0-9]+", topic) if len(w) >= 4]
        cap = max(50, min_hits)
        best, best_n = None, 0
        for span in (2, 3, 1):
            for i in range(len(words) - span + 1):
                sub = " ".join(words[i:i + span])
                n = 0
                for c in self.chunks.values():
                    if sub in c.text or sub in c.title:
                        n += 1
                        if n >= cap:
                            break
                if n > best_n:
                    best, best_n = sub, n
                    if n >= cap:
                        return best
            if best_n >= min_hits:
                return best
        return None

    def stats(self) -> dict:
        """语料概况。字段与领域无关：换任何行业的语料，这里都能回答「装了多大」，
        `top_sources` 与 `year_range` 用来快速判断装的是哪个领域的料。"""
        years = sorted({(c.date or "")[:4] for c in self.chunks.values() if c.date})
        src_count: dict[str, int] = {}
        for c in self.chunks.values():
            k = c.source or "(未知)"
            src_count[k] = src_count.get(k, 0) + 1
        return {
            "chunks": len(self.chunks),
            "documents": len(self.docs),
            "sources": len(src_count),
            "data_file": os.path.basename(self.path),
            "data_path": os.path.abspath(self.path),
            "year_range": f"{years[0]}-{years[-1]}" if years else None,
            "top_sources": [k for k, _ in sorted(src_count.items(),
                                                key=lambda kv: -kv[1])[:3]],
        }
