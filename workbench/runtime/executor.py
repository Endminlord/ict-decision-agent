"""Temporary-workspace executor using only the Python standard library.

This is working-directory isolation, NOT an OS security sandbox. Child code
runs with the host user's permissions and can access host files and networks.
Run untrusted code only inside a separately provisioned container/VM.

CLI commands use shell=False: pipes, redirections and shell builtins are not
interpreted. Pass an argv list to avoid quoting ambiguity, especially on Windows.
A CLI payload may contain {"command": argv, "files": {relative_path: source}}.
No generated files are exported; artifacts is therefore always an empty list.

For e2e function mode, entrypoint is a function name, module.py:function, or a
.py script. Optional verification args/kwargs are passed to the named function.
This is an execution smoke check, not proof of test coverage or correctness.
For CLI mode, entrypoint specifies the command; Python source, if supplied, is
written to the command's .py operand. Optional args are appended as argv items.

exit_code=-1 is a sentinel when no process exit code was observed. UI and
preflight failures are explicitly not_run, success=False, stub=True.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any

from runtime import sandbox
from runtime.audit import record as audit_record
from runtime.execution import execution_allowed, run_trusted
from runtime.security_scanner import scan_code


EXECUTION_TIMEOUT = 30.0
TERMINATION_TIMEOUT = 5.0
MAX_OUTPUT_BYTES = 1024 * 1024

_EXECUTION_DISABLED = (
    "execution_disabled: set EXECUTION_MODE=trusted only in a trusted/disposable "
    "environment; subprocess is not a sandbox"
)


def _not_run(mode: str, reason: str) -> dict:
    return {
        "success": False,
        "exit_code": -1,
        "exit_code_observed": False,
        "stdout": "",
        "stderr": "",
        "artifacts": [],
        "mode": mode,
        "status": "not_run",
        "executed": False,
        "stub": True,
        "timed_out": False,
        "reason": reason,
    }


def _argv(command: Any) -> list[str]:
    if isinstance(command, str):
        # String commands use POSIX-style quoting on every platform.
        arguments = shlex.split(command, posix=True)
    elif isinstance(command, (list, tuple)):
        arguments = list(command)
    else:
        raise TypeError("command must be a string or an argv list")
    if not arguments or any(
        not isinstance(part, str) or "\x00" in part for part in arguments
    ) or not arguments[0]:
        raise ValueError("command requires nonempty executable and string arguments")
    return arguments


def _safe_relative_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError("file path must be a nonempty string")
    normalized = path.replace("\\", "/")
    if (
        normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in ("", ".", "..") for part in normalized.split("/"))
    ):
        raise ValueError("file paths must stay inside the temporary directory")
    return normalized


def _environment(directory: str) -> dict:
    # Do not automatically pass API credentials or PYTHONPATH to child code.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TZ")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment.update({
        "HOME": directory,
        "USERPROFILE": directory,
        "TMPDIR": directory,
        "TMP": directory,
        "TEMP": directory,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    return environment


def _terminate(process: subprocess.Popen) -> None:
    """Best-effort process-tree cleanup; all waits are bounded."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif os.name == "nt" and process.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TERMINATION_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=TERMINATION_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _read_output(stream: Any) -> tuple[str, bool]:
    stream.seek(0)
    data = stream.read(MAX_OUTPUT_BYTES + 1)
    truncated = len(data) > MAX_OUTPUT_BYTES
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), truncated


def execute_code(code_str: Any, mode: str) -> dict:
    """Execute a Python script or CLI command with a hard per-process timeout.

    Execution is denied unless ``EXECUTION_MODE=trusted``; this module is a
    working-directory isolation layer, never a security boundary.
    """
    if mode == "ui":
        return _not_run(mode, "ui_execution_not_implemented")
    if mode not in ("function", "cli"):
        return _not_run(mode, "unsupported_execution_mode")
    if not execution_allowed():
        audit_record('code_execution_refused', mode=mode, reason='execution_disabled')
        return _not_run(mode, _EXECUTION_DISABLED)
    process = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="personal-agent-", ignore_cleanup_errors=True
        ) as directory:
            if mode == "function":
                if not isinstance(code_str, str) or not code_str.strip():
                    return _not_run(mode, "Python source must be a nonempty string")
                source_path = os.path.join(directory, "program.py")
                with open(source_path, "w", encoding="utf-8") as stream:
                    stream.write(code_str)
                command = [sys.executable, "-I", "-B", "-u", "-X", "utf8", source_path]
            else:
                payload = code_str if isinstance(code_str, dict) else {"command": code_str}
                command = _argv(payload.get("command"))
                files = payload.get("files", {})
                if not isinstance(files, dict):
                    raise TypeError("CLI files must be a mapping of relative paths to source")
                for relative, source in files.items():
                    relative = _safe_relative_path(relative)
                    if not isinstance(source, str):
                        raise TypeError("CLI file contents must be strings")
                    path = os.path.join(directory, *relative.split("/"))
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as stream:
                        stream.write(source)

            # File-backed output avoids pipe deadlocks and unbounded RAM use.
            # It does not impose a disk quota; use OS isolation for that.
            with tempfile.TemporaryFile(dir=directory) as stdout_file, \
                    tempfile.TemporaryFile(dir=directory) as stderr_file:
                options = {"start_new_session": True} if os.name == "posix" else {}
                if os.name == "nt":
                    options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                # This path previously applied no resource limits at all: a
                # generated program could allocate without bound.
                argv, applied = sandbox.wrap_command(command, workdir=directory)
                preexec = sandbox.preexec_for()
                # Report what was actually passed, not what is merely available.
                applied['limits_applied'] = preexec is not None
                process = subprocess.Popen(
                    argv,
                    cwd=directory,
                    env=_environment(directory),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    preexec_fn=preexec,
                    **options,
                )
                timed_out = False
                try:
                    process.wait(timeout=EXECUTION_TIMEOUT)
                except subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    _terminate(process)
                stdout, stdout_truncated = _read_output(stdout_file)
                stderr, stderr_truncated = _read_output(stderr_file)
                observed = process.returncode is not None
                exit_code = process.returncode if observed else -1
                success = observed and exit_code == 0 and not timed_out
                audit_record(
                    'code_executed',
                    mode=mode,
                    exit_code=exit_code,
                    exit_code_observed=observed,
                    timed_out=timed_out,
                    success=success,
                )
                return {
                    "success": success,
                    "exit_code": exit_code,
                    "exit_code_observed": observed,
                    "stdout": stdout,
                    "stderr": stderr,
                    "artifacts": [],
                    "mode": mode,
                    "status": "timed_out" if timed_out else "passed" if success else "failed",
                    "executed": True,
                    "stub": False,
                    "timed_out": timed_out,
                    **applied,
                    "stdout_truncated": stdout_truncated,
                    "stderr_truncated": stderr_truncated,
                    "reason": "execution_timeout" if timed_out else "",
                }
    except Exception as exc:
        if process is not None:
            _terminate(process)
            result = _not_run(mode, f"executor_error:{type(exc).__name__}: {exc}")
            result.update({
                "status": "failed",
                "executed": True,
                "stub": False,
                "exit_code": process.returncode if process.returncode is not None else -1,
                "exit_code_observed": process.returncode is not None,
            })
            return result
        return _not_run(mode, f"launch_error:{type(exc).__name__}: {exc}")


