#!/usr/bin/env python3
"""赛题 2 · 端到端演示：检索 → MCP 核验 → 带出处的回答

本脚本**不依赖 AgentArts**，在本机跑通同一条链路，用来证明两件事：

1. MCP 服务（`ict-track2-mcp`）在真实问答里到底起什么作用；
2. 「确定性核验」与「让模型自由发挥」的差别有多大。

链路（节点编号对应 `247-ict-track2-workflow.md`）：

    [1] 问题 → embedding → ES kNN 检索候选片段     ← 对应「平台知识库检索」节点
    [2] list_sources(query) 列出可用来源分布       ← 对应「片段回查」节点
    [3] 模型生成候选回答 + 它打算引用的原句          ← 对应「组织回答」节点
    [4] verify_quote(每条候选引用) 逐条核验         ← 对应「引用核验」节点（关键）
    [5] 只保留通过核验的引用，输出最终回答

`--no-verify` 跳过 [4]，作为对照组：同一批片段、同一个模型，看它会编出什么。

用法：

    python3 demo/ask.py --query "国家层面的《优化营商环境条例》是哪一年出台的？"
    python3 demo/ask.py --query "..." --no-verify      # 对照组

依赖：仅标准库（urllib / subprocess / json）。ES 与 embedding 服务需在本机运行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG_ROOT = HERE.parent

# ---- 外部服务（都可用环境变量覆盖） ----
ES = os.environ.get("ES_URL", "http://localhost:9220")
ES_INDEX = os.environ.get("ES_INDEX", "1-43ce427e8b5447ecad015534f953e084")
EMBED = os.environ.get("EMBED_URL", "http://127.0.0.1:8792/v1/embeddings")
LLM_BASE = os.environ.get("LLM_BASE", "http://172.20.0.1:8790/v1")
# 默认 glm-5.3-flash：实测 reasoning_tokens = 0（deepseek-v4.1-flash 在复杂问题上
# 会把 4000 的预算全烧在思考里，导致 content 为空、演示中断）。
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-5.3-flash")
LLM_KEY = os.environ.get("LLM_KEY", "")


def _load_es_key() -> str:
    """从环境变量或本机凭据文件读 ES API key；源码里不硬编码。"""
    key = os.environ.get("ES_API_KEY", "").strip()
    if key:
        return key
    for candidate in (Path.home() / ".dsh" / "es-key.env",
                      Path.home() / ".config" / "ict-demo.env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.startswith("ES_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


ES_KEY = _load_es_key()


# ---------------------------------------------------------------- 检索


def embed(text: str) -> list[float]:
    req = urllib.request.Request(
        EMBED,
        data=json.dumps({"input": text, "model": "bge-m3"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=120))["data"][0]["embedding"]


def knn(vec: list[float], k: int) -> list[dict]:
    body = {
        "knn": {"field": "embedding", "query_vector": vec, "k": k,
                "num_candidates": max(50, k * 10)},
        "_source": ["title", "content", "date", "path_or_url", "process_source"],
    }
    req = urllib.request.Request(
        f"{ES}/{ES_INDEX}/_search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "ApiKey " + ES_KEY},
    )
    return json.load(urllib.request.urlopen(req, timeout=60))["hits"]["hits"]


# ---------------------------------------------------------------- MCP 客户端


class Mcp:
    """极简 stdio JSON-RPC 客户端：起一个 ict-track2-mcp 子进程并对话。"""

    def __init__(self) -> None:
        src = str(PKG_ROOT / "src")
        self.proc = subprocess.Popen(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {src!r}); "
             "from ict_track2_mcp.server import main; main()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._id = 0
        self.call("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "demo", "version": "0.1"}})
        self.notify("notifications/initialized")

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id,
                    "method": method, "params": params or {}})
        assert self.proc.stdout
        line = self.proc.stdout.readline()
        return json.loads(line).get("result", {})

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def tool(self, name: str, args: dict) -> dict:
        res = self.call("tools/call", {"name": name, "arguments": args})
        text = (res.get("content") or [{}])[0].get("text", "{}")
        return json.loads(text)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ---------------------------------------------------------------- 模型


def chat(system: str, user: str, max_tokens: int = 4000) -> str:
    """调用模型。注意：`deepseek-v4.1-flash` 是推理模型，思考也计入 max_tokens
    （实测一个简单问题就烧掉 94 个 reasoning token），预算给小了正文会是空的。"""
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        LLM_BASE.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + LLM_KEY},
    )
    out = json.load(urllib.request.urlopen(req, timeout=300))
    ch = out["choices"][0]
    msg = ch.get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        usage = out.get("usage") or {}
        rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        print(f"    ⚠ 模型 content 为空（finish_reason={ch.get('finish_reason')} · "
              f"reasoning_tokens={rt} · max_tokens={max_tokens}）", file=sys.stderr)
    return content


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出 JSON 对象。

    实测模型有三种写法：纯 JSON、```json 围栏、JSON 前后带解释文字。
    这里都容忍，抠不出来才返回 None。
    """
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- 主流程


