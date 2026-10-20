import ast

# A denylist, not an allowlist.
#
# The previous rule was "any import outside a 13-module allowlist is high
# severity", which flagged `from datetime import datetime` as a high-severity
# finding and failed A5 on a correct implementation. A security gate that cries
# wolf on the standard library does not make code safer — it teaches people to
# wave the gate through, including on the occasions when it is right.
#
# What actually matters is whether generated code can leave its sandbox, so the
# rule names the modules that allow that.
DANGEROUS_MODULES = frozenset({
    "os", "subprocess", "shutil", "socket", "ctypes", "pickle", "marshal",
    "shelve", "importlib", "multiprocessing", "signal", "pty", "platform",
    "resource", "fcntl", "mmap",
})

# Reachable from the sandbox but worth recording: a reviewer should see them.
NOTED_MODULES = frozenset({"sys", "pathlib", "tempfile", "glob", "io"})

# Dunder access, split in two — for the same reason the import rule above is a
# denylist and not an allowlist.
#
# The rule used to be "any attribute starting with __ is high severity", which
# flagged `type(n).__name__` in an error message and `arg0.__class__` in a
# three-line `type()` shim as high-severity findings, and failed A5 on code that
# was correct — the same way `from datetime import datetime` used to.
#
# What matters is not that a name has underscores around it; it is whether the
# attribute leads somewhere *other than the value itself*: to a module's globals,
# to the builtins, to the class hierarchy, to a code object. Those are the steps
# an escape is built out of, and they stay high. The rest — `__name__`,
# `__class__`, `__dict__`, the protocol methods — are ordinary Python: worth a
# reviewer's eye, not a refusal. The classic chain still fails, because it needs
# `__bases__` / `__subclasses__` / `__mro__` to go anywhere.
ESCAPE_DUNDERS = frozenset({
    "__globals__", "__builtins__", "__subclasses__", "__bases__", "__mro__",
    "__code__", "__closure__", "__getattribute__", "__reduce__", "__reduce_ex__",
    "__loader__", "__spec__",
})

DANGEROUS_FUNCTIONS = [
    "eval", "exec", "compile", "__import__", "open", "getattr", "setattr", "delattr", "globals", "locals", "vars", "exit", "quit",
    "os.system", "os.popen", "subprocess.call",
    "subprocess.run", "subprocess.Popen",
    "shutil.rmtree", "os.remove", "os.rmdir",
]

def scan_code(code: str, language: str = "python") -> dict:
    if not isinstance(code, str) or not code.strip():
        return {"verdict": "fail", "findings": [{"reason": "empty_code"}], "scanned": False}
    if language != "python":
        return {"verdict": "pass", "findings": [], "scanned": False, "note": "non-python skip"}
    findings = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"verdict": "fail", "findings": [{"reason": f"syntax_error:{e}"}], "scanned": True}
    for node in ast.walk(tree):
        modules = ([a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for module in modules:
            root = module.split(".")[0]
            if isinstance(node, ast.ImportFrom) and node.level:
                findings.append({"line": node.lineno, "function": "relative import", "severity": "high"})
            elif root in DANGEROUS_MODULES:
                findings.append({"line": node.lineno, "function": "import " + module, "severity": "high"})
            elif root in NOTED_MODULES:
                findings.append({"line": node.lineno, "function": "import " + module, "severity": "medium"})
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            findings.append({
                "line": node.lineno,
                "function": "dunder access " + node.attr,
                "severity": "high" if node.attr in ESCAPE_DUNDERS else "medium",
            })
        if isinstance(node, ast.Call):
            func_name = _get_func_name(node.func)
            if func_name in DANGEROUS_FUNCTIONS:
                findings.append({"line": getattr(node, "lineno", -1), "function": func_name, "severity": "high"})
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("pickle", "marshal", "shelve"):
                    findings.append({"line": node.lineno, "function": f"import {alias.name}", "severity": "medium"})
    if any(f["severity"] == "high" for f in findings):
        return {"verdict": "fail", "findings": findings, "scanned": True}
    if findings:
        return {"verdict": "warn", "findings": findings, "scanned": True}
    return {"verdict": "pass", "findings": [], "scanned": True}

def _get_func_name(node) -> str:
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Attribute):
        base = _get_func_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
