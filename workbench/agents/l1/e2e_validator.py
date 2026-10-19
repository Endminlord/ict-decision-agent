import hashlib
from typing import Any
import re
from collections.abc import Mapping
from runtime.executor import execute_e2e_test, parse_case
# 骨架的渲染与解析共用同一份定义（tools/verify_md_schema.py 是唯一解析器）：
# 写与读分家正是「数据说 pass、markdown 说 fail 而没人报」的成因。
from tools.verify_md_schema import parse as _parse_verify_md
from tools.verify_md_schema import render as _render_verify_md_skeleton

PHASE = 1
VALID_MODES = {"function", "cli", "ui"}
PERSONAL_FUSE_STUB_TARGET = "TDD Coder"

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(rr: Any) -> str:
    if isinstance(rr, dict):
        tid = rr.get("task_id")
        if isinstance(tid, str) and tid: return tid
    try: return _hash_id(rr)
    except Exception: return "unknown"

def _check_b1(rr): return rr.get("verdict") == "pass" and rr.get("security") != "fail"
def _check_b2(gc, sv):
    """The entrypoint metadata must agree with the code that was produced.

    Two things are checked, and they are different questions:

    * every callable the coder claims to provide must exist in the code;
    * if the spec *named* an entrypoint, the coder must provide it.

    `unknown` is the spec failing to name a symbol, not a claim the code can
    contradict. Comparing the raw strings treated "the spec could not guess" as
    "the coder is wrong", which failed a run whose classes were all correct —
    the same distinction A1 already makes.
    """
    code = gc.get("code")
    if not isinstance(code, str) or not code.strip():
        return False
    language = gc.get("language")

    def _defined(name: Any) -> bool:
        if not isinstance(name, str) or not name:
            return False
        if language == "python":
            return re.search(
                r"^\s*(?:def|class)\s+" + re.escape(name) + r"\b", code, re.M
            ) is not None
        if language == "verilog":
            return re.search(r"\bmodule\s+" + re.escape(name) + r"\b", code) is not None
        return re.search(r"\b" + re.escape(name) + r"\b", code) is not None

    provided = [n for n in (gc.get("entrypoints") or []) if isinstance(n, str) and n]
    for name in provided:
        if not _defined(name):
            return False

    declared = gc.get("entrypoint")
    if not declared:
        return False
    symbol = str(declared).partition(":")[2]
    if not _defined(symbol) and not provided:
        return False

    if isinstance(sv, Mapping):
        spec_declared = sv.get("entrypoint")
        spec_symbol = str(spec_declared).partition(":")[2] if spec_declared else ""
        if spec_symbol and spec_symbol != "unknown":
            # The spec made a real claim; the coder must satisfy it.
            if not _defined(spec_symbol) and spec_symbol not in provided:
                return False
    return True


def _check_b3(sv):
    m = sv.get("mode")
    return isinstance(m, str) and m in VALID_MODES
def _check_b4(tc):
    c = tc.get("cases", [])
    return isinstance(c, list) and len(c) > 0

def _assertion_mapping(assertions) -> dict:
    """`[{id, check, result}, …]` → `{id: result}`，供 gate 的数据面读取。

    与 markdown 面（`_render_verify_md`）同源、同一次调用产出：等价性因此是
    **可判定的**，而不是两条各自为政的路（research/222 §6 第 1 条）。
    """
    mapping: dict = {}
    for a in assertions:
        identifier = str(a.get("id"))
        if identifier in mapping:
            # 重复 id 不静默覆盖：chain._read_assertions 对数据面也是同样态度。
            raise ValueError("duplicate assertion: " + identifier)
        mapping[identifier] = a.get("result")
    return mapping


