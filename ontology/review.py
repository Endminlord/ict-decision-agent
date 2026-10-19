#!/usr/bin/env python3
"""review_gate 的人工确认工具（本体三元组的最后一道闸门）。

`build.py --loose-out` 把候选层（块内共现 / 裸后缀 / 发布机关 / 概念提及）
的候选单独写出来。本工具负责把它们分成三类：

  accept  → `triples-confirmed.jsonl`（可入库）
  reject  → `triples-rejected.jsonl`（不入库，留档说明为什么）
  defer   → 留在 pending（下一轮再看）

三种工作方式：

  1. **交互逐条**（默认）—— 一次一条，看 subject / predicate / object / evidence
     按 `a` 接受、`r` 拒绝、`s` 跳过、`q` 退出。适合小批量精看。
  2. **按置信度批量** —— `--accept medium` 把 medium 全部接受、`--reject low`
     把 low 全部拒绝。适合「已经看过样本、确认这一档可以整批处理」。
  3. **只看统计** —— `--stats` 打印各置信度、各谓词的分布，不动任何文件。

设计取舍：**不提供 `--accept-all`**。闸门的意义就是「有人看过」，
一个能一键接受全部的参数会让这个意义消失；要整批处理必须显式写出置信度档位。
"""
import argparse
import collections
import json
import sys
from pathlib import Path


def load_doc_names(path):
    """读一份「已知文档名」清单（一行一个），用于标出 object 是否在语料里真实存在。

    **不做过滤** —— 引用可以指向语料外的文档（《民法典》不在政务语料里也照样能被引）。
    这个信号只帮人分配注意力：语料里查不到的名字，更可能是共现误判。
    """
    if not path:
        return None
    names = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            names.add(line)
            i = line.find("《")
            while i >= 0:
                j = line.find("》", i)
                if j < 0:
                    break
                inner = line[i + 1:j].strip()
                if inner:
                    names.add(inner)
                i = line.find("《", j)
    return names


def in_corpus(obj, names):
    if not names:
        return None
    if obj in names:
        return True
    return any(obj in a for a in names if len(a) >= len(obj))


