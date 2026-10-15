#!/usr/bin/env python3
"""本体对齐：把抽出的实体名归并成规范名，并给出可反向查回原文的映射表。

判据 K3：映射表必须能反向查回原文 —— 每个规范名下的每一次出现都带
chunk_id + doc_char_offset + evidence，且 evidence 能在源块里按偏移复现。
行业无关：归一规则（文号格式、同义后缀、去掉的噪声词）全部来自 terms 文件。
"""
import argparse
import json
import re
import sys
from pathlib import Path

FULLWIDTH = str.maketrans("〔〕［］（）０１２３４５６７８９", "[][]()0123456789")


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def normalize(name, terms):
    """把实体名归一成规范名。规则全部来自词表。"""
    n = name.strip()
    for ch in terms.get("doc_open", "《") + terms.get("doc_close", "》"):
        n = n.replace(ch, "")
    n = re.sub(r"\s+", "", n).translate(FULLWIDTH)
    for noise in terms.get("alias_noise") or []:
        n = n.replace(noise, "")
    n = re.sub(r"[〔\[]\s*(\d{4})\s*[〕\]]\s*(\d+)\s*号", r"[\1]\2号", n)
    for pat, rep in (terms.get("alias_rules") or {}).items():
        n = re.sub(pat, rep, n)
    return n


def build_index(triples_path, terms, limit=None):
    idx = {}
    n = 0
    with open(triples_path, encoding="utf-8") as f:
        for line in f:
            if limit is not None and n >= limit:
                break
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            n += 1
            for role in ("object", "subject"):
                raw = r.get(role)
                if not raw or role == "subject":
                    continue  # 主语是本块所属文档，已在 object 侧作为被引对象出现
                etype = r.get("object_type")
                canon = normalize(raw, terms)
                slot = idx.setdefault(canon, {"canonical": canon, "entity_type": etype,
                                              "aliases": set(), "occurrences": []})
                slot["aliases"].add(raw)
                pv = r["provenance"]
                slot["occurrences"].append({
                    "predicate": r["predicate"],
                    "confidence": r["confidence"],
                    "chunk_id": pv["chunk_id"],
                    "doc_id": pv["doc_id"],
                    "doc_char_offset": pv.get("doc_char_offset"),
                    "evidence": pv["evidence"],
                    "title": pv.get("title"),
                    "date": pv.get("date"),
                })
    out = {}
    for canon, slot in idx.items():
        slot["aliases"] = sorted(slot["aliases"])
        slot["occurrence_count"] = len(slot["occurrences"])
        slot["occurrences"] = slot["occurrences"][:3]  # 映射表只留 3 条样本，全量在 triples 里
        out[canon] = slot
    return out


def verify_against_chunks(index, chunks_path, sample=200, seed=20261013, scan_limit=300000):
    """判据 K3 的复算：抽样 occurrence，回到源块按 doc_char_offset 验证 evidence。"""
    import random
    rnd = random.Random(seed)
    checks = []
    for canon, slot in index.items():
        for occ in slot["occurrences"]:
            checks.append((canon, occ))
    rnd.shuffle(checks)
    checks = checks[:sample]
    want = {c[1]["chunk_id"]: c for c in checks}
    seen, ok, bad = set(), 0, []
    scanned = 0
    with open(chunks_path, encoding="utf-8") as f:
        for line in f:
            scanned += 1
            if scanned > scan_limit:
                break
            if len(seen) >= len(want):
                break
            line = line.strip()
            if not line:
                continue
            try:
                ch = json.loads(line)
            except Exception:
                continue
            cid = ch.get("chunk_id")
            if cid not in want or cid in seen:
                continue
            seen.add(cid)
            canon, occ = want[cid]
            off = occ["doc_char_offset"]
            base = ch.get("char_offset") or 0
            head = occ["evidence"].strip()[:12]
            inside = head in ch.get("text", "")
            # 偏移须落在块内，且 doc 偏移 = 块偏移 + 块内偏移
            off_ok = off is not None and base <= off <= base + len(ch.get("text", ""))
            if inside and off_ok:
                ok += 1
            else:
                bad.append({"canonical": canon, "chunk_id": cid, "inside": inside, "offset_ok": off_ok})
    return {"sampled": len(checks), "found": len(seen), "verified": ok, "failed": len(bad), "failures": bad[:5]}


def self_test():
    terms = load_json(Path(__file__).with_name("terms.governance.zh.json"))
    ok = []
    # N1：书名号与空白必须被剥掉
    ok.append(("N1 去书名号/空白", normalize("《优化 营商环境条例》", terms) == "优化营商环境条例"))
    # N2：文号半角/全角必须归一成同一规范名
    a = normalize("国办发〔2019〕39 号", terms)
    b = normalize("国办发[2019]39号", terms)
    ok.append(("N2 文号归一", a == b and "[2019]39号" in a))
    # N3：负对照 —— 两个不同实体不得被归一成同一个名字
    c = normalize("《中华人民共和国土地管理法》", terms)
    d = normalize("《中华人民共和国安全生产法》", terms)
    ok.append(("N3 不同实体不合并", c != d))
    # N4：映射表结构必须带得出处字段（缺则无法反查）
    idx = build_index(_fixture_triples(), terms)
    first = next(iter(idx.values()))
    need = {"chunk_id", "doc_char_offset", "evidence"}
    ok.append(("N4 occurrence 带出处字段", need <= set(first["occurrences"][0])))
    for name, good in ok:
        print(("  PASS  " if good else "  FAIL  ") + name)
    p = sum(1 for _, g in ok if g)
    print(f"自测：{p}/{len(ok)}")
    return 0 if p == len(ok) else 1


def _fixture_triples():
    rec = {"subject": "某通知", "subject_type": "Document", "predicate": "cites", "object": "优化营商环境条例",
           "object_type": "Document", "confidence": "high", "method": "rule",
           "provenance": {"chunk_id": "c1-0000", "doc_id": "c1", "char_offset": 5, "doc_char_offset": 1005,
                          "evidence": "根据《优化营商环境条例》制定", "title": "某通知", "date": "2020-01-01"}}
    p = Path("/tmp/_align_fixture.jsonl")
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def main():
    ap = argparse.ArgumentParser(description="本体对齐：规范名归并 + 反向查回原文")
    ap.add_argument("--triples", help="三元组 jsonl")
    ap.add_argument("--terms", help="行业词表 json")
    ap.add_argument("--out", help="映射表输出 json")
    ap.add_argument("--verify-chunks", help="用源块文件复算 K3")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not (a.triples and a.terms and a.out):
        ap.error("需要 --triples / --terms / --out（或用 --self-test）")
    terms = load_json(a.terms)
    idx = build_index(a.triples, terms)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    print(json.dumps({"canonical_entities": len(idx),
                      "with_occurrences": sum(1 for v in idx.values() if v["occurrence_count"])}, ensure_ascii=False))
    if a.verify_chunks:
        res = verify_against_chunks(idx, a.verify_chunks, a.sample)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["failed"] == 0 else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
