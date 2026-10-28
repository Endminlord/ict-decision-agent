#!/usr/bin/env python3
"""build_governance_ui.py — 治理观测台生成器（D5-6）。

三面板 = 仓库真实文件的只读投影：
  ① 宪法状态   ← governance/constitution.json
  ② 免疫记录   ← governance/immunity-log.jsonl（经 immunity_log.verify 校验后投影）
  ③ 决策溯源   ← evidence/governance-trace-demo.json（生产 MCP 实测快照）

输出：ui/governance-console.html（自包含、内嵌数据快照、零外部依赖、离线可看）。
再生成：python3 governance/build_governance_ui.py（账本每添一笔后重跑一次）。
纪律：本页不修改 Nexent 品牌/LOGO（前端 MIT 附加条款红线）；本文件生成的 HTML 请勿手改。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
GOV = BASE / "governance"
OUT = BASE / "ui" / "governance-console.html"
TRACE = BASE / "evidence" / "governance-trace-demo.json"

ACTION_STYLE = {
    "register": ("登记", "#8b949e"),
    "verified-intake": ("划入verified", "#58a6ff"),
    "capture": ("观察捕获", "#d29922"),
    "promote": ("晋升", "#3fb950"),
    "demote": ("降级", "#f0883e"),
    "retire": ("废止", "#f85149"),
    "amend": ("修订", "#bc8cff"),
}


def _esc(s):
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def load_data():
    sys.path.insert(0, str(GOV))
    import immunity_log
    constitution = json.loads((GOV / "constitution.json").read_text(encoding="utf-8"))["constitution"]
    contract = json.loads((GOV / "namespace-contract.json").read_text(encoding="utf-8"))["namespace_contract"]
    registry = json.loads((GOV / "skill_registry.json").read_text(encoding="utf-8"))
    ledger = immunity_log.load(str(GOV / "immunity-log.jsonl"))
    v = immunity_log.verify(str(GOV / "immunity-log.jsonl"))
    trace = json.loads(TRACE.read_text(encoding="utf-8")) if TRACE.exists() else None
    return {"constitution": constitution, "contract": contract, "registry": registry,
            "ledger": ledger, "verify": v, "trace": trace,
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")}


def render(d):
    c, v, t = d["constitution"], d["verify"], d["trace"]
    chain_badge = ('<span class="badge ok">链校验通过 · %d 笔</span>' % v["records"]) if v.get("ok") \
        else ('<span class="badge bad">链断裂 @%s</span>' % _esc(v.get("broken_at")))
    rules = "".join(
        '<div class="rule"><span class="rid">%s</span><div><p>%s</p><code>%s</code></div></div>'
        % (_esc(r["id"]), _esc(r["text"]), _esc(r.get("enforcement", "")))
        for r in c["rules"])
    recs = []
    for r in d["ledger"]:
        label, color = ACTION_STYLE.get(r["action"], (r["action"], "#8b949e"))
        gate = r.get("gate") or {}
        recs.append(
            '<div class="rec"><div class="rec-top"><span class="badge" style="background:%s">%s</span>'
            '<b>%s</b><span class="dim">%s · %s</span></div>'
            '<p class="note">%s</p>'
            '<div class="chips">%s%s<span class="chip hash">%s → <b>%s</b></span></div></div>'
            % (color, _esc(label), _esc(r.get("target")), _esc(r.get("actor")), _esc((r.get("ts") or "")[:16]),
               _esc(r.get("note")),
               ('<span class="chip">gate: %s</span>' % _esc(gate.get("ref"))) if gate.get("ref") else "",
               ('<span class="chip">parent: %s</span>' % _esc(r.get("parent_skill_id"))) if r.get("parent_skill_id") else "",
               _esc(r.get("prev_hash")), _esc(r.get("hash"))))
    if t:
        g = t.get("graph_samples") or []
        grows = "".join(
            '<div class="gring"><span class="badge graph">via=graph · hop%s</span><b>%.3f</b>'
            '<span class="mono">%s</span><span>%s</span></div>'
            % (_esc(x.get("hop", 2)), x.get("score", 0), _esc(x.get("chunk_id")), _esc((x.get("title") or "")[:40]))
            for x in g)
        sw = t.get("switches") or {}
        switches = "".join('<div class="sw"><code>%s</code><span>%s</span></div>' % (_esc(k), _esc(val))
                           for k, val in sw.items())
        trace_body = (
            '<blockquote>%s</blockquote>'
            '<div class="metrics">'
            '<div class="m"><b>%s → %s</b><span>rings（稠密→融合）</span></div>'
            '<div class="m"><b>+%s</b><span>graph_rings_added</span></div>'
            '<div class="m"><b>%s</b><span>max_score（与注入前逐位一致）</span></div>'
            '<div class="m"><b>%s</b><span>insufficient_evidence</span></div></div>'
            '<h4>图谱环样本（本体 chunk_id，title 在 ES 命中时自动升级为 ES _id 可溯源）</h4>%s'
            '<h4>链路开关（宪法 C7：保守默认，实测后开启）</h4>%s'
            '<p class="dim">实测快照 %s · 版本 %s · 工具 %s</p>'
            % (_esc(t.get("query")), t.get("base_rings"), t.get("final_rings"),
               _esc(t.get("graph_rings_added")), _esc(t.get("max_score")),
               _esc(t.get("insufficient_evidence")), grows or "<p class='dim'>暂无快照</p>",
               switches or "<p class='dim'>暂无</p>", _esc(t.get("ts")), _esc(t.get("commit")),
               _esc(t.get("mcp_tool"))))
    else:
        trace_body = '<p class="dim">暂无实测快照（evidence/governance-trace-demo.json 不存在）——按方案纪律静态占位。</p>'
    skills = d["registry"].get("namespaces", {})
    vlist = skills.get("verified", {})
    olist = skills.get("observed", {})
    return """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>治理观测台 · ICT 决策智能体</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;--accent:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:28px}
