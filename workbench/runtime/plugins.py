"""The plugin seam: one script in a folder becomes one button on the page.

A plugin is **one file**. Its manifest is a module-level ``PLUGIN`` dict, and the
file is *parsed with* ``ast`` — never imported. That is the whole point: asking
"这个目录里有哪些按钮" must not run anybody's code. Running one is a separate,
gated act, and it happens in a child process with a JSON envelope on stdin and
an answer on stdout.

Three things this module refuses to pretend:

  写得出文件 ≠ 按得动。语法错了、清单不是字面量、id 撞了——都照样列出来，
  写清是哪一条不对，而不是从列表里消失（看不见的错误没法修）。

  解析 ≠ 执行。扫描只读文件、只做 ``ast.parse``。谁写的脚本都不会因为「页面
  打开着」而被跑一遍。

  跑得起来 ≠ 跑得到你。插件拿不到这个进程的环境变量——密钥不递过去，脚本要
  用什么都得自己说清楚。这一条是设计，不是疏漏。

The runner is deliberately not a sandbox (same stance as ``runtime/executor.py``):
it is a separate process with a timeout and a minimal environment, which keeps a
plugin from taking the server down with it, and nothing more.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

PLUGIN_DIRNAME = "plugins"

MAX_PLUGIN_BYTES = 64 * 1024
MAX_PLUGINS = 200
MAX_PARAMS = 8
MAX_TITLE_CHARS = 60
MAX_WHAT_CHARS = 200
MAX_VALUE_CHARS = 4000
MAX_OUTPUT_BYTES = 256 * 1024

DEFAULT_TIMEOUT = 20.0
TERMINATION_TIMEOUT = 3.0

PARAM_KINDS = ("text", "number", "choice", "flag")

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
_SKIP_PREFIXES = ("_", ".")

#: Handed to a plugin process, and nothing else. Credentials are not on this
#: list on purpose: a script the user drops in a folder is not an argument for
#: giving it the keys this process is holding.
_ENV_ALLOWED = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TZ")

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSY = frozenset({"0", "false", "no", "off", "n", "f", ""})


def _text(value: Any, limit: int = 0) -> str:
    if isinstance(value, str):
        out = value.strip()
    elif value is None:
        out = ""
    else:
        out = ""
    return out[:limit] if limit else out


def _why(reason: str, detail: str) -> dict:
    return {"ok": False, "plugin": None, "reason": reason, "why": detail}


def _manifest_node(tree: ast.AST) -> ast.expr | None:
    """The value assigned to a module-level ``PLUGIN``, if there is one."""
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "PLUGIN" in names:
                return node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "PLUGIN":
                return node.value
    return None


def read_manifest(source: Any) -> dict:
    """Read a plugin's manifest out of its text — without running it.

    Returns ``{"ok", "plugin", "reason", "why"}``. A file that cannot be read is
    reported, not skipped: the caller wants to show it in the list with the
    reason attached.
    """
    if not isinstance(source, str) or not source.strip():
        return _why("empty", "这个文件是空的。")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        line = f"第 {exc.lineno} 行" if exc.lineno else "某一行"
        return _why(
            f"syntax error: {type(exc).__name__}",
            f"{line}读不通（{exc.msg}）——脚本本身还没跑，光看就错了。",
        )
    node = _manifest_node(tree)
    if node is None:
        return _why(
            "no manifest",
            "文件里没有 PLUGIN = {...} 这一行——加上了，这个按钮才会出现。",
        )
    try:
        raw = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError) as exc:
        return _why(
            f"manifest is not a literal: {type(exc).__name__}",
            "PLUGIN 只能是字面量（字符串、数字、列表、字典）：读清单的时候不跑你的代码，所以不能是函数调用或变量。",
        )
    if not isinstance(raw, dict):
        return _why("manifest is not a dict", f"PLUGIN 是 {type(raw).__name__}，得是个字典。")
    return _read_fields(raw)


def _read_fields(raw: dict) -> dict:
    title = _text(raw.get("title"), MAX_TITLE_CHARS)
    if not title:
        return _why("title is empty", "PLUGIN 里少了 title——按钮上总得写点什么。")
    plugin_id = _text(raw.get("id"))
    if plugin_id and not _ID.match(plugin_id):
        return _why(
            f"bad id: {plugin_id}",
            "id 只能用小写字母、数字和 . _ -（最长 48 个字符），而且要以字母或数字开头。",
        )
    raw_params = raw.get("params")
    if raw_params is None:
        raw_params = []
    if not isinstance(raw_params, list):
        return _why("params is not a list", f"params 是 {type(raw_params).__name__}，得是个列表。")
    if len(raw_params) > MAX_PARAMS:
        return _why("too many params", f"一个插件最多 {MAX_PARAMS} 个参数，这个写了 {len(raw_params)} 个。")
    params: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_params, 1):
        spec = _param_spec(item)
        if not spec.get("ok"):
            return _why(spec["reason"], f"第 {index} 个参数：{spec['why']}")
        if spec["param"]["name"] in seen:
            return _why(
                f"duplicate param name: {spec['param']['name']}",
                f"第 {index} 个参数和第 {index - 1} 个重名了。",
            )
        seen.add(spec["param"]["name"])
        params.append(spec["param"])
    return {
        "ok": True,
        "plugin": {
            "id": plugin_id,
            "title": title,
            "what": _text(raw.get("what"), MAX_WHAT_CHARS),
            "params": params,
        },
        "reason": "",
        "why": "",
    }


def _param_spec(item: Any) -> dict:
    if not isinstance(item, dict):
        return {"ok": False, "reason": "param is not a dict", "why": f"参数得是个字典，这个是 {type(item).__name__}。"}
    name = _text(item.get("name"))
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$", name):
        return {
            "ok": False,
            "reason": "param has no name",
            "why": "参数得有 name（字母或下划线开头，最长 32 个字符）。",
        }
    kind = _text(item.get("kind")) or "text"
    if kind not in PARAM_KINDS:
        return {
            "ok": False,
            "reason": f"unknown param kind: {kind}",
            "why": f"kind 只能是 {' / '.join(PARAM_KINDS)} 里的一个。",
        }
    options: list[str] = []
    if kind == "choice":
        raw_options = item.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            return {"ok": False, "reason": "choice has no options", "why": "choice 参数得有 options（非空列表）。"}
        for option in raw_options:
            text = _text(option, 80)
            if not text:
                return {"ok": False, "reason": "empty choice option", "why": "options 里有空字符串。"}
            options.append(text)
    default = item.get("default")
    if kind == "number" and default is not None and not isinstance(default, (int, float)):
        return {"ok": False, "reason": "number default is not a number", "why": "number 参数的 default 得是数字。"}
    if kind == "choice" and default is not None and _text(default) not in options:
        return {"ok": False, "reason": "default is not an option", "why": "default 不在 options 里。"}
    if kind in ("text", "choice") and default is not None and not isinstance(default, str):
        return {"ok": False, "reason": "default is not a string", "why": f"{kind} 参数的 default 得是字符串。"}
    if kind == "flag" and default is not None and not isinstance(default, bool):
        return {"ok": False, "reason": "flag default is not a bool", "why": "flag 参数的 default 得是 True 或 False。"}
    return {
        "ok": True,
        "param": {
            "name": name,
            "label": _text(item.get("label"), MAX_TITLE_CHARS) or name,
            "kind": kind,
            "required": bool(item.get("required")),
            "default": default,
            "options": options,
            "hint": _text(item.get("hint"), MAX_WHAT_CHARS),
        },
        "reason": "",
        "why": "",
    }


def read_plugin(path: Any, *, source: str = "") -> dict:
    """One row for one file. Never raises; a bad file is a row that says why."""
    name = os.path.basename(str(path))
    stem = os.path.splitext(name)[0]
    row: dict = {
        "id": stem,
        "title": stem,
        "what": "",
        "params": [],
        "file": str(path),
        "name": name,
        "source": source,
        "bytes": 0,
        "mtime": 0.0,
        "ok": False,
        "reason": "unreadable",
        "why": "",
    }
    try:
        stat = os.stat(path)
        row["bytes"] = int(stat.st_size)
        row["mtime"] = float(stat.st_mtime)
    except OSError as exc:
        row["why"] = f"读不到这个文件（{type(exc).__name__}）。"
        return row
    if row["bytes"] > MAX_PLUGIN_BYTES:
        row["reason"] = "too big"
        row["why"] = f"这个文件 {row['bytes']} 字节，超过 {MAX_PLUGIN_BYTES} 字节——清单不该藏在这么大的文件里。"
        return row
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source_text = handle.read(MAX_PLUGIN_BYTES)
    except (OSError, UnicodeDecodeError) as exc:
        row["reason"] = f"unreadable: {type(exc).__name__}"
        row["why"] = f"这个文件读不出文本（{type(exc).__name__}）——插件得是 UTF-8 的 Python 文件。"
        return row
    read = read_manifest(source_text)
    if not read["ok"]:
        row["reason"] = read["reason"]
        row["why"] = read["why"]
        return row
    plugin = read["plugin"]
    row.update(
        {
            "id": plugin["id"] or stem,
            "title": plugin["title"],
            "what": plugin["what"],
            "params": plugin["params"],
            "ok": True,
            "reason": "",
            "why": "",
        }
    )
    return row


def scan_plugins(*, roots: Any) -> dict:
    """Every plugin under every root, plus every file that failed to be one.

    ``roots`` is a list of ``{"path", "source"}``. The first root wins an id;
    a later file claiming the same id is listed as a broken row (with the
    winner's path in ``why``) rather than silently dropped.
    """
    rows: list[dict] = []
    seen_roots: list[dict] = []
    if isinstance(roots, (str, bytes)) or not isinstance(roots, (list, tuple)):
        roots = []
    for root in roots:
        if not isinstance(root, dict):
            continue
        path = _text(root.get("path"))
        if not path:
            continue
        source = _text(root.get("source")) or "plugin"
        exists = os.path.isdir(path)
        seen_roots.append({"path": path, "source": source, "exists": exists})
        if not exists:
            continue
        try:
            names = sorted(os.listdir(path))
        except OSError:
            continue
        for name in names:
            if len(rows) >= MAX_PLUGINS:
                break
            if name.startswith(_SKIP_PREFIXES) or not name.endswith(".py"):
                continue
            rows.append(read_plugin(os.path.join(path, name), source=source))
    by_id: dict[str, dict] = {}
    for row in rows:
        if not row.get("ok"):
            continue
        owner = by_id.get(row["id"])
        if owner is None:
            by_id[row["id"]] = row
            continue
        row["ok"] = False
        row["reason"] = "duplicate id"
        row["why"] = f"这个 id 已经被 {owner['file']} 占了——改一个 id，或者把其中一个改名。"
    order = {"": 0}
    rows.sort(key=lambda row: (not row.get("ok"), order.get(row.get("source", ""), 1), row["id"], row["file"]))
    return {
        "plugins": rows,
        "count": len(rows),
        "ok_count": sum(1 for row in rows if row.get("ok")),
        "broken": [row["id"] for row in rows if not row.get("ok")],
        "roots": seen_roots,
    }


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
    return None


def validate_values(plugin: Any, values: Any) -> dict:
    """Coerce what the page sent into what the plugin asked for.

    Returns ``{"ok", "args", "errors", "unknown"}``. An argument the manifest
    never asked for is dropped and named in ``unknown`` — the page and the
    manifest disagreeing is worth saying out loud, not worth a 400.
    """
    if not isinstance(plugin, dict):
        return {"ok": False, "args": {}, "errors": [{"param": "", "why": "没有这个插件。"}], "unknown": []}
    if values is None:
        values = {}
    if not isinstance(values, dict):
        return {
            "ok": False,
            "args": {},
            "errors": [{"param": "", "why": f"values 是 {type(values).__name__}，得是个字典。"}],
            "unknown": [],
        }
    params = plugin.get("params")
    params = params if isinstance(params, list) else []
    known = {str(spec.get("name")) for spec in params if isinstance(spec, dict)}
    args: dict[str, Any] = {}
    errors: list[dict] = []
    for spec in params:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name"))
        label = str(spec.get("label") or name)
        kind = str(spec.get("kind") or "text")
        raw = values.get(name, spec.get("default"))
        if kind == "flag":
            flag = _flag(raw if raw is not None else False)
            if flag is None:
                errors.append({"param": name, "why": f"「{label}」是开/关，收到的是 {raw!r}。"})
                continue
            args[name] = flag
            continue
        if kind == "number":
            if raw is None or raw == "":
                if spec.get("required"):
                    errors.append({"param": name, "why": f"「{label}」是必填的。"})
                else:
                    args[name] = spec.get("default")
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                errors.append({"param": name, "why": f"「{label}」要一个数字，收到的是 {raw!r}。"})
                continue
            args[name] = int(number) if number.is_integer() else number
            continue
        text = _text(raw)
        if len(text) > MAX_VALUE_CHARS:
            errors.append({"param": name, "why": f"「{label}」最长 {MAX_VALUE_CHARS} 个字符，这份有 {len(text)} 个。"})
            continue
        if kind == "choice":
            options = spec.get("options") if isinstance(spec.get("options"), list) else []
            if text not in options:
                errors.append({"param": name, "why": f"「{label}」只能选 {' / '.join(str(o) for o in options)}。"})
                continue
        if spec.get("required") and not text:
            errors.append({"param": name, "why": f"「{label}」是必填的。"})
            continue
        args[name] = text
    unknown = sorted(key for key in values if key not in known)
    return {"ok": not errors, "args": args, "errors": errors, "unknown": unknown}


def envelope(plugin_id: str, args: dict, *, workspace: str) -> dict:
    """What the child process finds on its stdin."""
    return {
        "plugin": plugin_id,
        "args": args,
        "workspace": workspace,
        "protocol": 1,
    }


def read_answer(stdout: Any) -> dict:
    """What the child said, whether it said it in JSON or just printed it.

    A script that prints a line is a valid plugin: ``{"ok": True, "json": False}``
    with the text. A JSON object can also say ``ok: false`` and give a reason.

    ``print("x")`` leaves a newline behind, and that newline is not part of what
    the script meant to say — trailing whitespace is trimmed off the answer.
    """
    text = stdout if isinstance(stdout, str) else ""
    stripped = text.strip()
    if not stripped:
        return {"ok": False, "json": False, "text": "", "reason": "empty answer", "why": "脚本什么都没输出。"}
    if stripped[0] in "{[":
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            answer_text = payload.get("text")
            answer_text = answer_text if isinstance(answer_text, str) else text.rstrip()
            reason = payload.get("reason")
            return {
                "ok": bool(payload.get("ok", True)),
                "json": True,
                "text": answer_text,
                "reason": reason if isinstance(reason, str) else "",
                "detail": payload,
            }
        if isinstance(payload, list):
            return {"ok": True, "json": True, "text": text.rstrip(), "reason": "", "detail": payload}
    return {"ok": True, "json": False, "text": text.rstrip(), "reason": "", "detail": None}


def _environment() -> dict:
    environment = {key: os.environ[key] for key in _ENV_ALLOWED if key in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _cut(raw: str) -> tuple[str, bool]:
    if len(raw) <= MAX_OUTPUT_BYTES:
        return raw, False
    return raw[:MAX_OUTPUT_BYTES], True


def run_plugin(
    row: Any,
    args: Any,
    *,
    workspace: str,
    timeout: float = DEFAULT_TIMEOUT,
    python: str = "",
) -> dict:
    """Run one plugin as a child process. Never raises.

    The child gets ``python <plugin file>``, the envelope on stdin, the
    workspace as its working directory, and no credentials in its environment.
    A timeout kills it and says so; a non-zero exit is reported, not raised.
    """
    result: dict = {
        "ok": False,
        "exit_code": None,
        "exit_code_observed": False,
        "stdout": "",
        "stderr": "",
        "text": "",
        "json": False,
        "truncated": False,
        "timed_out": False,
        "ms": 0,
        "reason": "",
        "why": "",
    }
    if not isinstance(row, dict) or not row.get("ok"):
        result["reason"] = "not a plugin"
        result["why"] = "这一条不是一个能跑的插件。"
        return result
    path = str(row.get("file") or "")
    if not os.path.isfile(path):
        result["reason"] = "no such file"
        result["why"] = f"文件不在了：{path}"
        return result
    # The child runs *in* the workspace, so the script's own path has to be
    # absolute before it gets there — otherwise a relative path resolves against
    # the working directory and the child cannot find itself.
    command = [python or sys.executable, os.path.abspath(path)]
    cwd = os.path.abspath(workspace) if workspace and os.path.isdir(workspace) else None
    payload = json.dumps(envelope(str(row.get("id") or ""), args if isinstance(args, dict) else {}, workspace=cwd or ""), ensure_ascii=False)
    started = time.time()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=_environment(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        result["ms"] = int((time.time() - started) * 1000)
        result["reason"] = f"spawn failed: {type(exc).__name__}"
        result["why"] = f"起不来这个进程（{type(exc).__name__}: {exc}）。"
        return result
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        try:
            stdout, stderr = process.communicate(timeout=TERMINATION_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):
            stdout, stderr = "", ""
    except (OSError, ValueError) as exc:
        result["ms"] = int((time.time() - started) * 1000)
        result["reason"] = f"pipe failed: {type(exc).__name__}"
        result["why"] = f"跟这个进程说不上话（{type(exc).__name__}: {exc}）。"
        return result
    result["ms"] = int((time.time() - started) * 1000)
    stdout, cut_out = _cut(stdout or "")
    stderr, cut_err = _cut(stderr or "")
    result.update(
        {
            "exit_code": process.returncode,
            "exit_code_observed": process.returncode is not None,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": cut_out or cut_err,
            "timed_out": timed_out,
        }
    )
    if timed_out:
        result["reason"] = "timeout"
        result["why"] = f"脚本跑了 {timeout:g} 秒还没停，已经掐掉了。"
        result["text"] = stdout
        return result
    answer = read_answer(stdout)
    result.update({"ok": bool(answer["ok"]), "json": bool(answer["json"]), "text": answer["text"]})
    if not answer["ok"]:
        if process.returncode and not stdout.strip():
            # 退出码不为 0 又什么都没印：多半是脚本自己就报错了。stderr 的头一行
            # 比「脚本什么都没输出」有用得多——那句话说不出是哪一行出的错。
            first = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
            result["reason"] = f"exit {process.returncode}"
            result["why"] = first[:300] or f"脚本没印东西就退出了（退出码 {process.returncode}）。"
        else:
            result["reason"] = answer.get("reason") or "the script said no"
            result["why"] = answer.get("why") or "脚本自己说这次没成。"
    elif process.returncode:
        result["ok"] = False
        result["reason"] = f"exit {process.returncode}"
        result["why"] = f"脚本印了话，但退出码是 {process.returncode}——按「没成」算。"
    elif answer.get("reason"):
        result["reason"] = answer["reason"]
    return result
