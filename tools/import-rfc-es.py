#!/usr/bin/env python3
"""把 RFC 语料导入 ES，供检索评测使用。

**为什么不用 `tools/import-corpus.py`**：那个脚本走 Nexent 后端的
`/indices/{index}/documents`，要求索引已由平台创建（实测 404：
`Knowledge base 'rfc-1024' not found`）。这里只需要一个能被检索脚本
直接用的索引，直连 ES 更简单。

字段与政务索引对齐（`content` / `title` / `date` / `author` /
`path_or_url` / `file_size` / `process_source` / `embedding`），
所以同一套检索与评测代码可以跑在两个索引上。

用法：
    ES_AUTH=elastic:<密码> python3 tools/import-rfc-es.py --src /tmp/rfc.jsonl
    ES_AUTH=elastic:<密码> python3 tools/import-rfc-es.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ES = os.environ.get("ES_URL", "http://localhost:9220")
AUTH = os.environ.get("ES_AUTH", "")
EMBED_URL = os.environ.get("EMBED_URL", "http://127.0.0.1:8792/v1/embeddings")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
INDEX = os.environ.get("RFC_INDEX", "rfc-1024")

MAPPING = {
    "properties": {
        "content": {"type": "text"},
        "title": {"type": "text"},
        "date": {"type": "date"},
        "author": {"type": "keyword"},
        "path_or_url": {"type": "keyword"},
        "filename": {"type": "keyword"},
        "file_size": {"type": "long"},
        "process_source": {"type": "keyword"},
        "language": {"type": "keyword"},
        "embedding_model_name": {"type": "keyword"},
        "embedding": {"type": "dense_vector", "dims": 1024, "index": True,
                      "similarity": "cosine"},
    }
}


def _req(url: str, data=None, method=None, timeout=120):
    headers = {"Content-Type": "application/json"}
    if AUTH:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(AUTH.encode()).decode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        # 把服务端的错误体打出来，否则只能对着 400 猜
        detail = e.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"{method or 'GET'} {url} → HTTP {e.code}: {detail}") from e


def embed(texts: list[str]) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    out = json.loads(_req(EMBED_URL, body, timeout=300))
    return [d["embedding"] for d in out["data"]]


def to_doc(c: dict, vec: list[float]) -> dict:
    return {
        "content": c["text"],
        "title": c["title"],
        "date": c["date"] or None,
        "author": c["source"],
        "path_or_url": f"{c['doc_id']}.txt",
        "filename": f"{c['doc_id']}.txt",
        "file_size": len(c["text"]),
        "process_source": "fetch-rfc.py",
        "language": "en",
        "embedding_model_name": EMBED_MODEL,
        "embedding": vec,
    }


def ensure_index(recreate: bool = False) -> str:
    try:
        _req(f"{ES}/{INDEX}", method="GET")
        if not recreate:
            return "exists"
        _req(f"{ES}/{INDEX}", method="DELETE")
    except RuntimeError as e:
        # _req 已把 HTTPError 转成 RuntimeError；404 = 索引不存在，属于预期
        if "HTTP 404" not in str(e):
            raise
    # 建索引的 body 必须包一层 "mappings"，直接发 {"properties": ...} 会被
    # ES 拒为 `unknown key [properties] for create index`（8.17 实测）
    _req(f"{ES}/{INDEX}", json.dumps({"mappings": MAPPING}).encode(), method="PUT")
    return "created"


def bulk(docs: list[dict]) -> tuple[int, list[str]]:
    lines = []
    for d in docs:
        lines.append(json.dumps({"index": {"_index": INDEX}}))
        lines.append(json.dumps(d, ensure_ascii=False))
    body = ("\n".join(lines) + "\n").encode()
    out = json.loads(_req(f"{ES}/_bulk", body, timeout=300))
    errs = [i["index"]["error"] for i in out.get("items", [])
            if "error" in i.get("index", {})]
    return len(out.get("items", [])) - len(errs), errs


def self_test() -> int:
    ok = total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail and not cond else ""))

    c = {"text": "abc", "title": "T", "date": "2022-06-01", "source": "S",
         "doc_id": "d1", "chunk_id": "d1-0000"}
    d = to_doc(c, [0.0] * 1024)
    check("E1 字段齐全", set(d) == {"content", "title", "date", "author", "path_or_url",
                                    "filename", "file_size", "process_source",
                                    "language", "embedding_model_name", "embedding"})
    check("E2 content 而非 text", "text" not in d and d["content"] == "abc")
    check("E3 向量维度", len(d["embedding"]) == 1024)
    check("E4 溯源不是 corpus-chunk.py", d["process_source"] == "fetch-rfc.py")
    check("E5 空日期给 None 不给空串", to_doc({**c, "date": ""}, [0.0] * 1024)["date"] is None)
    print(f"\n[import-rfc-es] {ok}/{total}")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="RFC 语料 → ES")
    ap.add_argument("--src", default="/tmp/rfc.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--recreate", action="store_true", help="先删掉同名索引再建")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not AUTH:
        print("必须给 ES_AUTH（形如 elastic:<密码>）", file=sys.stderr)
        return 2

    rows = []
    with open(a.src, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if a.limit and i >= a.limit:
                break
            rows.append(json.loads(line))
    print(f"读入 {len(rows)} 块")

    print(f"索引 {INDEX}: {ensure_index(a.recreate)}")
    t0 = time.time()
    n_ok = n_err = 0
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        vecs = embed([c["text"] for c in chunk])
        got, errs = bulk([to_doc(c, v) for c, v in zip(chunk, vecs)])
        n_ok += got
        n_err += len(errs)
        print(f"  批 {i // a.batch + 1}: 提交 {len(chunk)} / 索引 {got}"
              + (f" · 错误 {errs[0][:100]}" if errs else ""), flush=True)
    dt = time.time() - t0
    print(f"\n[import-rfc-es] 索引 {n_ok} · 失败 {n_err} · {dt:.1f}s"
          f"（{n_ok / dt:.1f} 块/秒）")
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
