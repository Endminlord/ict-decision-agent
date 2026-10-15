#!/usr/bin/env python3
"""政务语料切分与出处标注（《开发设计文档》§4.6 处理流程第 ⑥ 步）——流式，内存安全。

做的事：在原文里找安全断点（段落换行后、句末标点后）→ 贪心取块 → 每块记录**出处锚**
       （doc_id + 原文起始字符偏移 char_offset）→ 输出 chunks.jsonl（供第 ⑦ 步知识库导入）。

关键约束：**块是原文的忠实切片**（chunk == text[char_offset : char_offset+len(chunk)]），
所以出处锚可原样回查。不做 strip、不做 join——首版用「片段 strip + join」拼块，
300 个抽样里 81% 回查不一致，是判据 D3 把它抓出来的。

判据（全过才算 PASS）：
  D1 输出块数 > 0
  D2 每块长度 ∈ [min_len, max_len]
  D3 出处可回查：抽样块按 (doc_id, char_offset) 回源切片，必须与块文本逐字符一致
  D4 幂等：同一输入跑两次，输出 sha256 相同

用法：python3 tools/corpus-chunk.py <clean.jsonl> --out <dir> [--min-len 80] [--target 800] [--max 1200] [--overlap 100] [--sample 300] [--limit N] [--self-test]
"""
import json, re, sys, os, hashlib, random, statistics as st


def breakpoints(text):
    """原文中的安全断点（字符位置，含 0 与 len）。"""
    bps = {0, len(text)}
    for m in re.finditer("\n", text):
        bps.add(m.end())
    for m in re.finditer("[。！？；!?;]", text):
        bps.add(m.end())
    return sorted(bps)


def chunk_text(text, min_len, target, max_len, overlap):
    """返回 [(chunk_str, start_offset)]；chunk 恒为 text 的忠实切片。"""
    bps = breakpoints(text)
    n = len(text)
    res, start = [], 0
    while start < n:
        want = start + target
        cand = [b for b in bps if want <= b <= start + max_len]
        if cand:
            end = cand[0]
        else:
            c2 = [b for b in bps if start + min_len <= b <= start + max_len]
            end = c2[-1] if c2 else min(start + max_len, n)
        chunk = text[start:end]
        if len(chunk) >= min_len:
            res.append((chunk, start))
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)
        prev = [b for b in bps if b <= nxt]
        nxt = prev[-1] if prev else nxt
        start = nxt if nxt > start else end
    return res


def run(src, outdir, min_len, target, max_len, overlap, limit=0):
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, "chunks.jsonl")
    stt = {"source": os.path.basename(src), "source_bytes": os.path.getsize(src),
           "params": {"min_len": min_len, "target": target, "max_len": max_len, "overlap": overlap},
           "in_docs": 0, "out_chunks": 0, "docs_without_chunk": 0, "chunk_chars": 0,
           "slice_mismatch": 0, "len_sample": []}
    with open(out_path, "w", encoding="utf-8") as fo:
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if limit and stt["in_docs"] >= limit:
                    break
                stt["in_docs"] += 1
                o = json.loads(line)
                doc_id, text = o.get("id", ""), o.get("text", "")
                cs = chunk_text(text, min_len, target, max_len, overlap)
                if not cs:
                    stt["docs_without_chunk"] += 1
                    continue
                for i, (c, s) in enumerate(cs):
                    if text[s:s + len(c)] != c:
                        stt["slice_mismatch"] += 1
                    stt["out_chunks"] += 1
                    stt["chunk_chars"] += len(c)
                    if len(stt["len_sample"]) < 200000:
                        stt["len_sample"].append(len(c))
                    fo.write(json.dumps({
                        "chunk_id": "%s-%04d" % (doc_id, i), "doc_id": doc_id,
                        "char_offset": s, "title": o.get("title", ""), "date": o.get("date", ""),
                        "source": o.get("source", ""), "text": c}, ensure_ascii=False) + "\n")
    ls = sorted(stt.pop("len_sample"))
    stt["chunk_len"] = {"min": ls[0] if ls else None, "median": int(st.median(ls)) if ls else None,
                        "p90": ls[int(len(ls) * .9)] if ls else None, "max": ls[-1] if ls else None}
    stt["chunks_bytes"] = os.path.getsize(out_path)
    stt["chunks_sha256"] = hashlib.sha256(open(out_path, "rb").read()).hexdigest()
    return stt, out_path


