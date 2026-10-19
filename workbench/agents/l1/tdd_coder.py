import hashlib, re
from typing import Any

# 边界校验：上游（tdd_planner）已经归一过一次，这里再认一次是因为本函数是公开的，
# 别的调用方可能直接塞一份 `test_cases` 进来。认不出来原样放行，见 runtime/lang.py。
from runtime.lang import normalize_language

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(tc: Any) -> str:
    if isinstance(tc, dict):
        tid = tc.get("task_id")
        if isinstance(tid, str) and tid: return tid
    try: return _hash_id(tc)
    except Exception: return "unknown"

def _extract_signature(when: str):
    import ast
    if not isinstance(when, str): return None
    try:
        call = ast.parse(when.strip(), mode='eval').body
        # `Rectangle(2, 3).area()` is implemented by generating `Rectangle`, so
        # walk down to the leftmost Name(...) call rather than rejecting the
        # expression. Without this the coder found no signature at all and
        # emitted an empty stub for every object-oriented request.
        guard = 0
        while not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)):
            guard += 1
            if guard > 20:
                return None
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                call = call.func.value
            elif isinstance(call, ast.Call) and isinstance(call.func, ast.Call):
                # `time_decorator(func)(10)` applies a decorator and calls the
                # result. Descend into the inner call: the outermost name is
                # what has to be implemented.
                call = call.func
            elif isinstance(call, ast.Attribute):
                call = call.value
            else:
                return None
        params = []
        for i, value in enumerate(call.args):
            params.append(value.id if isinstance(value, ast.Name) else 'arg' + str(i))
        params.extend(k.arg for k in call.keywords if k.arg)
        if len(set(params)) != len(params): return None
        return call.func.id, params
    except (SyntaxError, ValueError): return None

def _render_skeleton(func, params, language):
    if language == "python":
        pstr = ", ".join(params) if params else ""
        return f"def {func}({pstr}):\n    # TODO: implement {func}\n    pass\n"
    if language in ("javascript", "typescript"):
        pstr = ", ".join(params) if params else ""
        return f"function {func}({pstr}) {{\n  // TODO\n}}\n"
    if language == "go": return f"package main\n\nfunc {func}() {{\n    // TODO\n}}\n"
    return f"// TODO: implement {func} in {language}\n"

def _defined_and_called(code, cases, language) -> list[str]:
    """The functions the code defines that the tests actually call.

    `entrypoint` is a single string because the pipeline started single-function.
    With several, the honest comparison is the set of callables both sides agree
    on, so it is reported alongside.
    """
    if language != "python" or not isinstance(code, str):
        return []
    defined = _defined_names(code)
    called = [name for name, _ in _signatures_in(cases)]
    return [name for name in called if name in defined]


def _strip_code_fence(code: Any) -> str:
    """Remove a markdown code fence, whatever language tag it carries.

    The Python branch recognised only the literal tag "python", and the Verilog
    branch never stripped at all — it returned before the stripping code. A model
    that answered with ```verilog produced source beginning with the word
    "verilog", which fails to compile for a reason that has nothing to do with
    the model's answer.
    """
    if not isinstance(code, str):
        return ""
    text = code.strip()
    if not text.startswith("```"):
        return text
    body = text[3:]
    newline = body.find("\n")
    if newline == -1:
        # No newline at all: an inline fence like ```x``` carries no tag, only
        # a closing fence to remove.
        return body.rstrip().removesuffix("```").strip()
    tag = body[:newline].strip().lower()
    # A language tag is a bare word; anything else is the first line of code.
    if tag and (tag.isalpha() or tag.isalnum()) and len(tag) <= 12:
        body = body[newline + 1:]
    body = body.rstrip()
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()


def _hdl_target(test_cases) -> tuple[str | None, list | None]:
    """The module name and ports the planner planned against, if any."""
    if not isinstance(test_cases, dict):
        return None, None
    module = None
    entrypoint = test_cases.get("entrypoint")
    if isinstance(entrypoint, str) and ":" in entrypoint:
        module = entrypoint.partition(":")[2] or None
    ports = test_cases.get("ports")
    if not isinstance(ports, list) or not ports:
        return module, None
    return module, ports


def _case_description(case: dict) -> str:
    """One case as the design has to read it, clock cycles included.

    A clocked case reads `reset=0 → count=3`, and the cycle count is what makes
    that expectation mean anything: without it the coder has to guess how many
    edges the bench waits before it reads the output. The combinational shape is
    unchanged, so nothing is added where there is no clock.
    """
    given = str(case.get("given", ""))
    cycles = case.get("cycles")
    if cycles is not None:
        given = f"{given}（经过 {cycles} 个时钟上升沿）"
    return f"{given} → {case.get('then', '')}"