def execute_e2e_test(spec_verification: dict, green_code: Any) -> dict:
    """Build an execution request; verdict always comes from the real process."""
    if not isinstance(spec_verification, dict):
        return _not_run("unknown", "spec_verification must be a dict")
    mode = spec_verification.get("mode")
    entrypoint = spec_verification.get("entrypoint")
    if mode == "ui":
        result = execute_code("", "ui")
        result["entrypoint"] = entrypoint
        return result
    if mode not in ("function", "cli"):
        return _not_run(str(mode), "unsupported_execution_mode")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return _not_run(mode, "entrypoint must be a nonempty string")
    if not isinstance(green_code, (str, dict)):
        return _not_run(mode, "green_code must be Python source or a code mapping")
    source = green_code.get("code", "") if isinstance(green_code, dict) else green_code
    if not isinstance(source, str):
        return _not_run(mode, "green_code.code must be a string")
    if isinstance(green_code, dict) and green_code.get("code_stub"):
        return _not_run(mode, "green_code_is_stub")

    try:
        if mode == "function":
            if isinstance(green_code, dict) and green_code.get("language", "python") != "python":
                return _not_run(mode, "function mode supports Python only")
            if not source.strip():
                return _not_run(mode, "missing_python_source")
            if entrypoint.endswith(".py") and ":" not in entrypoint:
                _safe_relative_path(entrypoint)
                request = source
            else:
                parts = entrypoint.split(":")
                if len(parts) > 2:
                    raise ValueError("invalid function entrypoint")
                function_name = parts[-1]
                if len(parts) == 2:
                    _safe_relative_path(parts[0])
                if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", function_name):
                    raise ValueError("invalid function name")
                args = spec_verification.get("args", [])
                kwargs = spec_verification.get("kwargs", {})
                if not isinstance(args, list) or not isinstance(kwargs, dict):
                    raise TypeError("function args/kwargs must be a list/dict")
                if any(not isinstance(key, str) for key in kwargs):
                    raise TypeError("function keyword names must be strings")
                arguments = json.dumps({"args": args, "kwargs": kwargs}, allow_nan=False)
                # Some arguments cannot survive JSON: a decorator needs a
                # callable, and `func=lambda x: x * 2` has no serialised form.
                # These arrive as source text and are evaluated in the child,
                # which already has the generated code's namespace.
                arg_exprs = spec_verification.get("arg_exprs") or []
                if not isinstance(arg_exprs, list) or any(
                    not isinstance(item, str) for item in arg_exprs
                ):
                    raise TypeError("arg_exprs must be a list of strings")
                exprs = json.dumps(arg_exprs, ensure_ascii=False)
                call = (
                    "_value = _target(*_exprs_args, **_arguments['kwargs'])\n"
                    if arg_exprs
                    else "_value = _target(*_arguments['args'], **_arguments['kwargs'])\n"
                )
                request = (
                    "import json\n"
                    "_scope = {'__name__': '__executor_target__', '__file__': __file__}\n"
                    f"exec(compile({source!r}, '<green_code>', 'exec'), _scope)\n"
                    f"_parts = {function_name!r}.split('.')\n"
                    "_target = _scope[_parts[0]]\n"
                    "for _part in _parts[1:]:\n"
                    "    _target = getattr(_target, _part)\n"
                    "if not callable(_target):\n"
                    "    raise TypeError('entrypoint is not callable')\n"
                    f"_arguments = json.loads({arguments!r})\n"
                    f"_exprs = json.loads({exprs!r})\n"
                    "_exprs_args = [eval(_e, _scope) for _e in _exprs]\n"
                    + call
                    + "if _value is not None:\n"
                    "    print(repr(_value))\n"
                )
            result = execute_code(request, "function")
        else:
            command = _argv(entrypoint)
            extra_args = spec_verification.get("args", [])
            if not isinstance(extra_args, list) or any(not isinstance(arg, str) for arg in extra_args):
                raise TypeError("CLI args must be a list of strings")
            files = {}
            if source.strip():
                python_files = [part for part in command if part.endswith(".py")]
                if len(python_files) != 1:
                    raise ValueError("Python source requires exactly one .py operand in entrypoint")
                filename = _safe_relative_path(python_files[0])
                files[filename] = source
                if command[0] == python_files[0]:
                    command = [sys.executable, "-I", "-B", "-u", "-X", "utf8"] + command
            result = execute_code({"command": command + extra_args, "files": files}, "cli")
        result["entrypoint"] = entrypoint
        result["verification_level"] = "execution_smoke" if result["success"] else "not_verified"
        return result
    except Exception as exc:
        return _not_run(mode, f"invalid_execution_request:{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Literal test-case parsing. Expressions are never evaluated.
# --------------------------------------------------------------------------

_CALL_RE = re.compile(
    r"^(?P<name>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\((?P<params>[^()]*)\)$"
)
_PARAM_RE = re.compile(r"[A-Za-z_]\w*")


def _split_top_level(text: str) -> list[str]:
    """Split on commas that are not inside brackets, quotes or escapes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if quote:
            current.append(char)
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char in "([{":
            depth += 1
            current.append(char)
            continue
        if char in ")]}":
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _literal(text: str):
    """Parse a Python literal. Any expression raises ValueError.

    ``ast.literal_eval`` accepts only literals, so ``__import__(...)``,
    ``(1).__class__``, ``1+2`` and ``*[1,2]`` are all rejected.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("expected a Python literal, got empty text")
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise ValueError(f"not a Python literal: {text.strip()!r}") from exc


def _parse_named_given(given: Any) -> list[tuple[str | None, Any]]:
    """Return ``(name, value)`` pairs for a ``given`` string."""
    if given is None or given == "":
        return []
    if not isinstance(given, str):
        raise ValueError("given must be a string")
    pairs: list[tuple[str | None, Any]] = []
    for part in _split_top_level(given):
        name, separator, value = part.partition("=")
        if separator and _PARAM_RE.fullmatch(name.strip()):
            pairs.append((name.strip(), _literal(value)))
        else:
            pairs.append((None, _literal(part)))
    return pairs


def _parse_given(given_str: Any) -> list:
    """Values of a ``given`` string, in order. Expressions are never evaluated."""
    return [value for _, value in _parse_named_given(given_str)]


def _bind_arguments(params: list[str], bindings: list[tuple[str | None, Any]]) -> list:
    """Bind parsed values onto ``params``, by name when names are supplied."""
    named = [name for name, _ in bindings if name is not None]
    if named and len(named) != len(bindings):
        raise ValueError("mixing positional and named arguments is not supported")
    if named:
        values: dict[str, Any] = {}
        for name, value in bindings:
            if name not in params:
                raise ValueError(
                    f"unknown parameter {name!r} for ({', '.join(params)})"
                )
            values[name] = value
        missing = [name for name in params if name not in values]
        if missing:
            raise ValueError(f"missing arguments: {', '.join(missing)}")
        return [values[name] for name in params]
    if len(bindings) > len(params):
        raise ValueError("too many positional arguments")
    return [value for _, value in bindings]


def parse_case(case: Any) -> dict:
    """Validate and normalize one test case. Raises ValueError when malformed.

    Two ``when`` spellings are accepted:

    ``add(a,b)`` with ``given="a=1,b=2"``
        The parentheses name the parameters and ``given`` supplies the values,
        by name (so ``given="b=2,a=5"`` binds correctly) or positionally.
    ``add(1,2)`` with ``given="1,2"``
        The parentheses already carry literal arguments; ``given`` is redundant.
    """
    if not isinstance(case, dict):
        raise ValueError("test case must be a dict")
    when = case.get("when")
    if not isinstance(when, str) or not when.strip():
        raise ValueError("test case requires a nonempty 'when'")
    match = _CALL_RE.match(when.strip())
    if not match:
        raise ValueError(f"cannot parse call expression: {when!r}")
    inner = match.group("params").strip()
    items = _split_top_level(inner) if inner else []

    if items and not all(_PARAM_RE.fullmatch(item) for item in items):
        # The call already carries literal arguments.
        params: list[str] = []
        args = [_literal(item) for item in items]
    else:
        params = items
        args = _bind_arguments(params, _parse_named_given(case.get("given", "")))

    then = case.get("then")
    if not isinstance(then, str) or not then.strip().startswith("=="):
        raise ValueError(f"expected value must be written as '==<literal>': {then!r}")
    return {
        "id": case.get("id"),
        "function": match.group("name"),
        "params": params,
        "args": args,
        "expected": _literal(then.strip()[2:]),
    }


def assertion_source(case: Any) -> str:
    """Compile one test case into a real pytest assertion line.

    The case id is deliberately not interpolated: only its index is used by the
    caller, so an id cannot inject Python into the generated suite.
    """
    parsed = parse_case(case)
    arguments = ", ".join(repr(value) for value in parsed["args"])
    return (
        f"assert green_code.{parsed['function']}({arguments}) "
        f"== {parsed['expected']!r}"
    )


def _case_result(results: list[dict], logs: list[str]) -> dict:
    passed = sum(1 for item in results if item.get("passed"))
    failed = sum(1 for item in results if not item.get("passed") and not item.get("error"))
    errors = sum(1 for item in results if item.get("error"))
    success = bool(results) and failed == 0 and errors == 0
    return {
        "success": success,
        "exit_code": 0 if success else 1,
        "logs": "\n".join(logs),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "total": len(results),
        "results": results,
    }


_PYTHON_HARNESS = '''\
import ast
import json
import sys

sys.path.insert(0, sys.argv[1])
request = json.loads(sys.argv[2])

try:
    import green_code
except BaseException as exc:
    print(json.dumps({"import_error": "%s: %s" % (type(exc).__name__, exc)}))
    raise SystemExit(0)

results = []
for item in request["cases"]:
    entry = {"id": item.get("id")}
    try:
        target = green_code
        for part in str(item["function"]).split("."):
            target = getattr(target, part)
        if not callable(target):
            raise TypeError("entrypoint is not callable")
        value = target(*ast.literal_eval(item["args_repr"]))
        entry["actual_repr"] = repr(value)
    except BaseException as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
    results.append(entry)

print(json.dumps(results, ensure_ascii=False))
'''


def execute_python(code: Any, test_cases: Any) -> dict:
    """Run a Python implementation against literal test cases.

    Order matters: the static scan runs before the authorization gate, so
    dangerous code is rejected identically in every execution mode, and the gate
    runs before any subprocess is created.
    """
    if not isinstance(code, str) or not code.strip():
        return _case_result([], ["empty_code"])
    if not isinstance(test_cases, dict):
        return _case_result([], ["test_cases_must_be_a_dict"])
    cases = test_cases.get("cases")
    if not isinstance(cases, list) or not cases:
        return _case_result([], ["no_cases"])

    scan = scan_code(code, str(test_cases.get("language") or "python"))
    if scan.get("verdict") == "fail":
        findings = json.dumps(scan.get("findings", []), ensure_ascii=False)
        return _case_result([], [f"security_scan_failed: {findings}"])
    if not execution_allowed():
        return _case_result([], [_EXECUTION_DISABLED])

    results: list[dict] = []
    logs: list[str] = []
    resolved: list[dict] = []
    for index, case in enumerate(cases):
        try:
            resolved.append(parse_case(case))
        except ValueError as exc:
            results.append(
                {
                    "id": case.get("id") if isinstance(case, dict) else None,
                    "passed": False,
                    "error": f"invalid_case: {exc}",
                }
            )
            logs.append(f"ERROR case[{index}]: {exc}")
    if results:
        return _case_result(results, logs)

    payload = json.dumps(
        {
            "cases": [
                {
                    "id": item["id"],
                    "function": item["function"],
                    "args_repr": repr(item["args"]),
                }
                for item in resolved
            ]
        },
        ensure_ascii=False,
    )

    with tempfile.TemporaryDirectory(
        prefix="python-cases-", ignore_cleanup_errors=True
    ) as directory:
        with open(os.path.join(directory, "green_code.py"), "w", encoding="utf-8") as stream:
            stream.write(code)
        harness = os.path.join(directory, "run_cases.py")
        with open(harness, "w", encoding="utf-8") as stream:
            stream.write(_PYTHON_HARNESS)
        completed = run_trusted(
            [sys.executable, "-B", "-u", harness, directory, payload],
            directory,
            timeout=EXECUTION_TIMEOUT,
        )

    output = completed.get("output", "")
    if completed.get("timed_out"):
        return _case_result(
            [{"id": item["id"], "passed": False, "error": "timeout"} for item in resolved],
            [f"TIMEOUT after {EXECUTION_TIMEOUT}s"] + [output],
        )
    try:
        observed = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _case_result(
            [{"id": item["id"], "passed": False, "error": "harness_failure"} for item in resolved],
            ["harness_failure: no parseable result"] + [output],
        )
    if isinstance(observed, dict) and "import_error" in observed:
        return _case_result(
            [{"id": item["id"], "passed": False, "error": observed["import_error"]} for item in resolved],
            [f"import_error: {observed['import_error']}"] + [output],
        )
    if not isinstance(observed, list) or len(observed) != len(resolved):
        return _case_result(
            [{"id": item["id"], "passed": False, "error": "harness_result_mismatch"} for item in resolved],
            ["harness_result_mismatch"] + [output],
        )

    for item, observation in zip(resolved, observed):
        entry = {"id": item["id"]}
        if observation.get("error"):
            entry["passed"] = False
            entry["error"] = observation["error"]
            logs.append(f"ERROR {item['id']}: {observation['error']}")
        else:
            try:
                actual = ast.literal_eval(observation["actual_repr"])
            except (ValueError, SyntaxError):
                actual = observation.get("actual_repr")
            entry["actual"] = actual
            entry["passed"] = bool(actual == item["expected"])
            logs.append(
                f"{'PASS' if entry['passed'] else 'FAIL'} {item['id']}: "
                f"{item['function']} -> {actual!r} (expected {item['expected']!r})"
            )
        results.append(entry)
    return _case_result(results, logs)


class _HighZ:
    """用例里的 `z`：这根线**没有人驱动**。

    Verilog 里 `z` 不是值，是「没有驱动器」。它不能像 `abc` 那样当「读不出来」丢掉
    ——三态总线的验收条件写的就是「oe == 1'b0 时 bus 输出为高阻态 8'bz」，丢掉它
    等于把这条要求从用例里删掉，然后再因为「用例少了」停下来等人；也不能当整数比
    ——`=== {8{1'bz}}` 才是它真正的意思，按 `8'd0` 写出来会变成「总线该是 0」，
    于是正确的设计反而被判失败。真值恒为 True，好让 `if value` 之类的写法不会把
    它当成 0。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "z"

    def __bool__(self) -> bool:
        return True


class _Literal:
    """用例直接用 Verilog 位型写的值（混着 z/x，如 `4'b000z`）。

    这种值没有十进制对应物，只能原样发出去让 `===` 比。宽度由写的人负责，
    所以也不参与 `_unfit_values` 的宽度检查。
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text

    def __bool__(self) -> bool:
        return True


HIGH_Z = _HighZ()
# 全是 z 的字面量（`8'bz`）在 Verilog 里按 z 扩展，所以它就是整个宽度的高阻。
_ALL_Z = re.compile(r"^(?:(\d+)'([bodhBODH]))?(z+)$", re.IGNORECASE)
# 混着 z/x 的位型：没有十进制对应物，原样发出。
_MIXED_BITS = re.compile(r"^(\d+)'([bodhBODH])([0-9a-fA-F_xXzZ]*[xzXZ][0-9a-fA-F_xXzZ]*)$")


def _high_z_value(text: str):
    """`z` / `8'bz` → HIGH_Z；`4'b000z` 这类位型 → `_Literal`；其余 None。"""
    if _ALL_Z.match(text) is not None:
        return HIGH_Z
    if _MIXED_BITS.match(text) is not None:
        return _Literal(text)
    return None


def _verilog_literal(width: int, value) -> str:
    """A literal iverilog accepts at this width.

    ``8'd-1`` is a syntax error, so a negative value is written as its two's
    complement bit pattern: for an unsigned port that is the same bit vector, and
    a signed design is driven with the same bits either way. Without this, any
    case that used a negative number produced a testbench that would not compile,
    and the run blamed the design for a bench that never ran.

    高阻写不出来是同一类毛病：`z` 到了这儿要是当 0，三态总线的用例就变成了
    「总线该是 0」，好设计会被判失败。`{8{1'bz}}` 才是「没人驱动」。
    """
    if isinstance(value, _HighZ):
        return f"{{{width}{{1'bz}}}}"
    if isinstance(value, _Literal):
        return value.text
    return f"{width}'d{value & ((1 << width) - 1)}"


def _safe_label(text: Any, fallback: str) -> str:
    """A case id that can be pasted into Verilog without ending the statement.

    Ids come from the planner. An id like `T1 "x"` used to be interpolated straight
    into a `$display` string and a `//` comment, so a quote made the bench fail to
    compile and the run blamed the design — the failure mode this whole file keeps
    having to fix.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(text))[:24].strip("_")
    return cleaned or fallback


def _unfit_values(assignments: dict, ports: list) -> list:
    """Values that cannot be expressed in their port's width.

    Verilog truncates silently, so ``gt=5`` on a 1-bit output would quietly
    compare against 1 and pass. Naming them is the difference between a case that
    was checked and a case that was dropped.
    """
    widths = {str(p["name"]): int(p["width"]) for p in ports}
    unfit = []
    for name, value in assignments.items():
        if isinstance(value, (_HighZ, _Literal)):
            # 高阻和位型没有十进制大小，宽度要么由端口定、要么由写的人定。
            continue
        width = widths.get(name)
        if width is None:
            continue
        if value > (1 << width) - 1 or value < -(1 << (width - 1)):
            unfit.append(f"{name}={value} does not fit in {width} bit(s)")
    return unfit


_VERILOG_NUMBER = re.compile(r"^(?:(\d+)'([bodhBODH]))?([0-9a-fA-F_]+)$")


def _parse_value(raw: str):
    """One written value, or None when it is not a number this bench can compare.

    `int(raw, 0)` was the obvious choice and it was wrong: it rejects `01`, because
    a leading zero means octal to base 0. The planner writes the traffic-light
    colours the way its own acceptance criteria spell them — `light=01` for green —
    so half the cases parsed to nothing, were dropped as "states no expected
    output", and the run reported two passed of four with no sign that two cases
    had vanished. Decimal with leading zeros, 0x/0b/0o, and Verilog's `2'b01` /
    `8'hFF` all mean what they look like here.

    `z` 是第三种东西：它不是数字，也不该被当成读不出来。三态总线的验收条件里
    写着 `8'bz`，用例照着写 `bus=z`，而这里返回 None 的时候，那条用例就被
    「case states a value the bench cannot read」丢掉，A3 再因为「计划了六条、
    只跑过三条」停下来——**要什么就丢什么**。见 `_high_z_value`。
    """
    text = raw.strip()
    negative = text.startswith("-")
    if negative or text.startswith("+"):
        text = text[1:].strip()
    if not text:
        return None
    high_z = _high_z_value(text.replace("_", ""))
    if high_z is not None:
        return high_z
    try:
        value = int(text, 0)
    except ValueError:
        match = _VERILOG_NUMBER.match(text)
        if match is None:
            return None
        size, radix, digits = match.groups()
        base = {"b": 2, "o": 8, "d": 10, "h": 16}.get((radix or "d").lower())
        try:
            value = int(digits.replace("_", ""), base)
        except ValueError:
            return None
        # A sized literal is truncated to its width by the language; `2'b01` is 1
        # either way, but `2'hFF` is 3, and comparing against 255 would be a case
        # about a different number.
        if size and value > (1 << int(size)) - 1:
            value = value & ((1 << int(size)) - 1)
    return -value if negative else value


def _assignment_text(value: Any) -> str | None:
    """A case field written as `名=值` text, or as pairs the model chose instead.

    The planner asks for a string and passes whatever the model returns straight
    through. A small model sometimes answers with `{"a": 3}` or `["a=3"]`; reading
    those is not a guess — the name and the value are both spelled out — while
    dropping them would have said "the case states no expected output", which is
    a true sentence about the wrong problem.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        for name, raw in value.items():
            if isinstance(raw, (dict, list, tuple)) or isinstance(raw, bool):
                return None
            parts.append(f"{name}={raw}")
        return ", ".join(parts)
    if isinstance(value, (list, tuple)):
        if not value or not all(isinstance(item, (str, int)) for item in value):
            return None
        return ", ".join(str(item) for item in value)
    return None


def _parse_sequence(case: Any) -> tuple[dict, str]:
    """Read a case's optional per-edge input sequence, or say why it cannot be read.

    A case's `given` holds one value per port for the whole case, so a requirement
    whose input changes every cycle — "detected 在 din 上连续出现 1011 时输出 1" —
    could not be expressed at all. `sequence` maps a port to one value per rising
    edge: `{"din": [1, 0, 1, 1]}`.
    """
    if not isinstance(case, dict):
        return {}, ""
    raw = case.get("sequence")
    if raw is None:
        return {}, ""
    if not isinstance(raw, dict):
        return {}, f"sequence is {type(raw).__name__}, not 端口名 -> 值列表"
    sequence = {}
    for name, values in raw.items():
        if not isinstance(name, str) or not name.strip():
            return {}, f"sequence has a port name that is not text: {name!r}"
        if isinstance(values, (str, int)) or not isinstance(values, (list, tuple)):
            return {}, f"sequence[{name}] is {type(values).__name__}, not a list of values"
        if not values:
            return {}, f"sequence[{name}] is empty"
        parsed = []
        for value in values:
            number = _parse_value(value) if isinstance(value, str) else (
                value if isinstance(value, int) and not isinstance(value, bool) else None
            )
            if number is None:
                return {}, f"sequence[{name}] contains {value!r}, which the bench cannot read"
            parsed.append(number)
        sequence[name.strip()] = parsed
    return sequence, ""


def _parse_assignments(text: Any) -> dict[str, int]:
    """`"a=3, b=4"` -> `{"a": 3, "b": 4}`. Unreadable values are left out."""
    out: dict[str, int] = {}
    written = _assignment_text(text)
    if written is None:
        return out
    for name, value in _assignment_chunks(written):
        if value is not None:
            out[name] = value
    return out


def _case_field_problem(case: dict) -> str:
    """Why a case's fields cannot be read, or "" when they can."""
    for field in ("given", "then"):
        value = case.get(field)
        if value is None:
            continue
        if _assignment_text(value) is None:
            return f"{field} is {type(value).__name__}, not 名称=值 文本"
    return ""


def _assignment_chunks(text: Any) -> list[tuple[str, int | None]]:
    """Every `name=value` pair, with None for a value that could not be read.

    Keeping the unreadable ones is the point: they used to disappear into a case
    that then looked like it simply had no expectations.
    """
    chunks = []
    if not isinstance(text, str):
        return chunks
    for chunk in text.replace(";", ",").split(","):
        name, _, raw = chunk.partition("=")
        name, raw = name.strip(), raw.strip()
        if not name or not raw:
            continue
        chunks.append((name, _parse_value(raw)))
    return chunks


def _unreadable_values(text: Any) -> list[str]:
    """The written pairs whose value is not a number — named, not dropped."""
    written = _assignment_text(text)
    if written is None:
        return []
    unreadable = []
    for chunk in written.replace(";", ",").split(","):
        name, _, raw = chunk.partition("=")
        name, raw = name.strip(), raw.strip()
        if name and raw and _parse_value(raw) is None:
            unreadable.append(f"{name}={raw}")
    return unreadable


_CLOCK_PORT = re.compile(
    # `clk`, `clock`, and the conventional second-clock spellings (`clk2`,
    # `clk_2`, `clk_a`, `clk_b`). `clk_enable` / `clk_div` are deliberately not
    # clocks: those are signals about the clock, not the clock.
    r"^(?:i_)?(?:clk|clock)(?:_?(?:[0-9]|[abc]))?(?:_i|_in)?$", re.IGNORECASE
)
_RESET_PORT = re.compile(
    r"^(?:i_)?(?:n?rst|n?reset|arst|areset)(?:_i|_in|_n|_ni|n)?$", re.IGNORECASE
)
_ACTIVE_LOW_RESET = re.compile(r"^(?:i_)?n|(?:_n|_ni|n)$", re.IGNORECASE)


def clock_ports(inputs: list) -> list:
    """Every clock-named input port, in declaration order.

    A design can have more than one (a divided clock, two domains). The bench has
    to drive all of them: a clock it mistakes for data is an input no case can
    drive, and the run then reports "no usable case" — a refusal that names the
    wrong cause.
    """
    return [p for p in inputs if _CLOCK_PORT.match(str(p.get("name")))]


def clock_and_reset(inputs: list) -> tuple[dict | None, dict | None]:
    """The primary clock and the reset port, recognised by their names.

    A clocked design cannot be checked by the combinational bench: that bench
    drives the inputs, waits 1ns and reads the outputs, so a counter never sees
    an edge and its state stays X. Nothing else in the requirement says which
    port is the clock, and `clk`/`clock`/`rst`/`reset` are the conventions every
    RTL interface follows, so the name is the evidence.

    The first clock is the one `cycles` counts: with two clocks, "one cycle" has
    to mean one of something specific.
    """
    clocks = clock_ports(inputs)
    reset = next((p for p in inputs if _RESET_PORT.match(str(p.get("name")))), None)
    return (clocks[0] if clocks else None), reset


def reset_levels(reset: dict) -> tuple[int, int]:
    """(asserted, deasserted) levels for this reset port, from its name."""
    asserted = 0 if _ACTIVE_LOW_RESET.search(str(reset.get("name"))) else 1
    return asserted, 1 - asserted


def _case_cycles(case: dict) -> int | None:
    """How many rising edges this case waits for, or None when unusable.

    One cycle by default: a case that says nothing about time still has to be
    read after an edge, because that is when a synchronous design updates.
    """
    raw = case.get("cycles")
    if raw is None:
        return 1
    try:
        cycles = int(raw)
    except (TypeError, ValueError):
        return None
    return cycles if 0 <= cycles <= 64 else None


def build_verilog_testbench(
    module: str, ports: Any, cases: Any, problems: list | None = None
) -> str | None:
    """A self-checking testbench for one module, from its ports and its cases.

    The old testbench was written by hand for a 4-bit adder: its module name,
    port list, expected value and 16x16x2 sweep were all literals, so every
    request that was not that adder was compiled and "verified" as one.

    The expectations come from the planner's cases, which are derived from the
    requirement — never from the module under test, which would be the design
    grading its own homework. Returns None when the inputs cannot produce a
    meaningful bench, so the caller can report "unverifiable" instead of a pass.

    A design with a clock port gets a clocked bench: the bench owns every clock,
    waits the case's `cycles` rising edges of the first one and only then reads
    the outputs. When a reset port exists the bench pulses it before each case,
    so every case starts from a known state. Without one the cases run in order
    and the state carries over — a register that loads a value can still be
    verified, and a case that depends on an unknown initial state fails visibly
    rather than being checked against a guess.

    A bidirectional (`inout`) port is connected as a wire: the design drives it,
    so the bench can read it but never drive it. It is checked when the case
    states a value for it, and a case that assigns one is skipped — checking
    against a stimulus the bench never applied would be the silent version of
    the same mistake.
    """
    if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_]\w*", module):
        return None
    if not isinstance(ports, list) or not ports:
        return None
    if not isinstance(cases, list) or not cases:
        return None
    inputs = [p for p in ports if isinstance(p, dict) and p.get("direction") == "input"]
    outputs = [p for p in ports if isinstance(p, dict) and p.get("direction") == "output"]
    inouts = [p for p in ports if isinstance(p, dict) and p.get("direction") == "inout"]
    if not inputs or not (outputs or inouts):
        return None

    clocks = clock_ports(inputs)
    clock_names = {str(p["name"]) for p in clocks}
    reset = next((p for p in inputs if _RESET_PORT.match(str(p.get("name")))), None)
    reset_name = str(reset["name"]) if reset is not None else None
    # Signals the bench drives itself. The clock always; the reset only when it
    # pulses one, because a case may legitimately want to hold it asserted.
    bench_driven = clock_names | ({reset_name} if reset_name else set())

    declarations, connections = [], []
    for port in inputs + outputs + inouts:
        name, width = str(port["name"]), int(port["width"])
        kind = "reg" if port["direction"] == "input" else "wire"
        declarations.append(f"    {kind} [{width - 1}:0] {name};")
        connections.append(f".{name}({name})")

    checks = []
    copyable = []
    skipped = []
    default_notes = []
    for index, case in enumerate(cases, 1):
        # Every way of dropping a case is recorded. A case that quietly does not
        # run is how a run reports PASSED for the cases that were easy to build:
        # the traffic-light shape planned five cases and the simulation checked
        # two, and nothing in the result said so.
        if not isinstance(case, dict):
            skipped.append((f"T{index}", "case is not an object"))
            continue
        label = _safe_label(case.get("id"), f"T{index}")
        problem = _case_field_problem(case)
        if problem:
            skipped.append((label, "case " + problem))
            continue
        # A value the bench cannot read is named before anything else looks at the
        # case: otherwise `light=01` reads as "states no expected output", which is
        # a true sentence about the wrong problem.
        unreadable = _unreadable_values(case.get("given")) + _unreadable_values(case.get("then"))
        if unreadable:
            skipped.append(
                (label, "case states a value the bench cannot read: " + ", ".join(unreadable))
            )
            continue
        drives = _parse_assignments(case.get("given"))
        expects = _parse_assignments(case.get("then"))
        sequence, sequence_problem = _parse_sequence(case)
        if sequence_problem:
            skipped.append((label, "case " + sequence_problem))
            continue
        # An empty `given` is legitimate when every input is a clock: the bench
        # drives those itself. The undriven check below is what decides whether
        # the case actually supplies what the design needs.
        if not expects:
            skipped.append((label, "case states no expected output"))
            continue
        driven_inouts = [
            str(p["name"]) for p in inouts
            if str(p["name"]) in drives
            and not isinstance(drives[str(p["name"])], _HighZ)
        ]
        if driven_inouts:
            # Name them: the refusal has to say which port, or "a bidirectional
            # port" reads the same for every case.
            skipped.append((
                label,
                "case drives bidirectional port " + ", ".join(driven_inouts)
                + ", which the bench cannot drive",
            ))
            continue
        # `bus=z` 对双向端口不是「驱动它」，是「不驱动它」——那正是测试台一直在做
        # 的事，所以放行，但要说出来：日志里不写，读者会以为测试台驱动了它。
        undriven_inouts = [
            str(p["name"]) for p in inouts if isinstance(drives.get(str(p["name"])), _HighZ)
        ]
        # 反过来，把**输入**驱动成 z 等于没驱动它，而上面的规矩是每个输入都得有确定
        # 的值（`defaulted` 那段就是为这个）。点名字，别让它变成 X 传播之后的一句
        # 「设计错了」。
        z_inputs = [
            str(p["name"]) for p in inputs
            if isinstance(drives.get(str(p["name"])), _HighZ)
        ]
        if z_inputs:
            skipped.append((
                label,
                "case drives input " + ", ".join(z_inputs)
                + " to z, which leaves it undriven — every input needs a value",
            ))
            continue
        # A case that writes to a declared output is a mistake, and the bench only
        # drives inputs: taking the value as an expectation instead would quietly
        # answer a different question than the case asked.
        driven_outputs = [str(p["name"]) for p in outputs if str(p["name"]) in drives]
        if driven_outputs:
            skipped.append(
                (label, "case drives output " + ", ".join(driven_outputs)
                 + ", which the design drives")
            )
            continue
        # A sequence may only drive ports the bench is allowed to drive, and each
        # one has to be a declared input: otherwise the case asks for something the
        # bench cannot do, and naming the port is the whole point.
        unusable = []
        for name in sequence:
            port = next((p for p in ports if str(p["name"]) == name), None)
            if port is None:
                unusable.append(f"sequence drives {name}, which is not a port")
            elif port["direction"] != "input" or name in clock_names:
                unusable.append(f"sequence drives {name}, which the bench cannot drive")
        if unusable:
            skipped.append((label, "; ".join(unusable)))
            continue

        # Every declared input must be driven: an undriven input makes the result
        # depend on X propagation rather than on the design. Driving it to 0 and
        # saying so beats dropping the case — a reset-only case (`reset=1`, no
        # other input) is a legitimate case, and dropping it made the whole run
        # fail with cases_dropped:T1 while the reset behaviour itself was never
        # checked.
        defaulted = [
            str(p["name"]) for p in inputs
            if str(p["name"]) not in bench_driven
            and str(p["name"]) not in drives
            and str(p["name"]) not in sequence
        ]
        for name in defaulted:
            drives[name] = 0
        missing = [str(p["name"]) for p in outputs if str(p["name"]) not in expects]
        if missing:
            skipped.append((label, "case does not state output " + ", ".join(missing)))
            continue
        if defaulted:
            default_notes.append((label, "not given, driven to 0: " + ", ".join(defaulted)))
        if undriven_inouts:
            default_notes.append((
                label,
                "z on a bidirectional port means no drive, so the bench leaves it alone: "
                + ", ".join(undriven_inouts),
            ))
        cycles = _case_cycles(case) if clocks else 1
        if cycles is None:
            skipped.append((label, f"cycles={case.get('cycles')!r} is not a number of edges 0..64"))
            continue
        longest = max((len(values) for values in sequence.values()), default=0)
        if longest > cycles:
            # The sequence itself says how many edges it needs; raising `cycles` to
            # fit beats dropping a case whose intent is clear.
            default_notes.append(
                (label, f"cycles raised from {cycles} to {longest} to fit the input sequence")
            )
            cycles = longest
        # An inout is checked only when the case states a value for it: without
        # one there is nothing to compare, and the port would be dropped from
        # the check without anyone noticing.
        checked = list(outputs) + [p for p in inouts if str(p["name"]) in expects]
        unfit = _unfit_values(drives, inputs) + _unfit_values(expects, checked)
        if unfit:
            skipped.append((label, "; ".join(unfit)))
            continue
        # Can every expectation be read straight off this case's own inputs? For a
        # sequential design that means the case cannot tell a stateful design from
        # a wire, which is how a rotating shift register once passed against an
        # implementation that only loaded its input.
        known = set(drives.values())
        if reset is not None:
            known.add(reset_levels(reset)[0])
        copyable.append(all(value in known for value in expects.values()))

        lines = [f"        // {label}"]
        if clocks and reset is not None:
            asserted, deasserted = reset_levels(reset)
            # One asserted edge first. A synchronous reset needs the edge, an
            # asynchronous one needs only the level, and both end at a known 0.
            lines.append(
                f"        {reset_name} = {_verilog_literal(int(reset['width']), asserted)};"
            )
            lines.append(f"        @(posedge {clocks[0]['name']}); #1;")
            lines.append(
                f"        {reset_name} = {_verilog_literal(int(reset['width']), deasserted)};"
            )
        for port in inputs:
            name = str(port["name"])
            if name in clock_names or name not in drives:
                continue
            lines.append(
                f"        {name} = {_verilog_literal(int(port['width']), drives[name])};"
            )
        if clocks:
            if sequence:
                # One value per rising edge, assigned just before that edge: this is
                # what makes "din 依次是 1-0-1-1" expressible at all.
                widths = {str(p["name"]): int(p["width"]) for p in ports}
                for edge in range(cycles):
                    for name, values in sequence.items():
                        if edge < len(values):
                            lines.append(
                                f"        {name} = "
                                f"{_verilog_literal(widths[name], values[edge])};"
                            )
                    lines.append(f"        @(posedge {clocks[0]['name']});")
            else:
                lines += [f"        @(posedge {clocks[0]['name']});"] * cycles
        lines.append("        #1;")
        condition = " && ".join(
            f"{port['name']} === {_verilog_literal(int(port['width']), expects[str(port['name'])])}"
            for port in checked
        )
        # The line a reader needs to tell "checked" from "planned": the run
        # result is assembled from what the simulation printed, not from a copy of
        # the planner's list.
        lines.append(
            f'        if ({condition}) begin passed = passed + 1; '
            f'$display("CASE {label} PASS"); end'
        )
        lines.append(
            '        else begin failed = failed + 1; '
            f'$display("CASE {label} FAIL"); end'
        )
        checks.append("\n".join(lines))
    if not checks:
        if problems is not None:
            problems.extend(skipped)
        return None

    clock_drive = "".join(
        f"    always #5 {p['name']} = ~{p['name']};\n" for p in clocks
    )
    if clock_drive:
        clock_drive += "\n"
    clock_init = "".join(f"        {p['name']} = 0;\n" for p in clocks)
    # Say it in the log, not only in the bench: without a reset the cases are a
    # sequence, and a reader has to know that to read a failure correctly.
    carry_note = (
        '        $display("NOTE no reset port: the state carries over between cases");\n'
        if clocks and reset is None else ""
    )
    # A case the bench could not express is named in the log. Dropping it quietly
    # is how a run reports PASSED for the cases that were easy to build.
    skip_notes = "".join(
        f'        $display("NOTE case {case_id} skipped: {why}");\n'
        for case_id, why in skipped
    )
    # A case that ran with an input the planner did not give is still a case that
    # ran; the log says which input was filled in, so nobody reads the pass as
    # "the planner specified everything".
    default_notes_text = "".join(
        f'        $display("NOTE case {case_id}: {why}");\n'
        for case_id, why in default_notes
    )
    # A tripwire, not a gate: a load register legitimately expects its input, so
    # this only fires when *every* case is like that and the design is clocked.
    copyable_note = (
        '        $display("NOTE every expectation is one of that case\'s own input values: '
        'these cases do not tell a stateful design apart from a wire");\n'
        if clocks and copyable and all(copyable) else ""
    )
    return (
        "`timescale 1ns/1ps\n"
        f"module tb_{module};\n"
        + "\n".join(declarations)
        + "\n    integer passed;\n    integer failed;\n\n"
        + f"    {module} uut (" + ", ".join(connections) + ");\n\n"
        + clock_drive
        + "    initial begin\n        passed = 0;\n        failed = 0;\n"
        + clock_init
        + carry_note
        + copyable_note
        + skip_notes
        + default_notes_text
        + "\n".join(checks)
        + "\n        $display(\"PASSED %0d FAILED %0d\", passed, failed);\n"
        + "        $finish;\n    end\nendmodule\n"
    )


