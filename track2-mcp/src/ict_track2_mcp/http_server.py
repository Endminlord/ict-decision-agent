"""MCP Streamable HTTP 传输（规范 2025-03-26），零第三方依赖。

为什么换传输层不用碰协议实现
----------------------------
`server.py` 里的 `handle(msg: dict) -> dict | None` 是**纯函数**：它不读 stdin、
不写 stdout，只做 JSON-RPC 分发。stdio 的 `main()` 只是「读一行 → handle →
写一行」的循环。所以 HTTP 传输只需要把请求体喂给同一个 `handle()` —— 本模块
`from .server import handle`，`server.py` 一行不改，两个入口共用同一份协议逻辑。

对照规范（2025-03-26 Streamable HTTP）实现了哪些条
--------------------------------------------------
1. **单一端点** `/mcp` 同时承担 POST（客户端→服务端）与 GET（服务端→客户端 SSE）。
2. POST 请求体是**单个** JSON-RPC 消息；响应按 `Accept` 协商成
   `application/json`（同步）或 `text/event-stream`（单帧流式）。
3. 通知/响应（消息里没有 `id`）→ `202 Accepted` + 空 body。
4. `initialize` 的成功响应带 `Mcp-Session-Id` 响应头（`secrets.token_urlsafe`，
   密码学随机、可见 ASCII）；后续请求必须回带该头。
5. 会话缺失或未知 → `404`，客户端据此重新 initialize。
6. `Accept` 必须允许 `application/json` 或 `text/event-stream`（`*/*` 视为都允许），
   否则 `406`。缺省 `Accept` 按 RFC 9110 视为「任意类型都接受」。
7. `DELETE /mcp` 结束会话 → `204`；未知会话 → `404`。
8. `GET /mcp` 建立 SSE 流（心跳 + `Session.push()` 的服务端主动消息）；
   `--disable-get-stream` 时降级为 `405` + `Allow: POST, DELETE` —— 规范允许
   服务端不提供 GET 流，但必须显式声明而不是 500。
9. `OPTIONS /mcp` CORS 预检 → `204` + `Access-Control-Allow-*`。
10. 鉴权：`Authorization: Bearer <token>`（也接受裸 token）与 `--token` 比对，
    不匹配/缺失 → `401`；未给 `--token` 则不校验（仅限本地调试）。

与 `server.py` 的一处已知差异（不改它，只记录）
-----------------------------------------------
`server.py` 的 `PROTOCOL_VERSION` 是 `"2024-11-05"`（stdio 时代的常量）。
`initialize` 的应答体由 `handle()` 生成，本传输层**不篡改**应答体 —— 所以
HTTP 侧协商出的协议版本仍是 `2024-11-05`，而传输语义按 `2025-03-26` 实现。
若网关强制要求 `2025-03-26`，改 `server.py` 那一行常量即可（属主会话决定）。
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty, Queue
from typing import Any
from urllib.parse import urlsplit

from .server import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, corpus, handle

MCP_PATH = "/mcp"
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
#: 单条 JSON-RPC 消息的上限。MCP 的请求都很小（最大的是 verify_quote 的引用
#: 文本），4 MiB 足够；设上限是为了不让一个坏客户端把内存吃光。
MAX_BODY_BYTES = 4 * 1024 * 1024
#: `MCP-Protocol-Version` 请求头（2025-06-18 起要求）。本服务只观测不拒绝 ——
#: 拒绝未知版本只会在网关升级时把连接打挂，收益为零。
KNOWN_PROTOCOL_VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18"})

JSON_TYPE = "application/json"
SSE_TYPE = "text/event-stream"


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Session:
    """一个 MCP 会话：只存 id、时间戳和一条推送队列，不存协议状态。

    `handle()` 是无状态的纯函数，会话对协议层没有意义 —— 它的作用有两个：
    让网关能识别「同一次连接生命周期」，以及给 GET 流一个服务端主动推送的落点。
    """

    __slots__ = ("id", "created_at", "last_seen", "protocol_version",
                 "stop_event", "queue")

    def __init__(self, sid: str, protocol_version: str | None = None) -> None:
        self.id = sid
        self.created_at = time.time()
        self.last_seen = self.created_at
        self.protocol_version = protocol_version
        self.stop_event = threading.Event()
        self.queue: Queue[dict] = Queue()

    def touch(self) -> None:
        self.last_seen = time.time()

    def push(self, message: dict) -> None:
        """投递一条服务端主动消息（GET 流上以 `event: message` 发出）。"""
        self.queue.put(message)


class McpHTTPServer(ThreadingHTTPServer):
    """线程化 HTTP 服务：每连接一个线程，会话表加锁。

    `lookup.Corpus` 加载后只读（`_norm_text` 是惰性缓存，写同 key 的并发
    是幂等的），所以工具调用天然并发安全，不需要把请求串行化。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_cls=None,
                 *, token: str | None = None, session_ttl: float = 3600.0,
                 enable_get_stream: bool = True,
                 get_stream_keepalive: float = 15.0) -> None:
        # handler_cls 缺省取本模块的 McpHandler（定义在下方，调用时才解析）。
        super().__init__(server_address, handler_cls or McpHandler)
        self.token = token
        self.session_ttl = session_ttl
        self.enable_get_stream = enable_get_stream
        self.get_stream_keepalive = get_stream_keepalive
        self.started_at = time.time()
        self.stopping = threading.Event()
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ---- 会话表 ----
    def new_session(self, protocol_version: str | None = None) -> Session:
        with self._lock:
            self._sweep_locked()
            sid = secrets.token_urlsafe(24)
            s = Session(sid, protocol_version)
            self._sessions[sid] = s
            return s

    def get_session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return None
            if self.session_ttl > 0 and time.time() - s.last_seen > self.session_ttl:
                self._sessions.pop(sid, None)
                s.stop_event.set()
                return None
            return s

    def drop_session(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s is None:
            return False
        s.stop_event.set()  # 让挂着的 GET 流立刻收尾
        return True

    def push_to_session(self, sid: str, message: dict) -> bool:
        s = self.get_session(sid)
        if s is None:
            return False
        s.push(message)
        return True

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def sweep(self) -> None:
        with self._lock:
            self._sweep_locked()

    def _sweep_locked(self) -> None:
        if self.session_ttl <= 0:
            return
        now = time.time()
        for sid in [k for k, v in self._sessions.items()
                    if now - v.last_seen > self.session_ttl]:
            self._sessions.pop(sid, None).stop_event.set()

    def server_close(self) -> None:
        self.stopping.set()
        with self._lock:
            for s in self._sessions.values():
                s.stop_event.set()
        super().server_close()


class McpHandler(BaseHTTPRequestHandler):
    """`/mcp` 上的四个动词 + `/healthz` 探活。"""

    server_version = f"{SERVER_NAME}-http/{SERVER_VERSION}"
    sys_version = ""
    #: HTTP/1.1 才有 keep-alive；每个响应都必须带 Content-Length（SSE 流除外）。
    protocol_version = "HTTP/1.1"
    #: 半开连接不能把线程永远挂住（读 body、读请求行都受这个超时约束）。
    timeout = 60

    # ------------------------------------------------------------------ 工具
    @property
    def srv(self) -> McpHTTPServer:
        return self.server  # type: ignore[return-value]

    def _log(self, text: str) -> None:
        sys.stderr.write(f"[{SERVER_NAME}-http] {text}\n")
        sys.stderr.flush()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{SERVER_NAME}-http] {self.address_string()} {fmt % args}\n")
        sys.stderr.flush()

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        self.send_header("Access-Control-Allow-Origin", origin or "*")
        if origin:
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Expose-Headers", SESSION_HEADER)

    def _respond(self, status: int, body: bytes = b"", content_type: str | None = None,
                 extra: dict[str, str] | None = None, *,
                 include_session: str | None = None, close: bool = False) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if status not in (204, 304):
            self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        if include_session:
            self.send_header(SESSION_HEADER, include_session)
        if close:
            # 早退（401/406/405）时请求体还没读：keep-alive 下残留字节会被当成
            # 下一条请求行解析，所以必须显式关连接。
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and status not in (204, 304):
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

    # ---- 路由 ----
    def _target(self) -> str | None:
        path = urlsplit(self.path).path
        if path in (MCP_PATH, MCP_PATH + "/"):
            return "mcp"
        if path in ("/healthz", "/health", "/"):
            return "health"
        self._respond(404, _json_bytes(
            {"error": "not_found", "path": path, "endpoint": MCP_PATH}), JSON_TYPE)
        return None

    def _allow(self) -> str:
        return ("POST, GET, DELETE, OPTIONS" if self.srv.enable_get_stream
                else "POST, DELETE")

    def _method_not_allowed(self) -> None:
        allow = self._allow()
        self._respond(405, _json_bytes({"error": "method_not_allowed", "allow": allow}),
                      JSON_TYPE, {"Allow": allow}, close=True)

    def _unauthorized(self) -> None:
        self._respond(401, _json_bytes({
            "error": "unauthorized",
            "message": "缺少或错误的 Authorization。需要：Authorization: Bearer <token>",
        }), JSON_TYPE,
            {"WWW-Authenticate": 'Bearer realm="ict-track2-mcp"'}, close=True)

    def _not_acceptable(self) -> None:
        self._respond(406, _json_bytes({
            "error": "not_acceptable",
            "message": "Accept 必须允许 application/json 或 text/event-stream",
            "accept": self.headers.get("Accept"),
        }), JSON_TYPE, close=True)

    def _session_not_found(self, missing: bool) -> None:
        msg = ("缺少 Mcp-Session-Id：先 POST initialize 拿会话" if missing
               else "会话未知或已过期：重新 POST initialize")
        self._respond(404, _json_bytes({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32001, "message": msg},
        }), JSON_TYPE)

    def _jsonrpc_error(self, mid: Any, code: int, message: str, *,
                       sse: bool) -> None:
        payload = {"jsonrpc": "2.0", "id": mid,
                   "error": {"code": code, "message": message}}
        if sse:
            self._sse_once([payload])
        else:
            self._respond(200, _json_bytes(payload), JSON_TYPE)

    # ---- 鉴权与内容协商 ----
    def _authorized(self) -> bool:
        token = self.srv.token
        if not token:  # 未配置 token：不校验（仅限本地调试）
            return True
        raw = (self.headers.get("Authorization") or "").strip()
        value = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
        if not value:
            return False
        # 常量时间比较；先 encode 避免非 ASCII token 触发 TypeError。
        return hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8"))

    def _negotiate_accept(self) -> tuple[bool, bool]:
        """返回 `(可接受 JSON, 可接受 SSE)`。缺省 Accept 视为两者都接受。"""
        raw = self.headers.get("Accept")
        if raw is None or not raw.strip():
            return True, True
        ok_json = ok_sse = False
        for part in raw.split(","):
            item = part.strip()
            if not item:
                continue
            bits = [b.strip() for b in item.split(";")]
            media = bits[0].lower()
            q = 1.0
            for b in bits[1:]:
                if b.lower().startswith("q="):
                    try:
                        q = float(b[2:])
                    except ValueError:
                        q = 0.0
            if q <= 0:
                continue
            if media in ("*/*", "application/*"):
                ok_json = ok_sse = True
            elif media == JSON_TYPE:
                ok_json = True
            elif media == SSE_TYPE:
                ok_sse = True
        return ok_json, ok_sse

    # ---- 请求体 ----
    def _read_body(self) -> bytes | None:
        """读完整请求体；失败时已发出响应并返回 None。"""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return self._read_chunked()
        raw_len = self.headers.get("Content-Length")
        try:
            n = int(raw_len or 0)
        except ValueError:
            self._respond(400, _json_bytes({"error": "bad_content_length"}),
                          JSON_TYPE, close=True)
            return None
        if n < 0:
            self._respond(400, _json_bytes({"error": "bad_content_length"}),
                          JSON_TYPE, close=True)
            return None
        if n > MAX_BODY_BYTES:
            self._respond(413, _json_bytes(
                {"error": "payload_too_large", "limit": MAX_BODY_BYTES}),
                JSON_TYPE, close=True)
            return None
        if n == 0:
            return b""
        try:
            return self.rfile.read(n)
        except OSError:
            return None

    def _read_chunked(self) -> bytes | None:
        """`Transfer-Encoding: chunked` 解码（部分网关/代理会用分块发送）。"""
        out = bytearray()
        while True:
            line = self.rfile.readline(65537)
            if not line:
                return None
            size_s = line.split(b";", 1)[0].strip()
            try:
                size = int(size_s, 16)
            except ValueError:
                self._respond(400, _json_bytes({"error": "bad_chunk"}), JSON_TYPE,
                              close=True)
                return None
            if size == 0:
                while True:  # 吃掉 trailer
                    t = self.rfile.readline(65537)
                    if not t or t in (b"\r\n", b"\n"):
                        break
                return bytes(out)
            if len(out) + size > MAX_BODY_BYTES:
                self._respond(413, _json_bytes(
                    {"error": "payload_too_large", "limit": MAX_BODY_BYTES}),
                    JSON_TYPE, close=True)
                return None
            out += self.rfile.read(size)
            self.rfile.read(2)  # 块尾 CRLF

    # ---- SSE ----
    def _sse_write(self, text: str) -> bool:
        try:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            return False

    def _sse_once(self, messages: list[dict], *,
                  include_session: str | None = None) -> None:
        """把若干 JSON-RPC 消息作为一帧 SSE 同步返回（POST 的流式应答）。"""
        body = b"".join(
            b"event: message\ndata: " + _json_bytes(m) + b"\n\n" for m in messages)
        self._respond(200, body, f"{SSE_TYPE}; charset=utf-8",
                      {"Cache-Control": "no-cache, no-transform"},
                      include_session=include_session)

    # ---- 分发 ----
    def _dispatch(self, msg: dict) -> dict | None:
        try:
            return handle(msg)
        except Exception as e:  # noqa: BLE001 — 服务端边界，一条消息不能带走会话
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603,
                              "message": f"Internal error: {type(e).__name__}: {e}"}}

    # ------------------------------------------------------------------ 动词
    def do_OPTIONS(self) -> None:  # noqa: N802
        target = self._target()
        if target is None:
            return
        # 预检不带 Authorization（浏览器不发），所以鉴权在 OPTIONS 上放行。
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", self._allow())
        self.send_header("Access-Control-Allow-Headers",
                         f"Content-Type, Authorization, {SESSION_HEADER}, "
                         f"{PROTOCOL_HEADER}, Last-Event-ID")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        target = self._target()
        if target is None:
            return
        if target == "health":
            return self._health()
        if not self._authorized():
            return self._unauthorized()
        if not self.srv.enable_get_stream:
            # 规范允许服务端不提供 GET 流；必须显式 405 + Allow，而不是 500。
            return self._method_not_allowed()
        ok_json, ok_sse = self._negotiate_accept()
        if not ok_sse:
            return self._not_acceptable()
        sid = self.headers.get(SESSION_HEADER)
        sess = self.srv.get_session(sid)
        if sess is None:
            return self._session_not_found(not sid)
        self._stream(sess)

    def _health(self) -> None:
        self._respond(200, _json_bytes({
            "status": "ok",
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "protocol": PROTOCOL_VERSION,
            "sessions": self.srv.session_count(),
            "uptime_s": round(time.time() - self.srv.started_at, 1),
        }), JSON_TYPE)

    def _stream(self, sess: Session) -> None:
        """GET 的 SSE 长连接：心跳 + `Session.push()` 投递的消息。

        响应没有 Content-Length，靠 `Connection: close` 定界 —— 客户端读到 EOF
        即流结束（curl -N 的行为一致）。心跳间隔默认 15 s，短于常见网关的 60 s
        空闲回收。
        """
        self.send_response(200)
        self.send_header("Content-Type", f"{SSE_TYPE}; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header(SESSION_HEADER, sess.id)
        self._cors_headers()
        self.end_headers()
        self.close_connection = True

        if not self._sse_write(f": connected session={sess.id}\n\n"):
            return
        keepalive = max(1.0, float(self.srv.get_stream_keepalive))
        deadline = time.monotonic() + keepalive
        while not sess.stop_event.is_set() and not self.srv.stopping.is_set():
            try:
                item = sess.queue.get(timeout=0.5)
            except Empty:
                if time.monotonic() >= deadline:
                    if not self._sse_write(": keepalive\n\n"):
                        break
                    deadline = time.monotonic() + keepalive
                continue
            frame = (f"event: message\ndata: "
                     f"{json.dumps(item, ensure_ascii=False)}\n\n")
            if not self._sse_write(frame):
                break
            deadline = time.monotonic() + keepalive
        self._sse_write(": stream closed\n\n")

    def do_POST(self) -> None:  # noqa: N802
        target = self._target()
        if target is None:
            return
        if target != "mcp":
            return self._method_not_allowed()
        if not self._authorized():
            return self._unauthorized()
        ok_json, ok_sse = self._negotiate_accept()
        if not (ok_json or ok_sse):
            return self._not_acceptable()

        body = self._read_body()
        if body is None:
            return
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as e:
            return self._jsonrpc_error(
                None, -32700, f"Parse error: 请求体不是合法 UTF-8（{e.reason}）",
                sse=not ok_json)
        try:
            msg = json.loads(text)
        except json.JSONDecodeError as e:
            return self._jsonrpc_error(None, -32700, f"Parse error: {e.msg}",
                                       sse=not ok_json)
        if not isinstance(msg, dict):
            # MCP 的 POST 只收单个 JSON-RPC 消息（对象）；数组/标量属结构非法。
            return self._jsonrpc_error(
                None, -32600, "Invalid Request: 必须是 JSON 对象（不支持批量数组）",
                sse=not ok_json)

        pv_header = self.headers.get(PROTOCOL_HEADER)
        if pv_header and pv_header.strip() not in KNOWN_PROTOCOL_VERSIONS:
            self._log(f"未知 {PROTOCOL_HEADER}: {pv_header!r}（按已知版本继续）")

        method = msg.get("method")
        sid_in = self.headers.get(SESSION_HEADER)
        sess: Session | None = None

        if method == "initialize":
            params = msg.get("params")
            pv = params.get("protocolVersion") if isinstance(params, dict) else None
            resp = self._dispatch(msg)
            if isinstance(resp, dict) and "result" in resp:
                sess = self.srv.new_session(pv if isinstance(pv, str) else None)
                if isinstance(pv, str) and pv != PROTOCOL_VERSION:
                    self._log(
                        f"会话 {sess.id[:8]}…：客户端请求协议 {pv}，"
                        f"handle() 应答 {PROTOCOL_VERSION}（传输层不篡改应答体）")
        else:
            sess = self.srv.get_session(sid_in)
            if sess is None:
                return self._session_not_found(not sid_in)
            sess.touch()
            resp = self._dispatch(msg)

        sid_out = sess.id if sess else None
        if resp is None:
            # 通知/响应：202 + 空 body（规范要求）。
            return self._respond(202, b"", None, include_session=sid_out)
        if ok_json:
            return self._respond(200, _json_bytes(resp), JSON_TYPE,
                                 include_session=sid_out)
        return self._sse_once([resp], include_session=sid_out)

    def do_DELETE(self) -> None:  # noqa: N802
        target = self._target()
        if target is None:
            return
        if target != "mcp":
            return self._method_not_allowed()
        if not self._authorized():
            return self._unauthorized()
        body = self._read_body()  # DELETE 一般没有 body，有也要读完再回
        if body is None:
            return
        sid = self.headers.get(SESSION_HEADER)
        if not self.srv.drop_session(sid):
            return self._session_not_found(not sid)
        return self._respond(204)

    def do_PUT(self) -> None:  # noqa: N802
        if self._target() is not None:
            self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        if self._target() is not None:
            self._method_not_allowed()


def _install_signal_handlers(srv: McpHTTPServer) -> None:
    """SIGINT/SIGTERM → 在别的线程里 shutdown（serve_forever 占着主线程）。"""
    def _stop(signum, frame):  # noqa: ANN001, ARG001
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # 非主线程 / 平台不支持
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ict-track2-mcp-http",
        description="引用回查 MCP 服务的 Streamable HTTP 传输（规范 2025-03-26）。"
                    "与 stdio 入口共用同一份 handle()，语料换成任何行业的 "
                    "jsonl.gz 都能直接跑。",
    )
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    ap.add_argument("--data", default=None,
                    help="语料文件路径（.jsonl.gz 或 .jsonl）。缺省读环境变量 "
                         "ICT_TRACK2_DATA，再缺省用包内 data/chunks.jsonl.gz")
    ap.add_argument("--token", default=None,
                    help="Bearer token；不给则不校验（仅限本地调试）")
    ap.add_argument("--session-ttl", type=float, default=3600.0,
                    help="会话空闲过期秒数（默认 3600，0 表示不过期）")
    ap.add_argument("--disable-get-stream", action="store_true",
                    help="不提供 GET SSE 流：按规范回 405 + Allow: POST, DELETE")
    ap.add_argument("--version", action="version",
                    version=f"ict-track2-mcp-http {SERVER_VERSION}")
    a = ap.parse_args(argv)

    # 预热语料：与 stdio 入口一致，加载失败要立刻炸在启动阶段。
    try:
        st = corpus(a.data).stats()
        print(f"[ict-track2-mcp-http] 已加载 {st['chunks']} 块 / "
              f"{st['documents']} 篇 · 来源 {st['sources']} 个 · "
              f"年份 {st['year_range']} · {st['data_file']}",
              file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[ict-track2-mcp-http] 语料加载失败：{type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        raise

    srv = McpHTTPServer(
        (a.host, a.port), McpHandler,
        token=a.token,
        session_ttl=a.session_ttl,
        enable_get_stream=not a.disable_get_stream,
    )
    host, port = srv.server_address[0], srv.server_address[1]
    print(f"[ict-track2-mcp-http] listening on http://{host}:{port}/mcp",
          file=sys.stderr, flush=True)
    print(f"[ict-track2-mcp-http] auth: "
          + ("Bearer <token> required" if a.token
             else "DISABLED（未提供 --token，仅限本地调试）"),
          file=sys.stderr, flush=True)
    print(f"[ict-track2-mcp-http] GET stream: "
          + ("enabled (SSE)" if srv.enable_get_stream else "disabled (405)"),
          file=sys.stderr, flush=True)

    _install_signal_handlers(srv)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:  # 没有装信号处理器时的兜底
        pass
    finally:
        srv.server_close()
        print("[ict-track2-mcp-http] stopped", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