def _generate_verilog_llm(module: str, ports: list, cases: list) -> str:
    """The prompt asks for the module the spec named, not a fixed one.

    It used to read "请用Verilog实现一个4位加法器模块" with the port list spelled
    out, so every HDL request produced a 4-bit adder — and the testbench, built
    from the derived ports, then failed to find the module it referenced.
    """
    lines = [
        f"请用 Verilog-2001 实现一个名为 {module} 的模块。",
        "端口：",
    ]
    for port in ports:
        lines.append(f"- {port['name']}：{port['direction']}，{port['width']} 位")
    case_descs = "; ".join(
        _case_description(c) for c in cases if isinstance(c, dict)
    )
    lines += [
        "根据以下测试用例实现该模块的功能。",
        "只给 Verilog 代码，不要解释，不要 markdown 代码块标记。",
        "模块名必须与上面给出的名字完全一致。",
        f"测试用例：{case_descs}",
    ]
    return "\n".join(lines)


def _defined_names(code) -> set:
    """Every callable the code defines: functions and classes alike.

    Matching only ``def`` meant a correctly generated class was invisible to the
    gates, so a class-based request failed A1 with "entrypoints disagree" even
    though the class was right there in the code.
    """
    if not isinstance(code, str):
        return set()
    return set(re.findall(r"^\s*(?:def|class)\s+([A-Za-z_]\w*)", code, re.M))


def _signatures_in(cases) -> list[tuple[str, list[str]]]:
    """Every distinct function the test cases actually call, in order.

    The coder used to read only `cases[0]`, so a spec declaring two functions
    produced one implementation and the run reported success for half the work.
    """
    found: list[tuple[str, list[str]]] = []
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, dict):
            continue
        sig = _extract_signature(case.get("when", ""))
        if sig and sig not in found:
            found.append(sig)
    return found


def _generate_code_llm(signatures, test_cases, language, code_hint=""):
    """Prompt for every function the cases call, not just the first."""
    sigs = [f"{name}({', '.join(params)})" if params else name for name, params in signatures]
    case_descs = "; ".join(
        f"given={c.get('given','')}; {c.get('when','')} → {c.get('then','')}"
        for c in test_cases if isinstance(c, dict)
    )
    if len(sigs) > 1:
        head = (
            f"实现以下 {len(sigs)} 个 {language} 函数或类：" + "、".join(sigs) + "。\n"
            "**每个函数都必须实现**，测试会分别调用它们。"
        )
    else:
        head = f"实现一个 {language} 函数或类 {sigs[0]}。"
    prompt = f"""{head}
根据以下测试用例生成完整实现代码，只给代码，不要解释，不要 markdown 代码块标记。
如果用例用「构造后调用方法」的形式（如 `Rectangle(2,3).area()`），就生成对应的类。
测试用例：{case_descs}
函数签名：{'、'.join(sigs)}"""
    # 仓库里已有的、跟这次需求像的代码。它**不是**「参考实现」——是这个仓库自己的
    # 写法（怎么切词、怎么报错、怎么命名），模仿它比模仿任何模板都合适。
    # 空字符串 = 这次没找到足够像的，那就什么都不说——**给错例子比不给更糟**。
    if code_hint:
        prompt = f"{prompt}\n\n{code_hint}"
    return prompt

def _emit_minimal(task_id: str) -> dict:
    return {"green_code": {"task_id": task_id, "language": "python", "code": "", "entrypoint": "green_code.py:main", "code_stub": True}, "test_run_result": {"task_id": task_id, "passed": 0, "failed": 0, "cases": [], "stub": True}}

def _resolve_entrypoint(entrypoint, code, cases, language):
    """Trust the generated code over the spec's entrypoint metadata.

    The spec derives the entrypoint from the requirements text, which may never
    name the function — it becomes ``green_code.py:unknown``. Everything
    downstream then calls a symbol that does not exist and the e2e gate fails,
    even though the code and its tests are correct. The coder is the authority
    on what it actually wrote, so the entrypoint is reconciled against the code
    and, failing that, against the function the tests call.
    """
    if language != "python" or not isinstance(code, str):
        return entrypoint
    file_part, _, current = str(entrypoint).partition(":")
    file_part = file_part or "green_code.py"
    defined = _defined_names(code)
    if current in defined:
        return entrypoint
    for case in cases:
        if not isinstance(case, dict):
            continue
        match = re.search(r"([A-Za-z_]\w*)\s*\(", str(case.get("when") or ""))
        if match and match.group(1) in defined:
            return f"{file_part}:{match.group(1)}"
    if defined:
        return f"{file_part}:{sorted(defined)[0]}"
    return entrypoint