def _bench_shortfall(module: Any, ports: Any, cases: Any, problems: Any = ()) -> str:
    """Why no bench could be built — reported as the cause, not as a failure.

    "Unverifiable" has to say which input was missing, or a refusal to guess
    looks the same as a design that failed. `problems` are the per-case reasons
    the bench itself recorded while reading the cases: a case it could not express
    is the most common reason of all, and "no case drives every input" says nothing
    about which case or which input.
    """
    if problems:
        named = "; ".join(f"{label}: {why}" for label, why in list(problems)[:3])
        return f"unverifiable: no case could be built — {named}"
    entries = [p for p in (ports or []) if isinstance(p, dict)]
    inputs = [p for p in entries if p.get("direction") == "input"]
    outputs = [p for p in entries if p.get("direction") == "output"]
    inouts = [p for p in entries if p.get("direction") == "inout"]
    if (
        not isinstance(module, str) or not module.strip()
        or not inputs or not (outputs or inouts)
    ):
        return "unverifiable: no module/ports/cases to build a testbench from"
    if not isinstance(cases, list) or not cases:
        return "unverifiable: no cases to check the design against"
    bidirectional = {str(p["name"]) for p in inouts}
    for case in cases:
        if not isinstance(case, dict):
            continue
        driven = bidirectional & set(_parse_assignments(case.get("given")))
        if driven:
            return (
                "unverifiable: case " + str(case.get("id") or "?")
                + " drives bidirectional port " + ", ".join(sorted(driven))
                + ", and the bench cannot drive a wire the design drives"
            )
    outputs = [p for p in entries if p.get("direction") == "output"]
    for case in cases:
        if not isinstance(case, dict):
            continue
        problem = _case_field_problem(case)
        if problem:
            return (
                "unverifiable: case " + str(case.get("id") or "?") + " " + problem
            )
        unreadable = _unreadable_values(case.get("given")) + _unreadable_values(case.get("then"))
        if unreadable:
            return (
                "unverifiable: case " + str(case.get("id") or "?")
                + " states a value the bench cannot read: " + ", ".join(unreadable)
            )
        drives = _parse_assignments(case.get("given"))
        driven_outputs = [str(p["name"]) for p in outputs if str(p["name"]) in drives]
        if driven_outputs:
            return (
                "unverifiable: case " + str(case.get("id") or "?")
                + " drives output " + ", ".join(driven_outputs)
                + ", which the design drives"
            )
        unfit = _unfit_values(drives, entries)
        unfit += _unfit_values(_parse_assignments(case.get("then")), entries)
        if unfit:
            return (
                "unverifiable: case " + str(case.get("id") or "?")
                + " states " + "; ".join(unfit)
            )
    # Ports and cases exist, so the refusal is about what the cases contain:
    # a case that leaves an input undriven or an output unstated cannot be run.
    return "unverifiable: no case drives every input and expects every output"




