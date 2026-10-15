#!/usr/bin/env python3
"""JSONL 语料盘点（流式，内存安全）：记录数 / 解析失败 / 字段 / 日期范围 / 正文长度分布 / 来源分布 / 重复标题。

用法：python3 tools/corpus-inventory.py <file.jsonl> [<file2.jsonl> ...] [--json 出.json]
判据：① 记录数 > 0；② 解析失败率为 0；③ 日期范围可确定；④ 正文长度中位数 > 0。
"""
import json, sys, hashlib, statistics as st, collections, datetime as dt, os


def norm_time(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(v / 1000 if v > 1e11 else v).strftime("%Y-%m-%d")
        except Exception:
            return None
    s = str(v)
    return s[:10] if len(s) >= 10 and s[:4].isdigit() else None


def scan(path, text_keys=("contentText", "content", "text", "body")):
    n = bad = 0
    keys = collections.Counter()
    dates = []
    lens = []
    srcs = collections.Counter()
    titles = set()
    html = 0
    first = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                o = json.loads(line)
            except Exception:
                bad += 1
                continue
            if first is None:
                first = {k: (str(v)[:80] if not isinstance(v, (dict, list)) else type(v).__name__) for k, v in o.items()}
            keys.update(o.keys())
            d = norm_time(o.get("dataTime"))
            if d:
                dates.append(d)
            for k in text_keys:
                v = o.get(k)
                if isinstance(v, str) and v:
                    lens.append(len(v))
                    if "<p>" in v or "</div>" in v or "<br" in v:
                        html += 1
                    break
            s = o.get("publishSource") or o.get("source")
            if isinstance(s, str) and s:
                srcs[s] += 1
            t = o.get("title")
            if isinstance(t, str) and t:
                titles.add(t)
    out = {
        "file": os.path.basename(path),
        "bytes": os.path.getsize(path),
        "records": n,
        "parse_failures": bad,
        "fields": dict(keys),
        "first_record_shape": first,
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "date_days": len(set(dates)),
        "text_len": {
            "n": len(lens),
            "min": min(lens) if lens else None,
            "p25": int(st.quantiles(lens, n=4)[0]) if len(lens) > 3 else None,
            "median": int(st.median(lens)) if lens else None,
            "p75": int(st.quantiles(lens, n=4)[2]) if len(lens) > 3 else None,
            "max": max(lens) if lens else None,
            "total_chars": sum(lens),
        },
        "html_records": html,
        "unique_titles": len(titles),
        "top_sources": srcs.most_common(10),
    }
    return out


def main():
    argv = sys.argv[1:]
    outf = None
    if "--json" in argv:
        i = argv.index("--json")
        outf = argv[i + 1]
        del argv[i:i + 2]
    args = argv
    res = [scan(p) for p in args]
    for r in res:
        print("=== %s ===" % r["file"])
        print("  字节=%d 记录=%d 解析失败=%d 唯一标题=%d 含 HTML 记录=%d"
              % (r["bytes"], r["records"], r["parse_failures"], r["unique_titles"], r["html_records"]))
        print("  字段=%s" % r["fields"])
        print("  日期=%s → %s（去重 %s 天）" % (r["date_min"], r["date_max"], r["date_days"]))
        print("  正文长度: min=%s p25=%s 中位=%s p75=%s max=%s 总字符=%s"
              % (r["text_len"]["min"], r["text_len"]["p25"], r["text_len"]["median"],
                 r["text_len"]["p75"], r["text_len"]["max"], r["text_len"]["total_chars"]))
        print("  来源 Top: %s" % (r["top_sources"][:3],))
    if outf:
        with open(outf, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print("落盘 %s" % outf)
    ok = all(r["records"] > 0 and r["parse_failures"] == 0 and r["date_min"] and (r["text_len"]["median"] or 0) > 0 for r in res)
    print("判据：%s" % ("PASS 4/4" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
