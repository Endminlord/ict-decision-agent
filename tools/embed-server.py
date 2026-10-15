#!/usr/bin/env python3
"""bge-m3 (ONNX) embedding 服务 —— 暴露 OpenAI 兼容的 /v1/embeddings。

只依赖 onnxruntime + tokenizers + numpy（无需 torch）。

用法:
  embed-server.py [--model-dir DIR] [--host 0.0.0.0] [--port 8792]
                  [--max-length 512] [--batch-size 8] [--threads N]

端点:
  POST /v1/embeddings  {"input": "文本" | ["文本", ...], "model": "bge-m3"}
                       -> {"data":[{"embedding":[...],"index":i}], "model":..., "usage":{...}}
  GET  /v1/models      -> OpenAI 风格模型列表
  GET  /health         -> 存活 + 已加载模型信息
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class Embedder:
    """bge-m3 ONNX 推理：tokenize -> session.run -> pooling -> L2 归一化。"""

    def __init__(self, model_dir, max_length=512, batch_size=8, threads=0):
        self.model_dir = model_dir
        self.max_length = max_length
        self.batch_size = batch_size
        opts = ort.SessionOptions()
        if threads > 0:
            opts.intra_op_num_threads = threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        onnx_path = f"{model_dir}/model.onnx"
        self.session = ort.InferenceSession(onnx_path, opts, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.output_names = [o.name for o in self.session.get_outputs()]
        # 导出是否自带句向量输出（决定是否需要自己做 CLS pooling）
        self.pre_pooled = any("sentence_embedding" in n for n in self.output_names)
        self.tokenizer = Tokenizer.from_file(f"{model_dir}/tokenizer.json")
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding()
        self.lock = threading.Lock()  # tokenizers 的 encode_batch 非线程安全
        self.calls = 0
        self.texts = 0
        self.started = time.time()

    def _select_vectors(self, outs):
        """取句向量：优先用导出已算好的 sentence_embedding；否则对 token 级输出做 CLS pooling。

        注意：bge-m3 的 onnx 导出同时给出 token_embeddings(3D) 与 sentence_embedding(2D)，
        必须取后者——取前者会得到「每 token 一个向量」而不是「每句一个向量」。
        """
        for name, arr in zip(self.output_names, outs):
            if "sentence_embedding" in name:
                v = np.asarray(arr, dtype=np.float32)
                break
        else:
            hidden = None
            for name, arr in zip(self.output_names, outs):
                if "last_hidden_state" in name or arr.ndim == 3:
                    hidden = np.asarray(arr, dtype=np.float32)
                    break
            if hidden is None:
                raise RuntimeError("onnx 输出里既没有 sentence_embedding 也没有 token 级输出")
            v = hidden[:, 0]  # CLS pooling（bge-m3 官方用法）
        if v.ndim != 2:
            raise RuntimeError(f"句向量维度应为 2，实际 {v.shape}")
        norm = np.linalg.norm(v, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return v / norm

    def encode(self, texts):
        vecs = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            with self.lock:
                encs = self.tokenizer.encode_batch(batch)
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed = {}
            if "input_ids" in self.input_names:
                feed["input_ids"] = ids
            if "attention_mask" in self.input_names:
                feed["attention_mask"] = mask
            if "token_type_ids" in self.input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            outs = self.session.run(None, feed)
            vecs.append(self._select_vectors(outs))
        self.calls += 1
        self.texts += len(texts)
        return np.vstack(vecs) if vecs else np.zeros((0, 1024), dtype=np.float32)


EMB = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, {
                "status": "ok", "model": "bge-m3",
                "dim": 1024, "max_length": EMB.max_length,
                "batch_size": EMB.batch_size, "pre_pooled": EMB.pre_pooled,
                "calls": EMB.calls, "texts": EMB.texts,
                "uptime_s": round(time.time() - EMB.started, 1),
            })
        elif self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [
                {"id": "bge-m3", "object": "model", "owned_by": "local-onnx"}]})
        else:
            self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        if not self.path.startswith("/v1/embeddings"):
            self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})
            return
        inp = req.get("input")
        sys.stderr.write("[req] keys=%s model=%r input_type=%s input_preview=%r\n" % (
            sorted(req.keys()), req.get("model"), type(inp).__name__,
            (inp[:120] if isinstance(inp, str) else
             [x[:60] if isinstance(x, str) else x for x in inp][:3] if isinstance(inp, list) else inp)))
        if isinstance(inp, str):
            texts, single = [inp], True
        elif isinstance(inp, list) and all(isinstance(x, str) for x in inp):
            texts, single = inp, False
        else:
            self._send(400, {"error": {"message": "input must be a string or list of strings",
                                       "type": "invalid_request_error"}})
            return
        if not texts or not any(t.strip() for t in texts):
            self._send(400, {"error": {"message": "input is empty or blank",
                                       "type": "invalid_request_error"}})
            return
        try:
            t0 = time.time()
            vecs = EMB.encode(texts)
        except Exception as e:
            self._send(500, {"error": {"message": f"inference failed: {e}", "type": "server_error"}})
            return
        toks = sum(len(t) for t in texts)
        self._send(200, {
            "object": "list",
            "model": req.get("model") or "bge-m3",
            "data": [{"object": "embedding", "index": i, "embedding": v.tolist()}
                     for i, v in enumerate(vecs)],
            "usage": {"prompt_tokens": toks, "total_tokens": toks},
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "single": single,
        })


def main():
    global EMB
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default="/home/endmin/.cache/bge-m3-onnx")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--threads", type=int, default=0)
    a = ap.parse_args()
    t0 = time.time()
    EMB = Embedder(a.model_dir, a.max_length, a.batch_size, a.threads)
    print(f"已加载 bge-m3（{time.time()-t0:.1f}s）| 输入 {sorted(EMB.input_names)} | "
          f"输出 {EMB.output_names} | pre_pooled={EMB.pre_pooled}", flush=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"监听 http://{a.host}:{a.port}  max_length={a.max_length} batch={a.batch_size}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
