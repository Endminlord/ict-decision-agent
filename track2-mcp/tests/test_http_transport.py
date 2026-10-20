#!/usr/bin/env python3
"""MCP Streamable HTTP 传输自测（规范 2025-03-26）。

用法：python3 tests/test_http_transport.py

不依赖任何第三方库：服务端在**本进程**里起一个 `ThreadingHTTPServer`（端口 0，
由内核分配），客户端用标准库 `http.client` / `socket` 手写请求 —— 这样能精确
控制 `Accept`、`Authorization`、`Mcp-Session-Id` 这些决定协议分支的请求头。

覆盖的判据分四组：
  会话生命周期（initialize / session 校验 / DELETE）
  内容协商与错误码（Accept 406 / 401 / 404 / 202 / 405）
  JSON-RPC 错误（-32700 解析错误 / -32600 非对象）
  两个入口（HTTP CLI 子进程 / stdio 回归）
"""

from __future__ import annotations

import http.client
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ict_track2_mcp.http_server import (  # noqa: E402
    MCP_PATH,
    SESSION_HEADER,
    McpHandler,
    McpHTTPServer,
)
from ict_track2_mcp.server import corpus  # noqa: E402

CHUNK_ID = "be1e65625142f499-0003"


def hget(headers: list[tuple[str, str]], name: str) -> str | None:
    for k, v in headers:
        if k.lower() == name.lower():
            return v
    return None


class Client:
    """最小 HTTP 客户端（每次请求新建连接，避免连接复用干扰错误码判据）。"""

    def __init__(self, port: int, token: str | None = None, timeout: float = 30.0):
        self.port = port
        self.token = token
        self.timeout = timeout

    def request(self, method: str, body=None, headers: dict | None = None,
                path: str = MCP_PATH, timeout: float | None = None):
        payload = None
        hdrs: dict[str, str] = {}
        if body is not None:
            payload = (body if isinstance(body, bytes)
                       else json.dumps(body, ensure_ascii=False).encode("utf-8"))
            hdrs["Content-Type"] = "application/json"
        if self.token is not None:
            hdrs["Authorization"] = f"Bearer {self.token}"
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, list(resp.getheaders()), data
        finally:
            conn.close()

    def json_request(self, method: str, body=None, headers: dict | None = None):
        status, hdrs, data = self.request(method, body, headers)
        try:
            parsed = json.loads(data.decode("utf-8")) if data else None
        except json.JSONDecodeError:
            parsed = None
        return status, hdrs, parsed, data


def start_server(**kw) -> tuple[McpHTTPServer, int]:
    srv = McpHTTPServer(("127.0.0.1", 0), McpHandler, **kw)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return srv, port


def stop_server(srv: McpHTTPServer) -> None:
    srv.shutdown()
    srv.server_close()


def init_msg(mid: int = 1, pv: str = "2025-03-26") -> dict:
    return {"jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {"protocolVersion": pv, "capabilities": {},
                       "clientInfo": {"name": "http-selftest", "version": "0"}}}


def open_session(cli: Client) -> str:
    status, hdrs, _ = cli.request("POST", init_msg())
    assert status == 200, f"initialize 失败：{status}"
    sid = hget(hdrs, SESSION_HEADER)
    assert sid, "initialize 没返回 Mcp-Session-Id"
    return sid


def read_until(sock: socket.socket, marker: bytes, timeout: float = 8.0) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        if marker in buf:
            break
    return buf


