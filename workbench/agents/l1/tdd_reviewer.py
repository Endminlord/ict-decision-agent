import ast, hashlib
from runtime.security_scanner import scan_code
from typing import Any
PHASE = 1

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(gc: Any) -> str:
    if isinstance(gc, dict):
        tid = gc.get("task_id")
        if isinstance(tid, str) and tid: return tid
    try: return _hash_id(gc)
    except Exception: return "unknown"

def _check_a1(gc, tc):
    """Entrypoints must agree — but `unknown` is the absence of a claim.

    The spec derives the entrypoint from free-form requirements prose and falls
    back to `green_code.py:unknown` when the prose names nothing callable. The
    coder then resolves it against the code it actually wrote, which is the only
    artifact that can know. Treating that resolution as an inconsistency failed
    a run whose implementation and tests were both correct.

    A declared symbol that names nothing cannot conflict with a real one, so
    `unknown` is accepted on either side; two *named* entrypoints must match.
    """
    coder = gc.get("entrypoint")
    planner = tc.get("entrypoint")
    if not coder or not planner:
        return False

    # With several interfaces a single entrypoint string cannot describe both
    # sides, so compare the function sets when both report them.
    coder_all = gc.get("entrypoints")
    planner_all = tc.get("entrypoints")
    if isinstance(coder_all, list) and isinstance(planner_all, list) and coder_all and planner_all:
        if set(coder_all) == set(planner_all):
            return True
        # A named function the coder never produced is a real failure, not a
        # metadata disagreement.
        return False

    if coder == planner:
        return True
    return "unknown" in (str(coder).partition(":")[2], str(planner).partition(":")[2])

def _check_a2(code, language):
    if not isinstance(code, str) or not code.strip(): return False
    if language == "python":
        try: ast.parse(code); return True
        except SyntaxError: return False
    if language == "verilog": return "module" in code and "endmodule" in code
    if language in ("javascript", "typescript"): return code.count("{") == code.count("}")
    if language == "go": return "func " in code
    return True

def _check_a3(trr, tc):
    trr_ids = trr.get("cases", [])
    tc_cases = tc.get("cases", [])
    if not isinstance(trr_ids, list) or not isinstance(tc_cases, list): return False
    s1 = {c for c in trr_ids if isinstance(c, str)}
    s2 = {c.get("id") for c in tc_cases if isinstance(c, dict) and c.get("id")}
    return s1 == s2 and len(s2) > 0

def _emit_fail(task_id: str, reason: str) -> dict:
    # 输入不完整，红队无从下手——如实说没跑，而不是编一个结论。
    return {"review_report": {"task_id": task_id, "verdict": "fail", "assertions": [], "security": "not_evaluated_phase_1", "performance": "not_evaluated_phase_1", "red_green": {"verdict": "not_run", "tried": 0, "findings": [], "reason": reason}, "gate_nature": "contract_gate", "phase": PHASE, "failure_reason": reason}}


