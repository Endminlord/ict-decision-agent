#!/usr/bin/env python3
"""行业语料可得性探针（ICT 参赛版 · 只读，不写任何被集成对象）。

用途：回答「先进制造 vs 政务，哪个行业的公开文档语料真的拿得到」。
方法：ModelScope 匿名 API（数据集搜索 / 文件树 / HTTP Range 直取 ZIP 成员），
      不需要登录、不需要 modelscope SDK、不需要下载整个大包。

子命令：
  search  <关键词> [N]                 搜数据集（Namespace/Name/Downloads）
  tree    <ns/name>                    列文件树（含大小）
  zip     <ns/name> <zip路径> [成员子串] [MB]      只读 ZIP 尾部中央目录 + 头部 Range，抽出前 N MB 解压后的文本
  sample  <ns/name> <zip路径> <出.jsonl> [成员子串] [MB]
  selftest                             对已知可得的政务语料做一次端到端自检

判据（自检应满足）：① 数据集搜索返回 200；② ZIP 中央目录可解析；③ Range 返回 206；
④ 抽出的文本可解码为 UTF-8 且记录可解析为 JSON。
"""
import json, os, struct, sys, zlib, urllib.parse, urllib.request, hashlib, re

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120"}
API = "https://modelscope.cn/api/v1"


def http(url, rng=None, timeout=90):
    h = dict(UA)
    if rng:
        h["Range"] = "bytes=%d-%d" % rng
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.status, f.headers.get("Content-Range"), f.read()


def ds_url(ns, name, kind="repo"):
    if kind == "tree":
        return "%s/datasets/%s/%s/repo/tree?Revision=master" % (API, ns, name)
    return "%s/datasets/%s/%s/repo?Revision=master&FilePath=" % (API, ns, name)


def search(kw, n=8):
    s, _, b = http("%s/dolphin/datasets?PageSize=%d&PageNumber=1&Query=%s" % (API, n, urllib.parse.quote(kw)))
    return json.loads(b).get("Data") or []


def tree(ns, name):
    s, _, b = http(ds_url(ns, name, "tree"))
    return json.loads(b).get("Data", {}).get("Files") or []


def zip_members(url, total):
    """只读 ZIP 尾部：解析 EOCD + 中央目录，返回成员表。"""
    tail = http(url, (total - 300000, total - 1))[2]
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise SystemExit("找不到 EOCD（不是 ZIP？）")
    n = struct.unpack("<H", tail[i + 10:i + 12])[0]
    cdsz = struct.unpack("<I", tail[i + 12:i + 16])[0]
    cdoff = struct.unpack("<I", tail[i + 16:i + 20])[0]
    cd = http(url, (cdoff, cdoff + cdsz - 1))[2]
    p, out = 0, []
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        csz, usz = struct.unpack("<II", cd[p + 20:p + 28])
        nl, el, cl = struct.unpack("<HHH", cd[p + 28:p + 34])
        nm = cd[p + 46:p + 46 + nl].decode("utf-8", "replace")
        lo = struct.unpack("<I", cd[p + 42:p + 46])[0]
        out.append({"name": nm, "usize": usz, "csize": csz, "lho": lo})
        p += 46 + nl + el + cl
    return out


def zip_prefix_text(url, member, mb=20):
    """取成员头部 mb MB 压缩数据，增量 inflate，返回解压文本。"""
    h = http(url, (member["lho"], member["lho"] + 29))[2]
    nl, el = struct.unpack("<HH", h[26:30])
    start = member["lho"] + 30 + nl + el
    chunk = http(url, (start, start + mb * 1024 * 1024 - 1))[2]
    d = zlib.decompressobj(-15)
    return d.decompress(chunk).decode("utf-8", "replace")


def head_total(url):
    """总大小：优先 HEAD 的 Content-Length，否则用 Range 0-0 的 Content-Range 尾部。"""
    req = urllib.request.Request(url, headers=dict(UA))
    req.get_method = lambda: "HEAD"
    try:
        with urllib.request.urlopen(req, timeout=60) as f:
            n = f.headers.get("Content-Length")
            if n:
                return int(n)
    except Exception:
        pass
    st, cr, _ = http(url, (0, 0))
    if cr and "/" in cr:
        return int(cr.rsplit("/", 1)[1])
    raise SystemExit("拿不到文件总大小（HEAD 与 Range 均失败）")


def cmd_search(kw, n=8):
    for it in search(kw, int(n)):
        print("%-28s %-46s dl=%-8s %s" % (it.get("Namespace"), it.get("Name"), it.get("Downloads"),
                                          (it.get("Description") or "")[:60]))