def main() -> int:
    ok = total = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            print(f"  ✗ {name} {detail}")

    # 预热语料：判据里第一次 tools/call 不该把加载时间算进去。
    st = corpus().stats()
    print(f"  语料 {st['chunks']} 块 / {st['documents']} 篇")

    srv, port = start_server()
    cli = Client(port)
    try:
        # ---------------- 会话生命周期 ----------------
        status, hdrs, body = cli.request("POST", init_msg())
        sid = hget(hdrs, SESSION_HEADER)
        check("H1 initialize 200 且返回 Mcp-Session-Id 响应头",
              status == 200 and bool(sid), f"status={status} sid={sid!r}")
        check("H2 会话 ID 是可见 ASCII（规范要求）",
              bool(sid) and all(33 <= ord(c) <= 126 for c in sid or ""),
              f"sid={sid!r}")
        payload = json.loads(body.decode("utf-8"))
        check("H3 initialize 应答来自 server.handle()（serverInfo 一致）",
              payload.get("result", {}).get("serverInfo", {}).get("name")
              == "ict-track2-mcp", f"got {payload.get('result')}")

        # 带 session 的 tools/list
        status, hdrs, payload, _ = cli.json_request(
            "POST", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {SESSION_HEADER: sid})
        tools = (payload or {}).get("result", {}).get("tools", [])
        names = sorted(t["name"] for t in tools)
        check("H4 带 session 的 tools/list 返回 3 个工具",
              status == 200 and len(tools) == 3,
              f"status={status} tools={names}")

        # 带 session 的 tools/call
        status, hdrs, payload, _ = cli.json_request(
            "POST", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "lookup_chunk",
                                "arguments": {"chunk_id": CHUNK_ID}}},
            {SESSION_HEADER: sid})
        text = (payload or {}).get("result", {}).get("content", [{}])[0].get("text", "")
        hit = json.loads(text) if text else {}
        check("H5 带 session 的 tools/call 返回真实块（found + 标题）",
              status == 200 and hit.get("found") is True
              and hit.get("chunk_id") == CHUNK_ID and bool(hit.get("title")),
              "status=%s found=%s chunk_id=%s title=%r"
              % (status, hit.get("found"), hit.get("chunk_id"), hit.get("title")))

        # 不带 session
        status, hdrs, payload, _ = cli.json_request(
            "POST", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        check("H6 不带 Mcp-Session-Id 的请求返回 404",
              status == 404, f"status={status}")

        # 未知 session
        status, hdrs, payload, _ = cli.json_request(
            "POST", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
            {SESSION_HEADER: "not-a-real-session"})
        check("H7 未知 Mcp-Session-Id 返回 404",
              status == 404, f"status={status}")

        # ---------------- 内容协商与错误码 ----------------
        status, hdrs, data = cli.request(
            "POST", {"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
            {SESSION_HEADER: sid, "Accept": "text/plain"})
        check("H8 Accept 不含允许类型返回 406",
              status == 406, f"status={status}")

        status, hdrs, payload = cli.request(
            "POST", b'{"jsonrpc": "2.0", "id": 7, ', {SESSION_HEADER: sid})
        parsed = json.loads(payload.decode()) if payload else {}
        check("H9 非法 JSON 返回 -32700（HTTP 200 + JSON-RPC 错误）",
              status == 200 and parsed.get("error", {}).get("code") == -32700,
              f"status={status} body={parsed}")

        status, hdrs, payload = cli.request(
            "POST", b'[{"jsonrpc": "2.0", "id": 8, "method": "tools/list"}]',
            {SESSION_HEADER: sid})
        parsed = json.loads(payload.decode()) if payload else {}
        check("H10 非对象 JSON（数组）返回 -32600",
              status == 200 and parsed.get("error", {}).get("code") == -32600,
              f"status={status} body={parsed}")

        # 通知 → 202 空 body
        status, hdrs, data = cli.request(
            "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {SESSION_HEADER: sid})
        check("H11 通知返回 202 且 body 为空",
              status == 202 and data == b"", f"status={status} len={len(data)}")

        # SSE 协商：只接受 text/event-stream 时，POST 也走流式应答
        status, hdrs, data = cli.request(
            "POST", {"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            {SESSION_HEADER: sid, "Accept": "text/event-stream"})
        ctype = hget(hdrs, "Content-Type") or ""
        check("H12 Accept 仅 text/event-stream 时以 SSE 帧返回",
              status == 200 and ctype.startswith("text/event-stream")
              and b"event: message" in data and b"tools" in data,
              f"status={status} ctype={ctype} head={data[:60]!r}")

        # CORS 预检
        status, hdrs, data = cli.request(
            "OPTIONS", None, {"Origin": "https://agentarts.example",
                              "Access-Control-Request-Method": "POST"})
        acam = hget(hdrs, "Access-Control-Allow-Methods") or ""
        check("H13 OPTIONS 返回 204 + CORS 头（Allow-Methods 含 POST/DELETE）",
              status == 204 and "POST" in acam and "DELETE" in acam
              and hget(hdrs, "Access-Control-Allow-Origin") is not None,
              f"status={status} methods={acam!r}")

        # DELETE 结束会话
        status, hdrs, data = cli.request("DELETE", None, {SESSION_HEADER: sid})
        check("H14 DELETE 会话返回 204", status == 204, f"status={status}")

        status, hdrs, payload, _ = cli.json_request(
            "POST", {"jsonrpc": "2.0", "id": 10, "method": "tools/list"},
            {SESSION_HEADER: sid})
        check("H15 DELETE 后原 session 再请求返回 404",
              status == 404, f"status={status}")

        status, hdrs, data = cli.request("DELETE", None,
                                         {SESSION_HEADER: "gone-already"})
        check("H16 DELETE 未知会话返回 404", status == 404, f"status={status}")
    finally:
        stop_server(srv)

    # ---------------- 鉴权（独立 server） ----------------
    token = "s3cr3t-token-2026"
    srv2, port2 = start_server(token=token)
    try:
        bad = Client(port2, token="wrong-token")
        status, hdrs, data = bad.request("POST", init_msg())
        check("H17 错误 token 返回 401", status == 401, f"status={status}")

        status, hdrs, data = bad.request("POST", init_msg(),
                                         {"Authorization": "Bearer wrong"})
        check("H18 401 带 WWW-Authenticate 头",
              status == 401 and (hget(hdrs, "WWW-Authenticate") or "").startswith("Bearer"),
              f"status={status} www={hget(hdrs, 'WWW-Authenticate')!r}")

        good = Client(port2, token=token)
        status, hdrs, _ = good.request("POST", init_msg())
        check("H19 正确 token 返回 200 + 会话",
              status == 200 and bool(hget(hdrs, SESSION_HEADER)),
              f"status={status}")
    finally:
        stop_server(srv2)

    # ---------------- GET SSE 流 + 服务端推送 ----------------
    srv3, port3 = start_server()
    try:
        cli3 = Client(port3)
        sid3 = open_session(cli3)
        sock = socket.create_connection(("127.0.0.1", port3), timeout=8)
        sock.sendall(
            f"GET {MCP_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port3}\r\n"
            f"Accept: text/event-stream\r\n{SESSION_HEADER}: {sid3}\r\n\r\n"
            .encode())
        head = read_until(sock, b": connected", timeout=8)
        check("H20 GET /mcp 建立 SSE 流（200 + text/event-stream + 首帧注释）",
              b" 200 " in head.split(b"\r\n", 1)[0]
              and b"text/event-stream" in head and b": connected" in head,
              f"head={head[:120]!r}")

        pushed = {"jsonrpc": "2.0", "method": "notifications/message",
                  "params": {"level": "info", "data": "hello-from-server"}}
        srv3.push_to_session(sid3, pushed)
        got = read_until(sock, b"event: message", timeout=8)
        check("H21 服务端推送经 GET 流以 event: message 到达",
              b"event: message" in got and b"hello-from-server" in got,
              f"got={got[:200]!r}")
        sock.close()
    finally:
        stop_server(srv3)

    # ---------------- 不支持 GET 流时的 405 降级 ----------------
    srv4, port4 = start_server(enable_get_stream=False)
    try:
        cli4 = Client(port4)
        sid4 = open_session(cli4)
        status, hdrs, data = cli4.request(
            "GET", None, {SESSION_HEADER: sid4, "Accept": "text/event-stream"})
        allow = hget(hdrs, "Allow") or ""
        check("H22 --disable-get-stream 时 GET 返回 405 + Allow: POST, DELETE",
              status == 405 and "POST" in allow and "DELETE" in allow
              and "GET" not in allow,
              f"status={status} allow={allow!r}")
    finally:
        stop_server(srv4)

    # ---------------- HTTP CLI 子进程（真实入口 + banner） ----------------
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ict_track2_mcp.http_server",
         "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        banner = ""
        deadline = time.time() + 60
        while time.time() < deadline:
            r, _, _ = select.select([proc.stderr], [], [], 0.5)
            if not r:
                continue
            line = proc.stderr.readline()
            if not line:
                break
            if "listening on" in line:
                banner = line.strip()
                break
        check("H23 CLI 启动打印 listening banner",
              banner.startswith("[ict-track2-mcp-http] listening on http://"),
              f"banner={banner!r}")
        port5 = int(banner.rsplit(":", 1)[1].split("/")[0]) if banner else 0
        cli5 = Client(port5) if port5 else None
        status = 0
        if cli5 is not None:
            status, hdrs, _ = cli5.request("POST", init_msg())
        check("H24 CLI 进程上的 initialize 可用（HTTP 入口真跑通）",
              status == 200 and bool(hget(hdrs, SESSION_HEADER)) if cli5 else False,
              f"status={status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ---------------- stdio 入口回归 ----------------
    r = subprocess.run(
        [sys.executable, "-m", "ict_track2_mcp.server"],
        input=json.dumps(init_msg(1)) + "\n",
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    first = (r.stdout or "").strip().splitlines()
    stdio_payload = json.loads(first[0]) if first else {}
    check("H25 stdio main() 未被破坏（喂一条 initialize 仍正常应答）",
          r.returncode == 0
          and stdio_payload.get("result", {}).get("serverInfo", {}).get("name")
          == "ict-track2-mcp",
          f"rc={r.returncode} out={first[:1]} err={(r.stderr or '')[:200]}")

    print(f"\n[test_http_transport] {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