def load(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def dump(rows, path, mode="w"):
    with open(path, mode, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def show_stats(rows, names=None):
    print(f"待确认：{len(rows)} 条\n")
    by_conf = collections.Counter(r.get("confidence") for r in rows)
    print("按置信度：")
    for k, v in by_conf.most_common():
        print(f"  {k:8s} {v:7d}  ({v / len(rows):.1%})")
    print("\n按谓词（前 10）：")
    by_pred = collections.Counter(r.get("predicate") for r in rows)
    for k, v in by_pred.most_common(10):
        print(f"  {k:16s} {v:7d}")
    # 谓词 × 置信度：哪一类最需要人看
    print("\n谓词 × 置信度（前 10，按量降序）：")
    cross = collections.Counter((r.get("predicate"), r.get("confidence")) for r in rows)
    for (pred, conf), v in cross.most_common(10):
        print(f"  {pred:16s} {conf:8s} {v:7d}")
    # 可疑度分档：object 越短、证据越短，越可能是「共现误判」
    def suspicion(r):
        obj = (r.get("object") or "")
        ev = ((r.get("provenance") or {}).get("evidence") or "")
        # 短 object + 短 evidence = 最可疑（「从而无法」这类）
        return (len(obj) < 10) + (len(ev) < 60)
    if names:
        known = sum(1 for r in rows if in_corpus(r.get("object") or "", names))
        print(f"\n语料内可查：{known} 条（{known / len(rows):.1%}）· 语料外：{len(rows) - known} 条")
        print("  （语料外不等于错 —— 《民法典》不在政务语料里也照样能被引；它只说明「这个名字在语料里找不到同名文档」）")
    buckets = collections.Counter(suspicion(r) for r in rows)
    print("\n可疑度（object<10字 记 1 分，evidence<60字 记 1 分）：")
    for k in (2, 1, 0):
        print(f"  {k} 分：{buckets.get(k, 0):7d} 条" + ("  ← 建议先看这一档" if k == 2 else ""))


def interactive(rows, confirmed_path, rejected_path, names=None):
    """逐条确认。返回 (accepted, rejected, deferred)。"""
    acc, rej = [], []
    for i, r in enumerate(rows, 1):
        prov = r.get("provenance") or {}
        print(f"\n[{i}/{len(rows)}]  {r.get('subject')}  --{r.get('predicate')}-->  {r.get('object')}")
        print(f"  置信度 {r.get('confidence')} · 方法 {r.get('method')}")
        print(f"  出处 {prov.get('chunk_id')} 偏移 {prov.get('char_offset')}")
        flag = in_corpus(r.get("object") or "", names)
        if flag is True:
            print("  语料里有同名文档 ✓")
        elif flag is False:
            print("  语料里没有同名文档（可能是语料外引用，也可能是共现误判）")
        ev = (prov.get("evidence") or "").strip()
        if ev:
            print(f"  证据「{ev[:160]}」")
        while True:
            ans = input("  [a]接受 / [r]拒绝 / [s]跳过 / [q]退出 > ").strip().lower()
            if ans in ("a", "r", "s", "q"):
                break
            print("  请输入 a / r / s / q")
        if ans == "q":
            break
        if ans == "a":
            r["review_status"] = "confirmed"
            acc.append(r)
        elif ans == "r":
            r["review_status"] = "rejected"
            rej.append(r)
    dump(acc, confirmed_path)
    dump(rej, rejected_path)
    print(f"\n接受 {len(acc)} 条 → {confirmed_path}")
    print(f"拒绝 {len(rej)} 条 → {rejected_path}")
    print(f"未处理 {len(rows) - len(acc) - len(rej)} 条（仍在 pending）")
    return len(acc), len(rej)


def batch(rows, accept_conf, reject_conf, confirmed_path, rejected_path, defer_path):
    """按置信度整批处理。没有显式列出的档位全部回写 pending（defer）。"""
    acc = [r for r in rows if r.get("confidence") in accept_conf]
    rej = [r for r in rows if r.get("confidence") in reject_conf]
    defer = [r for r in rows if r.get("confidence") not in accept_conf | reject_conf]
    for r in acc:
        r["review_status"] = "confirmed"
    for r in rej:
        r["review_status"] = "rejected"
    dump(acc, confirmed_path)
    dump(rej, rejected_path)
    dump(defer, defer_path)
    print(f"接受 {len(acc)} 条 → {confirmed_path}")
    print(f"拒绝 {len(rej)} 条 → {rejected_path}")
    print(f"留待下轮 {len(defer)} 条 → {defer_path}")
    return len(acc), len(rej), len(defer)


def main():
    ap = argparse.ArgumentParser(description="review_gate 的人工确认工具")
    ap.add_argument("--pending", default="triples-governance-loose.jsonl",
                    help="build.py --loose-out 的产物（候选层，全部须人工）")
    ap.add_argument("--confirmed", default="triples-confirmed.jsonl")
    ap.add_argument("--rejected", default="triples-rejected.jsonl")
    ap.add_argument("--defer", default="triples-review-deferred.jsonl",
                    help="批量模式下未处理的回写位置（--pending 的剩余部分）")
    ap.add_argument("--stats", action="store_true", help="只打印分布，不动文件")
    ap.add_argument("--predicate", help="只看某个谓词（如 cites）")
    ap.add_argument("--confidence", choices=["medium", "low"], help="只看某一档")
    ap.add_argument("--sort", choices=["file", "suspicious"], default="file",
                    help="suspicious：最短 object + 最短证据排前面（最可疑的先看）")
    ap.add_argument("--limit", type=int, help="只看前 N 条")
    ap.add_argument("--doc-names", help="已知文档名清单（一行一个），用于标注 object 是否在语料里")
    ap.add_argument("--accept", choices=["medium", "low"], action="append", default=[],
                    help="整批接受某一档（可重复）")
    ap.add_argument("--reject", choices=["medium", "low"], action="append", default=[],
                    help="整批拒绝某一档（可重复）")
    a = ap.parse_args()

    p = Path(a.pending)
    if not p.exists():
        print(f"找不到 {p} —— 先跑 build.py --loose-out {p}（候选层不随包，约 200 MB）", file=sys.stderr)
        return 2
    rows = load(p)
    if a.predicate:
        rows = [r for r in rows if r.get("predicate") == a.predicate]
    if a.confidence:
        rows = [r for r in rows if r.get("confidence") == a.confidence]
    if a.sort == "suspicious":
        def _susp(r):
            obj = (r.get("object") or "")
            ev = ((r.get("provenance") or {}).get("evidence") or "")
            return (len(obj) >= 10) + (len(ev) >= 60), len(obj)
        rows.sort(key=_susp)
    if a.limit:
        rows = rows[:a.limit]
    if not rows:
        print("pending 为空：闸门里没有待确认的候选。")
        return 0
    names = load_doc_names(a.doc_names)
    if a.stats:
        show_stats(rows, names)
        return 0
    if a.accept or a.reject:
        overlap = set(a.accept) & set(a.reject)
        if overlap:
            print(f"--accept 与 --reject 同时列了 {overlap}", file=sys.stderr)
            return 2
        batch(rows, set(a.accept), set(a.reject), a.confirmed, a.rejected, a.defer)
        return 0
    return 0 if interactive(rows, a.confirmed, a.rejected, names)[0] >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
