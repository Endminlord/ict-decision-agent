#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ICT 参赛 PPT 生成脚本（12 页 · Build Mode）。

主题来源：ppt_tasks/ict-deck/resolved-theme.json（Theme Lock v1）
设计记录：brief.md / page-plan.md / visual-direction.md
"""
import json
import os

from pptx_designer import Presentation
from pptx_designer.renderer.theme_context import resolve_color_context
from pptx_designer.tools.charts import bar_chart
from pptx_designer.tools.cards import kpi_card
from pptx_designer.tools.layout import page_header, page_number
from pptx_designer.tools.shapes import arrow, rect, rrect
from pptx_designer.tools.text import text
from pptx.util import Inches

TASK_DIR = "/home/endmin/dsh/research/ppt_tasks/ict-deck"
MATERIAL = "/home/endmin/dsh/research/ict-material-20261010"
OUT = os.path.join(TASK_DIR, "ict-deck.pptx")

theme = json.load(open(os.path.join(TASK_DIR, "resolved-theme.json"), encoding="utf-8"))
prs = Presentation(theme=theme, strict_theme=True)

# 从当前 resolved theme 派生的语义色（正例/负例/强调），仅用于页级语义区分
C = dict(resolve_color_context(prs))
C.update({"pos": "#1F7A5A", "neg": "#A33A2A", "emph": "#C77D0A"})

MARGIN_L = 0.65
BODY_W = 12.03
TOTAL_PAGES = 12


def blank():
    return prs.slides.add_slide(prs.slide_layouts[6])


def table(slide, left, top, width, rows, col_ratios=None, row_h=0.40,
          font_size=10, head_size=10.5):
    """手绘细线表格：rows[0] 为表头。返回底边 y。"""
    ncol = len(rows[0])
    if col_ratios is None:
        col_ratios = [1.0 / ncol] * ncol
    widths = [width * r for r in col_ratios]
    y = top
    for i, row in enumerate(rows):
        x = left
        for j, cell in enumerate(row):
            if i == 0:
                rect(slide, x, y, widths[j], row_h, C["primary"], C=C)
                text(slide, x + 0.07, y + 0.055, widths[j] - 0.14, row_h - 0.11,
                     str(cell), font_size=head_size, color="#FFFFFF", bold=True, C=C)
            else:
                if i % 2 == 1:
                    rect(slide, x, y, widths[j], row_h, C["surface"], C=C)
                text(slide, x + 0.07, y + 0.055, widths[j] - 0.14, row_h - 0.11,
                     str(cell), font_size=font_size, color="text_body", C=C)
            x += widths[j]
        y += row_h
    return y


def bullets(slide, left, top, width, items, font_size=12, gap=0.52, color="text_body",
            bullet_color=None):
    """带方点符号的要点列。items: list[str]"""
    bc = bullet_color or C["accent"]
    y = top
    for it in items:
        rect(slide, left, y + 0.10, 0.09, 0.09, bc, C=C)
        text(slide, left + 0.24, y, width - 0.24, gap, it, font_size=font_size,
             color=color, C=C)
        y += gap
    return y


def stat(slide, left, top, width, number, unit, label, num_size=40, color=None):
    """大数字 + 单位 + 说明。"""
    nc = color or C["emph"]
    text(slide, left, top, width, 0.72, number, font_size=num_size, color=nc,
         bold=True, C=C)
    if unit:
        text(slide, left, top + 0.70, width, 0.30, unit, font_size=13,
             color="text_muted", C=C)
    text(slide, left, top + 1.02, width, 0.60, label, font_size=11.5,
         color="text_body", C=C)


def footer(slide, n):
    page_number(slide, n, TOTAL_PAGES, C=C)


# ───────────────────────── 1 封面 ─────────────────────────
s = blank()
rect(s, 0, 0, 13.333, 0.16, C["primary"], C=C)
text(s, MARGIN_L, 1.55, 11.5, 0.5,
     "第十一届华为 ICT 大赛 · 中国创新赛 · 赛题 3", font_size=14, color="text_muted", C=C)
text(s, MARGIN_L, 2.15, 11.9, 1.5,
     "让每条决策依据\n都能回查原文出处", font_size=42, color="text_dark", bold=True, C=C)
rect(s, MARGIN_L, 4.30, 2.2, 0.05, C["emph"], C=C)
text(s, MARGIN_L, 4.62, 11.5, 0.9,
     "基于 ModelEngine Nexent 打造可进化决策智能体", font_size=19, color="text_body", C=C)
text(s, MARGIN_L, 6.35, 11.5, 0.6,
     "政务公开语料 757,810 条 · 本体三层已实现 · 换行业零改码（已实测）",
     font_size=12.5, color="text_muted", C=C)

# ───────────────────────── 2 痛点 ─────────────────────────
s = blank()
page_header(s, "问题：决策依据散在数十万份文件里", "人工梳理「谁引用了谁」不可行", C=C)
stat(s, 0.85, 2.40, 3.6, "757,810", "条政务公开记录", "覆盖法规、规章、公文、媒体报道等多类来源")
stat(s, 4.85, 2.40, 3.6, "15.7", "亿字符正文（4.18 GB）", "清洗后 692,698 条，切分为 1,864,451 块")
stat(s, 8.85, 2.40, 3.6, "?", "跨层级引用关系", "省级条例 → 国家文件 → 市级落实，链条长且分散")
rect(s, 0.65, 5.05, 12.03, 0.02, C["border"], C=C)
text(s, 0.85, 5.45, 11.6, 1.2,
     "真正的难点不是「检索不到」，而是检索到之后无法确认依据是否成立：\n"
     "哪一条是上位文件？哪一条只是正文里提到了文件名？哪一条已经被废止？",
     font_size=13.5, color="text_body", C=C)
footer(s, 2)

# ───────────────────────── 3 答案总览 ─────────────────────────
s = blank()
page_header(s, "我们的答案", "2 套自建 MCP + 2 个自建 Skill + 本体三层", C=C)
s.shapes.add_picture(os.path.join(MATERIAL, "figures/nexent-callgraph.png"),
                     left=Inches(MARGIN_L),
                     top=Inches(1.72),
                     width=Inches(6.6))
bullets(s, 8.85, 1.85, 3.85, [
    "宿主侧自建 2 套 MCP（6 个工具），经 Nexent MCP 网关注册",
    "2 个自建 Skill 定义编排流程与拒答闸门",
    "本体三层：规则层召回 → 推理层连链 → 模型判定层判真假",
    "回答逐条给出 chunk_id，可回原文核对",
], font_size=11.5, gap=1.05)
footer(s, 3)

# ───────────────────────── 4 正例 ─────────────────────────
s = blank()
page_header(s, "它做对了什么：跨层级链条一次答出", "每条依据都带可回查的出处锚", C=C)
s.shapes.add_picture(os.path.join(MATERIAL, "evidence/09-ui-qa-cross-doc.png"),
                     left=Inches(MARGIN_L),
                     top=Inches(1.60),
                     width=Inches(7.5))
bullets(s, 8.45, 1.80, 4.25, [
    "链条：河北省条例 2017 → 国办函〔2018〕46 号 → 国办函〔2019〕89 号 → 常州钟楼区人大决定 2020 → 国办发〔2022〕35 号",
    "耗时 73.44 s，答案含引用块清单",
    "每条引用均带 chunk_id，可回原文",
    "自述证据边界，不外推",
], font_size=11, gap=1.15)
footer(s, 4)

# ───────────────────────── 5 负例 ─────────────────────────
s = blank()
page_header(s, "它什么时候不说：拒答闸门", "证据不足时明确拒答，而不是编造", C=C)
s.shapes.add_picture(os.path.join(MATERIAL, "evidence/10-ui-qa-insufficient.png"),
                     left=Inches(MARGIN_L),
                     top=Inches(1.60),
                     width=Inches(6.9))
table(s, 7.85, 1.75, 4.85, [
    ["判定项", "值"],
    ["insufficient_evidence", "true"],
    ["max_score", "0.7349"],
    ["阈值", "0.75"],
    ["grounded_rings", "0"],
    ["系统动作", "拒答 + 给可改问方向"],
], col_ratios=[0.58, 0.42], row_h=0.46, font_size=10.5)
text(s, 7.85, 4.90, 4.85, 1.2,
     "问「量子计算芯片的光刻胶配方工艺参数」→\n回答「语料库中没有找到足够依据」。",
     font_size=11.5, color="neg", C=C)
footer(s, 5)

# ───────────────────────── 6 编排深度 ─────────────────────────
s = blank()
page_header(s, "编排深度：2 套 MCP（6 工具）+ 2 个 Skill", "工具与流程都由本项目自建", C=C)
table(s, MARGIN_L, 1.70, 12.03, [
    ["层", "名称", "能力", "实测"],
    ["MCP", "工作台 MCP（:8793）", "workbench_task_status / workbench_recent_commits / workbench_run_demo", "真实调用成功，rc=0"],
    ["MCP", "检索 MCP（:8793）", "retrieval_cross_doc / retrieval_trace_source / retrieval_corpus_stats", "跨文档检索 + 逐环回查"],
    ["Skill", "workbench-status-report", "生成工作台状态简报（4 次工具调用）", "13.81 s / 真实调用 3 个工具"],
    ["Skill", "governance-policy-trace", "政策溯源：检索 → 拒答闸门 → 逐环回查 → 成文", "50.43 s / 5 条 chunk_id"],
], col_ratios=[0.08, 0.22, 0.48, 0.22], row_h=0.62, font_size=10)
text(s, MARGIN_L, 4.95, 12.03, 0.9,
     "端到端实测：智能体 17 次工具调用 / 54.3 s / 答案 7,525 字符；技能驱动的文件闭环写 43 字节并读回一致。",
     font_size=12, color="text_body", C=C)
footer(s, 6)

# ───────────────────────── 7 本体三层 ─────────────────────────
s = blank()
page_header(s, "本体三层：已实现，不是设计稿", "规则层召回 → 推理层连链 → 模型判定层判真假", C=C)
boxes = [
    ("规则层", "行业词表驱动的三元组抽取\n+ 规范名归并", "188,423 条三元组\n出处失败 0"),
    ("推理层", "引用类谓词连成有向图\n+ 有界传递闭包", "强证据层 2,827 节点 / 7,009 边\n（含候选层 14,035 / 148,005）"),
    ("模型判定层", "LLM 逐条判定候选是否为真文件\n（零温度 + 缓存 + 入库校验）", "subject 1,762 真文件\nobject 6,890 真文件"),
]
x = 0.75
for i, (name, desc, num) in enumerate(boxes):
    rrect(s, x, 2.15, 3.55, 3.05, C["surface"], C=C)
    rect(s, x, 2.15, 3.55, 0.09, C["primary"], C=C)
    text(s, x + 0.25, 2.42, 3.05, 0.45, name, font_size=17, color="text_dark", bold=True, C=C)
    text(s, x + 0.25, 3.02, 3.05, 1.0, desc, font_size=11.5, color="text_body", C=C)
    text(s, x + 0.25, 4.28, 3.05, 0.7, num, font_size=11.5, color=C["emph"], C=C)
    if i < 2:
        arrow(s, x + 3.62, 3.55, 0.55, 0.30, C["accent"], C=C)
    x += 4.17
text(s, 0.75, 5.55, 11.9, 1.0,
     "人工闸门兜底：schema.json 的 review_gate 规定只有 high 置信度可自动入库，medium/low 须人工确认（本轮 0 条入库）。",
     font_size=12, color="text_body", C=C)
footer(s, 7)

# ───────────────────────── 8 规则层 + 推理层 ─────────────────────────
s = blank()
page_header(s, "规则层与推理层：抽了多少、连了多深", "60,000 块语料 → 18.8 万条三元组 → 引用图", C=C)
stat(s, 0.80, 1.90, 3.5, "188,423", "条三元组（出处失败 0）", "来自 60,000 块政务语料 · 强证据层 1.3 万条可自动入库", num_size=34)
stat(s, 4.60, 1.90, 3.5, "116,958", "个规范实体", "Document 105,182 / Organization 11,645 等 5 类", num_size=34)
stat(s, 8.40, 1.90, 3.9, "7,009", "条引用边 / 2,827 节点", "强证据层；含候选层时 148,005 边", num_size=34)
text(s, 0.80, 4.05, 5.4, 0.4, "谓词分布（前 5 / 共 8 类，全量口径）", font_size=13, color="text_dark", bold=True, C=C)
_max = 178099.0
bar_chart(s, 1.75, 4.55, [
    ("cites 引用", 1.0, "141,002"),
    ("issued_by 发布", 28060 / _max, "19,961"),
    ("implements 实施", 14963 / _max, "12,271"),
    ("mentions 提及", 9470 / _max, "5,503"),
    ("amends 修订", 8273 / _max, "6,389"),
], max_width=3.85, bar_height=0.30, C=C)   # 3.85：给右侧数值标签留出到 x≈6.7 的空间，不与 x=6.95 的文字块相撞
text(s, 6.95, 4.30, 5.7, 2.0,
     "推理层做了什么：\n"
     "· 只取 4 类引用谓词（cites / implements / amends / repeals），排除 mentions 噪声\n"
     "· 节点名规范化后去重、去自环\n"
     "· 有界传递闭包（最大 4 跳），判据 R1–R4 全过",
     font_size=11.5, color="text_body", C=C)
footer(s, 8)

# ───────────────────────── 9 判定层 ─────────────────────────
s = blank()
page_header(s, "模型判定层：把「正文提到」与「真引用」分开", "双侧过滤后才敢说这是文件→文件引用图", C=C)
funnel = [
    (11.4, "候选 45,992 个", "subject 14,058 + object 31,934", C["primary"]),
    (8.6, "判为真文件 8,652 个", "subject 1,762（12.5%）· object 6,890（21.6%）", C["accent"]),
    (5.8, "双侧都清 → 15,870 条边", "8,532 个节点 · 文件图 1,661 节点 / 9,167 边", C["pos"]),
]
y = 1.95
for w, title, sub, col in funnel:
    rrect(s, 0.80, y, w, 0.80, col, C=C)
    text(s, 1.05, y + 0.13, w - 0.5, 0.34, title, font_size=13.5, color="#FFFFFF", bold=True, C=C)
    text(s, 1.05, y + 0.48, w - 0.5, 0.26, sub, font_size=10.5, color="#FFFFFF", C=C)
    y += 1.06
rect(s, 0.80, 5.30, 11.9, 0.02, C["border"], C=C)
text(s, 0.80, 5.55, 11.9, 1.4,
     "一处必须写明的修正：早期用「各 400 个候选」抽样得出「双侧都清 0 条边」，并据此推断「政务文件之间的引用链很稀疏」。\n"
     "全量判定后发现那是抽样错在层 —— 400 个 subject 候选里只有 6 个是真文件。真实数字是 15,870 条。",
     font_size=12, color="text_body", C=C)
footer(s, 9)

# ───────────────────────── 10 迁移 ─────────────────────────
s = blank()
page_header(s, "换行业会怎样：零改码迁移（已实测）", "政务 → 先进制造，四项指标全部达标", C=C)
table(s, MARGIN_L, 1.75, 12.03, [
    ["指标", "结果", "说明"],
    ["L1 改动（MCP 服务）", "0 行", "mcp/workbench-mcp-server.py 逐字节未变"],
    ["L2 改动（Skill）", "0 行", "skills/*/SKILL.md 逐字节未变"],
    ["L3 改动（本体）", "0 行", "schema.json 与 build.py sha256 与基线一致"],
    ["脚本复用率", "5 / 5", "生成 → 抽取 → 归并 → 推理 全链路复用"],
    ["迁移工时", "约 8 分钟", "含合成语料编写；全链路执行 < 1 秒"],
], col_ratios=[0.26, 0.16, 0.58], row_h=0.52, font_size=11)
text(s, MARGIN_L, 5.15, 12.03, 1.2,
     "关键读数：同一批 25 块语料只换词表 —— 制造词表抽出 50 条三元组，政务词表只抽 22 条（差 2.3 倍）。\n"
     "结论：换行业必须换词表，而且只需要换词表。",
     font_size=12.5, color="text_dark", C=C)
footer(s, 10)

# ───────────────────────── 11 能力沉淀 ─────────────────────────
s = blank()
page_header(s, "能力沉淀：每条结论都能复跑", "不是一次性 demo —— 判据与命令都在材料包里", C=C)
table(s, MARGIN_L, 1.72, 12.03, [
    ["判据类别", "结果", "复跑入口"],
    ["契约一致性（MCP ↔ Skill ↔ 文档）", "5 通过 / 0 失败", "tools/check-contract-doc.py"],
    ["契约负对照自测", "6 / 6", "同上 --self-test"],
    ["本体三层自测", "12/12 · 4/4 · 6/6", "ontology/{build,align,prune}.py --self-test"],
    ["语料链自测", "5/5 · 5/5 · 5/5 · 3/3", "tools/corpus-clean|chunk|select-subset|import-corpus.py"],
    ["检索效果评测", "政务 10/10 · RFC 15/15", "tools/eval-retrieval.py --domain gov|rfc"],
    ["引用回查 MCP", "96 条判据全过", "track2-mcp/tests/（见复核命令 X 段）"],
    ["全包完整性", "全部 OK（可逐条复跑）", "sha256sum -c MANIFEST.sha256"],
], col_ratios=[0.40, 0.24, 0.36], row_h=0.42, font_size=10.5)
text(s, MARGIN_L, 5.15, 12.03, 1.0,
     "复核命令.md 的 A–Z 段给出每条结论对应的单条命令与预期输出，评审可逐条验证。",
     font_size=12, color="text_body", C=C)
footer(s, 11)

# ───────────────────────── 12 边界 ─────────────────────────
s = blank()
page_header(s, "诚实边界与下一步", "哪些还没做、为什么", C=C)
left_items = [
    "本材料为单人备赛产出：团队成员、指导教师、队名与报名编号尚未确定",
    "官方参数未确认：初赛确切截止日、晋级名额、是否强制华为技术栈、是否需 PPT/视频",
    "先进制造语料通道断链：国标全文受版权、gov.cn 政策 API 匿名 0 结果、ModelScope 403",
    "迁移实测用的是合成语料 —— 验证的是机制，不是真实行业效果",
]
right_items = [
    "判定层用的是自备网关的外部模型，换模型结论可能变；强证据层 12,594 条自动入库，候选层 175,829 条零人工确认",
    "GPU 已落地到现役 embedding 服务（端到端 387 条/秒 = 26.9×；纯推理 635 条/秒 = 44×），但显存压力与多并发行为未测",
    "示例 7 的原始长问题复现不稳定（两次失败），材料中使用的是简化提问版本",
    "知识库全量导入按 batch 200 实测约 15.8 小时（1,864,451 块），但要占满写入窗口，故采用 15,494 块分层子集策略",
]
bullets(s, MARGIN_L, 1.75, 5.85, left_items, font_size=11.5, gap=1.02, bullet_color=C["neg"])
bullets(s, 6.85, 1.75, 5.85, right_items, font_size=11.5, gap=1.02, bullet_color=C["neg"])
rect(s, MARGIN_L, 5.95, 12.03, 0.02, C["border"], C=C)
text(s, MARGIN_L, 6.18, 12.03, 0.7,
     "下一步：知识库定全量筛选策略（15,494 块子集已入库）→ 人工确认闸门入库 → 第二行业真实语料迁移。",
     font_size=12.5, color="text_dark", C=C)
footer(s, 12)

prs.save(OUT)
print("saved", OUT, "slides =", len(prs.slides.__iter__.__self__._sldIdLst))
