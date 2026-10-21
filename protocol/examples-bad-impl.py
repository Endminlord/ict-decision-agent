#!/usr/bin/env python3
"""故意写错的实现：凡是有命中就算通过（典型的「RAG 都这么做」实现）。"""
import json,sys
req=json.loads(sys.stdin.readline() or "{}")
q=req.get("quote","")
# 错误实现：不做语境判定、不回传区分度
print(json.dumps({"verified": bool(q), "partial": False, "verbatim": True,
                  "risk_flags": [], "fragment": False, "specificity": 0.0,
                  "message": "ok"}, ensure_ascii=False))
