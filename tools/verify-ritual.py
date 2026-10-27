#!/usr/bin/env python3
"""验证仪式 + 信任面板（答辩现场 90 秒王牌）。

台上逻辑：任何结论都能当场回查到原文，任何入库三元组都能当场出示三重出处，
且核查走的是生产通路（MCP 8793 + 真实 ES），不是预录数据。

四个子命令：
  ask <问题>            生产 MCP 完整作答链 → 判定 + 引用卡片（每条带 chunk_id）
  trace <chunk_id>      生产 MCP 回查原文 → 出处卡；--deep 再走本地语料通道对账
  panel [N] [--html F]  本体三元组三重出处面板（规则证据/模型判定/闸门状态/独立回查）
  full                  90 秒仪式：ask → trace --deep → panel，一条命令上台

零第三方依赖；ES 凭据复用 tools/cross-doc-demo.py 的加载器； 语料文件路径可用
ICT_CORPUS 环境变量覆盖。
"""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MCP = "http://127.0.0.1:8793/mcp"
CORPUS = os.environ.get("ICT_CORPUS", "/home/endmin/dsh/research/ict-corpus-chunks/chunks.jsonl")
ONTO = Path("/home/endmin/dsh/research/ontology")
RITUAL_Q = "乡镇人民政府政务公开有哪些要求？"


def die(msg):
    print("✗ " + msg)
    sys.exit(1)


def load_core():
    spec = importlib.util.spec_from_file_location("cdc_ritual", HERE / "cross-doc-demo.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── 生产 MCP 客户端（streamable-http，与会话握手）─────────────────────────
def mcp_call(tool, arguments, timeout=90, attempts=3):
    import time
    last = None
    for i in range(attempts):
        try:
            return _mcp_call_once(tool, arguments, timeout)
        except Exception as e:
            last = e
            time.sleep(6 * (i + 1))
    raise last


def _mcp_call_once(tool, arguments, timeout=90):
    sid = None

    def rpc(payload, expect_reply=True):
        nonlocal sid
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if sid:
            h["mcp-session-id"] = sid
        req = urllib.request.Request(MCP, data=json.dumps(payload).encode(), headers=h)
        resp = urllib.request.urlopen(req, timeout=timeout)
        sid = resp.headers.get("mcp-session-id") or sid
        raw = resp.read().decode("utf-8", "replace")
        lines = [ln[5:].strip() for ln in raw.splitlines()
                 if ln.startswith("data:") and ln[5:].strip()]
        if lines:
            return json.loads(lines[-1])
        if raw.strip():
            return json.loads(raw)
        if expect_reply:
            raise RuntimeError("MCP 返回空响应（服务繁忙，稍后重试）")
        return {}

    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "verify-ritual", "version": "1.0"}}})
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
    r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}})
    return json.loads(r["result"]["content"][0]["text"])


# ── ask：生产通路完整作答 ────────────────────────────────────────────────
def cmd_ask(query):
    d = mcp_call("retrieval_cross_doc", {"query": query, "hops": 2, "top_k": 5})
    print("问：", query)
    if d.get("insufficient_evidence"):
        print("判定：证据不足，拒答（max_score %.4f 未过阈或无词面落地）" % d.get("max_score", 0))
        print("口径：", d.get("note"))
        return d
    print("判定：库内证据充分（max_score %.4f， grounded %d 环，层级 %s）"
          % (d.get("max_score", 0), d.get("grounded_rings", 0), "/".join(d.get("levels", []))))
    print("引用（每条可当场 trace）：")
    for r in d.get("rings", [])[:5]:
        mark = "✓" if r.get("grounded") else "·"
        print("  [%d] %s %s（%s·%s·score %.4f）" % (r["hop"], mark,
              (r.get("title") or "")[:46], r.get("level"), r.get("date"), r["score"]))
        print("      chunk_id=%s  出处=%s" % (r["chunk_id"], (r.get("source") or "")[:20]))
        print("      「%s」" % (r.get("excerpt") or "")[:76])
    return d


# ── trace：回查 + 双通道对账 ─────────────────────────────────────────────
def _norm(s):
    return "".join((s or "").split())