def _render_verify_md(task_id, mode, assertions, verdict, failure_reason, e2e_result=None):
    """渲染骨架，并**当场自校验**：读回来的断言必须与写进去的逐条相等。

    自校验放在这里而不是测试里，是因为它要挡住的是「渲染器改了而解析器没跟上」——
    那种改动在测试里只有跑过测试的路径才会被发现。
    """
    md = _render_verify_md_skeleton(task_id, mode, assertions, verdict, failure_reason, e2e_result)
    parsed = _parse_verify_md(md)
    expected = {str(a["id"]).upper(): str(a["result"]).lower() for a in assertions}
    if parsed.assertions != expected:
        raise ValueError(
            "VERIFY.md 骨架自校验失败: 渲染后解析回 " + repr(parsed.assertions)
            + " 与写入的 " + repr(expected) + " 不一致"
        )
    return md

def _emit_fail(task_id: str, reason: str) -> dict:
    md = _render_verify_md(task_id, "not_determined", [], "fail", reason)
    return {"verify_md": md, "assertions": {}, "screenshots": [], "recording": None, "rollback_signal": {"target": PERSONAL_FUSE_STUB_TARGET, "decided_by": "Personal Fuse", "reason": reason, "auto": True}}

def _check_b6(spec_interfaces, test_cases, spec_verification=None):
    """Every declared interface must be covered by at least one test case.

    The pipeline is single-entrypoint end to end: one `verification.entrypoint`,
    one planned function, one generated function. A spec declaring several
    interfaces was therefore silently reduced to the first, and a run that
    implemented half of what was asked still reported `contract_pass`.
    """
    # A domain that verifies through a declared capability (e.g. an HDL
    # simulation) is not verified by the planner's cases, so requiring the
    # interfaces to appear there would fail a correctly verified run.
    if isinstance(spec_verification, dict) and spec_verification.get("capability"):
        return True, []
    if not isinstance(spec_interfaces, list) or not spec_interfaces:
        return True, []
    cases = (test_cases or {}).get("cases") if isinstance(test_cases, dict) else None
    called = " ".join(
        str(c.get("when") or "") for c in (cases or []) if isinstance(c, dict)
    )
    uncovered = [
        str(i.get("name")) for i in spec_interfaces
        # `unknown` is the absence of a name, not a name that went uncovered —
        # the same reasoning as the reviewer's A1.
        if isinstance(i, dict) and i.get("name")
        and str(i["name"]) not in ("unknown", "")
        and str(i["name"]) not in called
    ]
    return (not uncovered), uncovered


def _runs_disagree(measured: Any, previous: Any) -> str:
    """Why the two independent simulations are not the same run twice, or "".

    B5 re-runs the simulation to check the coding stage independently, but it only
    looked at `success`: 4/0 from the coder and 2/2 from the validator were the
    same verdict. The same design and the same cases should produce the same
    counts; when they do not, the evidence is not reproducible and the pass is not
    worth having.
    """
    if not isinstance(measured, dict) or not isinstance(previous, dict):
        return ""
    for key in ("passed", "failed"):
        now, before = measured.get(key), previous.get(key)
        if isinstance(now, int) and isinstance(before, int) and now != before:
            return f"{key} {before} -> {now}"
    now = measured.get("cases_checked")
    before = previous.get("cases")
    if isinstance(now, list) and isinstance(before, list) and sorted(map(str, now)) != sorted(map(str, before)):
        return ("cases " + ",".join(map(str, before))
                + " -> " + ",".join(map(str, now)))
    return ""


