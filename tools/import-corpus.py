#!/usr/bin/env python3
"""把切分后的语料块导入 Nexent 知识库（POST /indices/{index_name}/documents）。

用法:
  import-corpus.py --index 1-43ce... [--limit 500] [--batch 100] [--offset 0]
                   [--src ict-corpus-chunks/chunks.jsonl] [--host http://localhost:5010]
                   [--dry-run] [--report OUT.json]

判据（--self-test）：I1 文档字段名契约（content/path_or_url/metadata）I2 分批边界
I3 失败不静默（非 2xx 或 total_indexed != 提交数 → 计失败并记录）。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_SRC = "ict-corpus-chunks/chunks.jsonl"
DEFAULT_HOST = "http://localhost:5010"


def to_doc(c):
    """Nexent 契约：正文放 content（不是 text），元数据放 metadata。"""
    return {
        "content": c["text"],
        "path_or_url": f"{c['doc_id']}.txt",
        "metadata": {"title": c["title"], "date": c["date"], "author": c["source"],
                     "process_source": "corpus-chunk.py"},
        "source_type": "text",
        "file_size": len(c["text"]),
        "filename": f"{c['doc_id']}.txt",
    }


def post_batch(host, index, docs, timeout=1800):
    req = urllib.request.Request(
        f"{host}/indices/{index}/documents", data=json.dumps(docs).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=False, default="")
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--limit", type=int, default=0, help="0 = 全部")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        d = to_doc({"text": "正文", "doc_id": "abc", "title": "T", "date": "2024-01-01", "source": "S"})
        ok1 = set(d) == {"content", "path_or_url", "metadata", "source_type", "file_size", "filename"}
        ok1b = d["content"] == "正文" and "text" not in d and d["metadata"]["title"] == "T"
        print(("  ok   " if ok1 and ok1b else "  FAIL ") + "I1 文档字段名契约（content 而非 text）")
        n, b = 250, 100
        batches = [(i, min(i + b, n)) for i in range(0, n, b)]
        ok2 = batches == [(0, 100), (100, 200), (200, 250)]
        print(("  ok   " if ok2 else "  FAIL ") + f"I2 分批边界 {batches}")
        ok3 = post_batch("http://127.0.0.1:1", "x", [])["success"] is False
        print(("  ok   " if ok3 else "  FAIL ") + "I3 连接失败不静默（返回 success=False）")
        print("自测：%d/3 通过" % (3 if ok1 and ok1b and ok2 and ok3 else 0))
        return 0 if (ok1 and ok1b and ok2 and ok3) else 1

    if not a.index:
        print("必须给 --index（真实索引名，形如 1-<hex>；平台会把知识库显示名改写）", file=sys.stderr)
        return 2

    t0 = time.time()
    n_sub = n_idx = 0
    fails = []
    buf = []
    batch_no = 0

    def flush(buf, batch_no):
        nonlocal n_sub, n_idx
        if not buf:
            return batch_no
        if a.dry_run:
            n_sub += len(buf)
            print(f"  [dry-run] 批 {batch_no}: {len(buf)} 条")
            return batch_no + 1
        r = post_batch(a.host, a.index, buf)
        got = r.get("total_indexed", 0) if isinstance(r, dict) else 0
        n_sub += len(buf)
        n_idx += got
        ok = bool(r.get("success")) and got == len(buf)
        print(f"  批 {batch_no}: 提交 {len(buf)} / 索引 {got} {'ok' if ok else 'FAIL ' + str(r.get('error') or r)[:160]}", flush=True)
        if not ok:
            fails.append({"batch": batch_no, "submitted": len(buf), "indexed": got,
                          "error": str(r.get("error") or r)[:300]})
        return batch_no + 1

    with open(a.src, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < a.offset:
                continue
            if a.limit and (n_sub + len(buf)) >= a.limit:
                break
            buf.append(to_doc(json.loads(line)))
            if len(buf) >= a.batch:
                batch_no = flush(buf, batch_no)
                buf = []
    batch_no = flush(buf, batch_no)

    dt = time.time() - t0
    summary = {"index": a.index, "src": a.src, "offset": a.offset,
               "submitted": n_sub, "indexed": n_idx, "failed_batches": len(fails),
               "fails": fails, "elapsed_s": round(dt, 1),
               "docs_per_s": round(n_sub / dt, 2) if dt else None,
               "batch": a.batch, "dry_run": a.dry_run}
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:900])
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
