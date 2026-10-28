#!/usr/bin/env python3
"""immunity_log.py — 免疫记录层（D2）：技能/治理变更的 append-only 防篡改账本。

设计要点（对应宪法 C1/C6 与三层治理方案）：
- 只追加不修改：每条记录经 SHA-256 链式哈希（prev_hash → hash）钉死，任何回改/删插都会破坏链条；
- 谱系：parent_skill_id 记录技能由谁派生/晋升自谁，lineage() 可回溯全链；
- 门控引用：gate 字段必须携带判据（配对检验统计量或迁移验收出处），"判定权外置"的落账形式；
- OTel 预留：trace_id 字段接入 OTel 后由运行时自动填充，当前允许 null；
- 红线：本模块不判定、不评分——它只忠实记账（C2 同源纪律）。

用法：
    python3 immunity_log.py append --action promote --target my-skill --gate-file gate.json ...
    python3 immunity_log.py verify            # 全链校验
    python3 immunity_log.py lineage my-skill  # 谱系回溯
    python3 immunity_log.py stats
    python3 immunity_log.py --selftest        # 临时目录自检（含篡改检测）
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LEDGER = os.path.join(BASE, "immunity-log.jsonl")

ACTIONS = (
    "register",            # 登记（宪法/契约/账本自身等治理文件）
    "verified-intake",     # 存量技能划入 verified（C4 不追溯依据必须给足）
    "capture",             # 新工作流落入 observed（只记录不判定）
    "promote",             # observed → verified（必须附 oracle 门控判据）
    "demote",              # verified → observed（遗忘同权，C6）
    "retire",              # 废止（同走门控）
    "amend",               # 宪法/契约修订（版本递增 + 人工确认）
)
REQUIRED = ("action", "target", "actor")
HASH_LEN = 16


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(rec):
    core = {k: rec.get(k) for k in ("rec_id", "ts", "action", "target", "namespace",
                                    "version", "parent_skill_id", "gate", "evidence",
                                    "trace_id", "actor", "note")}
    return json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(prev, rec):
    return hashlib.sha256((prev + "|" + _canonical(rec)).encode("utf-8")).hexdigest()[:HASH_LEN]


def load(path=DEFAULT_LEDGER):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise SystemExit("账本第 %d 行不是合法 JSON：%s" % (i, e))
    return out


def append(record, path=DEFAULT_LEDGER):
    for k in REQUIRED:
        if not record.get(k):
            raise SystemExit("缺少必填字段：%s" % k)
    if record.get("action") not in ACTIONS:
        raise SystemExit("action 必须是 %s" % (ACTIONS,))
    if record["action"] in ("promote", "demote", "retire") and not (record.get("gate") or {}).get("ref"):
        raise SystemExit("晋升/降级/废止必须附门控判据 gate.ref（宪法 C1/C6）")
    chain = load(path)
    prev = chain[-1]["hash"] if chain else "GENESIS"
    rec = {
        "rec_id": record.get("rec_id") or ("IMM-" + uuid.uuid4().hex[:10]),
        "ts": record.get("ts") or _now(),
        "action": record["action"],
        "target": record["target"],
        "namespace": record.get("namespace"),
        "version": record.get("version"),
        "parent_skill_id": record.get("parent_skill_id"),
        "gate": record.get("gate"),
        "evidence": record.get("evidence") or [],
        "trace_id": record.get("trace_id"),
        "actor": record["actor"],
        "note": record.get("note"),
    }
    rec["prev_hash"] = prev
    rec["hash"] = _hash(prev, rec)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    return rec


def verify(path=DEFAULT_LEDGER):
    chain = load(path)
    prev = "GENESIS"
    for i, rec in enumerate(chain, 1):
        if rec.get("prev_hash") != prev:
            return {"ok": False, "broken_at": i, "reason": "prev_hash 不连续（疑似删插）"}
        if rec.get("hash") != _hash(prev, rec):
            return {"ok": False, "broken_at": i, "reason": "hash 不匹配（记录被回改）"}
        prev = rec["hash"]
    return {"ok": True, "records": len(chain), "head": prev}


def lineage(skill, path=DEFAULT_LEDGER):
    chain = load(path)
    by_target = {}
    for rec in chain:
        by_target.setdefault(rec.get("target"), []).append(rec)
    trail, cur, seen = [], skill, set()
    while cur and cur not in seen:
        seen.add(cur)
        for rec in by_target.get(cur, []):
            trail.append(rec)
        parents = [r.get("parent_skill_id") for r in by_target.get(cur, [])
                   if r.get("parent_skill_id")]
        cur = parents[-1] if parents else None
    return trail


def stats(path=DEFAULT_LEDGER):
    chain = load(path)
    by_action = {}
    for rec in chain:
        by_action[rec["action"]] = by_action.get(rec["action"], 0) + 1
    v = verify(path)
    return {"records": len(chain), "by_action": by_action, "chain_ok": v.get("ok")}


def _selftest():
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "ledger.jsonl")
        r1 = append({"action": "register", "target": "constitution.json", "actor": "selftest",
                     "version": "1.0.0", "evidence": ["mock"]}, path=p)
        r2 = append({"action": "capture", "target": "demo-skill", "actor": "selftest",
                     "namespace": "observed", "note": "初次观察"}, path=p)
        r3 = append({"action": "promote", "target": "demo-skill", "actor": "selftest",
                     "namespace": "verified", "parent_skill_id": "demo-skill",
                     "gate": {"method": "wilcoxon-paired", "n": 15, "p": 0.03, "ref": "mock-gate"}},
                    path=p)
        v = verify(p)
        assert v["ok"] and v["records"] == 3, v
        assert r3["prev_hash"] == r2["hash"] and r2["prev_hash"] == r1["hash"]
        # 篡改检测：回改第二条 → 链条必须在第 3 条断裂
        lines = open(p, encoding="utf-8").read().splitlines()
        bad = json.loads(lines[1]); bad["note"] = "被回改"
        lines[1] = json.dumps(bad, ensure_ascii=False, sort_keys=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        v2 = verify(p)
        assert not v2["ok"] and v2["broken_at"] == 2, v2
        # 门控强制：promote 无 gate.ref 必须拒绝
        try:
            append({"action": "promote", "target": "x", "actor": "selftest"}, path=p)
            raise AssertionError("应拒绝无判据的 promote")
        except SystemExit:
            pass
        lt = lineage("demo-skill", path=p)
        assert len(lt) == 2 and lt[-1]["action"] == "promote"
        print("selftest OK：append/verify/篡改检测/门控强制/谱系 全通过")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    ap_append = sub.add_parser("append")
    ap_append.add_argument("--action", required=True)
    ap_append.add_argument("--target", required=True)
    ap_append.add_argument("--actor", default=os.environ.get("USER") or "unknown")
    ap_append.add_argument("--namespace")
    ap_append.add_argument("--version")
    ap_append.add_argument("--parent-skill-id")
    ap_append.add_argument("--gate-json")
    ap_append.add_argument("--gate-file")
    ap_append.add_argument("--evidence", nargs="*", default=[])
    ap_append.add_argument("--trace-id")
    ap_append.add_argument("--note")
    sub.add_parser("verify")
    lin = sub.add_parser("lineage")
    lin.add_argument("skill")
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        return
    if a.cmd == "append":
        gate = None
        if a.gate_file:
            gate = json.load(open(a.gate_file, encoding="utf-8"))
        elif a.gate_json:
            gate = json.loads(a.gate_json)
        rec = append({"action": a.action, "target": a.target, "actor": a.actor,
                      "namespace": a.namespace, "version": a.version,
                      "parent_skill_id": a.parent_skill_id, "gate": gate,
                      "evidence": a.evidence, "trace_id": a.trace_id, "note": a.note},
                     path=a.ledger)
        print(json.dumps({"rec_id": rec["rec_id"], "hash": rec["hash"]}, ensure_ascii=False))
    elif a.cmd == "verify":
        print(json.dumps(verify(a.ledger), ensure_ascii=False))
    elif a.cmd == "lineage":
        print(json.dumps(lineage(a.skill, a.ledger), ensure_ascii=False, indent=1))
    elif a.cmd == "stats":
        print(json.dumps(stats(a.ledger), ensure_ascii=False))
    else:
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
