#!/usr/bin/env python3
"""verify_quote 第二轮对抗测试。

目标：找出「引用语义被改变、但仍判 verified=True」的路径。
所有引用均由 data/chunks.jsonl.gz 中的真实原文改造而来（脚本内断言）。

判定（与 server._call_tool 一致）：
  绕过    verified=True  且 partial=False 且 risk_flags=[]
  弱拦住  verified=True  但 partial=True 或有 risk_flags
  强拦住  verified=False

用法：cd track2-mcp && .venv/bin/python demo/adversarial-round2.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ict_track2_mcp.lookup import Corpus  # noqa: E402
from ict_track2_mcp.server import _call_tool  # noqa: E402

OMIT = re.compile(r"…+|\.{3,}|⋯+")


def segments(q: str) -> list[str]:
    return [s for s in OMIT.split(q) if s]


def main() -> int:
    C = Corpus()
    cases = CASES
    rows: list[dict] = []
    failures: list[str] = []

    for cs in cases:
        q = cs["quote"]
        # 断言：未变异且指定了 chunk 的引用，必须是原文真子串（段序一致）。
        # mutated=True 的是刻意改字/改词，chunk_id="" 的是全语料级构造，均跳过。
        if cs["chunk_id"] and not cs.get("mutated"):
            chunk = C.chunks.get(cs["chunk_id"])
            if chunk is None:
                failures.append(f"{cs['id']}: chunk {cs['chunk_id']} 不存在")
                continue
            segs = segments(q)
            if len(segs) == 1:
                if q not in chunk.text:
                    failures.append(f"{cs['id']}: quote 不是 {cs['chunk_id']} 的子串")
                    continue
            else:
                pos = 0
                ok = True
                for s in segs:
                    i = chunk.text.find(s, pos)
                    if i < 0:
                        ok = False
                        break
                    pos = i + len(s)
                if not ok:
                    failures.append(f"{cs['id']}: 段无法按序在 {cs['chunk_id']} 中找到")
                    continue

        p, _err = _call_tool("verify_quote", {"quote": q})
        h = p["hits"][0] if p["hits"] else {}
        if p["verified"] and not p["partial"] and not p["risk_flags"]:
            verdict = "绕过"
        elif p["verified"]:
            verdict = "弱拦住"
        else:
            verdict = "强拦住"
        if verdict != cs["expect"]:
            failures.append(f"{cs['id']}: 期望 {cs['expect']} 实际 {verdict}")

        rows.append(
            dict(
                id=cs["id"], group=cs["group"], q=q, chunk_id=cs["chunk_id"],
                verified=p["verified"], partial=p["partial"],
                flags=p["risk_flags"], kinds=p["match_kinds"], hits=p["hit_count"],
                neg=h.get("negation_words"), gap=h.get("gap_max"),
                sent_neg=h.get("sentence_negation"),
                ctx=f"{h.get('context_before')!r}/{h.get('context_after')!r}",
                omitted=h.get("omitted"), verdict=verdict, note=cs["note"],
                role=cs.get("role", "attack"), expect=cs["expect"],
                known_gap=cs.get("known_gap", False),
            )
        )

    w = 30
    print(f"{'构造':<{w}}|{'期望':<6}|{'verified':<9}|{'风险字段':<46}|判定")
    print("-" * 140)
    for r in rows:
        print(
            f"{r['id'] + ' ' + r['group']:<{w}}|{r['expect']:<6}"
            f"|{str(r['verified']):<9}|{str(r['flags']):<46}|{r['verdict']}"
        )
    print()
    print("明细：")
    for r in rows:
        print(f"\n[{r['id']}] {r['group']}  ({r['chunk_id']})")
        print(f"  quote   : {r['q']}")
        print(f"  verified={r['verified']} partial={r['partial']} flags={r['flags']} "
              f"kinds={r['kinds']} hits={r['hits']}")
        print(f"  neg={r['neg']} gap={r['gap']} sentence_negation={r['sent_neg']} "
              f"ctx={r['ctx']} omitted={r['omitted']!r}")
        print(f"  变化    : {r['note']}")

    atk = [r for r in rows if r["role"] == "attack"]
    ctl = [r for r in rows if r["role"] == "control"]
    n = {"绕过": 0, "弱拦住": 0, "强拦住": 0}
    for r in atk:
        n[r["verdict"]] += 1
    gaps = [r["id"] for r in atk if r.get("known_gap")]
    print(f"\n攻击用例 {len(atk)} 条：绕过 {n['绕过']} / 弱拦住 {n['弱拦住']} / 强拦住 {n['强拦住']}")
    if gaps:
        print(f"其中已知未修 {len(gaps)} 条（需新机制，见 round3 报告「没修的」）："
              + "、".join(gaps))
    print(f"对照用例 {len(ctl)} 条：" + "，".join(f"{r['id']}={r['verdict']}" for r in ctl))
    if failures:
        print("\n断言失败：")
        for f in failures:
            print("  !", f)
        return 1
    return 0


CASES: list[dict] = [
    # ============ R1：边界窗口（CONTEXT_CHARS=3 + _clause_tail/head 遇分隔符清空）============
    dict(id="A1", group="否定词被推出 3 字窗口", expect="强拦住",
         chunk_id="c4a46e60e15ca8da-0000",
         quote="为招标人指定招标代理机构，不得违法限定招标人选择招标代理机构的方式",
         note="原文「任何单位和个人不得以任何方式为招标人指定…」；「不得」距 span 起点 5 字，"
              "引用后半句自带「不得」故 q_neg 非空，前半句的禁止被整段剥掉"),
    dict(id="A2", group="列举式禁止条款", expect="强拦住",
         chunk_id="905f75d2364cb944-0002",
         quote="将供应商的注册资本、资产总额、营业收入、从业人员、利润、纳税额等规模条件和财务指标作为供应商资格要求或者评审因素",
         note="原文「…重点避免出现以下行为：（ 1 ）将供应商的注册资本…」；"
              "「（1）」全被 _CLAUSE_SEP 吃掉 → _clause_tail 为空；sentence_negation=True 但不进 risk_flags"),
    dict(id="A3", group="例外前置被丢弃", expect="强拦住",
         chunk_id="bca6ad8ffc96bb03-0001",
         quote="不得向小微企业收取贷款承诺费、资金管理费",
         note="原文「除银团贷款外，不得向…」；before 窗口「款外，」被逗号切断 → 有例外变绝对禁止"),
    dict(id="A4", group="条件/范围被丢弃", expect="强拦住",
         chunk_id="73141438cd19770b-0000",
         quote="各部门原则上不得要求办事群众出具",
         note="原文「对未列入清单的证明事项，各部门原则上不得…」；清单外事项变全域"),
    dict(id="A5", group="数量被截断（悬空比较句）", expect="强拦住",
         chunk_id="7d5768d519fad331-0000",
         quote="登记内容和记录备份保存时间不得少于",
         note="原文「…不得少于60日」；引用停在「不得少于」，after 窗口「60日」无否定词，"
              "模型可任意补数字（60日→6个月）"),
    dict(id="A8", group="限定词「原则上」丢弃", expect="强拦住",
         chunk_id="d3471bf198d8ee18-0002",
         quote="不再依据经营范围核定企业申领的发票种类",
         note="原文「原则上不再依据…」；before 窗口「原则上」无否定词 → 原则性要求变绝对"),
    dict(id="H1", group="限定词「一般」丢弃", expect="强拦住",
         chunk_id="e9017669859226c4-0003",
         quote="不得设置企业或项目负责人业绩、奖项",
         note="原文「一般不得设置…」；ctx_before='目一般' → 「一般不得」变「不得」"),
    dict(id="B3", group="主体范围消失", expect="强拦住",
         chunk_id="ce21586507469418-0000",
         quote="地方行政区划名称、行业或者经营特点不得作为字号",
         note="原文「县级以上地方行政区划名称…不得作为字号」；ctx_before='级以上' → 县级以上变全部"),
    dict(id="O1", group="例外后置被丢弃", expect="强拦住",
         chunk_id="ce21586507469418-0000",
         quote="县级以上地方行政区划名称、行业或者经营特点不得作为字号",
         note="原文「…不得作为字号，另有含义的除外」；after 窗口「，另有」被逗号切断，"
              "且「除外」不在 _NEG_ONLY → 例外消失"),
    dict(id="O2", group="例外前置被丢弃（法律除外）", expect="强拦住",
         chunk_id="5ca5fa49f2fd0178-0008",
         quote="不得设立对自贸试验区企业的考核、检查和评比项目",
         note="原文「除法律、行政法规或者国务院规定以及本条例规定的以外，不得设立…」；"
              "ctx_before='以外，' → 法律除外条款消失"),
    dict(id="B1", group="主体层级消失（各级）", expect="强拦住",
         chunk_id="7fa70830d5f471ce-0000",
         quote="人民防空主管部门不得设置任何限制条件",
         note="原文「各级人民防空主管部门不得…」；去「各级」"),
    dict(id="B2", group="主体层级消失（各级）", expect="强拦住",
         chunk_id="cb543ed0ae800853-0001",
         quote="国土资源部门不得法外设定权力，没有法律法规依据的不得作出减损公民、法人和其他组织合法权益或增加其义务的决定",
         note="原文「各级国土资源部门不得法外设定权力…」；去「各级」"),

    # ============ R2：reversed_risk 用集合存在性判断 → 洗白 ============
    dict(id="A6", group="否定词洗白（无关「严禁」顶包）", expect="强拦住",
         chunk_id="b47d32a67463ff31-0003",
         quote="严禁指定公章制作单位制作公章……要求企业前往公安机关办理公章刻制备案",
         note="原文「严禁指定公章制作单位制作公章，严禁要求企业前往…」。gap 里被吃掉的正是第二个「严禁」，"
              "但引用自带第一个「严禁」（指向别的事物）→ q_neg 非空 → reversed_risk=False"),
    dict(id="A6c", group="A6 对照组：去掉无关「严禁」", expect="强拦住", role="control",
         chunk_id="b47d32a67463ff31-0003",
         quote="指定公章制作单位制作公章……要求企业前往公安机关办理公章刻制备案",
         note="同一 gap、同一目标句，仅少一个指向别的事物「严禁」→ 立刻被拦住。"
              "证明闸门可被引用里任意否定词开关"),
    dict(id="K1", group="否定词洗白（单字「不」）", expect="强拦住",
         chunk_id="af3a20135b7967a1-0005",
         quote="金融机构贷款附加不合理条件",
         note="原文「严禁金融机构贷款附加不合理条件」。ctx_before='，严禁' —— 否定词就在 3 字窗口内，"
              "却因引用里「不合理」的「不」被记入 q_neg 而整条放行"),
    dict(id="K2", group="K1 对照组：无「不」的目标句", expect="强拦住", role="control",
         chunk_id="b47d32a67463ff31-0003",
         quote="要求企业前往公安机关办理公章刻制备案",
         note="ctx_before='，严禁' 且引用无任何否定字 → 正确拦住。与 K1 对照说明「不」是开关"),

    # ============ R3：_NEG_ONLY 缺「无需」============
    dict(id="G1", group="否定词表缺「无需」（紧邻）", expect="强拦住",
         chunk_id="a1b2b4850ac8283f-0000",
         quote="至工商窗口即可完成工商登记",
         note="原文「申请人申请无介质的电子营业执照，无需至工商窗口即可完成工商登记」。"
              "ctx_before='，无需' —— 否定词完整落在窗口内，negation_words 仍为 []（「无需」不在 _NEG_ONLY）"),
    dict(id="G2", group="否定词表缺「无需」（落在 gap）", expect="强拦住",
         chunk_id="f7ec89418a8d89fa-0048",
         quote="在年度纳税申报及享受优惠事项前……履行备案手续、报送《企业所得税优惠事项备案表》",
         note="omitted='无需再' —— 被省略区间字面就是「无需再」，omitted_negation=False。"
              "「无需履行」被引成「履行」，义务方向反转"),
    dict(id="G3", group="G1 对照组：语料里是「无需」不是「不得」", expect="强拦住", role="control", mutated=True,
         chunk_id="a1b2b4850ac8283f-0000",
         quote="不得至工商窗口即可完成工商登记",
         note="换成「不得」后 0 命中，反证原文确为「无需」"),

    # ============ 强拦住（已验证有效）============
    dict(id="C1", group="形近字 权力→权利", expect="强拦住", mutated=True,
         chunk_id="7acfbda868979510-0004",
         quote="滥用行政权利制定含有排除、限定竞争内容的规定",
         note="0 命中；靠「查不到」拦住，不是语义判定"),
    dict(id="C1c", group="C1 对照组：原文", expect="强拦住", role="control",
         chunk_id="7acfbda868979510-0004",
         quote="滥用行政权力制定含有排除、限定竞争内容的规定",
         note="原文可命中；因 before 窗口含「不得」被判 omitted_negation（该判定本身正确）。"
              "同时说明：server 层 verified 与 partial 互斥，不存在「verified=True 但带 flags」的中间态"),
    dict(id="C2", group="形近字 制定→制订", expect="强拦住", mutated=True,
         chunk_id="7acfbda868979510-0004",
         quote="不得滥用行政权力制订含有排除、限定竞争内容的规定",
         note="0 命中"),
    dict(id="C2c", group="C2 对照组：原文", expect="绕过", role="control",
         chunk_id="7acfbda868979510-0004",
         quote="不得滥用行政权力制定含有排除、限定竞争内容的规定",
         note="原文 verified=True（引用自带「不得」，未改变语义，正确）"),
    dict(id="D3", group="重复 5 次", expect="强拦住",
         chunk_id="", quote="优化营商环境" * 5,
         note="0 命中"),
    dict(id="E1", group="省略号变体 。。。", expect="强拦住",
         chunk_id="", quote="借乡村振兴之名。。。违法违规变相举债",
         note="normalize 成 ... 后与 …… 等价，判 omitted_negation（与 §13 路径 3 同判）"),
    dict(id="E2", group="省略号变体 ⋯⋯", expect="强拦住",
         chunk_id="", quote="借乡村振兴之名⋯⋯违法违规变相举债",
         note="同上"),
    dict(id="E3", group="省略号变体 ..（两点）", expect="强拦住",
         chunk_id="", quote="借乡村振兴之名..违法违规变相举债",
         note="0 命中（失败关闭，但原因是查不到）"),
    dict(id="F1", group="分号紧邻否定", expect="强拦住",
         chunk_id="", quote="强制或者变相强制市场主体接受中介服务",
         note="ctx_before='；不得'，_clause_tail 保住「不得」→ 拦住"),
    dict(id="I2", group="真跨块（两段在 15000 块中 co-occur=0）", expect="强拦住",
         chunk_id="", quote="实行注册资本认缴登记制……人防工程施工图设计文件审查资格审批",
         note="0 命中"),
    dict(id="L1", group="时间被截（自……起施行）", expect="强拦住", mutated=True,
         chunk_id="", quote="自……起施行，有效期5年",
         note="短段被丢弃 → unverified_segment"),
    dict(id="M1", group="可以→应当", expect="强拦住", mutated=True,
         chunk_id="", quote="应当采取禁止进入或限制市场主体资质、股权比例、经营范围、经营业态、商业模式、空间布局、国土空间开发保护等管理措施",
         note="0 命中"),
    dict(id="N1", group="跳词（排除、限定→排除）", expect="强拦住", mutated=True,
         chunk_id="", quote="不得滥用行政权力制定含有排除竞争内容的规定",
         note="0 命中"),
    dict(id="J2", group="逆序拼接", expect="强拦住",
         chunk_id="", quote="破坏营商环境……优化营商环境",
         note="命中 19 个「破坏在前」的正序块 → long_omission + omitted_negation。"
              "注意：这不是干净逆序样本，语料里存在正序块"),
]


def synthetic_english() -> None:
    """英文否定词覆盖测试。

    真实语料里 `shall\\s+not|may\\s+not|must\\s+not|prohibit` 命中 0 条，
    因此这里用【合成语料】证明机制盲区，不声称真实语料已被攻破。
    """
    import gzip
    import json
    import tempfile

    print("\n" + "=" * 100)
    print("附：英文否定词（SYNTHETIC 合成语料，真实语料 0 条英文禁止条款）")
    print("=" * 100)
    rows = [
        {"chunk_id": "synth-en-0000", "doc_id": "synth",
         "text": "Article 5. The authority shall not impose any additional conditions on market entities. "
                 "Article 6. Market entities may apply for registration online.", "title": "synthetic"},
        {"chunk_id": "synth-en-0001", "doc_id": "synth",
         "text": "Article 7. No department may charge fees for such services. Article 8. The list shall be published.",
         "title": "synthetic"},
    ]
    with tempfile.NamedTemporaryFile("wt", suffix=".jsonl.gz", delete=False, encoding="utf-8") as fh:
        path = fh.name
        with gzip.open(path, "wt", encoding="utf-8") as gz:
            for r in rows:
                gz.write(json.dumps(r, ensure_ascii=False) + "\n")
    C = Corpus(path=path)
    pairs = [
        ("shall not 被丢弃", "impose any additional conditions on market entities"),
        ("原文（对照）", "shall not impose any additional conditions on market entities"),
        ("No department may 被丢弃", "charge fees for such services"),
        ("原文（对照）", "No department may charge fees for such services"),
    ]
    for tag, q in pairs:
        r = C.verify_report(q)
        h = r.hits[0] if r.hits else {}
        print(f"  [{tag:<26}] q={q[:58]!r}")
        print(f"    matched={r.matched} omitted_negation={r.omitted_negation} "
              f"negation_words={r.negation_words} ctx_before={h.get('context_before')!r}")


if __name__ == "__main__":
    rc = main()
    synthetic_english()
    raise SystemExit(rc)
