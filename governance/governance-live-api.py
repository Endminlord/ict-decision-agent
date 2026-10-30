#!/usr/bin/env python3
"""governance-live-api.py — 治理观测台实时 API + 页面（stdlib 零依赖，:8794）
端点：
  /            -> live 观测台页面（自包含）
  /api/ledger  -> 账本 10 笔 + 哈希链校验（实时调 immunity_log.py verify）
  /api/scores  -> 五层最新成绩（replay 报告优先，回落 post-v4 / 冻结 hybrid）
  /api/import  -> 导入状态（单元状态 + marker 时间线尾）
CORS 全放（本机演示）；只读，不写任何仓库文件。
"""
import json, glob, subprocess, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = "/home/endmin/dsh/research/ict-open-source"

def api_ledger():
    try:
        out = subprocess.run(
            ["python3", "governance/immunity_log.py", "verify"],
            cwd=ROOT, capture_output=True, text=True, timeout=20).stdout.strip()
        verify = json.loads(out) if out.startswith("{") else {"raw": out}
    except Exception as e:
        verify = {"ok": False, "error": str(e)}
    recs = []
    try:
        with open(f"{ROOT}/governance/immunity-log.jsonl", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                recs.append({k: r.get(k) for k in
                    ("rec_id", "ts", "action", "target", "actor", "version", "note", "hash")})
    except Exception as e:
        pass
    return {"verify": verify, "count": len(recs), "records": recs[-12:]}

def _latest(pat):
    fs = sorted(glob.glob(f"{ROOT}/{pat}"))
    return fs[-1] if fs else None

def api_scores():
    layers = {}
    for L in "RMXCG":
        f = _latest(f"evidence/full-{L}-*.json")
        src = "full-replay" if f else None
        if not f and L == "G":
            f = _latest("evidence/g-layer-*.json")
            src = "g-layer" if f else src
        if not f and L in "MX":
            f = _latest("evidence/post-v4-*.json") if L == "X" else _latest(f"evidence/post-v4-{L.lower()}.json")
            src = "post-v4" if f else src
        if not f:
            f = _latest("evidence/eval-stratified-20261027-hybrid.json")
            src = "frozen-hybrid" if f else src
        if not f:
            layers[L] = {"source": "none"}
            continue
        d = json.load(open(f))
        v = d.get(L)
        got = None
        if isinstance(v, dict):
            for rk, ck in (("pass_rate", "passed"), ("chain_pass_rate", "chain_pass"), ("refusal_rate", "refused")):
                if rk in v:
                    got = {"rate": v[rk], "passed": v.get(ck), "n": v.get("n"), "kind": rk,
                           "source": src, "file": f.split("/")[-1]}
                    break
            if got is None and "mrr" in v:
                got = {"rate": v.get("hit@10"), "passed": None, "n": v.get("n"), "kind": "retrieval",
                       "mrr": v.get("mrr"), "hit@1": v.get("hit@1"), "hit@3": v.get("hit@3"), "hit@5": v.get("hit@5"),
                       "source": src, "file": f.split("/")[-1]}
            if got is None and "hit_rate" in v:
                got = {"rate": v.get("hit_rate"), "passed": v.get("hit"), "n": v.get("n"), "kind": "hit_rate",
                       "source": src, "file": f.split("/")[-1]}
        layers[L] = got or {"source": src, "file": (f or "").split("/")[-1], "note": "pending"}
    gate = {}
    for L in "MX":
        g = _latest(f"evidence/a2-gate-{L}-*.json")
        if g:
            d = json.load(open(g))
            gate[L] = {"decision": d.get("decision"), "p": d.get("p_one_sided")}
    return {"layers": layers, "gates": gate}

def api_import():
    try:
        st = subprocess.run(["systemctl", "--user", "is-active", "import-corpus"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as e:
        st = f"err:{e}"
    tail = []
    try:
        with open("/home/endmin/dsh/research/import-full.marker", encoding="utf-8") as f:
            tail = f.read().strip().splitlines()[-4:]
    except Exception:
        pass
    return {"unit": st, "marker_tail": tail, "now": datetime.datetime.now().isoformat(timespec="seconds")}


def api_constitution():
    try:
        c = json.load(open(f"{ROOT}/governance/constitution.json", encoding="utf-8"))
        return {"version": c.get("constitution", {}).get("version") if isinstance(c.get("constitution"), dict) else c.get("version"),
                "frozen_at": c.get("constitution", {}).get("frozen_at") if isinstance(c.get("constitution"), dict) else None,
                "principle": (c.get("constitution", {}).get("principle") if isinstance(c.get("constitution"), dict) else None),
                "rules": [{"id": r.get("id"), "text": r.get("text"), "enforcement": r.get("enforcement")}
                          for r in c.get("constitution", {}).get("rules", [])] if isinstance(c.get("constitution"), dict) else [],
                "amendment": c.get("constitution", {}).get("amendment_procedure")}
    except Exception as e:
        return {"error": str(e)}

PAGE = open("/home/endmin/dsh/research/ict-open-source/ui/governance-console-live.html", encoding="utf-8").read()

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/ledger":
            self._send(200, json.dumps(api_ledger(), ensure_ascii=False).encode(), "application/json")
        elif p == "/api/scores":
            self._send(200, json.dumps(api_scores(), ensure_ascii=False).encode(), "application/json")
        elif p == "/api/constitution":
            self._send(200, json.dumps(api_constitution(), ensure_ascii=False).encode(), "application/json")
        elif p == "/api/import":
            self._send(200, json.dumps(api_import(), ensure_ascii=False).encode(), "application/json")
        else:
            self._send(404, b'{"err":"not found"}', "application/json")
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8794), H).serve_forever()
