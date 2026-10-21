#!/usr/bin/env python3
"""QuoteVerification 协议的最小 CLI 适配器。

读 stdin 一行 JSON {"quote":..., "strict":...}，写 stdout 一行 JSON。
这是「把已有实现接入协议」的样板 —— 任何语言都可以照此实现。
"""
import json, sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from ict_track2_mcp import server

def main():
    line = sys.stdin.readline()
    req = json.loads(line) if line.strip() else {}
    payload, _is_err = server._call_tool("verify_quote", {
        "quote": str(req.get("quote", "")),
        "strict": bool(req.get("strict", False)),
    })
    # 只输出协议约定的字段，避免把内部结构泄露成协议依赖
    keep = ["verified", "partial", "verbatim", "risk_flags", "match_kinds",
            "hit_count", "specificity", "generic", "fragment", "message"]
    out = {k: payload.get(k) for k in keep if k in payload}
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    main()