def verify_offsets(src, out_path, sample=300, seed=7):
    """D3：抽样块，回源文件按 (doc_id, char_offset) 切片比对。"""
    rnd = random.Random(seed)
    picks = []
    with open(out_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < sample:
                picks.append(json.loads(line))
            elif rnd.random() < sample / float(i + 1):
                picks[rnd.randrange(sample)] = json.loads(line)
    want = {}
    for p in picks:
        want.setdefault(p["doc_id"], []).append(p)
    ok = bad = 0
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            did = o.get("id", "")
            if did not in want:
                continue
            t = o.get("text", "")
            for p in want.pop(did):
                if t[p["char_offset"]:p["char_offset"] + len(p["text"])] == p["text"]:
                    ok += 1
                else:
                    bad += 1
            if not want:
                break
    return ok, bad, len(picks)


def self_test():
    import tempfile
    d = tempfile.mkdtemp()
    src = os.path.join(d, "c.jsonl")
    long_text = "第一段内容。" * 60 + "\n" + "第二段内容。" * 60 + "\n短"
    rows = [{"id": "d1", "title": "T1", "date": "2024-01-01", "source": "S", "text": long_text},
            {"id": "d2", "title": "T2", "date": "2024-01-02", "source": "S", "text": "太短"}]
    with open(src, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    s1, p1 = run(src, os.path.join(d, "o1"), 80, 800, 1200, 100)
    ok, bad, n = verify_offsets(src, p1, sample=50)
    s2, p2 = run(src, os.path.join(d, "o2"), 80, 800, 1200, 100)
    s3, p3 = run(src, os.path.join(d, "o3"), 5000, 800, 1200, 100)
    cases = [
        ("N1 切出块且块长在界内", s1["out_chunks"] > 0 and s1["chunk_len"]["min"] >= 80 and s1["chunk_len"]["max"] <= 1200),
        ("N2 出处可回查（切片逐字符一致）", bad == 0 and ok == n and n > 0),
        ("N3 太短文档被丢弃并计数", s1["docs_without_chunk"] == 1),
        ("N4 幂等（两次跑 sha256 相同）", s1["chunks_sha256"] == s2["chunks_sha256"]),
        ("N5 负对照：min_len 调到 5000 → 输出 0 块", s3["out_chunks"] == 0),
    ]
    for name, v in cases:
        print("  %s  %s" % ("PASS" if v else "FAIL", name))
    print("自测：%d/%d 个对照通过" % (sum(1 for _, v in cases if v), len(cases)))
    return 0 if all(v for _, v in cases) else 1


def main():
    argv = sys.argv[1:]
    if "--self-test" in argv:
        return self_test()

    def take(flag, default=None):
        if flag in argv:
            i = argv.index(flag)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    outdir = take("--out")
    min_len = int(take("--min-len", "80"))
    target = int(take("--target", "800"))
    max_len = int(take("--max", "1200"))
    overlap = int(take("--overlap", "100"))
    sample = int(take("--sample", "300"))
    limit = int(take("--limit", "0"))
    if not outdir or not argv:
        print(__doc__)
        return 2
    stt, out_path = run(argv[0], outdir, min_len, target, max_len, overlap, limit)
    ok, bad, n = verify_offsets(argv[0], out_path, sample=sample)
    stt.update(offset_ok=ok, offset_bad_sample=bad, sampled=n)
    for k in ("in_docs", "out_chunks", "docs_without_chunk", "chunk_chars", "chunks_bytes",
              "chunks_sha256", "slice_mismatch"):
        print("  %-20s %s" % (k, stt[k]))
    print("  块长 %s" % stt["chunk_len"])
    checks = [("D1 输出块数 > 0", stt["out_chunks"] > 0),
              ("D2 块长均在 [min_len, max_len]", stt["chunk_len"]["min"] >= min_len and stt["chunk_len"]["max"] <= max_len),
              ("D3 抽样出处回查全部一致", bad == 0 and ok == n and n > 0),
              ("D3b 全量切片自检零失配", stt["slice_mismatch"] == 0)]
    for name, v in checks:
        print("  %s  %s" % ("ok " if v else "FAIL", name))
    with open(os.path.join(outdir, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stt, f, ensure_ascii=False, indent=1)
    good = all(v for _, v in checks)
    print("判据：%s（抽样 %d 块，一致 %d，不一致 %d）" % ("PASS" if good else "FAIL", n, ok, bad))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