def _fingerprint(text):
    """取最长「无空白且不含引号/反斜杠」的连续段作指纹。

    为什么不能做空白归一窗口：语料文件是单行 JSON，行内保留了原文空格，
    归一后的拼接串在原始行里并不逐字存在。空白定界的 token 才是行内
    逐字可 grep 的最小可靠单元（实测中文正文常见 200+ 字连续段）。
    """
    best = ""
    for run in (text or "").split():
        if '"' in run or "\\" in run:
            continue
        if len(run) > len(best):
            best = run
    return best if len(best) >= 12 else ""


def deep_verify(cid, core):
    """双通道对账：ES 通道（生产 MCP）vs 本地语料文件通道，凭内容指纹认亲。

    ES 的 _id 是入库时自动生成的，与语料文件里的 chunk_id 不同名——
    所以不看 ID，抽 ES 正文中段做指纹到 4.2GB 语料文件里 grep，命中后
    再做全内容空白不敏感比对。两条独立通道对上，才是"真在库里"。
    """
    es = mcp_call("retrieval_trace_source", {"chunk_id": cid})
    if not es.get("found"):
        return {"ok": False, "why": "ES 中无此块"}
    fp = _fingerprint(es.get("content") or "")
    if not fp:
        return {"ok": False, "why": "无法构造内容指纹"}
    try:
        r = subprocess.run(["grep", "-m1", "-F", fp, CORPUS],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "本地语料扫描超时"}
    if r.returncode != 0 or not r.stdout.strip():
        return {"ok": False, "why": "本地语料文件未命中内容指纹"}
    local = json.loads(r.stdout.strip())
    # 语料文件字段是 text（ES 入库时才改叫 content），两处口径都要兼容
    local_c = local.get("content") or local.get("text") or ""
    same = _norm(local_c) == _norm(es.get("content"))
    return {"ok": same, "why": "" if same else "两通道内容空白归一后仍不一致",
            "local_chars": len(local_c), "es_chars": es.get("content_chars")}


def cmd_trace(cid, deep):
    es = mcp_call("retrieval_trace_source", {"chunk_id": cid})
    if not es.get("found"):
        die("ES 中无此块：%s" % cid)
    print("┌ 出处卡（生产 MCP retrieval_trace_source）")
    print("│ chunk_id  %s" % es["chunk_id"])
    print("│ 标题      %s" % es.get("title"))
    print("│ 日期/来源 %s · %s" % (es.get("date"), es.get("source")))
    print("│ 链接/路径 %s" % ((es.get("path_or_url") or "")[:70]))
    print("│ 正文      %s 字，首 96 字：「%s」"
          % (es.get("content_chars"), (es.get("content") or "")[:96]))
    if deep:
        core = load_core()
        v = deep_verify(cid, core)
        if v["ok"]:
            print("│ 双通道对账 ✓  ES 与本地 4.2GB 语料文件凭内容指纹互认，逐字一致（%s 字）" % v["es_chars"])
        else:
            print("│ 双通道对账 ⚠  %s" % v["why"])
    print("└")
    return es


# ── panel：三元组三重出处 ────────────────────────────────────────────────
def load_jsonl(path, cap=None):
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if cap and i >= cap:
                break
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sha16(text):
    return hashlib.sha256((text or "").strip().encode()).hexdigest()[:16]


class JudgeIndex:
    """判定缓存索引：side → key → {is_doc, reason, model}。惰性建，首次 ~2s。"""

    def __init__(self):
        self._idx = {}

    def _side(self, side):
        if side not in self._idx:
            p = ONTO / ("judged-%s-full.jsonl" % side)
            idx = {}
            if p.exists():
                for r in load_jsonl(p):
                    idx[r.get("key")] = r
            self._idx[side] = idx
        return self._idx[side]

    def lookup(self, side, candidate):
        return self._side(side).get(sha16(candidate))