ASK_SYS = """你是一个政务决策依据助手。基于给定的检索片段回答问题。

输出严格的 JSON（不要 markdown 代码块）：
{
  "answer": "你的回答正文",
  "citations": [
    {"chunk_id": "片段里给出的 id", "quote": "你打算引用的原文原句（必须逐字来自片段）"}
  ]
}

规则：
1. 只使用给定片段里的内容，不得补充你自己的知识。
2. 每条结论都要有 citations 支撑。
3. quote 必须是片段里**逐字出现**的句子，不要改写、不要拼接。
4. 如果片段不足以回答，answer 写「现有资料不足以支撑结论」，citations 给空数组。
"""

# 宽松版：**不要求逐字引用**，只要求「给出依据」。
# 用来测真实场景下模型会编多少——用户提问时不会预先告诉模型「不许编」，
# 核验环节要拦的正是这种自然状态下的编造。
LOOSE_SYS = """你是一个政务决策依据助手。基于给定的检索片段回答问题。

输出严格的 JSON（不要 markdown 代码块）：
{
  "answer": "你的回答正文",
  "citations": [
    {"chunk_id": "片段里给出的 id", "quote": "支撑这条结论的依据"}
  ]
}

规则：
1. 尽量依据给定片段作答。
2. 每条结论都要有 citations 支撑，写清依据是什么。
3. 如果片段不足以回答，answer 写「现有资料不足以支撑结论」，citations 给空数组。
"""