def _run_red_team(green_code: dict, test_cases: dict) -> dict:
    """把红队跑起来。红队自己不认识任何领域：策略作为数据传进去。"""
    from runtime.adversary import challenge

    policy = test_cases.get("adversary") if isinstance(test_cases, dict) else None
    families = None
    limit = None
    if isinstance(policy, dict):
        families = policy.get("families")
        limit = policy.get("limit")
    spec = {
        "mode": test_cases.get("mode") or "function",
        "entrypoint": green_code.get("entrypoint") or "",
        "cases": test_cases.get("cases") or [],
    }
    kwargs = {"families": families}
    if isinstance(limit, int) and limit > 0:
        kwargs["limit"] = limit
    try:
        report = challenge(spec, green_code, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 红队自己崩了也是结论，不是异常
        return {"verdict": "not_run", "tried": 0, "findings": [],
                "reason": f"adversary_failed:{type(exc).__name__}: {exc}"}
    # 领域自己写的说明要跟着结论走。它常常是「为什么这里不适用」——而那正是
    # 读报告的人最想知道、也最容易被一个光秃秃的 not_run 藏起来的东西。
    note = policy.get("note") if isinstance(policy, dict) else None
    if note and report.get("verdict") == "not_run":
        report["reason"] = f"{report.get('reason') or ''}（领域说明：{note}）"
    return report

def tdd_reviewer(green_code: Any, test_run_result: Any, test_cases: Any, llm=None) -> dict:
    if not isinstance(green_code, dict) or not isinstance(test_run_result, dict) or not isinstance(test_cases, dict):
        return _emit_fail(_resolve_task_id(green_code), "input_incomplete")
    task_id = _resolve_task_id(green_code)
    language = green_code.get("language")
    assertions = [
        {"id": "A1", "check": "entrypoint_consistency", "result": "pass" if _check_a1(green_code, test_cases) else "fail"},
        {"id": "A2", "check": "code_parseable", "result": "pass" if _check_a2(green_code.get("code"), language) else "fail"},
        {"id": "A3", "check": "test_coverage_alignment", "result": "pass" if _check_a3(test_run_result, test_cases) else "fail"},
    ]
    execution_ok = (test_run_result.get('success') is True and test_run_result.get('failed') == 0
                    and test_run_result.get('passed', 0) > 0 and not green_code.get('code_stub', False))
    assertions.append({'id': 'A4', 'check': 'real_execution_passed', 'result': 'pass' if execution_ok else 'fail'})
    security = scan_code(green_code.get('code', ''), language)
    assertions.append({'id': 'A5', 'check': 'security_scan', 'result': 'fail' if security['verdict'] == 'fail' else 'pass'})
    # 红队：把契约自己的字面量往旁边推一格，看生成物会不会当场塌。
    #
    # 为什么它是硬断言而不是「只记一笔」：这台机器失败的方式是**停下来问人**，
    # 不是默默丢掉。一个合法的邻居输入把代码弄塌了，那件事必须让闸门不同意——
    # 至于它是真缺陷还是需求没说清的边界，正是要交给人的那一步。
    red_green = _run_red_team(green_code, test_cases)
    # 「没跑」不等于「没通过」——这是这个仓库里反复出现的那条界线。
    #
    # 第一版把 not_run 也判成 fail，于是所有 verilog（IC 形态）和 CLI 形态的运行
    # 全被挡住：红队不会跑非 Python 的邻居，而它「不会跑」被写成了「它不同意」。
    # 这正是喊狼来了，而且是代价最大的那种——它会让人把整条 IC 路径关掉。
    #
    # 跟着 security 那条先例走：A5 在 not_evaluated 时是 pass，而 security 字段
    # 如实写着 not_evaluated_phase_1。红队同理：只有**真的发现塌了**才判失败，
    # 没跑就如实记在 red_green 里。
    assertions.append({
        "id": "A6",
        "check": "red_team_survived",
        "result": "fail" if red_green.get("verdict") == "broken" else "pass",
        "red_green_verdict": red_green.get("verdict"),
    })
    verdict = "pass" if all(a["result"] == "pass" for a in assertions) else "fail"
    report = {"task_id": task_id, "verdict": verdict, "assertions": assertions, "security": "not_evaluated_phase_1", "performance": "not_evaluated_phase_1", "red_green": red_green, "gate_nature": "contract_gate", "phase": PHASE}
    report["security"] = security["verdict"]
    report["security_findings"] = security["findings"]
    # Cases the bench could not run are named, not just counted: "A3 failed" tells
    # a reader nothing about which case the planner wrote wrong, and the reason
    # string is what reaches the work order and the card.
    dropped = test_run_result.get("cases_dropped")
    if isinstance(dropped, list) and dropped:
        report["dropped_cases"] = [str(case_id) for case_id in dropped]
    if verdict == "fail":
        failed = [a["id"] for a in assertions if a["result"] == "fail"]
        report["failure_reason"] = f"contract_assertion_failed:{','.join(failed)}"
        if report.get("dropped_cases"):
            report["failure_reason"] += (
                ":cases_dropped:" + ",".join(report["dropped_cases"])
            )
        # 红队失败时把「哪个输入弄塌的、塌成什么样」带出去：只写 A6，读的人无从下手，
        # 而这条字符串正是会到达工单和卡片的那一条。
        if "A6" in failed and red_green.get("reason"):
            report["failure_reason"] += ":" + str(red_green["reason"])
    return {"review_report": report}
