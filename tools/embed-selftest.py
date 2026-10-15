#!/usr/bin/env python3
"""embed-server 自测：判据 E1–E5 + 负对照。用法: embed-selftest.py [base_url]"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8792").rstrip("/")


def post(path, payload, timeout=180):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer test-key"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def post_status(path, payload, timeout=60):
    try:
        post(path, payload, timeout)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        fails.append(name)


print("== E1 单条：形状与维度 ==")
r = post("/v1/embeddings", {"input": "政务公开数据", "model": "bge-m3"})
v = r["data"][0]["embedding"]
check("E1a 返回 1 条", len(r["data"]) == 1)
check("E1b 是 1 维向量而非 token 级二维", isinstance(v[0], float), f"dim={len(v)}")
check("E1c 维度 = 1024", len(v) == 1024, f"实际 {len(v)}")

print("== E2 归一化 ==")
n = sum(x * x for x in v) ** 0.5
check("E2 单位范数（|v|≈1）", abs(n - 1.0) < 1e-4, f"|v|={n:.6f}")

print("== E3 语义（相近应高、无关应低）==")
r2 = post("/v1/embeddings", {"input": ["政府信息公开条例", "政务数据的开放与共享", "香蕉的种植方法与土壤要求"],
                             "model": "bge-m3"})
e = [d["embedding"] for d in sorted(r2["data"], key=lambda d: d["index"])]
near, far = cos(e[0], e[1]), cos(e[0], e[2])
check("E3a 相近句 cos > 0.6", near > 0.6, f"cos={near:.4f}")
check("E3b 无关句 cos < 相近句", far < near, f"cos={far:.4f}")
check("E3c 无关句 cos < 0.45", far < 0.45, f"cos={far:.4f}")

print("== E4 批量与单条一致 ==")
single = post("/v1/embeddings", {"input": "政务数据的开放与共享", "model": "bge-m3"})["data"][0]["embedding"]
d = max(abs(a - b) for a, b in zip(single, e[1]))
check("E4 批量结果 == 单条结果（最大差 < 1e-5）", d < 1e-5, f"max|Δ|={d:.2e}")

print("== E5 契约（Nexent 的调用形态）==")
r3 = post("/v1/embeddings", {"model": "bge-m3", "input": ["跨文档推理与决策溯源"]})
check("E5a 响应含 data[].embedding", "embedding" in r3["data"][0])
check("E5b 响应含 usage", "usage" in r3)
check("E5c model 字段回显", r3.get("model") == "bge-m3")

print("== 负对照（必须被拒）==")
check("N1 空 input → 400", post_status("/v1/embeddings", {"input": "", "model": "bge-m3"}) == 400)
check("N2 input 非字符串 → 400", post_status("/v1/embeddings", {"input": [1, 2], "model": "bge-m3"}) == 400)
check("N3 未知路径 → 404", post_status("/v1/nope", {}) == 404)

print("\n结果：%d 项通过 / %d 项失败" % (17 - len(fails), len(fails)))
sys.exit(1 if fails else 0)
