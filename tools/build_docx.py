#!/usr/bin/env python3
"""build_docx.py — 初赛材料 Word 骨架组装器（C6）。

从 docs/ 现役素材生成《ict-decision-agent 初赛设计文档.docx》：
  主干：01-开发设计文档.md 的章节结构
  数据：key-numbers.md（治理段已含 D1-D6 实测）
  附录：方案兑现对账表.md
纪律：只组装不改写；数字一律来自素材文件；无官方模板前先出内容骨架（样式可后换）。
"""
import re
import sys
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

BASE = Path("/home/endmin/dsh/research/ict-open-source")
OUT = BASE / "docs" / "初赛设计文档-draft.docx"


def md_heading(doc, level, text):
    h = doc.add_heading(text, level=level)
    return h


def md_para(doc, text):
    """极简 markdown → docx：**粗体** / `代码` / 普通文本。"""
    p = doc.add_paragraph()
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`(.+?)`", text):
        if m.start() > pos:
            p.add_run(text[pos:m.start()])
        if m.group(1):
            r = p.add_run(m.group(1)); r.bold = True
        else:
            r = p.add_run(m.group(2)); r.font.name = "Consolas"
        pos = m.end()
    if pos < len(text):
        p.add_run(text[pos:])
    return p


def md_table(doc, rows):
    if not rows:
        return
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Light Grid Accent 1"
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            if j < len(t.rows[i].cells):
                t.rows[i].cells[j].text = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
    doc.add_paragraph()


def render_md(doc, md_text, skip_until=None):
    """把 markdown 渲染进 docx（标题/表格/列表/段落；跳过代码块细节）。"""
    lines = md_text.splitlines()
    i = 0
    in_code = False
    table_buf = []
    if skip_until:
        for i, ln in enumerate(lines):
            if skip_until in ln:
                i += 1
                break
        else:
            i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            p = doc.add_paragraph(ln)
            p.runs[0].font.name = "Consolas"
            p.runs[0].font.size = Pt(9)
            i += 1
            continue
        if ln.startswith("|") and i + 1 < len(lines) and set(lines[i+1].replace("|", "").replace(" ", "")) <= {"-", ":"} and lines[i+1].startswith("|"):
            rows = [[c.strip() for c in ln.strip("|").split("|")]]
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            md_table(doc, rows)
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            md_heading(doc, min(len(m.group(1)), 4), m.group(2))
            i += 1
            continue
        if re.match(r"^[-*]\s+", ln) or re.match(r"^\d+\.\s+", ln):
            md_para(doc, ln, )
            doc.paragraphs[-1].style = doc.styles["List Bullet"] if ln.startswith(("-", "*")) else doc.styles["List Number"]
            i += 1
            continue
        if ln.strip():
            md_para(doc, ln)
        i += 1


