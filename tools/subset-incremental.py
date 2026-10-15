#!/usr/bin/env python3
"""比对 ES 已导入块 与 15,000 块子集，算出真正需要新增导入的部分。

用法: python3 /tmp/check-overlap.py
"""
import hashlib
import json
import os
import subprocess

ES = os.environ.get("ES_URL", "http://localhost:9220/1-43ce427e8b5447ecad015534f953e084")
AUTH = os.environ.get("ES_AUTH", "")  # 形如 elastic:<密码>，从环境变量读取，不写入源码
SUB = os.environ.get("SUBSET_PATH", "/home/endmin/dsh/research/ict-corpus-subset/subset-env.jsonl")


def fetch_es():
    out = subprocess.run(
        ["curl", "-s", "--max-time", "40", "-u", AUTH,
         f"{ES}/_search?size=6000&_source=path_or_url,content"],
        capture_output=True, text=True).stdout
    d = json.loads(out)
    keys = set()
    for h in d["hits"]["hits"]:
        s = h["_source"]
        c = (s.get("content") or "").strip()
        keys.add((s["path_or_url"], hashlib.sha256(c.encode()).hexdigest()[:16]))
    return keys, d["hits"]["total"]["value"]


def main():
    es_keys, total = fetch_es()
    print(f"ES 已导入: {total} 块, 唯一键 {len(es_keys)}")

    sub = []
    with open(SUB) as f:
        for line in f:
            line = line.strip()
            if line:
                sub.append(json.loads(line))
    print(f"子集: {len(sub)} 块")

    dup = 0
    for c in sub:
        key = (f"{c['doc_id']}.txt", hashlib.sha256(c["text"].strip().encode()).hexdigest()[:16])
        if key in es_keys:
            dup += 1
    print(f"按 (文档, 内容哈希) 重叠: {dup} / {len(sub)}")

    es_docs = set(p for p, _ in es_keys)
    sub_docs = set(f"{c['doc_id']}.txt" for c in sub)
    inter = es_docs & sub_docs
    new_docs = sub_docs - es_docs
    print(f"ES 文档 {len(es_docs)} / 子集文档 {len(sub_docs)} / 交集 {len(inter)} / 新增文档 {len(new_docs)}")

    new_by_doc = [c for c in sub if f"{c['doc_id']}.txt" in new_docs]
    print(f"按文档粒度的新增块: {len(new_by_doc)}")

    with open("/tmp/subset-incremental.jsonl", "w") as f:
        for c in new_by_doc:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print("已写 /tmp/subset-incremental.jsonl")


main()