def tdd_coder(test_cases: Any, llm=None, code_hint: str = "", code_refs: Any = None) -> dict:
    """写代码的那个 Agent。`code_hint` 是「这个仓库里已有的、跟这次像的代码」。

    检索由链子做好递进来，而不是让它自己去翻仓库：写代码的 Agent 每次运行都顺手
    扫一遍仓库，是一件不在它职责里、也没人记账的事。空 = 这次没找到足够像的。
    `code_refs` 是那份提示词的**出处**，跟着产物一起留下——「这次参考了哪儿」和
    「这次写了什么」一样该能复核。
    """
    result = _tdd_coder_impl(test_cases, llm=llm, code_hint=code_hint)
    if isinstance(result, dict):
        result["code_index"] = {"refs": list(code_refs or []), "used": bool(code_hint)}
    return result


def _tdd_coder_impl(test_cases: Any, llm=None, code_hint: str = "") -> dict:
    if not isinstance(test_cases, dict): return _emit_minimal(_resolve_task_id(test_cases))
    language = normalize_language(test_cases.get("language"))
    entrypoint = test_cases.get("entrypoint") or "green_code.py:main"
    task_id = _resolve_task_id(test_cases)
    cases = test_cases.get("cases", [])
    if not isinstance(cases, list) or not cases: return _emit_minimal(task_id)
    if language == "verilog":
        module, ports = _hdl_target(test_cases)
        code = ""
        if llm is not None and module and ports:
            try:
                resp = llm([{"role": "user", "content": _generate_verilog_llm(module, ports, cases)}])
                code = _strip_code_fence(resp["content"])
            except Exception:
                code = ""
        if not code.strip():
            # No interface to implement, or the model failed. A skeleton for the
            # *requested* module, never a hardcoded one: emitting an adder for a
            # multiplier request is worse than emitting nothing, because the
            # simulation then reports a module-name mismatch that looks like the
            # testbench's fault.
            entrypoint = _resolve_entrypoint(entrypoint, "", cases, language)
            return {"green_code": {"task_id": task_id, "language": "verilog",
                                   "code": "", "entrypoint": entrypoint,
                                   "code_stub": True},
                    "test_run_result": {"task_id": task_id, "passed": 0, "failed": 0,
                                        "cases": [c.get("id") for c in cases
                                                  if isinstance(c, dict)],
                                        "stub": True, "executed": False}}
        case_ids = [c.get("id") for c in cases if isinstance(c, dict) and c.get("id")]
        return {"green_code": {"task_id": task_id, "language": "verilog", "code": code, "entrypoint": entrypoint, "code_stub": False}, "test_run_result": {"task_id": task_id, "passed": 0, "failed": 0, "cases": case_ids, "stub": False}}
    first = cases[0]
    if not isinstance(first, dict): return _emit_minimal(task_id)
    signatures = _signatures_in(cases)
    if not signatures: return _emit_minimal(task_id)
    func_name, params = signatures[0]
    code_error = ""
    if llm is not None and language == "python":
        try:
            prompt = _generate_code_llm(signatures, cases, language)
            resp = llm([{"role": "user", "content": prompt}])
            code = _strip_code_fence(resp["content"])
            entrypoint = _resolve_entrypoint(entrypoint, code, cases, language)
            entrypoints = _defined_and_called(code, cases, language) or [entrypoint]
            return {"green_code": {"task_id": task_id, "language": language, "code": code, "entrypoint": entrypoint, "entrypoints": entrypoints, "code_stub": False}, "test_run_result": {"task_id": task_id, "passed": 0, "failed": 0, "cases": [c.get("id") for c in cases if isinstance(c, dict)], "stub": False, "executed": False}}
        except Exception as exc:
            # 不吞。这一路失败过一次，链子会退回骨架并标成 stub；原因不留下来，
            # 下一个人看到的就只有「stub」两个字，分不清是模型不行还是提示词不行。
            code_error = f"{type(exc).__name__}: {exc}"[:200]
    code = _render_skeleton(func_name, params, language)
    case_ids = [c.get("id") for c in cases if isinstance(c, dict) and c.get("id")]
    green = {"task_id": task_id, "language": language, "code": code, "entrypoint": entrypoint, "code_stub": True}
    if code_error:
        green["code_error"] = code_error
    return {"green_code": green, "test_run_result": {"task_id": task_id, "passed": 0, "failed": 0, "cases": case_ids, "stub": True}}
