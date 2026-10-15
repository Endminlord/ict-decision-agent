#!/usr/bin/env python3
"""双侧过滤：只保留「subject 与 object 都被模型判为文件」的引用边。

为什么需要单独一个脚本：`reason.py --only-docs` 只过滤 **object** 侧
（见 reason.py:67），拿不到「两端都是文件」的图。本脚本先把三元组剪成子集，
再交给 reason.py，两个脚本各管一件事。

它同时产出 238 §4 那张表的**全量版**：
  ① 原样（规则层四谓词全边）
  ② 只清 object 侧
  ③ 只清 subject 侧
  ④ 双侧都清
样本版那张表只有 688 条边，这里跑的是全量。

用法：
  python3 prune.py --subject-judge judged-subject-full.jsonl \
                   --object-judge  judged-object-full.jsonl \
                   --out triples-doc2doc.jsonl --report prune-report.json
  python3 prune.py --self-test
"""
import argparse, json, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REF_PREDS = ("cites", "implements", "amends", "repeals")


def load_docs(path):
    """读 judge.py 的产出，返回被判为文件的候选集合。"""
    keep = set()
    total = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            total += 1
            if r.get("is_doc") is True:
                keep.add((r.get("candidate") or "").strip())
    return keep, total


def load_edges(path, preds=REF_PREDS):
    """读三元组里的引用边，保留原行以便整条写出。"""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            if t.get("predicate") not in preds:
                continue
            s = (t.get("subject") or "").strip()
            o = (t.get("object") or "").strip()
            if not s or not o:
                continue
            out.append((s, o, line))
    return out


def stage_counts(edges, docs_s, docs_o):
    """四个阶段的边/节点读数。"""
    stages = {}
    for name, fs, fo in (
        ("orig", None, None),
        ("obj_clean", None, docs_o),
        ("subj_clean", docs_s, None),
        ("both_clean", docs_s, docs_o),
    ):
        keep = [(s, o) for s, o, _ in edges
                if (fs is None or s in fs) and (fo is None or o in fo)]
        nodes = set()
        for s, o in keep:
            nodes.add(s); nodes.add(o)
        stages[name] = {"edges": len(keep), "nodes": len(nodes)}
    return stages


def run(src, subj_judge, obj_judge, out_path, report_path):
    docs_s, n_s = load_docs(subj_judge)
    docs_o, n_o = load_docs(obj_judge)
    edges = load_edges(src)
    stages = stage_counts(edges, docs_s, docs_o)

    keep_lines = [line for s, o, line in edges if s in docs_s and o in docs_o]
    with open(out_path, "w", encoding="utf-8") as f:
        for line in keep_lines:
            f.write(line + "\n")

    pred_dist = Counter()
    for line in keep_lines:
        pred_dist[json.loads(line)["predicate"]] += 1

    rep = {
        "src": os.path.basename(src),
        "subject_judge": {"path": os.path.basename(subj_judge),
                          "judged": n_s, "docs": len(docs_s)},
        "object_judge": {"path": os.path.basename(obj_judge),
                         "judged": n_o, "docs": len(docs_o)},
        "stages": stages,
        "doc2doc_predicates": dict(pred_dist.most_common()),
        "out": os.path.basename(out_path),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    print(f"[prune] subject 判定 {n_s} → 文件 {len(docs_s)}")
    print(f"[prune] object  判定 {n_o} → 文件 {len(docs_o)}")
    for k, v in stages.items():
        print(f"  ① {k:11s} 边 {v['edges']:7d} · 节点 {v['nodes']:7d}"
              if k == "orig" else
              f"  · {k:11s} 边 {v['edges']:7d} · 节点 {v['nodes']:7d}")
    print(f"[prune] 文件→文件 边 {len(keep_lines)} → {os.path.basename(out_path)}")
    return rep


def self_test():
    import tempfile
    ok = 0; fail = []
    d = tempfile.mkdtemp(prefix="prune-test-")
    tri = os.path.join(d, "t.jsonl")
    sj = os.path.join(d, "sj.jsonl")
    oj = os.path.join(d, "oj.jsonl")
    # A→B (A,B 都是文件) · C→D (C 非文件) · A→E (E 非文件)
    with open(tri, "w", encoding="utf-8") as f:
        for s, o in (("A", "B"), ("C", "D"), ("A", "E")):
            f.write(json.dumps({"subject": s, "object": o, "predicate": "cites",
                                "confidence": "high"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"subject": "A", "object": "B", "predicate": "mentions"},
                           ensure_ascii=False) + "\n")  # 非引用谓词，须被忽略
    for p, rows in ((sj, [("A", True), ("C", False)]),
                    (oj, [("B", True), ("D", True), ("E", False)])):
        with open(p, "w", encoding="utf-8") as f:
            for c, isd in rows:
                f.write(json.dumps({"candidate": c, "is_doc": isd},
                                   ensure_ascii=False) + "\n")

    rep = run(tri, sj, oj, os.path.join(d, "out.jsonl"), os.path.join(d, "rep.json"))
    st = rep["stages"]
    # P1 只取引用谓词：原样 3 条（mentions 不计）
    if st["orig"]["edges"] == 3: ok += 1
    else: fail.append(f"P1 原样边数 {st['orig']['edges']} != 3")
    # P2 只清 object：A→B、C→D 留下（2 条）
    if st["obj_clean"]["edges"] == 2: ok += 1
    else: fail.append(f"P2 只清object {st['obj_clean']['edges']} != 2")
    # P3 只清 subject：A→B、A→E 留下（2 条）
    if st["subj_clean"]["edges"] == 2: ok += 1
    else: fail.append(f"P3 只清subject {st['subj_clean']['edges']} != 2")
    # P4 双侧都清：只剩 A→B（1 条），节点 2
    if st["both_clean"]["edges"] == 1 and st["both_clean"]["nodes"] == 2: ok += 1
    else: fail.append(f"P4 双侧 {st['both_clean']} != 1 边 / 2 节点")
    # P5 单调：orig ≥ 每阶段 ≥ both
    if (st["orig"]["edges"] >= st["obj_clean"]["edges"] >= st["both_clean"]["edges"]
            and st["orig"]["edges"] >= st["subj_clean"]["edges"] >= st["both_clean"]["edges"]):
        ok += 1
    else: fail.append("P5 阶段单调性不成立")
    # P6 写出的子集行数 = both_clean 边数
    n = sum(1 for _ in open(os.path.join(d, "out.jsonl"), encoding="utf-8"))
    if n == st["both_clean"]["edges"]: ok += 1
    else: fail.append(f"P6 输出行数 {n} != {st['both_clean']['edges']}")

    print(f"self-test: {ok}/6")
    for m in fail:
        print("  ✗", m)
    return not fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(HERE, "triples-governance.jsonl"))
    ap.add_argument("--subject-judge", default=os.path.join(HERE, "judged-subject-full.jsonl"))
    ap.add_argument("--object-judge", default=os.path.join(HERE, "judged-object-full.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "triples-doc2doc.jsonl"))
    ap.add_argument("--report", default=os.path.join(HERE, "prune-report.json"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(0 if self_test() else 1)
    run(a.src, a.subject_judge, a.object_judge, a.out, a.report)


if __name__ == "__main__":
    main()
