#!/usr/bin/env python3
"""多模态：识别已清洗语料里「被线性化的表格」。

背景：政务 PDF 经纯文本清洗后，表格会变成一串空格分隔的词 ——
表头与数据粘在一起，行列关系丢失，但内容还在。例如：

    工程规划许可证号 工程名称 备注 你单位必须按照所提交的...

检索时这类块会被当成普通段落，用户看到的是「一堆名词堆在一起」。
本脚本做两件事：
1. 识别哪些块原本含表格（给 chunk 打 `has_table` / `table_hint`）
2. 尽量把「表头 + 数据」还原成 markdown（能还原多少算多少，还原不了就标注）

用法：
    python3 tools/detect-tables.py --src <chunks.jsonl> --out /tmp/tagged.jsonl
    python3 tools/detect-tables.py --src <chunks.jsonl> --stats
    python3 tools/detect-tables.py --self-test

设计取舍：
- **不猜行列**。没有 PDF 原始线条，硬拆列只会造出假结构。所以默认只做
  「标记 + 提取表格区段」，不强行还原成 markdown。
- `--restore` 才尝试还原，且只在「表头能被识别为 3+ 个连续短名词」时做，
  其余一律标 `restore_failed`，绝不编造。
"""
import argparse
import json
import re
import sys

# 引出表格的固定说法（政务公文高频）
_LEAD = (
    "如下表", "见下表", "下表", "附表", "附件", "清单如下", "名单如下",
    "情况如下", "明细如下", "具体如下", "如下所示", "详见下表",
)
# 表头特征词：出现两个以上，基本可判定这一串是表头
_HEAD_WORDS = (
    "序号", "名称", "单位", "数量", "金额", "备注", "编号", "代码", "日期",
    "地址", "电话", "联系人", "项目", "内容", "标准", "指标", "类别", "类型",
    "时间", "地点", "负责人", "部门", "科室", "职务", "姓名", "性别", "年龄",
    "许可证号", "批准文号", "发文机关", "成文日期", "标题", "正文",
)
_CN = r"[\u4e00-\u9fff]"


def _segments(text):
    """按句号/分号/换行切段，返回 [(start, seg)]。"""
    out, start = [], 0
    for m in re.finditer(r"[。；\n]", text):
        seg = text[start:m.start()]
        if seg.strip():
            out.append((start, seg))
        start = m.end()
    if text[start:].strip():
        out.append((start, text[start:]))
    return out


def _looks_like_header(seg):
    """一段里出现 >=2 个表头特征词，且没有明显叙述动词 → 疑似表头。"""
    hits = [w for w in _HEAD_WORDS if w in seg]
    if len(hits) < 2:
        return False, hits
    # 有「的/了/是/在」等叙述词，多半是正文不是表头
    if re.search(r"[的了是在把被将对于]", seg):
        return False, hits
    return True, hits


def detect(text, min_header_words=3):
    """返回 dict：has_table / table_hint / table_segment / restore。

    判据收紧过一轮：第一版只靠「如下表/附件」和「表头词 >=2」，实测精度约 20%
    （「附件」后面接的是正文方案、不是表格；「项目/标准」这类词在叙述句里也常出现）。
    现在只认两种结构证据：
      1. 「序号」出现在前 120 字 —— 政务表格几乎都以「序号」起头，这个信号最硬；
      2. 表头词 >= 3 个 **且** 同一段里紧跟数字（表头后面接数据，不是接句子）。
    """
    res = {"has_table": False, "table_hint": "", "table_segment": "", "restore": None}
    if not text:
        return res

    head120 = text[:120]

    # 证据 1：「序号」起头的表格
    if "序号" in head120:
        i = text.find("序号")
        res["has_table"] = True
        res["table_hint"] = "序号表头"
        res["table_segment"] = text[max(0, i - 60):i + 400]
        _try_restore(res)
        return res

    # 证据 2：表头词 >= 3 且同段紧跟数字
    for start, seg in _segments(text):
        hits = [w for w in _HEAD_WORDS if w in seg]
        if len(hits) < min_header_words:
            continue
        if re.search(r"[的了是在把被将对于]", seg):
            continue
        if not re.search(r"\d", seg):
            continue
        res["has_table"] = True
        res["table_hint"] = "表头+数据:" + "/".join(hits[:4])
        res["table_segment"] = text[start:start + 400]
        _try_restore(res)
        return res

    return res


