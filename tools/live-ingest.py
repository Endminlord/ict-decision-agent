#!/usr/bin/env python3
"""从 URL 实时抓取政策文本，走完整链路并入本体。

链路：URL → HTML 正文 → 切块 → build.py 抽三元组 → merge-triples.py 增量合并。

设计取舍（2026-10-19 实测后定）：
- 只处理 HTML 正文，不处理 PDF 附件。gov.cn 的 `/zhengce/pdf/` 路径实测 404，
  `gov.cn/gongbao/` 实测 403；能拿到的是政策页本身的 `<p>` 段落。
- 不走 HTTP 代理。实测走代理时 gov.cn 超时，直连 200。
- 抓取失败不中断：逐条记录 status，最后汇总。

用法：
    python3 tools/live-ingest.py --urls https://www.gov.cn/yaowen/... --base ontology/triples-governance.jsonl
    python3 tools/live-ingest.py --urls-file urls.txt --base ... --out /tmp/merged.jsonl
    python3 tools/live-ingest.py --self-test
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
BUILD = os.path.join(PKG, "ontology", "build.py")
MERGE = os.path.join(HERE, "merge-triples.py")
TERMS = os.path.join(PKG, "ontology", "terms.governance.zh.json")

CHUNK_SIZE = 800
MIN_CHUNK = 100
MIN_PARA = 20
TIMEOUT = 15
UA = "Mozilla/5.0 (compatible; ict-decision-agent/1.0)"


_OCR = None


def _get_ocr():
    """OCR 引擎单例 —— 模型加载约 1-2 秒，不要每张图重建。"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def _get_bytes(url):
    """取原始字节。不走代理 —— 实测代理会让 gov.cn 超时。

    `file://` 不能带 headers（Request 会报 unknown url type），单独走 urlopen。
    """
    if url.startswith("file://"):
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.read()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=TIMEOUT) as r:
        return r.read()


def fetch(url):
    """按扩展名分派：图片 → OCR；PDF → pymupdf 文本层；其余 → HTML。

    返回 (payload, err, kind)。kind 决定下游怎么解：
      "html" → 走 extract() 抽 <p>；"text" → OCR/PDF 的纯文本，直接用。
    不走代理 —— 实测代理会让 gov.cn 超时。
    """
    low = url.lower().split("?")[0]
    try:
        raw = _get_bytes(url)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", "html"

    # 图片：OCR（rapidocr-onnxruntime，纯 Python + ONNX，无外部二进制）
    if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")):
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            res, _ = _get_ocr()(img)
            text = "\n".join(r[1] for r in res) if res else ""
            return (text, None, "text") if text.strip() else (None, "OCR 未识别出文字", "text")
        except ImportError as e:
            return None, f"OCR 依赖缺失：{e}", "text"
        except Exception as e:
            return None, f"OCR 失败：{type(e).__name__}: {e}", "text"

    # PDF：pymupdf 抽文本层（覆盖文字版 PDF；扫描件无文本层，走 OCR 需先渲染成图）
    if low.endswith(".pdf"):
        try:
            import fitz
            doc = fitz.open(stream=raw, filetype="pdf")
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            return (text, None, "text") if text.strip() else (None, "PDF 无文本层（扫描件，需先渲染成图再 OCR）", "text")
        except Exception as e:
            return None, f"PDF 解析失败：{type(e).__name__}: {e}", "text"

    # 其余按 HTML
    try:
        return raw.decode("utf-8", errors="ignore"), None, "html"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", "html"


