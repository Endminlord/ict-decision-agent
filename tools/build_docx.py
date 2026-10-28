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

    # 目录占位
    md_heading(doc, 1, "目录")
    doc.add_paragraph("（Word 域目录：提交前 F9 刷新；骨架由 build_docx.py 生成）")

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

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print("written:", OUT, "(%.1f KB)" % (OUT.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