def main() -> int:
    global LLM_KEY, LLM_MODEL

    ap = argparse.ArgumentParser(description="检索 → MCP 核验 → 带出处的回答")
    ap.add_argument("--query", required=True, help="用户问题")
    ap.add_argument("--top-k", type=int, default=6, help="检索片段数（默认 6）")
    ap.add_argument("--no-verify", action="store_true",
                    help="对照组：跳过 verify_quote 核验")
    ap.add_argument("--inject-fake", action="store_true",
                    help="演示用：额外注入一条编造的引用，证明 verify_quote "
                         "会拦下语料里根本不存在的原句")
    ap.add_argument("--loose", action="store_true",
                    help="宽松提示词：不要求逐字引用。用来测模型在自然状态下"
                         "会编多少——这才是核验环节真实要拦的东西")
    ap.add_argument("--llm-key", default=None, help="模型 key（缺省读 $LLM_KEY）")
    ap.add_argument("--model", default=None, help=f"模型 id（默认 {LLM_MODEL}）")
    a = ap.parse_args()

    if a.llm_key:
        LLM_KEY = a.llm_key
    if a.model:
        LLM_MODEL = a.model
    if not LLM_KEY:
        print("✗ 缺少模型 key：请设 $LLM_KEY 或传 --llm-key", file=sys.stderr)
        return 2
    if not ES_KEY:
        print("✗ 缺少 ES key：请设 $ES_API_KEY 或写 ~/.dsh/es-key.env", file=sys.stderr)
        return 2

    t0 = time.time()
    tags = ["宽松提示词" if a.loose else "严格提示词",
            "不核验（对照组）" if a.no_verify else "MCP 核验"]
    print(f"问题：{a.query}")
    print(f"模式：{' + '.join(tags)}\n")

    # [1] 检索
    hits = knn(embed(a.query), a.top_k)
    chunks = []
    for h in hits:
        s = h["_source"]
        chunks.append({
            "chunk_id": s.get("path_or_url", "").replace(".txt", ""),
            "title": s.get("title", ""),
            "date": s.get("date", ""),
            "score": round(h["_score"], 4),
            "text": (s.get("content") or "")[:1200],
        })
    print(f"[1] 检索：top-{len(chunks)}，最高分 {chunks[0]['score'] if chunks else '—'}")
    for c in chunks[:3]:
        print(f"    · {c['score']} · {c['title'][:40]} · {c['chunk_id']}")

    mcp = Mcp()

    # [2] list_sources
    src = mcp.tool("list_sources", {"topic": a.query, "top": 10})
    by_source = src.get("by_source") or {}
    print(f"\n[2] list_sources：命中 {src.get('matched_chunks', 0)} 块 / "
          f"{src.get('matched_docs', 0)} 篇，来源分布 top-3 "
          f"{list(by_source.items())[:3]}")

    # [3] 模型生成候选回答
    ctx = "\n\n".join(
        f"[{i+1}] chunk_id={c['chunk_id']}\n标题：{c['title']}\n日期：{c['date']}\n正文：{c['text']}"
        for i, c in enumerate(chunks)
    )
    raw = chat(LOOSE_SYS if a.loose else ASK_SYS,
               f"问题：{a.query}\n\n检索片段：\n{ctx}")
    draft = _extract_json(raw)
    if draft is None:
        print(f"\n[3] 模型输出里找不到 JSON，原文：\n{raw[:600]}")
        mcp.close()
        return 1
    cites = draft.get("citations") or []
    print(f"\n[3] 模型生成：回答 {len(draft.get('answer',''))} 字 · "
          f"打算引用 {len(cites)} 条")

    if a.inject_fake:
        cites.append({
            "chunk_id": "（演示注入）",
            "quote": "《优化营商环境条例》明确规定各地应在三十个工作日内办结企业开办手续。",
        })
        print("    ⚠ 已注入 1 条**编造**引用（演示用）—— 这句话语料里不存在")

    # [4] 逐条核验
    if a.no_verify:
        print("\n[4] 核验：**跳过**（对照组）")
        verified = cites
        rejected = []
    else:
        verified, rejected = [], []
        for c in cites:
            r = mcp.tool("verify_quote", {"quote": c.get("quote", ""), "max_hits": 3})
            c["_verify"] = r
            (verified if r.get("verified") else rejected).append(c)
        print(f"\n[4] verify_quote 逐条核验：通过 {len(verified)} 条 · "
              f"**驳回 {len(rejected)} 条**")
        for c in rejected:
            print(f"    ✗ 「{(c.get('quote') or '')[:36]}…」"
                  f"（{c['_verify'].get('reason', '未命中语料')}）")

    mcp.close()

    # [5] 输出
    print("\n" + "─" * 68)
    print(draft.get("answer", "").strip())
    print("─" * 68)
    print(f"\n引用清单（{len(verified)} 条）：")
    for c in verified:
        mark = "✓ 已核验" if not a.no_verify else "· 未核验"
        print(f"  {mark} · {c.get('chunk_id', '?')}")
        print(f"      「{(c.get('quote') or '')[:60]}…」")

    if not a.no_verify and rejected:
        print(f"\n⚠ 被驳回的 {len(rejected)} 条引用**已从回答里剔除**——"
              f"这就是 MCP 核验的价值：模型写得出，语料里查不到。")

    print(f"\n耗时 {time.time() - t0:.1f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