def extract(html_text, url):
    """从 HTML 抽标题与正文段落。只认 <p>，因为政策页正文都在 <p> 里。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    title = html.unescape(m.group(1)).split("_")[0].strip() if m else ""
    if not title:
        title = url.rstrip("/").rsplit("/", 1)[-1].replace(".htm", "").replace(".html", "")
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html_text, re.S | re.I)
    clean = []
    for p in paras:
        t = html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
        if len(t) >= MIN_PARA:
            clean.append(t)
    return title, "\n".join(clean)


def to_chunks(title, body, doc_id, url):
    """按 CHUNK_SIZE 切块。字段名必须是 `text` —— build.py:191 读的是 chunk["text"]。"""
    out = []
    for i in range(0, len(body), CHUNK_SIZE):
        piece = body[i:i + CHUNK_SIZE]
        if len(piece) < MIN_CHUNK:
            continue
        out.append({
            "chunk_id": f"{doc_id}-{i // CHUNK_SIZE:04d}",
            "doc_id": doc_id,
            "char_offset": i,
            "title": title,
            "text": piece,
            "source": url,
            "date": None,
        })
    return out


def discover(index_url, limit=30):
    """从列表页自动发现政策链接。

    实测来源（2026-10-19）：`https://www.gov.cn/zhengce/xxgk/` 是静态 HTML，
    209 KB 里有 50 个 `content_*.htm` 链接，标题就在 <a> 文本里。
    `gongbao/` 与 `gongbao/2024/` 分别返回 898 B 空壳与 403，不可用；
    `/zhengce/pdf/` 是 404（政策页本来就不挂 PDF）。
    """
    h, err, _kind = fetch(index_url)
    if err:
        return [], err
    pat = r'<a[^>]*href="([^"]*content_\d+\.htm[^"]*)"[^>]*>(.*?)</a>'
    out, seen = [], set()
    for m in re.finditer(pat, h, re.S | re.I):
        url = m.group(1)
        if url.startswith("/"):
            url = "https://www.gov.cn" + url
        elif not url.startswith("http"):
            continue
        if url in seen:
            continue
        seen.add(url)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        out.append({"url": url, "title": title})
        if len(out) >= limit:
            break
    return out, None


def ingest(urls, base, out_path, workdir=None):
    tmp = workdir or tempfile.mkdtemp(prefix="live-ingest-")
    chunks_path = os.path.join(tmp, "chunks.jsonl")
    strict_path = os.path.join(tmp, "strict.jsonl")
    loose_path = os.path.join(tmp, "loose.jsonl")

    report = {"urls": [], "chunks": 0, "docs": 0, "strict": 0, "loose": 0}
    chunks = []
    for i, url in enumerate(urls, 1):
        payload, err, kind = fetch(url)
        if err:
            report["urls"].append({"url": url, "status": "failed", "error": err})
            continue
        if kind == "html":
            title, body = extract(payload, url)
        else:
            # OCR / PDF：payload 已是纯文本，标题取首个非空行
            lines = [l.strip() for l in payload.splitlines() if l.strip()]
            title = (lines[0][:60] if lines else url.rstrip("/").rsplit("/", 1)[-1])
            body = "\n".join(lines)
        cs = to_chunks(title, body, f"live-{i:03d}", url)
        if not cs:
            report["urls"].append({"url": url, "status": "no_text", "title": title,
                                   "chars": len(body)})
            continue
        chunks.extend(cs)
        report["urls"].append({"url": url, "status": "ok", "title": title,
                               "chars": len(body), "chunks": len(cs)})

    if not chunks:
        report["error"] = "no chunks extracted"
        return report

    with open(chunks_path, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    subprocess.run([sys.executable, BUILD, "--src", chunks_path, "--terms", TERMS,
                    "--out", strict_path, "--loose-out", loose_path],
                   check=True, capture_output=True)
    report["chunks"] = len(chunks)
    report["docs"] = len({c["doc_id"] for c in chunks})
    report["strict"] = sum(1 for _ in open(strict_path, encoding="utf-8")) if os.path.exists(strict_path) else 0
    report["loose"] = sum(1 for _ in open(loose_path, encoding="utf-8")) if os.path.exists(loose_path) else 0

    # strict 与 loose 都要合并 —— 只并 strict 会丢掉绝大多数实时抓到的三元组
    merged = loose_path
    if report["strict"] and report["loose"]:
        merged = os.path.join(tmp, "both.jsonl")
        with open(merged, "w", encoding="utf-8") as f:
            for p in (strict_path, loose_path):
                f.write(open(p, encoding="utf-8").read())
    elif report["strict"] and not report["loose"]:
        merged = strict_path

    r = subprocess.run([sys.executable, MERGE, "--base", base, "--new", merged,
                        "--out", out_path], check=True, capture_output=True, text=True)
    # merge-triples.py 把统计 JSON 打在 stderr（stdout 留给 --help 与错误），两路都看
    blob = (r.stdout or "") + (r.stderr or "")
    try:
        report["merge"] = json.loads(blob[blob.find("{"):])
    except Exception:
        report["merge"] = {"raw": blob[-500:]}
    report["workdir"] = tmp
    return report


def self_test():
    cases = []

    def ck(name, cond, detail=""):
        cases.append((name, bool(cond), detail))

    h = '<html><head><title>某市人民政府关于优化营商环境的实施意见_政策</title></head><body>' + \
        '<p>' + ('本市持续优化营商环境，深化放管服改革，提升政务服务效能。' * 12) + '</p>' + \
        '<p>短段落</p></body></html>'
    title, body = extract(h, "https://example.gov.cn/a.htm")
    ck("标题去掉站点后缀", title == "某市人民政府关于优化营商环境的实施意见", title)
    ck("只收 >=20 字的段落", "短段落" not in body)
    ck("正文非空", len(body) > 100, str(len(body)))

    cs = to_chunks(title, body, "live-001", "https://example.gov.cn/a.htm")
    ck("切块非空", len(cs) >= 1, str(len(cs)))
    ck("字段名是 text 不是 content", "text" in cs[0] and "content" not in cs[0])
    ck("chunk_id 带序号", cs[0]["chunk_id"] == "live-001-0000", cs[0]["chunk_id"])
    ck("带 char_offset", cs[0]["char_offset"] == 0)

    short = to_chunks("t", "太短", "live-002", "u")
    ck("正文过短不切块", len(short) == 0)

    t2, _ = extract("<html><body>no title</body></html>", "https://example.gov.cn/b/c.htm")
    ck("无 title 时回退到 URL 末段", t2 == "c", t2)

    # discover 用桩：直接喂 HTML 走同一套正则
    fake = ('<a href="/zhengce/content/2024-01/01/content_1.htm">某市人民政府令</a>'
            '<a href="/zhengce/content/2024-01/02/content_2.htm">另一份通知</a>'
            '<a href="/zhengce/content/2024-01/02/content_2.htm">重复</a>')
    pat = r'<a[^>]*href="([^"]*content_\d+\.htm[^"]*)"[^>]*>(.*?)</a>'
    got, seen = [], set()
    for m in re.finditer(pat, fake, re.S | re.I):
        u = m.group(1)
        if u.startswith("/"):
            u = "https://www.gov.cn" + u
        if u in seen:
            continue
        seen.add(u)
        got.append(u)
    ck("列表页能抽出 content 链接", len(got) == 2, str(len(got)))
    ck("列表页去重", len(got) == len(set(got)))
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        from PIL import Image  # noqa: F401
        ck("OCR 依赖可用（图片通道）", True)
    except ImportError:
        ck("OCR 依赖可用（图片通道）", False, "未装 rapidocr-onnxruntime")
    try:
        import fitz  # noqa: F401
        ck("pymupdf 可用（PDF 通道）", True)
    except ImportError:
        ck("pymupdf 可用（PDF 通道）", False)

    ok = all(c[1] for c in cases)
    for name, passed, detail in cases:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not passed else ""))
    print(f"\n{sum(1 for c in cases if c[1])}/{len(cases)} 通过")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="从 URL 实时抓取政策文本并入本体")
    ap.add_argument("--urls", nargs="*", default=[], help="要抓的 URL")
    ap.add_argument("--urls-file", help="每行一个 URL 的文件")
    ap.add_argument("--from-index", help="从列表页自动发现政策链接（实测可用：https://www.gov.cn/zhengce/xxgk/）")
    ap.add_argument("--index-limit", type=int, default=30, help="从列表页最多取多少个链接")
    ap.add_argument("--base", default=os.path.join(PKG, "ontology", "triples-governance.jsonl"),
                    help="要合并进的已有本体")
    ap.add_argument("--out", help="合并后的输出路径（默认 /tmp/merged-live.jsonl）")
    ap.add_argument("--workdir", help="中间产物目录（默认临时目录）")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    urls = list(a.urls)
    if a.urls_file:
        urls += [l.strip() for l in open(a.urls_file, encoding="utf-8") if l.strip()]
    if a.from_index:
        found, err = discover(a.from_index, a.index_limit)
        if err:
            ap.error(f"列表页抓取失败：{err}")
        print(f"从 {a.from_index} 发现 {len(found)} 个政策链接", file=sys.stderr)
        urls += [x["url"] for x in found]
    if not urls:
        ap.error("至少给一个 --urls / --urls-file / --from-index")

    out = a.out or "/tmp/merged-live.jsonl"
    rep = ingest(urls, a.base, out, a.workdir)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if not rep.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