def _try_restore(res):
    """尝试把「表头串 + 值串」还原成行列。只在结构明确时做。"""
    seg = res["table_segment"]
    m = re.search(rf"(?:{_CN}{{2,6}} ){{2,}}{_CN}{{2,6}}", seg)
    if not m:
        return
    head = m.group(0).split()
    tail = seg[m.end():m.end() + 200].strip()
    vals = re.findall(r"[0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]{1,8}", tail)
    if len(head) >= 3 and len(vals) >= len(head):
        rows = [head] + [vals[i:i + len(head)]
                         for i in range(0, len(vals) - len(vals) % len(head), len(head))]
        res["restore"] = {"n_cols": len(head), "rows": rows[:6], "confidence": "low"}


def self_test():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            fail += 1
            print(f"  ✗ {name} {detail}")

    # 1) 「如下表」单独出现不判（后面可能接正文，实测精度约 20%）
    r = detect("申请材料具体如下表所示，请按要求提交。")
    check("只有「如下表」不判表格", not r["has_table"], r)

    # 1b) 「如下表」+ 表头词 + 数字 才判
    r = detect("申请材料具体如下表所示：序号 名称 数量 金额 1 甲 10 100")
    check("「如下表」+ 序号 判表格", r["has_table"] and r["table_hint"] == "序号表头", r)

    # 2) 表头特征命中（无引出语）
    r = detect("序号 名称 数量 金额 备注")
    check("「序号」表头被识别", r["has_table"] and r["table_hint"] == "序号表头", r)

    # 2b) 只有「附件」没有「序号」—— 第一版会误判，现在不判
    r = detect("附件：北京市服务业扩大开放综合试点总体方案")
    check("只有「附件」不判表格", not r["has_table"], r)

    # 3) 叙述句不误判
    r = detect("我们按照上级要求，对项目名称和单位数量进行了核对，并填写了备注。")
    check("叙述句不误判", not r["has_table"], r)

    # 4) 普通段落不误判
    r = detect("优化营商环境是激发市场主体活力的重要举措。")
    check("普通段落不误判", not r["has_table"], r)

    # 5) 还原：表头 + 值
    r = detect("序号 名称 数量 金额 1 甲 10 100 2 乙 20 200")
    check("能还原出列数", r["restore"] and r["restore"]["n_cols"] >= 3, r.get("restore"))

    # 6) 空输入
    r = detect("")
    check("空输入不崩", r["has_table"] is False)

    # 7) 长文本只截 400 字
    r = detect("如下表" + "填" * 1000)
    check("table_segment 截断到 400", len(r["table_segment"]) <= 400, len(r["table_segment"]))

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="识别线性化表格")
    ap.add_argument("--src", help="输入 chunks.jsonl")
    ap.add_argument("--out", default="-", help="输出 tagged jsonl（默认 stdout）")
    ap.add_argument("--stats", action="store_true", help="只统计不输出")
    ap.add_argument("--restore", action="store_true", help="尝试还原行列")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.src:
        ap.error("需要 --src 或 --self-test")

    n = hit = restored = 0
    hints = {}
    out_lines = []
    with open(a.src, encoding="utf-8") as f:
        for line in f:
            if a.limit and n >= a.limit:
                break
            d = json.loads(line)
            n += 1
            r = detect(d.get("text", ""))
            if r["has_table"]:
                hit += 1
                hints[r["table_hint"]] = hints.get(r["table_hint"], 0) + 1
                if a.restore and r["restore"]:
                    restored += 1
                    d["table_restored"] = r["restore"]
                d["has_table"] = True
                d["table_hint"] = r["table_hint"]
                d["table_segment"] = r["table_segment"]
            if not a.stats:
                out_lines.append(json.dumps(d, ensure_ascii=False))

    print(f"扫描 {n} 块 · 含表格 {hit}（{hit / max(1, n) * 100:.1f}%）· 还原成功 {restored}")
    top = sorted(hints.items(), key=lambda x: -x[1])[:8]
    for k, v in top:
        print(f"  {k}: {v}")

    if not a.stats:
        if a.out == "-":
            print("\n".join(out_lines))
        else:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write("\n".join(out_lines) + ("\n" if out_lines else ""))
            print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
