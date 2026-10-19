#!/usr/bin/env python3
"""本体动态更新：把新增块抽出的三元组合并进已有本体。

赛题要求「本体持续动态更新」。`build.py` 是批处理（一次跑完 60,000 块），
真实业务里语料是持续进来的 —— 今天来 200 个新文件，不该重跑全量。

本脚本做增量合并：
    1. 读已有三元组（base）建 (subject, predicate, object) 索引
    2. 读新增三元组（new），跳过已存在的
    3. 输出合并结果 + 一份变更报告（新增 / 重复跳过 / 受影响实体）

用法：
    python3 tools/merge-triples.py --base ontology/triples-governance.jsonl \\
                                   --new  /tmp/new-triples.jsonl \\
                                   --out  /tmp/merged.jsonl --report /tmp/merge-report.json

    # 端到端：新块 → 抽取 → 合并
    python3 ontology/build.py --src /tmp/new-chunks.jsonl --out /tmp/new-triples.jsonl \\
        --loose-out /tmp/new-loose.jsonl --terms ontology/terms.governance.zh.json
    python3 tools/merge-triples.py --base ontology/triples-governance.jsonl \\
        --new /tmp/new-triples.jsonl --out /tmp/merged.jsonl

    python3 tools/merge-triples.py --self-test

设计取舍：
- **只做严格去重**（三元组完全相同才算重复）。同名实体不同表述（「杭州市」vs「杭州市人民政府」）
  不做模糊合并 —— 那需要实体消歧，属另一件事，混进来会悄悄改掉已有三元组的 subject。
- **保留新记录的 `review_status`**：增量进来的仍是 `pending`（除非 build 时就是 auto），
  不因为「合并」就把待人工的洗成自动。
- **报告里的 `affected_entities`** 是判据：如果新增只触及 3 个实体，说明这批文件与已有本体几乎无交集，
  应该回头检查抽取是否漏了。
"""
import argparse
import json
import sys
from collections import Counter


def load_base(path):
    """读已有三元组，返回 {(s,p,o): 记录}。"""
    idx = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            idx[(d["subject"], d["predicate"], d["object"])] = d
    return idx


def merge(base_idx, new_path):
    """合并新增，返回 (合并后列表, 统计)。"""
    added = []
    dup = 0
    new_entities = set()
    by_pred = Counter()
    with open(new_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            key = (d["subject"], d["predicate"], d["object"])
            if key in base_idx:
                dup += 1
                continue
            base_idx[key] = d
            added.append(d)
            by_pred[d["predicate"]] += 1
            new_entities.add(d["subject"])
            new_entities.add(d["object"])
    return list(base_idx.values()), {
        "base_total": len(base_idx) - len(added),
        "new_total": len(added) + dup,
        "added": len(added),
        "duplicates_skipped": dup,
        "merged_total": len(base_idx),
        "affected_entities": len(new_entities),
        "added_by_predicate": dict(by_pred),
    }


def self_test():
    import tempfile
    import os

    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            fail += 1
            print(f"  ✗ {name} {detail}")

    def rec(s, p, o, conf="medium"):
        return {"subject": s, "subject_type": "Document", "predicate": p,
                "object": o, "object_type": "Document", "confidence": conf,
                "method": "rule", "tier": "loose", "review_status": "pending"}

    d = tempfile.mkdtemp()
    base = os.path.join(d, "base.jsonl")
    new = os.path.join(d, "new.jsonl")
    with open(base, "w", encoding="utf-8") as f:
        for r in [rec("A", "cites", "B"), rec("A", "implements", "C")]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(new, "w", encoding="utf-8") as f:
        for r in [rec("A", "cites", "B"),          # 完全重复
                  rec("A", "cites", "D"),          # 新
                  rec("E", "amends", "F")]:        # 新
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    idx = load_base(base)
    check("base 读到 2 条", len(idx) == 2, len(idx))

    merged, stats = merge(idx, new)
    check("合并后 4 条", stats["merged_total"] == 4, stats)
    check("新增 2 条", stats["added"] == 2, stats)
    check("重复跳过 1 条", stats["duplicates_skipped"] == 1, stats)
    # 新增记录触及的实体 = A(已存在但被新增记录引用) + D + E + F = 4
    check("受影响实体 4 个（A/D/E/F）", stats["affected_entities"] == 4, stats["affected_entities"])
    check("按谓词统计正确", stats["added_by_predicate"].get("cites") == 1 and
          stats["added_by_predicate"].get("amends") == 1, stats["added_by_predicate"])
    check("重复项未被改写 confidence", all(
        r["confidence"] == "medium" for r in merged), "有记录被改")

    # 空新增
    empty = os.path.join(d, "empty.jsonl")
    open(empty, "w").close()
    idx2 = load_base(base)
    _, st2 = merge(idx2, empty)
    check("空新增不报错", st2["added"] == 0 and st2["merged_total"] == 2, st2)

    print(f"\n{ok}/{ok + fail} 通过")
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="三元组增量合并")
    ap.add_argument("--base", help="已有三元组 jsonl")
    ap.add_argument("--new", help="新增三元组 jsonl")
    ap.add_argument("--out", default="-", help="合并输出（默认 stdout）")
    ap.add_argument("--report", help="统计报告 json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not (a.base and a.new):
        ap.error("需要 --base 与 --new")

    idx = load_base(a.base)
    merged, stats = merge(idx, a.new)

    if a.out == "-":
        for r in merged:
            print(json.dumps(r, ensure_ascii=False))
    else:
        with open(a.out, "w", encoding="utf-8") as f:
            for r in merged:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    print(json.dumps(stats, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