def _display_int(output: str, label: str) -> int:
    """The last `LABEL n` in the output — the bench prints its summary last.

    The design and the bench share one stdout, and the design's own `$display`
    runs first. Taking the first match let a design's debug line that happened to
    contain "PASSED 3" decide the verdict for it.
    """
    matches = re.findall(rf"{label}\s+(\d+)", output)
    return int(matches[-1]) if matches else 0


def execute_verilog(
    verilog_code: Any,
    design_name: str = "design.v",
    tb_name: str = "tb_design.v",
    module: Any = None,
    ports: Any = None,
    cases: Any = None,
) -> dict:
    """Compile and simulate one module against a generated self-checking bench.

    The bench is built from the module's declared ports and the planner's cases;
    ``passed`` counts cases whose outputs match, so a design that compiles but is
    wrong reports ``success=False``. With no ports/cases there is nothing to
    check against, and the result says so rather than reporting a pass.
    """
    base = {
        "success": False,
        "passed": 0,
        "failed": 0,
        "exit_code": -1,
        "logs": "",
        "mode": "verilog",
        # The e2e gate decides "real execution" from `stub is False`. Without
        # this key a genuine simulation was indistinguishable from a stub, so a
        # correct Verilog module could never satisfy B5.
        "stub": True,
    }
    if not isinstance(verilog_code, str) or not verilog_code.strip():
        return {**base, "logs": "empty_verilog_source"}
    problems: list = []
    testbench_source = build_verilog_testbench(module, ports, cases, problems)
    if testbench_source is None:
        # No expectations means no verification. Reporting "unverifiable" is the
        # honest answer; the alternative is a bench that cannot fail.
        return {**base, "logs": _bench_shortfall(module, ports, cases, problems)}
    if not execution_allowed():
        audit_record('hdl_execution_refused', design=design_name, reason='execution_disabled')
        return {**base, "logs": _EXECUTION_DISABLED}
    if not (shutil.which("iverilog") and shutil.which("vvp")):
        return {**base, "logs": "hdl_tools_unavailable", "stub": True}
    try:
        design = _safe_relative_path(design_name)
        testbench = _safe_relative_path(tb_name)
    except ValueError as exc:
        return {**base, "logs": f"invalid_verilog_path: {exc}"}

    with tempfile.TemporaryDirectory(
        prefix="verilog-", ignore_cleanup_errors=True
    ) as directory:
        with open(os.path.join(directory, design), "w", encoding="utf-8") as stream:
            stream.write(verilog_code)
        with open(os.path.join(directory, testbench), "w", encoding="utf-8") as stream:
            stream.write(testbench_source)
        compile_result = run_trusted(
            ["iverilog", "-o", "sim.out", design, testbench],
            directory,
            timeout=EXECUTION_TIMEOUT,
        )
        if compile_result["exit_code"] != 0:
            return {
                **base,
                "exit_code": compile_result["exit_code"],
                "logs": "compile_failed\n" + compile_result["output"],
            }
        run_result = run_trusted(
            ["vvp", "sim.out"], directory, timeout=EXECUTION_TIMEOUT
        )

    output = run_result["output"]
    passed = _display_int(output, "PASSED")
    failed = _display_int(output, "FAILED")
    # What the simulation actually ran, taken from its own output. `cases` used to
    # be copied from the planner, so the gate that compares planned against run
    # cases was comparing a list with itself.
    checked_cases = re.findall(r"^CASE (\S+) (?:PASS|FAIL)$", output, re.MULTILINE)
    dropped_cases = re.findall(r'^NOTE case (\S+) skipped: (.*)$', output, re.MULTILINE)
    audit_record(
        'hdl_executed',
        design=design,
        passed=passed,
        failed=failed,
        exit_code=run_result["exit_code"],
    )
    return {
        **base,
        "success": run_result["exit_code"] == 0 and passed > 0 and failed == 0,
        "passed": passed,
        "failed": failed,
        "exit_code": run_result["exit_code"],
        "logs": output,
        "stub": False,
        "cases_checked": checked_cases,
        "cases_dropped": [case_id for case_id, _ in dropped_cases],
    }