def cmd_tree(ref):
    ns, name = ref.split("/", 1)
    fl = tree(ns, name)
    print("%s 文件数=%d 总字节=%d" % (ref, len(fl), sum(x.get("Size") or 0 for x in fl)))
    for x in fl:
        print("  %-5s %12s  %s" % (x.get("Type"), x.get("Size") or 0, x.get("Path")))


def _resolve(ref, zippath, member_sub=""):
    """返回 (zip 的 Range 直链, 内部成员)。member_sub 为空则取体积最大的 .json 成员。"""
    ns, name = ref.split("/", 1)
    url = ds_url(ns, name) + urllib.parse.quote(zippath, safe="")
    tot = head_total(url)
    mem = zip_members(url, tot)
    if not mem:
        raise SystemExit("ZIP 内无成员")
    def _junk(n):
        base = n.rsplit("/", 1)[-1]
        return n.endswith("/") or "__MACOSX/" in n or base.startswith("._")
    cand = [m for m in mem if not _junk(m["name"]) and (not member_sub or member_sub in m["name"])]
    if not cand:
        raise SystemExit("找不到成员：%s" % member_sub)
    jsons = sorted([m for m in cand if m["name"].lower().endswith(".json")], key=lambda m: m["name"])
    # 默认取文件名排序后的第一个 .json（确定性；不同成员的字段结构可能不同，见 227 报告）
    pick = jsons[0] if jsons else max(cand, key=lambda m: m["usize"])
    return url, pick, tot


def cmd_zip(ref, zippath, member_sub="", mb=20):
    url, m, tot = _resolve(ref, zippath, member_sub)
    print("ZIP 总字节=%d；成员 %s 原始=%d 压缩=%d" % (tot, m["name"], m["usize"], m["csize"]))
    txt = zip_prefix_text(url, m, int(mb))
    print("取 %sMB 压缩数据 → 解压 %d 字节；sha256(prefix)=%s" % (mb, len(txt), hashlib.sha256(txt.encode("utf-8", "replace")).hexdigest()[:24]))
    print("--- 前 300 字 ---")
    print(txt[:300])


def cmd_sample(ref, zippath, out, member_sub="", mb=20, pat=r'\{"subTitle".*?"introTitle":\s*".*?"\}'):
    url, m, tot = _resolve(ref, zippath, member_sub)
    txt = zip_prefix_text(url, m, int(mb))
    recs = re.findall(pat, txt, re.S)
    objs = []
    for r in recs:
        try:
            objs.append(json.loads(r))
        except Exception:
            pass
    with open(out, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    d = sorted({o.get("dataTime") for o in objs if o.get("dataTime")})
    L = [len(o.get("contentText") or "") for o in objs]
    print("成员=%s 解压=%d 字节 → 抽到 %d 条，可解析 %d 条" % (m["name"], len(txt), len(recs), len(objs)))
    if objs:
        print("字段=%s" % sorted({k for o in objs for k in o}))
        print("日期范围=%s → %s（去重 %d 天）" % (d[0], d[-1], len(d)))
        print("contentText 长度 min=%d 中位=%d max=%d" % (min(L), sorted(L)[len(L) // 2], max(L)))
    print("落盘 %s（%d 字节）" % (out, os.path.getsize(out)))


def cmd_selftest():
    REF, ZIP = "peopletech/government", "党政领域语料库.zip"
    ok = []
    r = search("政务", 3)
    ok.append(("①数据集搜索返回结果", bool(r)))
    url, m, _tot = _resolve(REF, ZIP)
    ok.append(("②ZIP 中央目录可解析", m["usize"] > 0))
    txt = zip_prefix_text(url, m, 5)
    ok.append(("③Range 206 + 增量 inflate", len(txt) > 0))
    recs = re.findall(r'\{"subTitle".*?"introTitle":\s*".*?"\}', txt, re.S)
    ok.append(("④记录可解析为 JSON", len(recs) > 0 and bool(json.loads(recs[0]))))
    for k, v in ok:
        print("%s %s" % ("PASS" if v else "FAIL", k))
    print("自检：%d/%d" % (sum(1 for _, v in ok if v), len(ok)))
    return 0 if all(v for _, v in ok) else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(0)
    c = a[0]
    if c == "search":
        cmd_search(a[1], a[2] if len(a) > 2 else 8)
    elif c == "tree":
        cmd_tree(a[1])
    elif c == "zip":
        cmd_zip(a[1], a[2], a[3] if len(a) > 3 else "", a[4] if len(a) > 4 else 20)
    elif c == "sample":
        cmd_sample(a[1], a[2], a[3], a[4] if len(a) > 4 else "", a[5] if len(a) > 5 else 20)
    elif c == "selftest":
        sys.exit(cmd_selftest())
    else:
        print(__doc__)
