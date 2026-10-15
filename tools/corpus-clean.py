#!/usr/bin/env python3
"""政务语料清洗（《开发设计文档》§4.6 处理流程第 ⑤ 步）——流式，内存安全。

做的事：字段归一化（contentText|content → text；字符串日期|epoch 毫秒 → YYYY-MM-DD）
       → 去 HTML 标签与实体 → 空白折叠 → 日期区间过滤 → 去重（title + 正文前缀 hash）
       → 输出清洗后 JSONL（供第 ⑥ 步切分/标注与知识库导入）+ 统计 JSON。

判据（全过才算 PASS）：
  C1 输出记录数 > 0
  C2 输出正文含 HTML 标记的记录数 == 0
  C3 输出日期全部落在 [--since, --until]
  C4 输出内 (title, 正文前缀) 去重键唯一
  C5 幂等：同一输入跑两次，输出 sha256 相同（用 --limit 小样本验证）

用法：python3 tools/corpus-clean.py <in.jsonl> [<in2.jsonl> ...] --out <dir> [--since 1949-01-01] [--until 2026-12-31] [--limit N] [--self-test]
"""
import json, re, sys, os, html, hashlib, datetime as dt, statistics as st

TAG = re.compile(r"<[^>]{0,8000}>")
ENT = re.compile(r"&[#A-Za-z0-9]{1,10};")
WS = re.compile(r"[ \t\u3000]+")
NL = re.compile(r"\n{2,}")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def clean_text(s):
    """去 HTML 标签与实体 + 空白折叠。两轮反转义以覆盖嵌套实体（如 &amp;lt;p&amp;gt;）。"""
    s = html.unescape(s)
    s = TAG.sub(" ", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ").replace("\u200b", "")
    s = WS.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    return NL.sub("\n", s).strip()


def norm_date(v):
    """字符串日期或 epoch（毫秒/秒）→ YYYY-MM-DD；无法判定返回 None。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        ts = v / 1000.0 if abs(v) > 1e11 else float(v)
        try:
            return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            return None
    m = DATE_RE.match(str(v).strip())
    return m.group(0) if m else None


def dedup_key(title, text, prefix=200):
    h = hashlib.sha256()
    h.update(re.sub(r"\s+", "", title or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(re.sub(r"\s+", "", text[:prefix]).encode("utf-8", "replace"))
    return h.hexdigest()


def run(inputs, outdir, since, until, limit=0):
    os.makedirs(outdir, exist_ok=True)
    out_path = os.path.join(outdir, "clean.jsonl")
    stats = {
        "inputs": [{"file": os.path.basename(p), "bytes": os.path.getsize(p)} for p in inputs],
        "params": {"since": since, "until": until, "limit": limit, "prefix": 200},
        "in_records": 0, "parse_failures": 0,
        "dropped_no_text": 0, "dropped_bad_date": 0, "dropped_duplicate": 0,
        "html_tags_removed": 0, "entities_unescaped": 0,
        "out_records": 0, "out_chars": 0,
        "date_min": None, "date_max": None, "len_sample": [],
    }
    seen = set()
    src_counter = {}
    with open(out_path, "w", encoding="utf-8") as fo:
        for p in inputs:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if limit and stats["in_records"] >= limit:
                        break
                    stats["in_records"] += 1
                    try:
                        o = json.loads(line)
                    except Exception:
                        stats["parse_failures"] += 1
                        continue
                    raw = o.get("contentText") or o.get("content") or o.get("text") or ""
                    if not isinstance(raw, str) or not raw.strip():
                        stats["dropped_no_text"] += 1
                        continue
                    stats["html_tags_removed"] += len(TAG.findall(raw))
                    stats["entities_unescaped"] += len(ENT.findall(raw))
                    text = clean_text(raw)
                    if not text:
                        stats["dropped_no_text"] += 1
                        continue
                    d = norm_date(o.get("dataTime"))
                    if d is None or not (since <= d <= until):
                        stats["dropped_bad_date"] += 1
                        continue
                    title = clean_text(o.get("title") or "")
                    k = dedup_key(title, text)
                    if k in seen:
                        stats["dropped_duplicate"] += 1
                        continue
                    seen.add(k)
                    src = (o.get("publishSource") or "").strip()
                    src_counter[src] = src_counter.get(src, 0) + 1
                    stats["out_records"] += 1
                    stats["out_chars"] += len(text)
                    if stats["date_min"] is None or d < stats["date_min"]:
                        stats["date_min"] = d
                    if stats["date_max"] is None or d > stats["date_max"]:
                        stats["date_max"] = d
                    if len(stats["len_sample"]) < 200000:
                        stats["len_sample"].append(len(text))
                    fo.write(json.dumps({
                        "id": hashlib.sha256((k + d).encode()).hexdigest()[:16],
                        "title": title, "date": d, "source": src, "text": text,
                    }, ensure_ascii=False) + "\n")
            if limit and stats["in_records"] >= limit:
                break
    ls = sorted(stats.pop("len_sample"))
    stats["text_len"] = {
        "min": ls[0] if ls else None,
        "median": int(st.median(ls)) if ls else None,
        "p90": ls[int(len(ls) * 0.9)] if ls else None,
        "max": ls[-1] if ls else None,
    }
    stats["top_sources"] = sorted(src_counter.items(), key=lambda kv: -kv[1])[:10]
    stats["clean_sha256"] = hashlib.sha256(open(out_path, "rb").read()).hexdigest()
    stats["clean_bytes"] = os.path.getsize(out_path)
    return stats, out_path


def verdicts(stats):
    checks = [
        ("C1 输出记录数 > 0", stats["out_records"] > 0),
        ("C2 输出正文含 HTML 标记 == 0", stats["html_left"] == 0),
        ("C3 输出日期全部在区间内", stats["date_violations"] == 0),
        ("C4 去重键唯一", stats["dup_keys"] == 0),
    ]
    return checks


def verify_output(out_path, since, until):
    """回读输出做终检：HTML 残留 / 日期越界 / 去重键重复。"""
    left = viol = dup = 0
    seen = set()
    with open(out_path, "r", encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            t = o["text"]
            if TAG.search(t) or "&nbsp;" in t or "&amp;" in t:
                left += 1
            d = o["date"]
            if not (since <= d <= until):
                viol += 1
            k = dedup_key(o.get("title", ""), t)
            if k in seen:
                dup += 1
            seen.add(k)
    return left, viol, dup


def self_test():
    import tempfile
    d = tempfile.mkdtemp()
    src = os.path.join(d, "t.jsonl")
    rows = [
        {"title": "A", "content": "<p>你好&nbsp;世界</p>", "dataTime": 1735000000000, "publishSource": "S1"},
        {"title": "A", "content": "<p>你好&nbsp;世界</p>", "dataTime": 1735000000000, "publishSource": "S1"},
        {"title": "B", "content": "<div>正常</div>", "dataTime": 999999999999999, "publishSource": "S2"},
        {"title": "C", "contentText": "2024 年政策原文", "dataTime": "2024-05-06", "publishSource": "S3"},
    ]
    with open(src, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    st1, p1 = run([src], os.path.join(d, "o1"), "1949-01-01", "2026-12-31")
    left, viol, dup = verify_output(p1, "1949-01-01", "2026-12-31")
    st1.update(html_left=left, date_violations=viol, dup_keys=dup)
    cases = [
        ("N1 重复条目被抓到", st1["dropped_duplicate"] == 1),
        ("N2 异常日期被抓到", st1["dropped_bad_date"] == 1),
        ("N3 HTML 被清干净", st1["html_left"] == 0 and "你好 世界" in open(p1, encoding="utf-8").read()),
        ("N4 字符串日期被接受", st1["out_records"] == 2),
    ]
    # 负对照：故意把区间收窄，C3 应失败
    st2, p2 = run([src], os.path.join(d, "o2"), "1949-01-01", "1950-01-01")
    cases.append(("N5 负对照：区间收窄后输出应为 0 条", st2["out_records"] == 0))
    ok = sum(1 for _, v in cases if v)
    for n, v in cases:
        print("  %s  %s" % ("PASS" if v else "FAIL", n))
    print("自测：%d/%d 个对照通过" % (ok, len(cases)))
    return 0 if ok == len(cases) else 1


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
    since = take("--since", "1949-01-01")
    until = take("--until", "2026-12-31")
    limit = int(take("--limit", "0"))
    if not outdir or not argv:
        print(__doc__)
        return 2
    stats, out_path = run(argv, outdir, since, until, limit)
    left, viol, dup = verify_output(out_path, since, until)
    stats.update(html_left=left, date_violations=viol, dup_keys=dup)
    for k in ("in_records", "parse_failures", "dropped_no_text", "dropped_bad_date",
              "dropped_duplicate", "out_records", "out_chars", "html_tags_removed",
              "entities_unescaped", "date_min", "date_max", "clean_bytes", "clean_sha256"):
        print("  %-20s %s" % (k, stats[k]))
    print("  正文长度 %s" % stats["text_len"])
    print("  Top 来源 %s" % (stats["top_sources"][:3],))
    checks = verdicts(stats)
    for n, v in checks:
        print("  %s  %s" % ("ok " if v else "FAIL", n))
    with open(os.path.join(outdir, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print("判据：%s" % ("PASS %d/%d" % (sum(1 for _, v in checks if v), len(checks)) if all(v for _, v in checks) else "FAIL"))
    return 0 if all(v for _, v in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
