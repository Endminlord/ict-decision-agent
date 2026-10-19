#!/usr/bin/env python3
"""多模态：从 PDF 抽取表格，转成可入库的 markdown 块。

赛题要求「对文本、表格、图文等多元行业数据统一识别」。政务文件里的表格
（申报表、任务单、清单、指标表）在纯文本清洗时会被压平成空格分隔的一串词，
表头与数据混在一起、行列关系丢失。本脚本保留行列关系。

用法：
    python3 tools/extract-tables.py --pdf <file.pdf> --out /tmp/tables.jsonl
    python3 tools/extract-tables.py --pdf <file.pdf> --out -        # 打到 stdout
    python3 tools/extract-tables.py --self-test

输出每行一个 JSON：
    {"table_id": "...", "page": 3, "title": "...", "n_rows": 5, "n_cols": 4,
     "markdown": "| a | b |\\n|---|---|\\n| 1 | 2 |", "text": "线性化文本（供检索）"}

设计取舍：
- 只用 pymupdf（本机已装 1.28.2），不引 pdfplumber（未装、且会拉一堆依赖）。
- `find_tables()` 是 pymupdf 1.23+ 的能力，基于矢量线/文本对齐；对无边框表格
  识别率低，所以额外提供 `--strategy text` 的启发式（按行内空格分列）。
- 表格块同时给 markdown（人看/LLM 看）与线性化 text（embedding 用）——
  纯 markdown 的 `|` 会稀释向量。
"""
import argparse
import json
import re
import sys


def _cell(v):
    """单元格规范化：去换行、压空格、转义竖线。"""
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s.replace("|", "\\|")


def to_markdown(rows):
    """二维数组 → markdown 表格。首行当表头。"""
    rows = [[_cell(c) for c in r] for r in rows if any(_cell(c) for c in r)]
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * n]
    for r in body:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def to_linear(rows):
    """二维数组 → 线性化文本（表头: 值; 表头: 值），供 embedding。"""
    rows = [[_cell(c) for c in r] for r in rows if any(_cell(c) for c in r)]
    if not rows:
        return ""
    head = rows[0]
    parts = []
    for r in rows[1:]:
        kv = [f"{head[i]}: {r[i]}" for i in range(min(len(head), len(r))) if r[i]]
        if kv:
            parts.append("；".join(kv))
    if not parts:  # 没有数据行，退化成逐行拼
        return " ".join(" ".join(r) for r in rows)
    return "。".join(parts)


def extract(pdf_path, strategy="lines", min_rows=2, min_cols=2):
    """返回表格块列表。strategy: lines（矢量线）| text（启发式）。"""
    import pymupdf

    blocks = []
    with pymupdf.open(pdf_path) as doc:
        for pno, page in enumerate(doc, start=1):
            found = []
            if strategy == "lines":
                try:
                    tf = page.find_tables()
                    for t in tf.tables:
                        data = t.extract()
                        if data and len(data) >= min_rows and max(len(r) for r in data) >= min_cols:
                            found.append(data)
                except Exception as e:  # 旧版或该页无表格结构
                    print(f"[warn] page {pno} find_tables 失败: {e}", file=sys.stderr)
            if strategy == "text" or (strategy == "lines" and not found):
                # 启发式：连续的「多空格分隔」行，且列数一致
                buf = []
                for line in page.get_text("text").splitlines():
                    cols = re.split(r"\s{2,}", line.strip())
                    if len(cols) >= min_cols:
                        buf.append(cols)
                    else:
                        if len(buf) >= min_rows:
                            found.append(buf)
                        buf = []
                if len(buf) >= min_rows:
                    found.append(buf)

            for k, data in enumerate(found):
                md = to_markdown(data)
                if not md:
                    continue
                # 表格标题：取该表格上方最近的一行短文本（若能定位）
                title = ""
                blocks.append({
                    "table_id": f"p{pno:03d}-t{k:02d}",
                    "page": pno,
                    "title": title,
                    "n_rows": len(data),
                    "n_cols": max(len(r) for r in data),
                    "markdown": md,
                    "text": to_linear(data),
                    "source_pdf": pdf_path,
                })
    return blocks


def self_test():
    """用一张内存里生成的 PDF 验证抽取链路。"""
    import pymupdf

    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            fail += 1
            print(f"  ✗ {name} {detail}")

    # 1) markdown 生成
    md = to_markdown([["项目", "代码"], ["甲", "001"], ["乙", "002"]])
    check("markdown 有表头分隔行", "|---|" in md, md)
    check("markdown 行数 = 1 头 + 1 分隔 + 2 数据", md.count("\n") == 3, md)

    # 2) 线性化
    lin = to_linear([["项目", "代码"], ["甲", "001"]])
    check("线性化带表头前缀", lin.startswith("项目: 甲"), lin)
    check("线性化不含竖线", "|" not in lin, lin)

    # 3) 竖线转义
    md2 = to_markdown([["a|b", "c"], ["1", "2"]])
    check("单元格竖线被转义", "a\\|b" in md2, md2)

    # 4) 空表
    check("空表返回空串", to_markdown([]) == "" and to_linear([]) == "")

    # 5) 真实 PDF 端到端（生成一张带线表格的 PDF）
    doc = pymupdf.open()
    page = doc.new_page()
    # 画 3x3 网格线
    x0, y0, w, h = 72, 72, 120, 30
    for r in range(4):
        page.draw_line((x0, y0 + r * h), (x0 + 3 * w, y0 + r * h))
    for c in range(4):
        page.draw_line((x0 + c * w, y0), (x0 + c * w, y0 + 3 * h))
    cells = [["项目", "代码", "金额"], ["甲", "001", "100"], ["乙", "002", "200"]]
    for r, row in enumerate(cells):
        for c, v in enumerate(row):
            page.insert_text((x0 + c * w + 4, y0 + r * h + 18), v, fontsize=9, fontname="china-s")
    tmp = "/tmp/_extract_tables_selftest.pdf"
    doc.save(tmp)
    doc.close()

    blocks = extract(tmp, strategy="lines")
    check("端到端抽到 ≥1 个表格", len(blocks) >= 1, f"got {len(blocks)}")
    if blocks:
        b = blocks[0]
        check("抽到的表含表头文字", "项目" in b["markdown"], b["markdown"][:80])
        check("table_id 形如 p001-t00", re.match(r"^p\d{3}-t\d{2}$", b["table_id"]) is not None, b["table_id"])
        check("text 字段非空", bool(b["text"]))

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="从 PDF 抽取表格并转 markdown / 线性化文本")
    ap.add_argument("--pdf", help="输入 PDF")
    ap.add_argument("--out", default="-", help="输出 jsonl（默认 stdout）")
    ap.add_argument("--strategy", choices=["lines", "text"], default="lines")
    ap.add_argument("--min-rows", type=int, default=2)
    ap.add_argument("--min-cols", type=int, default=2)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.pdf:
        ap.error("需要 --pdf 或 --self-test")

    blocks = extract(a.pdf, a.strategy, a.min_rows, a.min_cols)
    lines = [json.dumps(b, ensure_ascii=False) for b in blocks]
    if a.out == "-":
        print("\n".join(lines))
    else:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        print(f"写出 {len(lines)} 个表格块 → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