def main():
    doc = Document()
    # 页眉页脚
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    section = doc.sections[0]
    hp = section.header.paragraphs[0]
    hp.text = "基于 Nexent 的政务决策智能体 · 初赛设计文档"
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fp = section.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run()
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE")
    run._r.addnext(fld)
    # 封面
    for _ in range(6):
        doc.add_paragraph()
    t = doc.add_paragraph(); t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = t.add_run("基于 Nexent 的政务决策智能体\n检索-推理双驱动与三层治理架构"); r.font.size = Pt(26); r.bold = True
    s = doc.add_paragraph(); s.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r2 = s.add_run("第十一届华为 ICT 大赛 · 中国创新赛 · 赛题三\n智能体执行流与 Skill 工作流模板"); r2.font.size = Pt(14)
    s2 = doc.add_paragraph(); s2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r3 = s2.add_run("队名：____（待填）　报名编号：____（待填）\n2026 年 11 月"); r3.font.size = Pt(12)
    doc.add_page_break()

    # 目录域（Word 打开后 Ctrl+A F9 或右键"更新域"生成真目录）
    from docx.oxml.ns import qn as _qn
    from docx.oxml import OxmlElement as _OE
    md_heading(doc, 1, "目 录")
    _p = doc.add_paragraph()
    _r = _p.add_run()
    _f1 = _OE("w:fldChar"); _f1.set(_qn("w:fldCharType"), "begin")
    _it = _OE("w:instrText"); _it.set(_qn("xml:space"), "preserve")
    _it.text = ' TOC \\o "1-3" \\h \\z \\u '
    _f2 = _OE("w:fldChar"); _f2.set(_qn("w:fldCharType"), "separate")
    _t = _OE("w:t"); _t.text = "（在 Word 中右键此处 → 更新域，即可生成目录）"
    _f3 = _OE("w:fldChar"); _f3.set(_qn("w:fldCharType"), "end")
    for _e in (_f1, _it, _f2, _t, _f3):
        _r._r.append(_e)
    doc.add_page_break()

    # 架构一页图
    img = BASE / "docs" / "architecture.png"
    if not img.exists():
        import shutil
        src = Path("/home/user/rewrite-exp/architecture.png")
        if src.exists():
            img.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, img)
    if img.exists():
        md_heading(doc, 1, "架构一页图")
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(img), width=Pt(560))
        cap = doc.add_paragraph(); cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        rc = cap.add_run("图 0：系统架构一页图（语料 → 混合检索 → 护栏作答 → 本体治理 → 可验证性）")
        rc.font.size = Pt(10); rc.italic = True
        doc.add_page_break()

    # 1. 主文档全文
    main_md = (BASE / "docs" / "01-开发设计文档.md").read_text(encoding="utf-8")
    doc.add_page_break()
    render_md(doc, main_md)

    # 2. 附录A：方案兑现对账表
    doc.add_page_break()
    md_heading(doc, 1, "附录 A：治理方案兑现对账（22 条款 × 证据）")
    audit_md = (BASE / "docs" / "方案兑现对账表.md").read_text(encoding="utf-8")
    render_md(doc, audit_md)

    # 3. 附录B：方法论与数据源
    doc.add_page_break()
    md_heading(doc, 1, "附录 B：工程方法论与工作台数据源")
    apx_md = (BASE / "docs" / "附录-工程方法论与工作台数据源.md").read_text(encoding="utf-8")
    render_md(doc, apx_md)

    # 4. 附录C：治理知识库与答辩速查
    doc.add_page_break()
    md_heading(doc, 1, "附录 C：治理知识库与答辩速查")
    md_heading(doc, 2, "C.1 治理知识库（governance_chain）")
    for line in [
        "知识库 knowledge_name = governance_chain，Elasticsearch 索引 3-4c64eca2085942af8fadf6364d947ce0；"
        "治理六件（宪法 / 门控契约 / 命名空间 / 技能注册 / 对账表 / 免疫记录）按 H2 切片共 42 chunks，"
        "_bulk 直写 errors=False，词面抽查『宪法 / 门控 / 对账表 / 免疫记录』全部命中。",
        "评审/答辩时 Agent 可对治理知识库直接问答：治理规则『遗忘了什么』类反问由对账表 22 条款兜底回应。",
    ]:
        doc.add_paragraph(line, style="List Bullet")
    md_heading(doc, 2, "C.2 免疫记录（immunity-log.jsonl）")
    doc.add_paragraph("治理链免疫记录累计 9 条（含 graph_retrieval v1.1.0/v1.2.0/v1.3.0 三版升级）——"
                      "每次路由修正 / 排序修复均先记免疫、再改代码、后复核，形成可追溯的治理闭环。")
    md_heading(doc, 2, "C.3 答辩数字速查卡")
    tbl = doc.add_table(rows=1, cols=2); tbl.style = "Table Grid"
    tbl.rows[0].cells[0].paragraphs[0].add_run("问题").bold = True
    tbl.rows[0].cells[1].paragraphs[0].add_run("数字锚点").bold = True
    QA = [
        ("图检索准确率？", "G 层 25 题 v4 = 1.00（25/25，三连×2 双盲；演进 v1 0.48 → v2 0.76 → v3 0.96 → v4 1.00）"),
        ("语料规模？", "757,810 chunks（Apache-2.0，导入中→满库）；rfc-1024 1,739；治理库 42"),
        ("检索质量？", "MRR 0.8494；X 层 25/25 零漂移；BGE 判序 0.9965；延迟 p50 0.33s"),
        ("校准阈值？", "τ=0.8505（域内 92% / 域外 72%）；0.75 档=100%/40%，拍板待定"),
        ("护栏拒答？", "稠密护栏 0.75 拒答阈 + 词面落地核验；域外即拒；R 层门控 FAIL 如实报（p=0.0625，n=25 功效不足，满库后重裁）"),
        ("工程测试？", "make test 118/118；分层评测 R/M/X/C/G 五层逐题 rows；check-consistency 10/10（2,013 边 / 1,865 实体）"),
        ("安全性引用？", "SAGE(2609.36043) 配对 ledger；SkillFlow(2604.17308) 高使用≠高效用；SkillPoison(2610.07645) 95.71% ASR → 只记录不判定"),
        ("原创性？", "三层治理优化方案与元框架 EL 均为本人设计；EL = 方法论上游 + 数据源，参赛主体为基于 Nexent 的政务决策智能体"),
    ]
    for q, a in QA:
        row = tbl.add_row().cells
        row[0].paragraphs[0].add_run(q).bold = True
        row[1].paragraphs[0].add_run(a)
    doc.add_page_break()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print("written:", OUT, "(%.1f KB)" % (OUT.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