def e2e_validator(review_report: Any, green_code: Any, spec_verification: Any, test_cases: Any, llm=None, capability_runner=None, spec_interfaces=None, previous_run=None) -> dict:
    if not all(isinstance(x, dict) for x in [review_report, green_code, spec_verification, test_cases]):
        return _emit_fail(_resolve_task_id(review_report), "input_incomplete")
    task_id = _resolve_task_id(review_report)
    mode = spec_verification.get("mode") if isinstance(spec_verification.get("mode"), str) else "unknown"
    interfaces_ok, uncovered = _check_b6(spec_interfaces, test_cases, spec_verification)
    assertions = [
        {"id": "B1", "check": "upstream_gate_passed", "result": "pass" if _check_b1(review_report) else "fail"},
        {"id": "B2", "check": "entrypoint_consistency", "result": "pass" if _check_b2(green_code, spec_verification) else "fail"},
        {"id": "B3", "check": "mode_declared", "result": "pass" if _check_b3(spec_verification) else "fail"},
        {"id": "B4", "check": "cases_non_empty", "result": "pass" if _check_b4(test_cases) else "fail"},
        {"id": "B6", "check": "all_interfaces_covered",
         "result": "pass" if interfaces_ok else "fail",
         "detail": (
             "未覆盖的接口: " + ", ".join(uncovered)
             + "。当前流水线是单入口设计（verification.entrypoint 是单个字符串），"
             + "只会实现并验证第一个接口。请拆成多次请求，或等待多接口支持。"
         ) if uncovered else ""},
    ]
    verdict = "pass" if all(a["result"] == "pass" for a in assertions) else "fail"
    e2e_result = None
    failure_reason = None
    
    # 通用真实执行路径：交给 runtime.executor.execute_e2e_test 裁决。
    # 它按 spec_verification.mode 选择 function/cli，并在独立子进程中运行，
    # 返回值自带 success 与 stub，chain 据此判定 B5 是否为真实执行。
    e2e_result = None
    if verdict == "pass" and isinstance(spec_verification.get("capability"), str) and callable(capability_runner):
        # A declared capability is the verification mechanism for this domain;
        # its mode/entrypoint describe a simulation, not a runnable command.
        e2e_result = capability_runner(
            spec_verification["capability"], green_code.get("code") or ""
        )
        disagreement = _runs_disagree(e2e_result, previous_run)
        if isinstance(e2e_result, dict) and e2e_result.get("success") and not disagreement:
            assertions.append({"id": "B5", "check": "e2e_real_execution", "result": "pass"})
        else:
            verdict = "fail"
            detail = ""
            if isinstance(e2e_result, dict):
                detail = str(e2e_result.get("reason") or e2e_result.get("status") or "")
            if disagreement:
                failure_reason = f"two_runs_disagree:{disagreement}"
            else:
                failure_reason = "capability_execution_failed" + (f":{detail}" if detail else "")
            assertions.append({"id": "B5", "check": "e2e_real_execution", "result": "fail",
                               "detail": disagreement})
    elif verdict == "pass":
        e2e_spec = dict(spec_verification)
        if e2e_spec.get("mode") == "function" and "args" not in e2e_spec:
            # The spec declares the entrypoint but not the call arguments; the
            # first declared case supplies them, so the smoke check exercises
            # the function for real instead of calling it with no arguments.
            cases = test_cases.get("cases") if isinstance(test_cases, dict) else None
            if isinstance(cases, list) and cases:
                try:
                    e2e_spec["args"] = parse_case(cases[0])["args"]
                except ValueError:
                    pass
        e2e_result = execute_e2e_test(e2e_spec, green_code)
        if isinstance(e2e_result, dict) and e2e_result.get("success"):
            assertions.append({"id": "B5", "check": "e2e_real_execution", "result": "pass"})
        else:
            verdict = "fail"
            detail = ""
            if isinstance(e2e_result, dict):
                detail = str(e2e_result.get("reason") or e2e_result.get("status") or "")
            failure_reason = "e2e_execution_failed" + (f":{detail}" if detail else "")
            assertions.append({"id": "B5", "check": "e2e_real_execution", "result": "fail"})
    else:
        # B1-B4 已失败，未执行；记 not_run 而非 fail，不改变闸门结论。
        assertions.append({"id": "B5", "check": "e2e_real_execution", "result": "not_run"})
            
    if verdict == "fail" and not failure_reason:
        failed = [a["id"] for a in assertions if a["result"] == "fail"]
        failure_reason = f"contract_assertion_failed:{','.join(failed)}"
    md = _render_verify_md(task_id, mode, assertions, verdict, failure_reason, e2e_result)
    result = {"verify_md": md, "assertions": _assertion_mapping(assertions), "screenshots": [], "recording": None, "verdict": verdict, "execution": e2e_result, "stub": False}
    if verdict == "fail":
        result["rollback_signal"] = {"target": PERSONAL_FUSE_STUB_TARGET, "decided_by": "Personal Fuse", "reason": failure_reason, "auto": True}
    return result
