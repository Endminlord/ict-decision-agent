"""文件树与文本读写：哪些文件能看、多大算太大、存下去会不会盖掉别人的改动。

这个模块只做判断，不发 HTTP。服务端负责解析请求、把结果转成 JSON；**边界**
（必须待在工作区里）和**规矩**（覆盖要带版本戳、落笔前先存快照）写在这里，因为
界面、命令行、以后别的入口都该守同一份——两份判断迟早会走样，而走样的那一刻
「这里不能写」和「这里写得进去」就成了同一个仓库的两副面孔。

三条不肯让步的地方：

* **符号链接是另一条出路。** ``resolve_in_root`` 看的是字面路径，一个指到工作区
  外面的软链能绕过它。所以这里解析完还要比一次 ``realpath``。
* **哈希算在字节上，不算在字符串上。** 文件是 CRLF 的时候，按文本读一遍再编码
  回去，哈希就和磁盘上的对不上，于是「你读到的版本」和「磁盘上的版本」永远不等，
  保存功能看着在、其实每次都在拒绝。
* **写文件只有一条路：``patch.apply_file``。** 模型改文件和人在编辑器里改文件，
  守的必须是同一份规矩（要版本戳、先存快照、存不下就不改），否则「回退」这件事
  会对其中一方失效。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from runtime.patch import apply_file, resolve_in_root

__all__ = [
    "TREE_LIMIT",
    "FILE_MAX_BYTES",
    "IGNORED_DIRS",
    "IGNORED_FILES",
    "EDITOR_DIR",
    "EDITOR_URL",
    "language_of",
    "content_type_for",
    "editor_status",
    "list_directory",
    "read_text_file",
    "save_text_file",
    "vendor_path",
]

#: 一个目录一次最多列这么多条。再多就该用搜索了，而不是让浏览器画一万行。
TREE_LIMIT = 400

#: 编辑器能打开的字节上限。超过这个数，Monaco 也会开始卡，而人也不会在浏览器
#: 里读一个 5 MB 的文件——那时候他要的是搜索（见 /api/code），不是滚动条。
FILE_MAX_BYTES = 512 * 1024

#: 判断二进制看开头这么多字节。
SNIFF_BYTES = 8192

#: 不看的东西。这些目录里没有「这个仓库在说什么」，只有工具自己的噪音：
#: 版本库的对象、字节码、几十万个依赖文件。**跳过了要报出来**——一个悄悄少了
#: 一半的目录树，和一棵真的只有这么点东西的树，在界面上长得一模一样。
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", ".tox", ".runs", ".agent-backups", ".idea", ".vscode",
    ".cache", ".parcel-cache", "dist-info",
})
IGNORED_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

#: Monaco 的语言 id。认不出来就给 plaintext——**不猜**：猜错的语言会把一段
#: 中文注释染成一团红，比不着色更让人怀疑自己。
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".jsonl": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "ini",
    ".ini": "ini",
    ".cfg": "ini",
    ".env": "ini",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".rb": "ruby",
    ".php": "php",
    ".lua": "lua",
    ".pl": "perl",
    ".r": "r",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".xml": "xml",
    ".svg": "xml",
    ".bat": "bat",
    ".cmd": "bat",
    ".ps1": "powershell",
    ".dockerfile": "dockerfile",
    ".txt": "plaintext",
    ".log": "plaintext",
    ".csv": "plaintext",
    ".lock": "plaintext",
}
LANGUAGE_BY_NAME = {
    "Dockerfile": "dockerfile",
    "Makefile": "plaintext",
    ".gitignore": "plaintext",
    ".gitattributes": "plaintext",
}

#: Monaco 装在这儿（gitignore 的 vendor/，用 tools/install_editor.py 装）。
EDITOR_DIR = os.path.join("vendor", "monaco")
EDITOR_URL = "/vendor/monaco/vs"

#: 这条静态路由只发这几种东西。白名单不是洁癖：``vendor/`` 是**外面拿来的**，
#: 一份 tarball 里有什么不该由它的作者决定我们发什么。
VENDOR_SUFFIXES = frozenset({".js", ".css", ".ttf", ".woff", ".woff2", ".json", ".svg", ".png", ".html"})
MIME_BY_SUFFIX = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _now(stamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stamp))


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _resolve(root: Any, path: Any) -> tuple[str | None, str]:
    """把 ``path`` 解析到 ``root`` 里面，软链也算。出不去就返回原因，不抛。"""
    resolved, why = resolve_in_root(root, path)
    if resolved is None:
        return None, why
    real_root = os.path.realpath(str(root))
    real = os.path.realpath(resolved)
    if real != real_root and not real.startswith(real_root + os.sep):
        return None, f"symbolic link escapes the workspace: {path}"
    return resolved, ""


def language_of(name: Any) -> str:
    """给一个文件名猜语言 id。猜不出来是 ``plaintext``，不是空字符串。"""
    text = str(name or "")
    if text in LANGUAGE_BY_NAME:
        return LANGUAGE_BY_NAME[text]
    suffix = os.path.splitext(text)[1].lower()
    return LANGUAGE_BY_SUFFIX.get(suffix, "plaintext")


def content_type_for(path: Any) -> str:
    return MIME_BY_SUFFIX.get(os.path.splitext(str(path))[1].lower(), "application/octet-stream")


def editor_status(repo_root: Any) -> dict:
    """编辑器装了没。界面靠这个决定用 Monaco 还是轻编辑器，**不靠试**。"""
    root = os.path.abspath(str(repo_root))
    probe = os.path.join(root, EDITOR_DIR, "vs", "loader.js")
    status: dict = {
        "installed": os.path.isfile(probe),
        "dir": os.path.join(root, EDITOR_DIR),
        "url": EDITOR_URL,
    }
    if not status["installed"]:
        status["hint"] = "跑 python3 tools/install_editor.py 装一份（装到本地，界面不连 CDN）"
        return status
    try:
        with open(os.path.join(root, EDITOR_DIR, "VERSION"), encoding="utf-8") as stream:
            record = json.load(stream)
        for key in ("version", "files", "bytes", "installed_at"):
            if key in record:
                status[key] = record[key]
    except (OSError, ValueError) as exc:
        # 装是装了，但说不清是哪一版。这值得说出来，而不是猜一个版本号。
        status["note"] = f"VERSION 读不出来：{type(exc).__name__}: {exc}"
    return status


def vendor_path(repo_root: Any, rel: Any) -> tuple[str | None, str]:
    """把一条 ``vendor/monaco/vs/...`` 的请求解析成磁盘上的真实文件。"""
    if not isinstance(rel, str) or not rel.strip():
        return None, "empty asset path"
    cleaned = rel.split("?", 1)[0]
    if cleaned.startswith("/") or "\\" in cleaned:
        return None, f"bad asset path: {rel}"
    parts = [part for part in cleaned.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return None, f"bad asset path: {rel}"
    if parts[0] != "vs":
        return None, f"only /vendor/monaco/vs is served, not {parts[0]!r}"
    base = os.path.join(os.path.abspath(str(repo_root)), EDITOR_DIR)
    resolved = os.path.join(base, *parts)
    real_base = os.path.realpath(base)
    real = os.path.realpath(resolved)
    if real != real_base and not real.startswith(real_base + os.sep):
        return None, f"asset escapes the vendor directory: {rel}"
    suffix = os.path.splitext(real)[1].lower()
    if suffix not in VENDOR_SUFFIXES:
        return None, f"refused file type: {suffix or '(none)'}"
    if not os.path.isfile(real):
        return None, f"no such asset: {rel}"
    return real, ""


def list_directory(root: Any, path: Any = "", *, limit: int = TREE_LIMIT, show_hidden: bool = False) -> dict:
    """列一层目录。**一次只列一层**：整棵树一次发过去，在大仓库上就是一次卡死。

    返回 ``{ok, root, path, parent, entries, total, truncated, limit, skipped, hidden, notes}``；
    出错时返回 ``{ok: False, reason, detail?}``，理由都是能直接给人看的一句话。
    """
    resolved, why = _resolve(root, path if isinstance(path, str) and path.strip() else ".")
    if resolved is None:
        return {"ok": False, "reason": why}
    relative = "" if os.path.abspath(resolved) == os.path.abspath(str(root)) else os.path.relpath(resolved, str(root))
    if not os.path.exists(resolved):
        return {"ok": False, "reason": "no_such_directory", "path": relative}
    if not os.path.isdir(resolved):
        return {"ok": False, "reason": "not_a_directory", "path": relative}
    try:
        scanned = list(os.scandir(resolved))
    except OSError as exc:
        return {"ok": False, "reason": f"unreadable:{type(exc).__name__}", "detail": str(exc), "path": relative}

    skipped: list[str] = []
    hidden = 0
    visible: list[dict] = []
    for entry in scanned:
        name = entry.name
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
            is_link = entry.is_symlink()
        except OSError:
            is_dir = is_file = is_link = False
        if (is_dir and name in IGNORED_DIRS) or (is_file and name in IGNORED_FILES):
            skipped.append(name)
            continue
        if name.startswith(".") and not show_hidden:
            hidden += 1
            continue
        record: dict = {
            "name": name,
            "path": os.path.join(relative, name) if relative else name,
            "kind": "link" if is_link else ("dir" if is_dir else ("file" if is_file else "other")),
        }
        if not is_link and (is_dir or is_file):
            try:
                info = entry.stat(follow_symlinks=False)
                record["size"] = int(info.st_size)
                record["mtime"] = _now(info.st_mtime)
            except OSError:
                record["size"] = 0
                record["mtime"] = ""
        if record["kind"] == "file":
            record["language"] = language_of(name)
        visible.append(record)

    # 目录在前，各自按名字排。这不是审美：一屏里先看见「有哪些地方」，再看见
    # 「哪个文件」，比按修改时间混排更快找到路。
    visible.sort(key=lambda item: (0 if item["kind"] == "dir" else 1, item["name"].lower()))
    total = len(visible)
    truncated = total > limit
    notes: list[str] = []
    if truncated:
        notes.append(f"这个目录有 {total} 条，只列了前 {limit} 条。")
    if skipped:
        notes.append(f"跳过了 {len(skipped)} 个不看的东西：{'、'.join(sorted(skipped)[:6])}"
                     + ("…" if len(skipped) > 6 else ""))
    if hidden:
        notes.append(f"还有 {hidden} 个点开头的条目没显示。")
    parent = None
    if relative:
        parent = os.path.dirname(relative)
    return {
        "ok": True,
        "root": os.path.abspath(str(root)),
        "path": relative,
        "parent": parent,
        "entries": visible[:limit],
        "total": total,
        "truncated": truncated,
        "limit": limit,
        "skipped": sorted(skipped),
        "hidden": hidden,
        "show_hidden": bool(show_hidden),
        "notes": notes,
    }


def read_text_file(root: Any, path: Any, *, max_bytes: int = FILE_MAX_BYTES) -> dict:
    """读一个文本文件给编辑器。二进制、太大、读不出来，都各说各的理由。

    返回 ``{ok, path, size, mtime, sha256, text, lines, language, newline, bom}``。
    ``sha256`` 是**字节**的哈希，存回去的时候要原样带上——它就是「我读到的是哪一版」。
    """
    resolved, why = _resolve(root, path)
    if resolved is None:
        return {"ok": False, "reason": why, "path": str(path)}
    relative = os.path.relpath(resolved, os.path.abspath(str(root)))
    if not os.path.exists(resolved):
        return {"ok": False, "reason": "no_such_file", "path": relative}
    if os.path.isdir(resolved):
        return {"ok": False, "reason": "not_a_file", "path": relative}
    try:
        size = os.path.getsize(resolved)
    except OSError as exc:
        return {"ok": False, "reason": f"unreadable:{type(exc).__name__}", "detail": str(exc), "path": relative}
    if size > max_bytes:
        return {
            "ok": False,
            "reason": "too_big",
            "path": relative,
            "size": size,
            "limit": max_bytes,
            "detail": f"这个文件 {size} 字节，超过编辑器上限 {max_bytes} 字节。要找里面的东西，用「找代码」。",
        }
    try:
        with open(resolved, "rb") as stream:
            raw = stream.read()
        stamp = os.path.getmtime(resolved)
    except OSError as exc:
        return {"ok": False, "reason": f"unreadable:{type(exc).__name__}", "detail": str(exc), "path": relative}
    if b"\x00" in raw[:SNIFF_BYTES]:
        return {"ok": False, "reason": "binary", "path": relative, "size": len(raw),
                "detail": "这个文件是二进制（开头就有 NUL 字节）。"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"ok": False, "reason": "binary", "path": relative, "size": len(raw),
                "detail": f"这个文件不是 UTF-8 文本：{exc.reason}（第 {exc.start} 字节）"}
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {
        "ok": True,
        "path": relative,
        "size": len(raw),
        "mtime": _now(stamp),
        "sha256": _sha256(raw),
        "text": text,
        "lines": lines,
        "language": language_of(os.path.basename(resolved)),
        "newline": "crlf" if "\r\n" in text else "lf",
        "bom": text.startswith("\ufeff"),
    }


def save_text_file(
    root: Any,
    path: Any,
    text: Any,
    *,
    expected_sha256: str | None = None,
    snapshot_dir: Any = None,
) -> dict:
    """把编辑器里的正文存回去。**写不进去不是错误，是一次拒绝**——理由要能照做。

    返回 ``{ok, path, created, sha256_before, sha256_after, bytes_after, snapshot_id, undoable}``；
    拒绝时 ``{ok: False, reason, ...}``，其中 ``reason`` 取值：

    * ``changed_on_disk`` —— 你读它之后有人（或某次运行）改过。带上 ``sha256_now``，
      界面可以据此说「重读一遍再改」。
    * ``missing_version_stamp`` —— 文件已存在而你没带版本戳。
    * ``snapshot_failed:...`` —— 原文没存下来，所以一个字都没写。
    """
    if not isinstance(text, str):
        return {"ok": False, "reason": "text_must_be_a_string"}
    resolved, why = _resolve(root, path)
    if resolved is None:
        return {"ok": False, "reason": why, "path": str(path)}
    relative = os.path.relpath(resolved, os.path.abspath(str(root)))
    if os.path.isdir(resolved):
        return {"ok": False, "reason": "not_a_file", "path": relative}
    size = len(text.encode("utf-8"))
    if size > FILE_MAX_BYTES:
        return {"ok": False, "reason": "too_big", "path": relative, "size": size, "limit": FILE_MAX_BYTES}
    result = apply_file(resolved, text, expected_sha256=expected_sha256, snapshot_dir=snapshot_dir)
    if not result.get("applied"):
        reason = str(result.get("reason") or "refused")
        payload = {"ok": False, "path": relative, "reason": reason}
        if reason == "version_mismatch":
            payload.update({
                "reason": "changed_on_disk",
                "sha256_now": result.get("sha256_before", ""),
                "expected_sha256": result.get("expected_sha256", ""),
                "detail": "这个文件在你读它之后被改过（可能是某次运行写进去的）。重读一遍再改。",
            })
        elif reason == "missing_version_stamp":
            payload["detail"] = "文件已经存在，覆盖它要带上你读到的 sha256。"
        elif result.get("detail"):
            payload["detail"] = result["detail"]
        return payload
    return {
        "ok": True,
        "path": relative,
        "created": bool(result.get("created")),
        "sha256_before": result.get("sha256_before", ""),
        "sha256_after": result.get("sha256_after", ""),
        "bytes_after": int(result.get("bytes_after") or size),
        "snapshot_id": result.get("snapshot_id", ""),
        "undoable": bool(result.get("undoable")),
        "unchanged": result.get("sha256_before") == result.get("sha256_after"),
    }
