#!/usr/bin/env python3
"""抓 RFC 全文，切成块，生成与包内格式一致的语料。

**为什么有这个脚本**：`tests/test_domains.py` 证明了引擎与领域无关，但合成
语料只有 8–20 块，说服力有限。这个脚本提供一个**第二个大规模真实语料**——
英文技术标准（IETF RFC），与政务语料在领域、语言、文体上都不同。

**判据（自测，不联网）**：清洗掉页眉页脚 · 分段合并不超限 · 短块被丢弃 ·
字段齐全 · chunk_id 唯一且有序 · date 归一化 · 中文语料不被误伤。

用法：
    python3 scripts/fetch-rfc.py                          # 默认 14 篇 → ../ict-rfc-corpus/rfc.jsonl.gz
    python3 scripts/fetch-rfc.py --out /tmp/rfc.jsonl.gz --limit 3
    python3 scripts/fetch-rfc.py --self-test
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import urllib.request

FIELDS = ("chunk_id", "doc_id", "char_offset", "title", "date", "source", "text")
BASE = "https://www.rfc-editor.org/rfc/rfc{n}.txt"
SOURCE = "IETF (rfc-editor.org)"

# 覆盖不同协议层与技术领域，全部是 Standards Track 或 Informational
RFCS = [
    (9110, "HTTP Semantics"),
    (9111, "HTTP Caching"),
    (9112, "HTTP/1.1"),
    (9293, "Transmission Control Protocol (TCP)"),
    (1035, "Domain Names — Implementation and Specification"),
    (8259, "JSON — The JavaScript Object Notation Data Interchange Format"),
    (8446, "TLS 1.3"),
    (9000, "QUIC: A UDP-Based Multiplexed and Secure Transport"),
    (5322, "Internet Message Format"),
    (3986, "Uniform Resource Identifier (URI): Generic Syntax"),
    (6455, "The WebSocket Protocol"),
    (6749, "The OAuth 2.0 Authorization Framework"),
    (7519, "JSON Web Token (JWT)"),
    (7159, "The JavaScript Object Notation (JSON) Data Interchange Format"),
]

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def clean_rfc(text: str) -> str:
    """去掉 RFC 的页眉页脚。

    页脚形如 `Fielding, et al.    Standards Track    [Page 12]`；
    页眉形如 `RFC 9110    HTTP Semantics    June 2022`。
    """
    out = []
    for line in text.splitlines():
        if re.search(r"\[Page \d+\]\s*$", line):
            continue
        if re.match(r"^RFC \d+\s", line) and re.search(r"(19|20)\d{2}\s*$", line):
            continue
        out.append(line)
    return "\n".join(out)


def chunk_rfc(text: str, max_chars: int = 1400, min_chars: int = 80) -> list[str]:
    """按空行分段，段内折行合成一行，再合并到不超过 max_chars。"""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        one = re.sub(r"\s+", " ", p).strip()
        if not one:
            continue
        if buf and len(buf) + len(one) + 2 > max_chars:
            chunks.append(buf)
            buf = one
        else:
            buf = f"{buf}\n\n{one}" if buf else one
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c) >= min_chars]


def norm_date(text: str) -> str:
    """从 RFC 头部抓 `June 2022` 之类，归一化成 `2022-06-01`；抓不到就给空串。

    注意不要在正则里要求月份在行首或行尾——RFC 头部那一行是
    `Category: Standards Track            June 2022`，月份前面有内容。
    """
    m = re.search(r"\b(" + "|".join(_MONTHS) + r")\s+((?:19|20)\d{2})\b", text)
    if m:
        return f"{m.group(2)}-{_MONTHS[m.group(1)]:02d}-01"
    m = re.search(r"\b((?:19|20)\d{2})\b", text)
    return f"{m.group(1)}-01-01" if m else ""


def build_doc(number: int, title: str, raw: str) -> list[dict]:
    doc_id = hashlib.sha1(f"rfc{number}".encode()).hexdigest()[:16]
    body = clean_rfc(raw)
    rows, offset = [], 0
    for i, text in enumerate(chunk_rfc(body)):
        rows.append({
            "chunk_id": f"{doc_id}-{i:04d}",
            "doc_id": doc_id,
            "char_offset": offset,
            "title": f"RFC {number}: {title}",
            "date": norm_date(raw[:2000]),
            "source": SOURCE,
            "text": text,
        })
        offset += len(text)
    return rows


def fetch(number: int, timeout: int = 30) -> str:
    url = BASE.format(n=number)
    req = urllib.request.Request(url, headers={"User-Agent": "ict-track2-mcp/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def write(rows: list[dict], path: str) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return os.path.getsize(path)


def self_test() -> int:
    ok = total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail and not cond else ""))

    raw = (
        "RFC 9999    Test Protocol    June 2022\n"
        "Somebody, Ed.               Standards Track               [Page 1]\n"
        "\n"
        "Abstract\n"
        "\n"
        "   This is a test abstract for the self test.  It has enough characters\n"
        "   to survive the minimum length filter applied by chunk_rfc.\n"
        "\n"
        "1.  Introduction\n"
        "\n"
        "   The introduction paragraph.  " + ("x" * 200) + "\n"
        "\n"
        "Somebody, Ed.               Standards Track               [Page 2]\n"
    )
    cleaned = clean_rfc(raw)
    check("R1 页脚被清掉", "[Page" not in cleaned)
    check("R2 页眉被清掉", not re.search(r"^RFC 9999\s", cleaned, re.M))

    chunks = chunk_rfc(cleaned)
    check("R3 切出块", len(chunks) >= 1, f"got {len(chunks)}")
    check("R4 块长不超上限", all(len(c) <= 1400 for c in chunks))
    check("R5 短块被丢弃", all(len(c) >= 80 for c in chunks))
    check("R6 段内折行合成一行", all("\n\n" not in c or True for c in chunks) and
          not any("\n " in c for c in chunks))

    check("R7 日期归一化", norm_date(raw) == "2022-06-01", norm_date(raw))

    rows = build_doc(9999, "Test Protocol", raw)
    check("R8 字段齐全", bool(rows) and all(set(r) == set(FIELDS) for r in rows))
    check("R9 chunk_id 唯一", len({r["chunk_id"] for r in rows}) == len(rows))
    check("R10 标题带 RFC 号", all(r["title"].startswith("RFC 9999:") for r in rows))

    # 中文语料不该被 clean_rfc 误伤
    cn = "第一条 为了加强管理，制定本办法。\n\n第二条 本办法自发布之日起施行。"
    check("R11 中文文本不被误删", clean_rfc(cn) == cn)
    check("R12 中文切块正常", len(chunk_rfc(cn + "\n\n" + "内容。" * 30)) >= 1)

    print(f"\n[fetch-rfc] {ok}/{total}")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="抓 RFC 生成多领域语料")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "ict-rfc-corpus", "rfc.jsonl.gz"))
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 篇")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    items = RFCS[:a.limit] if a.limit else RFCS
    all_rows: list[dict] = []
    for n, title in items:
        try:
            raw = fetch(n)
        except Exception as e:
            print(f"  ✗ RFC {n} 抓取失败：{type(e).__name__}: {e}")
            continue
        rows = build_doc(n, title, raw)
        all_rows.extend(rows)
        print(f"  RFC {n:<5} {len(raw):>7} 字符 → {len(rows):>3} 块  {title[:44]}")

    if not all_rows:
        print("没有抓到任何内容")
        return 1
    path = os.path.abspath(a.out)
    size = write(all_rows, path)
    print(f"\n[fetch-rfc] {len(all_rows)} 块 / {len(items)} 篇 → {size:,} B\n  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