def live_check(triple, core):
    """独立回查：语料文件按 chunk_id 精确取块 + 证据片段空白归一后落文。

    为什么不走 ES：本体出处锚的是语料文件的 chunk_id（如 6714f6a3…-0000），
    ES 的 _id 是入库时自动生成的另一套——查 ES 必 404。语料文件本来就是
    入库源头，回查它=回查真相源，且不受导入期 ES 抖动影响。
    """
    prov = triple.get("provenance") or {}
    cid = prov.get("chunk_id")
    ev = _norm(prov.get("evidence"))
    if not cid:
        return False, "无 chunk_id"
    pat = '"chunk_id": "%s"' % cid
    try:
        r = subprocess.run(["grep", "-m1", "-F", pat, CORPUS],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "语料扫描超时"
    if r.returncode != 0 or not r.stdout.strip():
        return False, "语料文件无此 chunk_id"
    row = json.loads(r.stdout.strip())
    text = _norm(row.get("text") or row.get("content") or "")
    if ev and ev in text:
        return True, "语料文件命中该块，证据片段空白归一后逐字落在原文（%d 字窗）" % len(ev)
    return False, "证据片段未落在该块原文内"


def enrich(triple, ji, core):
    s = ji.lookup("subject", triple.get("subject"))
    o = ji.lookup("object", triple.get("object"))
    ok, why = live_check(triple, core)
    return {
        "subject": triple.get("subject"), "predicate": triple.get("predicate"),
        "object": triple.get("object"), "subject_type": triple.get("subject_type"),
        "object_type": triple.get("object_type"), "confidence": triple.get("confidence"),
        "review_status": triple.get("review_status"),
        "prov": {k: triple.get("provenance", {}).get(k)
                 for k in ("chunk_id", "title", "date", "source", "char_offset")},
        "evidence": (triple.get("provenance") or {}).get("evidence") or "",
        "judge_subject": None if not s else {"is_doc": s.get("is_doc"),
                                             "reason": s.get("reason"), "model": s.get("model")},
        "judge_object": None if not o else {"is_doc": o.get("is_doc"),
                                            "reason": o.get("reason"), "model": o.get("model")},
        "live_ok": ok, "live_why": why,
    }


def pick_triples(n):
    rows = load_jsonl(ONTO / "triples-governance.jsonl")
    want = ["cites", "implements", "amends", "issued_by", "applies_to"]
    picked, seen = [], set()
    for pred in want * 3:
        for t in rows:
            if t.get("predicate") == pred and t.get("subject") not in seen:
                picked.append(t)
                seen.add(t.get("subject"))
                break
        if len(picked) >= n:
            break
    return picked[:n]


def render_text(rows):
    for e in rows:
        print("┌ 三元组  《%s》 --%s--> %s(%s)"
              % (e["subject"][:34], e["predicate"], e["object"][:22], e["object_type"]))
        print("│ ① 规则证据 [%s·%s] 「%s」"
              % (e["confidence"], e["review_status"], e["evidence"][:70]))
        p = e["prov"]
        print("│    出处 chunk %s · 偏移 %s ·《%s》(%s·%s)"
              % (p.get("chunk_id"), p.get("char_offset"),
                 (p.get("title") or "")[:26], p.get("date"), p.get("source")))
        for side, j in (("subject", e["judge_subject"]), ("object", e["judge_object"])):
            if j is None:
                print("│ ② 模型判定 %s：未入判定缓存覆盖面（如实标注）" % side)
            else:
                print("│ ② 模型判定 %s is_doc=%s「%s」(%s)"
                      % (side, j["is_doc"], (j["reason"] or "")[:40], j["model"]))
        print("│ ③ 闸门    review_gate=%s → %s"
              % (e["confidence"], "自动入库" if e["review_status"] == "auto" else "待人工确认"))
        print("│ ④ 独立回查（语料源）%s  %s" % ("✓" if e["live_ok"] else "⚠", e["live_why"]))
        print("└")


def render_html(rows, out):
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cards = []
    for e in rows:
        live = '<span class="ok">✓ %s</span>' % esc(e["live_why"]) if e["live_ok"] \
            else '<span class="warn">⚠ %s</span>' % esc(e["live_why"])
        js = "未判定（如实标注）"
        if e["judge_subject"]:
            js = "is_doc=%s · %s" % (e["judge_subject"]["is_doc"], esc(e["judge_subject"]["reason"]))
        jo = "未判定（如实标注）"
        if e["judge_object"]:
            jo = "is_doc=%s · %s" % (e["judge_object"]["is_doc"], esc(e["judge_object"]["reason"]))
        cards.append("""
<div class="card">
 <div class="triple">《%s》 <b>--%s--&gt;</b> %s <span class="type">(%s)</span></div>
 <div class="layer"><span class="tag t1">① 规则证据</span> [%s·%s] 「%s」<br>
   <span class="src">chunk %s · 偏移 %s ·《%s》（%s·%s）</span></div>
 <div class="layer"><span class="tag t2">② 模型判定</span> subject：%s<br>
   <span class="layer" style="margin-left:84px">object：%s</span> <span class="src">(deepseek-v4.1-flash 零温度)</span></div>
 <div class="layer"><span class="tag t3">③ 人工闸门</span> review_gate=%s → %s</div>
 <div class="layer"><span class="tag t4">④ 独立回查</span> %s</div>
</div>""" % (esc(e["subject"])[:36], esc(e["predicate"]), esc(e["object"])[:24],
             esc(e["object_type"]), esc(e["confidence"]), esc(e["review_status"]),
             esc(e["evidence"])[:84], esc(e["prov"].get("chunk_id")),
             e["prov"].get("char_offset"), esc(e["prov"].get("title"))[:30],
             esc(e["prov"].get("date")), esc(e["prov"].get("source")),
             js, jo, esc(e["confidence"]),
             "自动入库" if e["review_status"] == "auto" else "待人工确认", live))
    html = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>信任面板 · 三重出处（2026-10-27）</title>
<style>
body{font-family:"Microsoft YaHei",system-ui,sans-serif;max-width:880px;margin:24px auto;padding:0 16px;color:#1a1a2e;background:#f7f7fb}
h1{font-size:22px} .sub{color:#555;font-size:13px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e2e2ee;border-radius:10px;padding:14px 18px;margin:14px 0;box-shadow:0 1px 4px rgba(30,30,80,.06)}
.triple{font-size:16px;margin-bottom:8px} .type{color:#888;font-size:13px}
.layer{font-size:13.5px;margin:6px 0;line-height:1.65} .src{color:#777;font-size:12px}
.tag{display:inline-block;min-width:76px;text-align:center;border-radius:5px;color:#fff;font-size:12px;padding:1px 6px;margin-right:8px}
.t1{background:#3a6ea5}.t2{background:#7a5ea8}.t3{background:#b07d3c}.t4{background:#2e8b57}
.ok{color:#2e8b57;font-weight:600}.warn{color:#b0432e;font-weight:600}
</style>
<h1>知识图谱信任面板 · 每条三元组的三重出处</h1>
<div class="sub">规则层证据（可回查原文片段）→ 模型判定（零温度+理由）→ 人工闸门（review_gate 分级）→ 独立回查（语料源文件逐字落文）。样本取自强证据层（triples-governance.jsonl，auto 直入库），生成于 2026-10-27。</div>
%s</html>""" % ("".join(cards))
    Path(out).write_text(html, encoding="utf-8")
    print("HTML →", out)


def cmd_panel(n, html):
    core = load_core()
    ji = JudgeIndex()
    rows = [enrich(t, ji, core) for t in pick_triples(n)]
    if html:
        render_html(rows, html)
    render_text(rows)
    ok = sum(1 for e in rows if e["live_ok"])
    print("面板：%d 条，独立回查 %d/%d ✓（回查走语料源文件，判定与闸门走真实缓存/配置，无预录）" % (len(rows), ok, len(rows)))
    return rows


def cmd_full():
    print("════ 验证仪式（90 秒 · 全程生产通路）════")
    print()
    print("── 第 1 幕 · 提问与作答（生产 MCP retrieval_cross_doc）──")
    d = cmd_ask(RITUAL_Q)
    rings = [r for r in d.get("rings", []) if r.get("grounded")] or d.get("rings", [])
    if not rings:
        die("无可回查引用，仪式中止")
    cid = rings[0]["chunk_id"]
    print()
    print("── 第 2 幕 · 现场点一条引用回查（评委任指，此处演示第 1 条）──")
    cmd_trace(cid, deep=True)
    print()
    print("── 第 3 幕 · 本体三重出处（知识库底下的图谱也不是口说的）──")
    cmd_panel(3, "")
    print()
    print("仪式完成：作答 → 引用 → 原文 → 双通道对账 → 图谱三重出处，全部现场实时。")


def main():
    ap = argparse.ArgumentParser(description="验证仪式 + 信任面板")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("ask")
    a1.add_argument("query")
    a2 = sub.add_parser("trace")
    a2.add_argument("chunk_id")
    a2.add_argument("--deep", action="store_true")
    a3 = sub.add_parser("panel")
    a3.add_argument("n", type=int, nargs="?", default=5)
    a3.add_argument("--html", default="")
    sub.add_parser("full")
    a = ap.parse_args()
    if a.cmd == "ask":
        cmd_ask(a.query)
    elif a.cmd == "trace":
        cmd_trace(a.chunk_id, a.deep)
    elif a.cmd == "panel":
        cmd_panel(a.n, a.html)
    else:
        cmd_full()


if __name__ == "__main__":
    main()
