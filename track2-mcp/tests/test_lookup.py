#!/usr/bin/env python3
"""lookup 层自测（不依赖 mcp SDK，可直接 python3 tests/test_lookup.py 运行）。

若已 `pip install -e .`，直接跑；否则脚本会自己把 src/ 加进 sys.path。
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ict_track2_mcp.lookup import Chunk, Corpus, normalize  # noqa: E402


def make_corpus() -> Corpus:
    tmp = tempfile.mkdtemp(prefix="corpus_selftest_")
    path = os.path.join(tmp, "chunks.jsonl.gz")
    rows = [
        {"chunk_id": "d1-0001", "doc_id": "d1", "char_offset": 0,
         "title": "优化营商环境条例", "date": "2019-10-22", "source": "中国政府网",
         "text": "第一条 为了持续优化营商环境，不断解放和发展社会生产力，加快建设现代化经济体系。"},
        {"chunk_id": "d1-0002", "doc_id": "d1", "char_offset": 120,
         "title": "优化营商环境条例", "date": "2019-10-22", "source": "中国政府网",
         "text": "第二条 本条例所称营商环境，是指企业等市场主体在市场经济活动中所涉及的体制机制性因素和条件。"},
        {"chunk_id": "d2-0001", "doc_id": "d2", "char_offset": 0,
         "title": "国务院办公厅关于进一步优化营商环境的意见", "date": "2020-07-15",
         "source": "国务院办公厅",
         "text": "深化“放管服”改革，进一步优化营商环境，激发市场主体活力。"},
    ]
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return Corpus(path)


def main() -> int:
    ok = total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name} {detail}")

    c = make_corpus()

    # L1 加载
    st = c.stats()
    check("L1 加载块数与文档数", st["chunks"] == 3 and st["documents"] == 2,
          f"got {st}")

    # L2 精确回查
    hit = c.lookup("d1-0002")
    check("L2 lookup 命中", hit is not None and "体制机制性因素" in hit.text)
    check("L3 lookup 未命中返回 None", c.lookup("nope-0001") is None)

    # L4 verify 精确
    hits = c.verify("持续优化营商环境")
    check("L4 verify 精确命中", len(hits) == 1 and hits[0]["chunk_id"] == "d1-0001",
          f"got {hits}")

    # L5 verify 规范化（全角引号 + 空白差异）
    hits = c.verify("深化“放管服”改革")
    check("L5 verify 全角引号可命中", len(hits) >= 1, f"got {hits}")
    hits2 = c.verify("深化 “放管服” 改革")
    check("L6 verify 带空格也能命中（规范化）", len(hits2) >= 1, f"got {hits2}")

    # L7 短引用拒绝
    check("L7 过短引用直接返回空", c.verify("优化") == [])

    # L8 未命中
    check("L8 语料外的引用返回空", c.verify("这是一段语料里不存在的引用文本") == [])

    # L9 sources 统计
    s = c.sources("营商环境")
    check("L9 sources 按主题统计", s["matched_chunks"] == 3 and s["matched_docs"] == 2,
          f"got {s['matched_chunks']}/{s['matched_docs']}")
    check("L10 sources 来源分组", s["by_source"].get("中国政府网") == 2,
          f"got {s['by_source']}")

    # L11 snippet 截断
    long_chunk = Chunk("x-0001", "x", 0, "T", "2020", "S", "字" * 1000)
    sn = long_chunk.snippet(limit=10)
    check("L11 snippet 超长截断并加省略号", sn == "字" * 10 + "…", f"got {sn[:20]}")
    check("L12 snippet 短文本不截断",
          Chunk("y", "y", 0, "T", "2020", "S", "短").snippet(10) == "短")

    # L13 normalize
    check("L13 normalize 去空白 + 全角转半角",
          normalize("a， b　c") == "a,bc", f"got {normalize('a， b　c')!r}")

    # L14/L15/L16 主题降级：模型常把整句问句当 topic 传进来（实测踩过），
    # 直接子串匹配会 0 命中。_topic_terms 会先拆出书名号/引号内的专名。
    q = c.sources("请问《营商环境》方面的政策有哪些？")
    check("L14 整句问句能降级命中", q["matched_chunks"] > 0,
          f"got {q['matched_chunks']}")
    check("L15 降级后记录 matched_by", bool(q.get("matched_by")),
          f"got {q.get('matched_by')!r}")
    nope = c.sources("量子纠缠的退相干时间是多少？")
    check("L16 无关问句不假匹配并给出 hint",
          nope["matched_chunks"] == 0 and bool(nope.get("hint")),
          f"got {nope['matched_chunks']} hint={nope.get('hint')!r}")

    # L17/L18 省略号分段：模型给的引用常带「……」，整句逐字匹配不到，
    # 但每一段都能在同一篇里找到 —— 这是省略不是编造，必须能区分出来。
    # 注意：本测试用的是上面 3 条合成语料，引用必须取自其中。
    seg = c.verify("第一条 为了持续优化营商环境……加快建设现代化经济体系。")
    check("L17 省略号引用识别为 segments",
          bool(seg) and seg[0]["match"] == "segments",
          f"got {[(h['match'], h['chunk_id']) for h in seg[:2]]}")
    fake = c.verify("《优化营商环境条例》明确规定各地应在三十个工作日内办结企业开办手续。")
    check("L18 编造引用仍然查不到（分段不放水）", not fake,
          f"got {len(fake)} hits")

    # L19/L20/L21 分段的顺序与跨度（2026-10-16 优化）：
    # 原来只要求「各段都在同一篇里」，于是把两段语义无关的话拼起来也能通过
    # ——实测「优化营商环境……破坏市场秩序」间隙 357 字、跨度占全文 42%，
    # 两段一在讲知识产权检察、一在讲侵害公共利益，毫无关系。
    # 现在要求各段按原文顺序出现（逆序 = 拼接，拒绝），并把跨度作为事实返回。
    rev = c.verify("加快建设现代化经济体系……持续优化营商环境")
    check("L19 逆序分段被拒绝（拼接不是省略）", not rev,
          f"got {[(h['match'], h['chunk_id']) for h in rev[:2]]}")
    fwd = c.verify("持续优化营商环境……加快建设现代化经济体系。")
    check("L20 segments 带 gap_max 与 span_ratio（跨度作为事实返回）",
          bool(fwd) and "gap_max" in fwd[0] and "span_ratio" in fwd[0],
          f"got {fwd[0] if fwd else None}")
    check("L21 规范化文本被缓存（不再每次重算全部块）",
          len(c._norm_text) > 0, f"cache={len(c._norm_text)}")

    # L22 零宽字符：从 PDF / 网页复制来的引用常夹带 U+200B 等 Cf 类字符，
    # 若不剥除，逐字匹配会被打断，合法引用被判"查不到"（假阴性）。
    zw = c.verify("持续优化营商\u200b环境")
    check("L22 零宽字符被剥除后可命中", len(zw) >= 1, f"got {zw[:1]}")

    # L23 H1：零宽字符不能当长度用。fragment 判据若取原始 len()，给 6 字引用
    # 末尾补 6 个 U+200B 就能把它撑到 12 字，警告被抹掉。
    long_raw = "破坏营商环境" + "\u200b" * 6
    check("L23 零宽填充不能抹掉 fragment 警告",
          c.verify_report("破坏营商环境").fragment
          and c.verify_report(long_raw).fragment,
          f"raw={c.verify_report(long_raw).qlen}")

    # L24 H3：4-5 字专名不得被「长度闸门」误杀成「语料中未找到」。
    # 下限从 6 降到 4；短引用仍由 fragment / generic 事实兜住。
    # 用 4 字的「营商环境」：改前阈值是 6，它必被判「语料中未找到」。
    short_proper = c.verify_report("营商环境")
    check("L24 4 字专名可核验（不再报未找到）",
          short_proper.matched >= 1 and not short_proper.short,
          f"matched={short_proper.matched} short={short_proper.short}")
    check("L25 低于下限仍走「无法核验」而非「不存在」",
          c.verify_report("优化").short and c.verify_report("优化").matched == 0)

    # L26 H2：context_sentence 不得跨句。normalize() 把「。」转成「.」，
    # 若 _SENT_SPLIT 不认「.」，normalized/segments 路径会返回整段多句。
    tmp2 = tempfile.mkdtemp(prefix="corpus_sent_")
    path2 = os.path.join(tmp2, "chunks.jsonl.gz")
    with gzip.open(path2, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "chunk_id": "s-0001", "doc_id": "s", "char_offset": 0,
            "title": "句界自测", "date": "2026-01-01", "source": "自测",
            "text": "第一句话讲的是背景。第二句话不得滥用 行政权力。第三句话是结尾。",
        }, ensure_ascii=False) + "\n")
    cs = Corpus(path2)
    rep = cs.verify_report("行政权力")          # 中间有空格 → 走 normalized 路径
    sent = (rep.hits[0].get("context_sentence") or "") if rep.hits else ""
    check("L26 context_sentence 不跨句",
          bool(sent) and "第一句" not in sent and "第三句" not in sent,
          f"got {sent!r}")

    # L27 小句边界：命中区间与否定词之间隔着逗号/引号时，那个否定词属于邻句，
    # 不能把整条引用判成「省略了否定」（「一网通办」387 块里 4 块踩过）。
    tmp3 = tempfile.mkdtemp(prefix="corpus_clause_")
    path3 = os.path.join(tmp3, "chunks.jsonl.gz")
    with gzip.open(path3, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "chunk_id": "c-0001", "doc_id": "c", "char_offset": 0,
            "title": "小句自测", "date": "2026-01-01", "source": "自测",
            "text": "推进“一网通办”,不动产登记同步办理。",
        }, ensure_ascii=False) + "\n")
    cc = Corpus(path3)
    check("L27 邻句否定词不计入本引用",
          not cc.verify_report("一网通办").omitted_negation,
          f"words={cc.verify_report('一网通办').negation_words}")

    # L28 省略号后的短段不丢弃整条引用（M6）：修复前 "优化营商环境……好"
    # 过滤后只剩 1 段 → return [] → 整条判「未找到」，而前半段本身命中数千块。
    # 修法：短段不参与匹配约束，但记进 dropped_segments 回传（不作约束 ≠ 不存在）。
    tmp4 = tempfile.mkdtemp(prefix="corpus_shortseg_")
    path4 = os.path.join(tmp4, "chunks.jsonl.gz")
    with gzip.open(path4, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "chunk_id": "s-0001", "doc_id": "s", "char_offset": 0,
            "title": "短段自测", "date": "2026-01-01", "source": "自测",
            "text": "持续优化营商环境，激发市场主体活力。",
        }, ensure_ascii=False) + "\n")
    cs2 = Corpus(path4)
    rep4 = cs2.verify_report("优化营商环境……好")
    check("L28 省略号短段不吞掉整条引用",
          rep4.matched > 0 and rep4.dropped_segments == ["好"]
          and not rep4.verbatim,
          f"matched={rep4.matched} dropped={rep4.dropped_segments} "
          f"verbatim={rep4.verbatim}")

    # L29 Corpus 的 snippet_limit 真的生效（L1）。修复前它是死参数：
    # Chunk.snippet() 用模块级 SNIPPET_LIMIT，to_dict() 永远给 600 字。
    cs3 = Corpus(path4, snippet_limit=5)
    hit = cs3.verify("持续优化营商环境")[0]
    check("L29 snippet_limit 生效",
          hit["snippet"].endswith("…") and len(hit["snippet"]) <= 6,
          f"got {hit['snippet']!r}")

    # L30 数据坏行报**文件行号**（M7）。修复前 gzip 流只给
    # "line 1 column 2"（解压流偏移），15,000 行里定位坏行只能二分。
    tmp5 = tempfile.mkdtemp(prefix="corpus_badline_")
    path5 = os.path.join(tmp5, "chunks.jsonl.gz")
    with gzip.open(path5, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "chunk_id": "b-0001", "doc_id": "bd", "char_offset": 0,
            "title": "坏行自测", "date": "2026-01-01", "source": "自测",
            "text": "第一行是好的。",
        }, ensure_ascii=False) + "\n")
        f.write('{"chunk_id": "b-0002", "text":\n')   # 第 2 行：JSON 不完整
    err = ""
    try:
        Corpus(path5)
    except ValueError as e:
        err = str(e)
    check("L30 数据坏行报文件行号", "第 2 行" in err, f"got {err!r}")

    # L30b 缺必需字段（chunk_id）也报行号，而不是裸 KeyError（M7）
    tmp6 = tempfile.mkdtemp(prefix="corpus_missingkey_")
    path6 = os.path.join(tmp6, "chunks.jsonl.gz")
    with gzip.open(path6, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"chunk_id": "c-0001", "doc_id": "cd",
                            "text": "好的。"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"doc_id": "cd", "text": "缺 chunk_id"},
                           ensure_ascii=False) + "\n")
    err2 = ""
    exc_type = ""
    try:
        Corpus(path6)
    except Exception as e:
        err2, exc_type = str(e), type(e).__name__
    # 关键：不是裸 KeyError，而是带文件行号的 ValueError（消息里提到
    # KeyError 是正常的 —— 它记录了根因类型）。
    check("L30b 缺必需字段报行号",
          exc_type == "ValueError" and "第 2 行" in err2,
          f"got {exc_type}: {err2!r}")

    # L32 转折词不能替否定词顶包（L10）。原文「严禁破坏营商环境，但是要
    # 依法监管」，引用省掉「严禁」却保留了「但是」—— 修复前
    # `bool(src_marks) and not q_marks` 认为「引用里也有标记」，于是不报风险，
    # 一条语义反转的引用被判成 verified。
    tmp7 = tempfile.mkdtemp(prefix="corpus_l10_")
    path7 = os.path.join(tmp7, "chunks.jsonl.gz")
    with gzip.open(path7, "wt", encoding="utf-8") as f:
        f.write(json.dumps({
            "chunk_id": "e-0001", "doc_id": "ed", "char_offset": 0,
            "title": "否定转折自测", "date": "2026-01-01", "source": "自测",
            "text": "严禁破坏营商环境，但是要依法监管。",
        }, ensure_ascii=False) + "\n")
    rep7 = Corpus(path7).verify_report("破坏营商环境……但是要依法监管")
    check("L32 转折词不能替否定词顶包",
          rep7.omitted_negation is True and "严禁" in rep7.negation_words
          and rep7.matched > 0,
          f"omitted={rep7.omitted_negation} words={rep7.negation_words} "
          f"matched={rep7.matched}")

    # ---- 第二轮对抗（round2）的四个根因，各一条判据 ----
    # 共同工具：一条 chunk 的临时语料 → verify_report。
    def _rep(text: str, quote: str):
        d = tempfile.mkdtemp(prefix="corpus_r2_")
        fp = os.path.join(d, "chunks.jsonl.gz")
        with gzip.open(fp, "wt", encoding="utf-8") as f:
            f.write(json.dumps({
                "chunk_id": "r2-0001", "doc_id": "r2", "char_offset": 0,
                "title": "第二轮对抗自测", "date": "2026-01-01", "source": "自测",
                "text": text,
            }, ensure_ascii=False) + "\n")
        return Corpus(fp).verify_report(quote)

    # L33 R2：否定词必须**同侧**对齐，不能被引用中间任意一个字洗白（K1）。
    # 修复前 `bool(src_neg) and not q_neg` 是集合存在性：引用里「不合理」的
    # 「不」被记入 q_neg 就放行，成本是一个汉字。
    rep33 = _rep("严禁金融机构贷款附加不合理条件。",
                 "金融机构贷款附加不合理条件")
    check("L33 边界否定词不能被引用中间的否定字洗白",
          rep33.omitted_negation is True and "严禁" in rep33.negation_words
          and rep33.matched > 0,
          f"omitted={rep33.omitted_negation} words={rep33.negation_words}")

    # L34 R2：省略区（gap）里的否定词**无条件**成风险 —— 引用在别处补一个
    # 否定词补不回它（A6：原文两处「严禁」，引用保留开头那个、省掉目标句那个；
    # 对照组 A6c 去掉开头那个立刻拦住了，同一 gap、唯一变量是错位的否定词）。
    rep34 = _rep("严禁干扰企业正常经营，严禁破坏营商环境。",
                 "严禁干扰企业正常经营……破坏营商环境")
    check("L34 省略区否定词不因引用自带否定词而放行",
          rep34.omitted_negation is True and "严禁" in rep34.negation_words,
          f"omitted={rep34.omitted_negation} words={rep34.negation_words} "
          f"omitted_text={[h.get('omitted') for h in rep34.hits[:1]]}")

    # L35 R3：`_NEG_WORDS` 缺「无需/无须/毋须」—— 义务的「零要求」形态。
    # 修复前 `omitted='无需再'` 被打印出来却报「无省略否定」，义务方向整个反转。
    rep35 = _rep("项目已备案的，无需再办理环境影响评价手续。",
                 "项目已备案的……办理环境影响评价手续")
    check("L35 「无需」计入否定词",
          rep35.omitted_negation is True and "无需" in rep35.negation_words,
          f"omitted={rep35.omitted_negation} words={rep35.negation_words} "
          f"omitted_text={[h.get('omitted') for h in rep35.hits[:1]]}")

    # L36 R1/A1：CONTEXT_CHARS 3 → 8 —— 「不得」被「以任何方式」推到第 5 字，
    # 3 字窗口里只剩「任何方式」，禁止条款整段消失（A1）。
    rep36 = _rep("任何单位和个人不得以任何方式为招标人指定招标代理机构。",
                 "为招标人指定招标代理机构")
    check("L36 8 字窗口能捞到「以任何方式」之外的否定词",
          rep36.omitted_negation is True and "不得" in rep36.negation_words,
          f"omitted={rep36.omitted_negation} words={rep36.negation_words}")

    # ---- 第三轮对抗（round2 剩余 9 条）：限定语覆盖比对（R4）----
    # 9 条的共同形态：引用是原文的**逐字子串**，被丢掉的限定语贴在子串边界
    # 外侧。扫 span 内部一条都抓不到（span 就是引用本身），所以判据锚在边界：
    # 紧贴边界、同属一句的限定语，引用没覆盖 ⇒ dropped_qualifier。
    def _dq(text: str, quote: str):
        r = _rep(text, quote)
        return r, [d["text"] for d in r.dropped_qualifiers]

    # L39 例外前置（A3）：「除银团贷款外，不得…」引成「不得…」——绝对禁止
    # 被读成无例外禁止，范围无限扩大。
    r39, d39 = _dq("除银团贷款外，不得向小微企业收取贷款承诺费、资金管理费。",
                   "不得向小微企业收取贷款承诺费、资金管理费")
    check("L39 前置例外被丢弃要报风险",
          r39.dropped_qualifiers and r39.dropped_qualifiers[0]["kind"] == "exception"
          and "除银团贷款外" in d39[0] and r39.matched > 0,
          f"dq={d39} kinds={[d.get('kind') for d in r39.dropped_qualifiers]}")

    # L40 例外后置（O1）：「…不得作为字号，另有含义的除外」引成前半句。
    r40, d40 = _dq("县级以上地方行政区划名称不得作为字号，另有含义的除外。",
                   "县级以上地方行政区划名称不得作为字号")
    check("L40 后置例外被丢弃要报风险",
          bool(r40.dropped_qualifiers)
          and any(d["side"] == "after" for d in r40.dropped_qualifiers)
          and any("除外" in t for t in d40),
          f"dq={d40} sides={[d.get('side') for d in r40.dropped_qualifiers]}")

    # L41 条件前置（A4）：「对未列入清单的证明事项，不得…」引成「不得…」——
    # 清单外事项被读成全域禁止。
    r41, d41 = _dq("对未列入清单的证明事项，各部门不得要求办事群众出具证明。",
                   "各部门不得要求办事群众出具证明")
    check("L41 前置条件被丢弃要报风险",
          bool(r41.dropped_qualifiers)
          and r41.dropped_qualifiers[0]["kind"] == "condition"
          and "对未列入清单的证明事项" in d41[0],
          f"dq={d41}")

    # L42 限定强度（A8/H1）：「原则上不再依据…」「一般不得设置…」——「原则上」
    # 「一般」允许例外，丢掉后规则从「可例外」变「绝对」。
    r42a, d42a = _dq("发票管理，原则上不再依据经营范围核定企业申领的发票种类。",
                     "不再依据经营范围核定企业申领的发票种类")
    r42b, d42b = _dq("工程建设项目一般不得设置企业或项目负责人业绩、奖项。",
                     "不得设置企业或项目负责人业绩、奖项")
    check("L42 限定强度「原则上」「一般」被丢弃要报风险",
          bool(r42a.dropped_qualifiers) and bool(r42b.dropped_qualifiers)
          and "原则上" in d42a[0] and "一般" in d42b[0],
          f"dq_a={d42a} dq_b={d42b}")

    # L43 主体范围（B1/B2/B3）：「各级人民政府」「县级以上」——层级被削掉后
    # 局部规则被读成全国/全省规则。
    r43a, d43a = _dq("各级人民防空主管部门不得设置任何限制条件。",
                     "人民防空主管部门不得设置任何限制条件")
    r43b, d43b = _dq("县级以上地方行政区划名称、行业或者经营特点不得作为字号。",
                     "地方行政区划名称、行业或者经营特点不得作为字号")
    check("L43 主体范围「各级」「县级以上」被丢弃要报风险",
          bool(r43a.dropped_qualifiers) and bool(r43b.dropped_qualifiers)
          and "各级" in d43a[0] and "县级以上" in d43b[0],
          f"dq_a={d43a} dq_b={d43b}")

    # L44 正例（硬门槛）：限定语**一起**引出的引用必须仍然 verified —— 这是
    # 「不许用假阳性换假阴性」的那道闸。四条覆盖四类限定语。
    ok44 = []
    for txt, q in [
        ("除银团贷款外，不得向小微企业收取贷款承诺费、资金管理费。",
         "除银团贷款外，不得向小微企业收取贷款承诺费、资金管理费"),
        ("对未列入清单的证明事项，各部门不得要求办事群众出具证明。",
         "对未列入清单的证明事项，各部门不得要求办事群众出具证明"),
        ("发票管理，原则上不再依据经营范围核定企业申领的发票种类。",
         "原则上不再依据经营范围核定企业申领的发票种类"),
        ("各级人民防空主管部门不得设置任何限制条件。",
         "各级人民防空主管部门不得设置任何限制条件"),
    ]:
        r = _rep(txt, q)
        ok44.append(r.matched > 0 and not r.dropped_qualifiers)
    check("L44 限定语一起引出的合法引用不报风险", all(ok44), f"per-case={ok44}")

    # L45 反向不报：引用**加了**原文没有的限定语是「加强」，不是削弱。
    # 逐字子串不可能凭空多出原文没有的字；能「加」限定语的只有省略号拼接，
    # 那条路径已由 omitted_negation / long_omission 兜住，另设判据只会误报。
    r45, d45 = _dq("各部门不得要求办事群众出具证明。",
                   "原则上不得要求办事群众出具证明")
    check("L45 引用加强（加限定语）不报 dropped_qualifier",
          not r45.dropped_qualifiers, f"dq={d45}")

    # L46 精度闸门：「对X，Y」在政策文本里多数是**并列举措**而非条件。
    # 无道义词（不得/应当/必须…）的 Y 一律不认 —— 实测语料 600 条真实小句
    # 抽样，加闸门前 5 条误报、加闸门后 0 条。
    r46, d46 = _dq("对各类市场主体一视同仁，加快打造市场化法治化国际化一流营商环境。",
                   "加快打造市场化法治化国际化一流营商环境")
    check("L46 「对X，<并列举措>」不误判为条件",
          not r46.dropped_qualifiers, f"dq={d46}")

    # L47 精度闸门：短于 fragment 下限（12 字）的名词短语，边界外侧的限定语
    # 多属邻句噪声（与 context_negation 同一条闸门）。
    r47, d47 = _dq("各级人民政府应当持续优化营商环境。", "优化营商环境")
    check("L47 短名词短语不因边界限定语被拦",
          not r47.dropped_qualifiers, f"dq={d47}")

    # L48 R2 假阴性修复（2026-10-17）：边界窗口里的「不」必须属于**紧邻的
    # 谓词**才叫否定。旧实现是子串命中，把「不利于/不符合/驰而不息/不遗余力/
    # 坚定不移」里的「不」当成对引用片段的否定，于是「优化营商环境」这类
    # 逐字命中的合法引用被判死（全库 7918 次出现里 73 次触发）。
    bad48 = []
    for txt, q in [
        ("包括不符合“放管服”要求，不利于优化营商环境，有碍改革决策落实。", "优化营商环境"),
        ("向世界释放改革开放积极信号，驰而不息抓好优化营商环境这件大事。", "优化营商环境"),
        ("要不遗余力优化营商环境，恪守契约精神。", "优化营商环境"),
        ("强化跟踪问效，坚定不移优化营商环境，更大激发市场主体活力。", "优化营商环境"),
        ("…实现质的有效提升和量的合理增长。但优化营商环境方面存在一些问题。", "优化营商环境"),
        ("优化营商环境要求政务服务不能仅专注于某个阶段。", "优化营商环境"),
    ]:
        r = _rep(txt, q)
        if r.omitted_negation:
            bad48.append((q, r.negation_words, txt))
    check("L48 假否定（不利于/不符合/驰而不息/不遗余力/坚定不移/但/新谓语）不报 omitted_negation",
          not bad48, f"误报={bad48}")

    # L50 比例闸门（2026-10-17）：`context_negation` 用**比例**不用存在性。
    # 高频短语的命中块里必然混有带否定的邻句 —— 实测「优化营商环境条例」
    # 379 块里 18 块（4.75%）、「一网通办」387 块里 42 块（10.85%）、
    # 「市场准入负面清单」192 块里 32 块（16.67%），全是少数派。any 语义
    # 会把这三条合法引用全部误伤。而 A2 要拦的「条款片段」，命中块几乎全带
    # 否定（≈1.0）。阈值 0.5 把两者分开。
    def _rep_multi(texts, quote):
        d = tempfile.mkdtemp(prefix="corpus_ratio_")
        fp = os.path.join(d, "chunks.jsonl.gz")
        with gzip.open(fp, "wt", encoding="utf-8") as f:
            for i, t in enumerate(texts):
                f.write(json.dumps({
                    "chunk_id": f"ratio-{i:04d}", "doc_id": "ratio",
                    "char_offset": 0, "title": "比例闸门自测", "date": "2026-01-01",
                    "source": "自测", "text": t,
                }, ensure_ascii=False) + "\n")
        return Corpus(fp).verify_report(quote)

    # 10 块命中里只有 1 块邻句带否定（10%）⇒ 高频短语形态，不拦。
    r50a = _rep_multi(["优化营商环境条例"] + ["本条规定如下。优化营商环境条例"] * 8
                      + ["不得违反优化营商环境条例的规定。"], "优化营商环境条例")
    # 3 块命中全部邻句带否定（100%）⇒ 条款片段形态，拦。
    r50b = _rep_multi(["不得把规模条件作为资格要求。"] * 3, "把规模条件作为资格要求")
    check("L50 邻句否定按比例判定（10% 不拦 / 100% 拦）",
          r50a.matched == 10 and r50a.sentence_neg_ratio < 0.5
          and r50b.matched == 3 and r50b.sentence_neg_ratio >= 0.5,
          f"a={r50a.matched}块/{r50a.sentence_neg_ratio} b={r50b.matched}块/{r50b.sentence_neg_ratio}")

    # L51 英文否定词（2026-10-17）：此前整层失明 —— 词表全是中文，RFC 语料
    # 与中英混排引用里 `No department may charge fees` 引成 `charge fees` 不报风险。
    # 英文窗口必须比中文宽：`No` 距引用 18 字符，8 字窗口看不到。
    r51a = _rep("Article 7. No department may charge fees for such services.",
                "charge fees for such services")
    r51b = _rep("Providers shall not disclose user data.", "disclose user data")
    # 正例：完整引用（带否定词）不报。
    r51c = _rep("Providers shall not disclose user data.", "shall not disclose user data")
    check("L51 英文否定词（No/shall not）被省略要报，完整引用不报",
          r51a.omitted_negation and r51b.omitted_negation and not r51c.omitted_negation,
          f"a={r51a.negation_words} b={r51b.negation_words} c={r51c.omitted_negation}")

    # L52 繁体否定词（2026-10-17）：词表全是简体，繁简混排引用匹配不到。
    # 用 `_T2S_NEG` 逐个 find，位置取原文偏移（不做全文转换 —— 转换会改偏移，
    # 而所有位置都用于边界窗口计算）。
    r52a = _rep("嚴禁破壞營商環境。", "破壞營商環境")
    r52b = _rep("未經批准不得開展活動。", "開展活動")
    check("L52 繁体否定词（嚴禁/未經）被省略要报",
          r52a.omitted_negation and r52b.omitted_negation,
          f"a={r52a.negation_words} b={r52b.negation_words}")

    # L49 同一闸门的反方向：真否定算子必须仍然认出，别用假阴性换假阳性。
    good49 = []
    for txt, q in [
        ("除银团贷款外，不得向小微企业收取贷款承诺费。", "向小微企业收取贷款承诺费"),
        ("各部门严禁设置任何限制条件。", "设置任何限制条件"),
        ("未经批准不得开展经营活动。", "开展经营活动"),
        ("不得优化营商环境考核指标。", "优化营商环境考核指标"),
    ]:
        r = _rep(txt, q)
        good49.append((q, r.omitted_negation))
    check("L49 真否定算子（不得/严禁/未经）仍报 omitted_negation",
          all(v for _q, v in good49), f"漏报={good49}")

    # L53 lookup() 对非字符串输入返回 None，而不是抛 AttributeError。
    # MCP 层已有 _params 校验，但直接调库（测试/脚本/未来别的入口）仍可能传错类型。
    c53 = make_corpus()
    bad53 = []
    for bad in (123, None, 3.14, ["a"], {"k": "v"}):
        try:
            r = c53.lookup(bad)
            if r is not None:
                bad53.append((bad, r))
        except Exception as e:
            bad53.append((bad, f"{type(e).__name__}: {e}"))
    check("L53 lookup() 非字符串输入返回 None 不抛",
          not bad53, f"异常/误返回={bad53}")

    # L54 超长 topic 不得把 sources() 拖成分钟级。
    # 2026-10-18 压测：10 KB 的 topic 走 _best_substring 滑窗，实测 180 秒。
    # 修法：TOPIC_MAX 截断 + WINDOW_MAX 限制滑窗总数。
    import time as _t54
    c54 = make_corpus()
    t0 = _t54.time()
    try:
        r54 = c54.sources("营" * 10000, top=3)
        dt54 = _t54.time() - t0
        ok54 = dt54 < 5.0
        check("L54 超长 topic（10 KB）在 5 秒内返回",
              ok54, f"耗时 {dt54:.2f}s" if not ok54 else "")
    except Exception as e:
        check("L54 超长 topic（10 KB）在 5 秒内返回",
              False, f"抛异常 {type(e).__name__}: {e}")

    # ---- 严格档（strict）：只改变「检测到之后算不算通过」，不改检测本身 ----
    # 同一段语料、同一条引用，strict 开关必须让判定从通过变为不通过，
    # 且使用的风险种类必须是三类「默认只回传事实」之一。
    sys.path.insert(0, SRC)
    from ict_track2_mcp import server as _srv
    _prev = _srv._corpus
    try:
        _srv._corpus = make_corpus()   # server.corpus() 读的是模块级单例变量
        for strict, want_verified, why in ((False, True, "默认档：短专名可引"),
                                           (True, False, "严格档：短/泛引用不通过")):
            payload, _err = _srv._call_tool("verify_quote",
                                            {"quote": "优化营商环境", "strict": strict})
            check(f"L55 strict={strict} 时 verified={want_verified}（{why}）",
                  payload.get("verified") is want_verified,
                  f"got verified={payload.get('verified')} flags={payload.get('risk_flags')}")
        # 严格档不得凭空造风险：完整长引用在严格档下仍应通过
        payload, _ = _srv._call_tool("verify_quote",
                                     {"quote": "深化“放管服”改革，进一步优化营商环境，激发市场主体活力",
                                      "strict": True})
        check("L56 严格档不误伤完整长引用",
              payload.get("verified") is True,
              f"got verified={payload.get('verified')} flags={payload.get('risk_flags')}")
    finally:
        _srv._corpus = _prev

    print(f"\n[test_lookup] {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