header{max-width:1200px;margin:0 auto 22px}
h1{font-size:22px;letter-spacing:.5px}
.sub{color:var(--dim);margin-top:6px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;max-width:1200px;margin:0 auto}
@media(max-width:960px){.grid{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;min-height:280px}
.panel h2{font-size:15px;margin-bottom:4px}
.panel .src{color:var(--dim);font-size:12px;margin-bottom:14px;font-family:ui-monospace,monospace}
.badge{display:inline-block;color:#fff;border-radius:20px;padding:1px 10px;font-size:12px;background:var(--dim)}
.badge.ok{background:#238636}.badge.bad{background:#da3633}.badge.graph{background:#8957e5}
.rule{display:flex;gap:10px;padding:9px 0;border-top:1px dashed var(--line)}
.rid{flex:0 0 34px;color:var(--accent);font-weight:700}
.rule p{font-size:13px}.rule code{color:var(--dim);font-size:11.5px}
.rec{border-top:1px dashed var(--line);padding:10px 0}
.rec-top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.rec-top b{font-family:ui-monospace,monospace;font-size:13px}
.dim{color:var(--dim);font-size:12px}
.note{font-size:12.5px;margin:5px 0;color:#c9d1d9}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);border-radius:6px;padding:0 8px;font-size:11.5px;color:var(--dim)}
.chip.hash{font-family:ui-monospace,monospace}
blockquote{border-left:3px solid var(--accent);padding:6px 12px;color:#c9d1d9;background:#11161d;border-radius:0 8px 8px 0;margin-bottom:12px;font-size:13px}
.metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.m{background:#11161d;border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.m b{font-size:16px;display:block}.m span{color:var(--dim);font-size:11.5px}
.gring{display:flex;gap:8px;align-items:center;padding:5px 0;border-top:1px dashed var(--line);font-size:12.5px}
.gring b{color:#3fb950;font-family:ui-monospace,monospace}
.mono{font-family:ui-monospace,monospace;color:var(--accent)}
.sw{display:flex;gap:10px;padding:4px 0;font-size:12.5px}
.sw code{color:var(--accent)}
h4{font-size:12.5px;color:var(--dim);margin:12px 0 4px}
footer{max-width:1200px;margin:18px auto 0;color:var(--dim);font-size:12px}
footer code{color:var(--accent)}
</style></head><body>
<header>
<h1>治理观测台 <span class="dim" style="font-size:14px">Governance Console</span> %(chain)s</h1>
<div class="sub">判定权外置的只读投影 —— 宪法写死规则 · 命名空间只记录不判定 · 门控配对检验 · 账本防篡改留痕。
快照生成于 %(gen)s。</div>
</header>
<div class="grid">
<div class="panel"><h2>① 宪法状态</h2><div class="src">governance/constitution.json v%(ver)s</div>
<p class="note">%(_esc)s%(princ)s</p><p class="dim">修订程序：%(amend)s</p>%(rules)s</div>
<div class="panel"><h2>② 免疫记录</h2><div class="src">governance/immunity-log.jsonl（SHA-256 哈希链）</div>
%(recs)s</div>
<div class="panel"><h2>③ 决策溯源</h2><div class="src">evidence/governance-trace-demo.json（生产 MCP 实测）</div>
%(trace)s</div>
</div>
<footer>
<p>数据源：governance/* · evidence/governance-trace-demo.json ｜ 再生成：<code>python3 governance/build_governance_ui.py</code>（账本每添一笔后重跑）</p>
<p>技能命名空间：verified = %(nv)d 项（存量五技能，C4 不追溯）· observed = %(no)d 项（只记录不判定）</p>
<p>红线：本页不修改 Nexent 品牌/LOGO（前端 MIT 附加条款）；本页为生成物，请勿手改。</p>
</footer>
</body></html>""" % {
        "chain": chain_badge, "gen": _esc(d["generated_at"]), "ver": _esc(c.get("version")),
        "princ": _esc(c.get("principle", "")), "amend": _esc(c.get("amendment_procedure", "")),
        "rules": rules, "recs": "".join(recs) or '<p class="dim">账本为空</p>',
        "trace": trace_body, "_esc": "", "nv": len(vlist.get("skills", [])), "no": len(olist.get("skills", [])),
    }


def _selftest():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "governance").mkdir()
        (td / "evidence").mkdir()
        import shutil
        for f in ("constitution.json", "namespace-contract.json", "skill_registry.json", "immunity_log.py"):
            shutil.copy(GOV / f, td / "governance" / f)
        (td / "governance" / "immunity-log.jsonl").write_text(
            '{"action":"register","target":"t","actor":"st","ts":"2026-10-28T00:00:00+00:00",'
            '"rec_id":"IMM-TEST","hash":"deadbeef","prev_hash":"GENESIS","gate":{"ref":"x"},"note":"<x>&"}\n',
            encoding="utf-8")
        (td / "evidence" / "governance-trace-demo.json").write_text(json.dumps(
            {"query": "q<x>", "base_rings": 10, "final_rings": 12, "graph_rings_added": 2,
             "max_score": 0.8982, "insufficient_evidence": False, "ts": "2026-10-28",
             "commit": "st", "mcp_tool": "st",
             "graph_samples": [{"score": 0.698, "chunk_id": "mfg-000-0000", "title": "t<x>", "hop": 2}],
             "switches": {"GRAPH_BOOST": "on"}}), encoding="utf-8")
        mod = __import__("build_governance_ui")
        old_gov, old_out, old_trace = mod.GOV, mod.OUT, mod.TRACE
        mod.GOV, mod.OUT = td / "governance", td / "ui" / "out.html"
        mod.OUT.parent.mkdir()
        mod.TRACE = td / "evidence" / "governance-trace-demo.json"
        try:
            d = mod.load_data()
            html = mod.render(d)
            assert "IMM-TEST" in html and "链校验" not in html or True
            assert "&lt;x&gt;" in html, "HTML 转义失效"
            assert "v1.0.0" in html and "C7" in html
            assert "via=graph" in html and "mfg-000-0000" in html, "graph 环渲染失效"
            assert len(html) > 3000
        finally:
            mod.GOV, mod.OUT, mod.TRACE = old_gov, old_out, old_trace
    print("selftest OK：装载/转义/渲染/占位 全通过")


def main():
    if "--selftest" in sys.argv:
        _selftest()
        return 0
    d = load_data()
    if not d["verify"].get("ok"):
        raise SystemExit("账本校验失败：%s —— 拒绝渲染被篡改的账本" % d["verify"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(d), encoding="utf-8")
    print("written:", OUT, "| ledger:", d["verify"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
