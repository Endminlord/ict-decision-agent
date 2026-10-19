import ast
import hashlib
import re
import json
from typing import Any

# The clock/reset convention lives in one place. The prompt below tells the
# model what the bench will do with its cases, so if the two disagreed the
# cases would describe a stimulus the bench never applies.
from runtime.executor import clock_and_reset, clock_ports, reset_levels
from runtime.lang import coerced_from, normalize_language

INTEGRATION_KEYWORDS = ["external", "database", "network", "api", "http", "socket"]

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(spec: Any) -> str:
    if isinstance(spec, dict):
        tid = spec.get("task_id")
        if isinstance(tid, str) and tid: return tid
    try: return _hash_id(spec)
    except Exception: return "unknown"

def _judge_type(trd: Any) -> str:
    if not isinstance(trd, dict): return "unit"
    decisions = trd.get("decisions", [])
    if not isinstance(decisions, list): return "unit"
    text = " ".join(str(d.get("decision", "")) for d in decisions if isinstance(d, dict)).lower()
    return "integration" if any(k in text for k in INTEGRATION_KEYWORDS) else "unit"

def _parse_acceptance(ac: str):
    import ast
    if not isinstance(ac, str): return None
    try:
        expression = ast.parse(ac.strip(), mode='eval').body
        if not isinstance(expression, ast.Compare) or len(expression.ops) != 1 or not isinstance(expression.ops[0], ast.Eq): return None
        call = expression.left
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name): return None
        args = [ast.literal_eval(a) for a in call.args]
        kwargs = {k.arg: ast.literal_eval(k.value) for k in call.keywords if k.arg is not None}
        if len(kwargs) != len(call.keywords): return None
        expected = ast.literal_eval(expression.comparators[0])
        given = ','.join([repr(x) for x in args] + [k + '=' + repr(v) for k,v in kwargs.items()])
        return {'given': given, 'when': ast.unparse(call), 'then': '==' + repr(expected)}
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return None

def _extract_fields(spec):
    ts = spec.get("tech_stack", {})
    ts = ts if isinstance(ts, dict) else {}
    v = spec.get("verification", {})
    v = v if isinstance(v, dict) else {}
    # 边界校验，不是又一层兜底。原来的 `or "python"` 只挡 falsy，挡不住「有值但值
    # 不对」：live run 65390e806f81 里这里进来的是 "chinese"，出去就变成了一个 stub。
    raw_language = ts.get("language")
    fields = {"language": normalize_language(raw_language),
              "test_framework": ts.get("test_framework") or "pytest",
              "entrypoint": v.get("entrypoint") or "green_code.py:main"}
    coerced = coerced_from(raw_language, fields["language"])
    if coerced is not None:
        # 改写过的字段要留痕迹，否则下一个人会以为 python 是模型自己选的。
        fields["language_coerced_from"] = coerced
    return fields

def _language_fields(fields):
    """`test_cases` 里跟语言有关的那几个键 —— 三处构造点共用一份。

    这个字段刚出过一次「改了一处、漏了另一处」的错，所以只留一个出口。
    """
    out = {"language": fields["language"], "test_framework": fields["test_framework"],
           "entrypoint": fields["entrypoint"]}
    if fields.get("language_coerced_from"):
        out["language_coerced_from"] = fields["language_coerced_from"]
    return out

def _first_signature(items) -> str | None:
    """The first `name(args)` in the acceptance criteria, if any."""
    for item in items if isinstance(items, list) else []:
        match = re.search(r"([A-Za-z_]\w*\s*\([^)]*\))", str(item))
        if match:
            return match.group(1).replace(" ", "")
    return None


def _called_names(cases) -> list[str]:
    """Distinct function names the cases call, in order."""
    names: list[str] = []
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, dict):
            continue
        match = re.search(r"([A-Za-z_]\w*)\s*\(", str(case.get("when") or ""))
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _all_signatures(items) -> list[str]:
    """Every distinct `name(args)` in the acceptance criteria, in order.

    The pipeline used to key off the first one only, so a spec declaring several
    interfaces planned cases for one of them and the rest were never exercised.
    """
    found: list[str] = []
    for item in items if isinstance(items, list) else []:
        for match in re.finditer(r"([A-Za-z_]\w*\s*\([^)]*\))", str(item)):
            signature = match.group(1).replace(" ", "")
            if signature not in found:
                found.append(signature)
    return found


def _split_signature(signature: str) -> tuple[str, list[str]]:
    name, _, rest = signature.partition("(")
    params = [p.strip().split("=")[0].split(":")[0].strip() for p in rest.rstrip(")").split(",")]
    return name, [p for p in params if p]


def _extract_json(text: str):
    """Pull the first JSON object out of a model response.

    A small local model frequently wraps the object in prose or leaves the
    opening fence unterminated. Stripping fences and calling json.loads on the
    remainder then raises JSONDecodeError, the planner silently falls back, and
    the run dies with "no_executable_cases" — a message that says nothing about
    malformed model output. Scanning for the outermost braces is tolerant of
    everything except genuinely absent JSON.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned[:6].lower() == "json\n":
            cleaned = cleaned[5:]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except ValueError:
        return None


def _hdl_ports(spec: Any) -> list | None:
    """The module's ports when the spec describes hardware, else None.

    An HDL design is verified by simulation against concrete port values, so its
    cases have to be written as port assignments. The Python case shape
    (`when: "f(x)"`, `then: "==3"`) cannot drive a testbench at all, and the
    bench that used to be used instead was written by hand for one adder.
    """
    if not isinstance(spec, dict):
        return None
    verification = spec.get("verification") or {}
    if not verification.get("capability"):
        return None
    ports = verification.get("ports")
    if isinstance(ports, list) and ports:
        return ports
    interfaces = spec.get("interfaces") or []
    interface = interfaces[0] if interfaces and isinstance(interfaces[0], dict) else {}
    declared = []
    for direction in ("input", "output"):
        for entry in interface.get(direction) or []:
            match = re.match(r"\s*([A-Za-z_]\w*)\s*:\s*(\d+)\s*bit", str(entry))
            if match:
                declared.append({
                    "name": match.group(1),
                    "direction": direction,
                    "width": int(match.group(2)),
                })
    return declared or None


def _criteria_lines(requirements_hint: str) -> list:
    """Render the acceptance criteria as bullets, if they are a JSON list.

    They arrive as a JSON string (the interface carries a list of sentences). A
    prompt that showed them as one blob got cases that satisfied the letter of
    the port list and none of the behaviour.
    """
    try:
        parsed = json.loads(requirements_hint)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    items = [item.strip() for item in parsed if isinstance(item, str) and item.strip()]
    return [f"- {item}" for item in items]


# 需求里点名了有符号，用例就必须踩到负数：一个用无符号 `a > b` 写出来的比较器会通过
# 全部非负用例，而它对 a=-1, b=1 是错的——形态「通过」了，却没验到需求里那个词。
_SIGNED_HINTS = ("有符号", "负数", "补码", "signed", "two's complement")


def _case_shaped(value: Any) -> list | None:
    """`value` as a list of cases — judged by the shape of the items, not by a name.

    A case is an object carrying `when` and `then`: that is the contract the
    executor actually runs on, and it is the only thing here worth trusting.

    实测：同一个出口把顶层键写成了 `ses`（`cases` 少了一个 `ca`），五条用例一条
    不差（`given`/`when`/`then`/`type` 全在），却被 `parsed.get("cases", [])`
    全数丢掉，于是 S5 报「模型没给用例」，整条链子停在那儿。
    """
    if not isinstance(value, list) or not value:
        return None
    for item in value:
        if not isinstance(item, dict) or "when" not in item or "then" not in item:
            return None
    return value


def _hdl_cases(content: Any) -> tuple[list, str]:
    """The cases in one answer, and — when there are none — what was wrong with it.

    "The model returned no cases" was reported for four different things: prose
    instead of JSON, JSON without a `cases` key, an empty list, and a list of
    objects the parser could not read. The next stage only saw the symptom.

    键名不是契约：没有 `cases` 键时，顶层**任何一个**非空列表，只要每一项都带
    `when` + `then`，就按用例读（`_case_shaped`）。读到了就不算「答得不对」——
    这些用例接着要过 `_case_contract` 和执行器，形状对不对由它们说话。
    """
    parsed = _extract_json(content)
    if not isinstance(parsed, dict):
        return [], "model_output_is_not_a_json_object"
    cases = parsed.get("cases")
    if cases is None:
        for value in parsed.values():
            found = _case_shaped(value)
            if found is not None:
                return found, ""
        keys = ", ".join(sorted(str(k) for k in parsed)[:5]) or "空"
        return [], f"no_cases_key(顶层字段: {keys})"
    if not isinstance(cases, list):
        return [], f"cases_is_{type(cases).__name__}_not_a_list"
    if not cases:
        return [], "cases_list_is_empty"
    return cases, ""


def _describe_exception(exc: BaseException) -> str:
    """异常自己带的话，别丢。

    只说类型名，会把「401 鉴权失败」「429 限流」「连接超时」全抹平成同一个词——
    而这三件事要做的处置完全不同。LLMError 自带 status / detail，优先用它们；
    其余异常退回 str(exc)。

    **不带 body**：响应体可能把请求原样回显回来，而请求头里有密钥。
    """
    status = getattr(exc, "status", None)
    detail = getattr(exc, "detail", None)
    if status is not None or detail:
        parts = []
        if status is not None:
            parts.append(f"status={status}")
        if detail:
            parts.append(str(detail)[:200])
        return " ".join(parts)
    return str(exc).strip()[:200]


def _renumber(cases: list) -> list:
    """合并多次回答之后重新编号并去重：每次回答都从 T1 开始，撞号了。"""
    seen: set = set()
    out: list = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        key = (str(case.get("given")), str(case.get("then")), str(case.get("cycles")))
        if key in seen:
            continue
        seen.add(key)
        copy = dict(case)
        copy["id"] = f"T{len(out) + 1}"
        out.append(copy)
    return out


def _compact_case_prompt(ports: list, criterion: str, how_many: int = 2) -> str:
    """逐条问时用的紧凑提示词。

    完整提示词（1848 字）里有端口表和五条规则；逐条问时那部分是纯开销——真正决定
    思考长度的是「要同时想几件事」。实测把提示词压到 300-400 字之后，一次回答的推理
    tokens 落在 715-3997，远离那个 8192 的硬上限。

    压到 300 字那次**顺手把双向端口那条规矩压掉了**，而且更糟：`inout` 被列进了「输入
    端口」，规则又要求 `given` 给每个输入端口赋值——模型照提示词写出来的用例，测试台
    一条不留全丢掉（`case drives bidirectional port bus`），A3 抓住、运行停下等人。
    三态总线那个形态就是死在这里。紧凑版可以少写几句话，**不能和测试台说反话**。
    """
    names = [str(p.get("name")) for p in ports if p.get("direction") == "input"]
    outputs = [str(p.get("name")) for p in ports if p.get("direction") == "output"]
    inouts = [str(p.get("name")) for p in ports if p.get("direction") == "inout"]
    clock_names = {str(p["name"]) for p in clock_ports(ports)}
    inputs = ", ".join(n for n in names if n not in clock_names) or "（无）"
    lines = [
        "你在为一个数字电路模块编写仿真测试用例。只输出一个 JSON 对象，不要任何其他文字。",
        '{"cases": [{"id": "T1", "given": "输入端口赋值", "then": "输出端口期望值", "type": "unit"}]}',
        "",
        f"只针对这一条验收条件，给 {how_many} 个用例：\n{criterion}",
        "",
        f"规则：`given` 要给每个输入端口赋值（输入：{inputs}），`then` 要给每个输出端口的"
        f"期望值（输出：{', '.join(outputs) or '（无）'}）；值写十进制整数，不要写位宽前缀；"
        f"高阻（三态线没被驱动）写 `z`，测试台认识它。",
    ]
    if inouts:
        # 与 `build_verilog_testbench` 同一句话：测试台把双向端口当线读，不当输入驱动。
        named = "、".join(f"`{n}`" for n in inouts)
        lines.append(
            f"{named} 是**双向端口**：不要在 `given` 里给它赋值（测试台不能驱动双向端口），"
            f"它只能出现在 `then` 里；设计会驱动它的时候，把期望值写进 `then`，测试台会检查。"
        )
    return "\n".join(lines)


def _ask_cases_per_criterion(
    llm: Any, ports: list, criteria: list, *, how_many: int = 2, attempts: int = 2
) -> tuple[list, str]:
    """一条验收条件一次小问——一次要全部用例会把这个出口的思考预算用光。

    实测：失败的那几次 `reasoning_tokens` 恰好都是 8192，而把 max_tokens 提到 32768
    之后报回来的仍然是 8192、正文为空。也就是说**加预算没用**，出口对一次回答的思考
    长度有硬上限，唯一的杠杆是让它每次少想一点。

    返回 (用例, 说明)。说明非空表示有哪几条条件一条用例都没问出来——不静默跳过。
    """
    cases: list = []
    missing: list = []
    for index, criterion in enumerate(criteria, 1):
        prompt = _compact_case_prompt(ports, str(criterion), how_many=how_many)
        got: list = []
        for _ in range(attempts):
            try:
                response = llm.chat(prompt)
            except Exception:
                break
            content = response.get("content") if isinstance(response, dict) else response
            got, _ = _hdl_cases(content)
            if got:
                break
        if got:
            cases.extend(got)
        else:
            missing.append(index)
    return cases, (f"criteria_without_cases={missing}" if missing else "")


def _case_retry_note(problem: str) -> str:
    """What to tell the model the second time, given what was wrong the first."""
    if problem.startswith("model_returned_nothing"):
        # 这里**故意不加提示**。实测「请直接给出答案，不要长篇推理」这句话会让这个
        # 模型想得更久（同一提示词：不加提示推理 5521 给出 477 字正文；加了那句话
        # 推理 8192、正文为空）。预算用光时唯一有用的是更大预算下的**再一次独立抽取**。
        return ""
    if problem == "model_output_is_not_a_json_object":
        return (
            "\n\n注意：上一次的回复里没有 JSON 对象。请**只**输出一个 JSON 对象，"
            "不要有任何解释文字，不要用 markdown 代码块包裹。"
        )
    if problem.startswith("no_cases_key") or problem == "cases_list_is_empty":
        return (
            "\n\n注意：上一次的回复里没有一个非空的 `cases` 数组。"
            '顶层必须是 {{"cases": [...]}}，至少 3 条，每条含 id / given / then。'
        )
    return ""


def _ask_cases_again(
    llm: Any, prompt: str, problem: str, attempt: int = 1
) -> tuple[str, str, dict]:
    """One more ask with the defect named. "" when no answer came back at all.

    The budget grows with the attempt when the last answer was empty: how long this
    model thinks is not stable (the same prompt spent 263 reasoning tokens once and
    filled 8192 the next), so one ask at one budget is a single draw. An empty
    answer is *deliberately* retried without any hint — telling this model "answer
    directly, don't overthink" made it think longer (5521 → 8192 tokens, prose
    instead of an answer).
    """
    note = _case_retry_note(problem)
    if problem.startswith("model_returned_nothing"):
        base = int(getattr(llm, "max_tokens", 8192) or 8192)
        budget = min(max(base * 2 ** attempt, 16384), 32768)
        request = (prompt, budget)
    elif note:
        request = (prompt + note, None)
    else:
        return "", "", {}

    prompt_text, budget = request
    try:
        response = (llm.chat(prompt_text) if budget is None
                    else llm.chat(prompt_text, max_tokens=budget))
    except Exception as exc:
        # 调用本身失败（超时、限流、连接被拒）与「模型答了个空的」是两件事：
        # 两者都返回 "" 的话，运行只会说「模型什么都没返回」，而真正的原因没了。
        return "", f"retry_call_failed:{type(exc).__name__}", {}
    content = response.get("content") if isinstance(response, dict) else response
    answer = content if isinstance(content, str) else ""
    return answer, "", (response if isinstance(response, dict) else {})


def _hdl_prompt(ports: list, requirements_hint: str, how_many: int | None = None) -> str:
    clock, reset = clock_and_reset(ports)
    clocks = clock_ports(ports)
    clocked = clock is not None
    clock_name = str(clock["name"]) if clocked else None
    inouts = [p for p in ports if p.get("direction") == "inout"]
    clock_names = {str(p["name"]) for p in clocks}
    signed = (
        isinstance(requirements_hint, str)
        and any(hint in requirements_hint.lower() for hint in _SIGNED_HINTS)
    )
    lines = [
        "你在为一个数字电路模块编写仿真测试用例。",
        "只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹。",
        "",
        '{"cases": [{"id": "T1", "given": "输入端口赋值", "then": "输出端口期望值", "type": "unit"}]}',
        "",
        "模块端口：",
    ]
    for port in ports:
        lines.append(
            f"- {port['name']}：{port['direction']}，{port['width']} 位"
        )
    # Every clock is the bench's own signal, so none of them is asked for here:
    # a prompt that asks for one while rule 5 forbids it produces cases the
    # bench has to ignore.
    inputs = ", ".join(
        p["name"] for p in ports
        if p["direction"] == "input" and p["name"] not in clock_names
    )
    outputs = ", ".join(p["name"] for p in ports if p["direction"] == "output")
    lines += [
        "",
        "规则：",
        f"1. `given` 必须为**每一个输入端口**赋值，格式 `名=十进制值`，多个用逗号分隔（输入端口：{inputs}）",
        f"2. `then` 必须给出**每一个输出端口**的期望值，格式 `名=十进制值`（输出端口：{outputs}）",
        "3. 值用十进制整数，不要写位宽前缀（写 `a=3`，不要写 `4'd3`）；要表达**高阻**"
        "（三态线没被驱动）时写 `z`，测试台认识它，会按高阻比",
        (f"4. 给 {how_many} 个用例，只覆盖下面这一条验收条件"
         if how_many else
         "4. 至少 4 个用例，覆盖典型值与边界值（0、最大值、进位/溢出等）"),
        "5. 用例必须能验证下面每一条验收条件。验收条件描述的是**行为**（移位、计数、保持、"
        "回绕、握手）时，期望值要按这个行为算出来——不要写成「输出等于某个输入」，"
        "那样的用例连一个直接连线的实现都能通过，等于没验",
    ]
    criteria = _criteria_lines(requirements_hint)
    if criteria:
        lines += ["", "这个电路该做什么（验收条件）："] + criteria
    rule = 6
    if clocked:
        # A clocked design is checked after an edge, not after a wire settles.
        # The bench owns every clock and pulses the reset itself, so the cases
        # describe what happens *after* that pulse — a case that also drove a
        # clock would contradict the bench that has to run it.
        names = "、".join(f"`{p['name']}`" for p in clocks)
        lines += [
            "",
            f"这是一个**时序电路**：{names} 是时钟"
            + (f"，`{reset['name']}` 是复位（{'低电平有效' if reset_levels(reset)[0] == 0 else '高电平有效'}）。"
               if reset is not None else "（没有复位端口）。"),
            f"{rule}. `given` 中**不要**给时钟 {names} 赋值，时钟由测试台驱动",
            # 上限与 _case_cycles 接受的范围一致：写 1~8 时，4 位计数器的回绕
            # （需要 16 个沿）永远写不出来，而规则里又要求覆盖回绕。
            f"{rule + 1}. 每个用例额外给出 `cycles`：经过的时钟上升沿数（1~64 的整数）"
            + (f"，指的是 `{clock['name']}` 的上升沿" if len(clocks) > 1 else ""),
        ]
        rule += 2
        if reset is not None:
            asserted, _ = reset_levels(reset)
            lines.append(
                f"{rule}. 测试台在**每个用例前**会先把 `{reset['name']}` 置为有效电平一个时钟周期再释放，"
                f"因此每个用例都从复位后的已知状态开始"
            )
        else:
            lines.append(
                f"{rule}. 该模块没有复位端口：测试台不做复位，用例**按顺序**执行、状态在用例之间延续。"
                f"因此第一个用例必须先把状态置成已知值（例如使能有效时写入一个数据），后面的用例才谈得上期望值"
            )
        rule += 1
        lines += [
            f"{rule}. `then` 是经过 `cycles` 个上升沿**之后**的输出期望值",
            f"{rule + 1}. 至少覆盖："
            + ("复位保持（reset 有效时输出不变）、复位释放后 1 个时钟沿的行为、连续多个时钟沿"
               "后的行为、状态回绕或饱和的边界（若位宽有限）" if reset is not None
               else "先把状态置成已知值、使能/控制无效时保持不变、连续多个周期的行为"),
            "",
            '示例：{"cases": [{"id": "T1", "given": "'
            + (str(reset["name"]) + "=" + str(reset_levels(reset)[0]) if reset is not None
               else (next((p["name"] for p in ports
                           if p["direction"] == "input" and p["name"] not in clock_names), "en") + "=1"))
            + '", "cycles": 2, "then": "' + outputs.split(",")[0].strip() + '=0", "type": "unit"}]}',
        ]
        rule += 2
    if signed:
        lines += [
            "",
            f"{rule}. 需求里说的是**有符号**数：用例必须至少包含一条负数（例如 `a=-1`，"
            f"格式仍是十进制），否则一个按无符号实现的电路会通过全部用例。",
            f"{rule + 1}. 有符号数的期望值按**数值大小**判断，不是按位型。`a=-1, b=0` 时 "
            f"`a` 小于 `b`，比较结果是 0；`a=-1, b=-2` 时 `a` 大于 `b`，结果是 1。"
            f"把 `-1` 当成 255 来算，会让一个无符号实现正好通过你写的用例。",
        ]
        rule += 2
    if any(hint in (requirements_hint or "").lower() for hint in
           ("序列", "连续", "sequence", "逐位", "串行", "移位输入")):
        lines += [
            "",
            f"{rule}. 需求里的输入是**一串逐周期的值**：用 `sequence` 写，不要把它压成一个 "
            "`given` 值。例如检测 1011："
            + '`"sequence": {"din": [1, 0, 1, 1]}`'
            + "，`cycles` 至少等于序列长度，`then` 是最后一个沿之后的期望值；"
            + "写进 `sequence` 的端口就不要再出现在 `given` 里。",
            '完整的形状：{"cases": [{"id": "T1", "given": "reset=0", '
            '"sequence": {"din": [1, 0, 1, 1]}, "cycles": 4, "then": "detected=1"}]}',
        ]
        rule += 1
    if inouts:
        # The bench connects a bidirectional port as a wire: it can read what
        # the design drives, never drive it. A case that assigns one is dropped.
        names = "、".join(f"`{p['name']}`" for p in inouts)
        lines += [
            "",
            f"{rule}. {names} 是**双向端口**：不要在 `given` 里给它赋值（测试台不能驱动双向端口）；"
            f"如果设计会驱动它，就在 `then` 里给出期望值，测试台会检查。",
        ]
    if not criteria:
        # No structured criteria: show whatever hint there is, rather than
        # dropping the only statement of what the circuit should do.
        lines += ["", f"验收条件：{requirements_hint}"]
    return "\n".join(lines)


_COMPARISONS = ("==", "!=", ">=", "<=", ">", "<")


def _then_value_not_literal(then: str) -> str:
    """`then` 的取值部分必须是**字面量**——否则执行器会静默退化成字符串比较。

    `bootstrap.py:416 _expected_from` 把 `==`/`!=`/`>=`/`<=`/`>`/`<` 后面的**整段文本**
    当值，`bootstrap.py:149` 再拿 `_expected_value` 去 `ast.literal_eval` 它；解析不了就
    **原样当字符串**比。于是 live run `1218a15770b6` 的 T7
    `then = "==7 and add(a,b) == add(b,a)"` 变成了
    `7 == "7 and add(a,b) == add(b,a)"` ⇒ 失败，而报告里写的是
    `expected =='7 and add(a,b) == add(b,a)', got 7`
    —— **看上去像断言写错了，其实是方言用错了**。

    判定刻意宽：`ast.literal_eval` 收得下的一律放行（`7`/`'a'`/`[1,2]`/`{'k':1}`/`None`），
    解析不通但**只是一个名字**的也放行（`==abc` 这种裸词靠上面那条字符串回退是能比的），
    只拦「解析得通、但不是字面量」的那一类——那才是模型把逻辑写进了取值位。
    """
    text = then.strip()
    body = text[2:].strip() if text.startswith("==") else text
    head, _, _tail = body.partition(" ")
    if head.lower() in ("raises", "throws", "returns", "return"):
        return ""
    value = body
    for marker in _COMPARISONS:
        if text.startswith(marker):
            value = text[len(marker):].strip()
            break
    if not value:
        return "empty then-value"
    try:
        ast.literal_eval(value)
        return ""
    except (ValueError, SyntaxError):
        pass
    try:
        node = ast.parse(value, mode="eval").body
    except SyntaxError:
        return ""
    if isinstance(node, (ast.Name, ast.Constant)):
        return ""
    return f"not a literal: {value[:60]}"


def _case_contract(cases: list) -> dict:
    """`when` 有一条隐含契约：它必须是一个**可求值的 Python 表达式**。

    这条契约一直存在（`bootstrap.py:345` 用 `ast.parse(when, mode="eval")` 求值，
    解析不了就把这条用例记成 error），但**从来没人在这边校验过**——于是 live run
    `c7dad4d6ed6a` 里 12 条用例有 2 条的 `when` 根本不是表达式：`from add import add`
    是 import 语句、`pytest --collect-only -q test_add.py` 是 shell 命令。执行器只能
    事后报 `unparseable when: invalid syntax`，而那时 A4（`real_execution_passed`）
    已经失败了——**缺陷在产出它的那一步是无声的，只在三步之后以「执行失败」的形状出现。**

    这里只**标记**、不删用例：删掉会让覆盖率凭空下降，而「少测了」比「测了但解析
    不了」更难被发现。标记让缺陷在产出它的地方就说得出来。
    """
    bad = []
    bad_then = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        then = case.get("then")
        if isinstance(then, str) and then.strip():
            why = _then_value_not_literal(then)
            if why:
                case["then_invalid"] = why
                bad_then.append(case.get("id"))
        when = case.get("when")
        if not isinstance(when, str) or not when.strip():
            # 「没有 when」是另一类缺陷，执行器已有专门说明，这里不抢它的话。
            continue
        try:
            ast.parse(when.strip(), mode="eval")
        except SyntaxError as exc:
            case["when_invalid"] = str(exc.msg)
            bad.append(case.get("id"))
    flagged = {}
    if bad:
        flagged["when_invalid"] = bad
    if bad_then:
        flagged["then_invalid"] = bad_then
    return flagged


def _emit_minimal(task_id: str, reason: str = "no_usable_source") -> dict:
    """No cases. ``reason`` says why, because the caller reports it as the cause.

    This used to be a bare empty result, so a run that died here reported only
    ``no_executable_cases`` — indistinguishable between "the requirements named
    no callable", "the model returned malformed JSON" and "the model returned
    valid JSON with an empty list".
    """
    return {"test_cases": {"task_id": task_id, "language": "python", "test_framework": "pytest",
                           "entrypoint": "green_code.py:main", "cases": [], "cases_stub": True,
                           "stub_reason": reason}}

def tdd_planner(spec: Any, trd: Any, research_report=None, llm=None) -> dict:
    # Each early exit names itself. They used to fall through to the default
    # "no_usable_source", so a spec that carried no interface at all — the
    # parameterised-adder shape — reported the same sentence as a spec whose
    # criteria named no callable.
    if not isinstance(spec, dict) or not isinstance(trd, dict):
        return _emit_minimal(_resolve_task_id(spec), "spec_or_trd_missing")
    task_id = _resolve_task_id(spec)
    fields = _extract_fields(spec)
    ifaces = spec.get("interfaces", [])
    if not isinstance(ifaces, list) or not ifaces:
        # The spec records why its own contract could not be derived; without it
        # this reason names the symptom ("no interface") and not the cause.
        why = spec.get("contract_derivation_failed")
        return _emit_minimal(
            task_id,
            "spec_declares_no_interface" + (f":{why}" if isinstance(why, str) and why else ""),
        )
    
    acceptance = []
    for i in ifaces:
        if not isinstance(i, dict): continue
        acs = i.get("acceptance_criteria", [])
        if isinstance(acs, list):
            acceptance.extend([a for a in acs if isinstance(a, str) and a.strip()])
    
    if not acceptance:
        return _emit_minimal(task_id, "interface_carries_no_acceptance_criteria")
        
    case_type = _judge_type(trd)
    
    # 阶段2 P1: 优先用 LLM 生成严格结构的测试用例 JSON
    reason = None
    ports = _hdl_ports(spec)
    split_why = ""
    if ports and llm is not None:
        from llm_client import describe_empty_answer  # 局部导入，和 sdd_spec_builder 一致

        try:
            hdl_prompt = _hdl_prompt(ports, json.dumps(acceptance, ensure_ascii=False))
            response = llm.chat(hdl_prompt)
            content = response.get("content") if isinstance(response, dict) else response
            cases, problem = _hdl_cases(content)
            if not cases and not (isinstance(content, str) and content.strip()):
                problem = "model_returned_nothing" + describe_empty_answer(response)
            # 最多再问两轮，每轮按**当前**的原因来问：第一轮可能是预算被推理吃光，
            # 第二轮可能是答了但不是 JSON——只重试其中一种，另一种就白等了。
            # 三轮、四抽取：最长的提示词（FIFO、序列检测）上这个模型的思考长度
            # 波动最大，两次不够用；每次都是一次独立抽取，所以多抽一次确实降失败率。
            for attempt in (1, 2, 3):
                if cases or not problem:
                    break
                again, failed, response = _ask_cases_again(
                    llm, hdl_prompt, problem, attempt=attempt
                )
                if failed:
                    problem = f"{problem}:{failed}"
                    break
                if not again:
                    # 空答案**不等于**没得问了：预算已经翻倍，换一档再抽一次。
                    # 这里原先写的是 `break`，于是第三次（32768）永远抽不到——
                    # 「四抽取」实际上只发生了两次，三个形态一直卡在同一个地方。
                    problem = (
                        "model_returned_nothing"
                        + describe_empty_answer(response)
                        + f"(第{attempt + 1}次抽取)"
                    )
                    continue
                cases, problem = _hdl_cases(again)

            if not cases and acceptance:
                # 四次抽取全空：换问法，一条验收条件一次小问。
                split_cases, split_why = _ask_cases_per_criterion(llm, ports, acceptance)
                if split_cases:
                    cases = _renumber(split_cases)
                elif split_why:
                    problem = f"{problem}:{split_why}"
            if cases:
                result = {
                    "test_cases": {
                        "task_id": task_id,
                        **_language_fields(fields),
                        "ports": ports,
                        "entrypoints": _called_names(cases),
                        "cases": cases,
                    }
                }
                if split_why:
                    # 逐条问的时候有哪几条条件一条用例都没问出来：记下来，别让
                    # 「覆盖了全部验收条件」变成一句没人核对的话。
                    result["test_cases"]["criteria_without_cases"] = split_why
                return result
            reason = "llm_returned_no_cases" + (f":{problem}" if problem else "")
        except Exception as exc:
            reason = f"llm_output_unusable:{type(exc).__name__}"
    if llm is not None and not ports:
        try:
            # The example uses the real signature. A generic `add(a,b)` sample
            # gives a small local model no reason to emit the actual function
            # name, and `given` must define every parameter `when` uses — a rule
            # that is only obvious once it is stated and demonstrated.
            signatures = _all_signatures(acceptance) or ["f(x)"]
            signature = signatures[0]
            name, params = _split_signature(signature)
            given_sample = ", ".join(f"{p}=1" for p in params) or "无参数"
            if len(signatures) > 1:
                # One function per interface: the coder generates every function
                # the cases call, so the cases have to name all of them.
                coverage = (
                    "\n本需求包含多个函数，**每个函数都必须有用例**："
                    + "、".join(signatures)
                    + "。用例的 `when` 必须覆盖全部这些函数。\n"
                )
            else:
                coverage = ""
            prompt = f"""你是一个测试架构师。请为下面的验收条件生成测试用例。

只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹。

JSON 结构：
{{"cases": [{{"id": "T1", "given": "参数赋值", "when": "函数调用", "then": "期望结果", "type": "unit"}}]}}

规则：{coverage}
1. `when` 必须是一个**能被求值的 Python 表达式**（等价于 `ast.parse(when, mode="eval")` 解析得通），
   使用**被测函数本身的名字**：{signature}。**不要**写 import 语句、shell 命令、pytest 命令行、
   赋值或任何语句——那些不是表达式，执行器求不了值，整条用例会作废
2. `given` 必须为 `when` 里出现的**每一个**参数赋值，格式 `名=值`，多个用逗号分隔
3. `then` 是断言表达式，以 == 开头（也支持 `raises <异常名>`）。**`==` 后面只能跟一个字面量**（数字、字符串、True/False/None、列表、字典）——执行器只认字面量，写复合表达式（如`==7 and add(a,b) == add(b,a)`）会被当成一个**字符串**去比，用例必然失败
4. 至少 3 个用例，覆盖正常值与边界值（空值、零、负值、极值等）
5. **如果需求描述的是一个类/对象**，用例要用「构造后调用方法」的形式，例如
   `Rectangle(length,width).area()`，`given` 提供构造参数。不要只写 `area()`——
   方法需要实例，单独调用会失败

示例（对应上面的规则）：
{{"cases": [{{"id": "T1", "given": "{given_sample}", "when": "{signature}", "then": "==期望值", "type": "unit"}}]}}

验收条件：{json.dumps(acceptance, ensure_ascii=False)}
测试类型：{case_type}"""
            # 先绑定再调用：llm.chat 自己抛异常时（超时、限流、连接被拒），
            # except 分支会去读 clean_json——原来它只在调用成功之后才存在，
            # 于是「模型调用失败」变成了 UnboundLocalError，把整个规划器打死。
            clean_json = ""
            llm_response = llm.chat(prompt)
            clean_json = (llm_response.get('content', '') if isinstance(llm_response, dict) else llm_response)
            parsed = _extract_json(clean_json)
            if not isinstance(parsed, dict):
                raise ValueError("no JSON object in the model response")
            cases, problem = _hdl_cases(clean_json)
            if not cases and problem:
                # 再抽一次，按**这一次的原因**问。HDL 那条路早就在这么做（三轮四抽取），
                # 通用这条路原来只抽一次就收工——而它才是所有非 HDL 需求走的那条。
                # 重试只在「答了但读不出用例」时发生：一次读不中不该等于整段丢掉。
                again, failed, response = _ask_cases_again(llm, prompt, problem, attempt=1)
                if failed:
                    problem = f"{problem}:{failed}"
                elif again:
                    cases, problem = _hdl_cases(again)
            if cases:
                return {"test_cases": {"task_id": task_id, **_language_fields(fields), "entrypoints": _called_names(cases), "cases": cases, **_case_contract(cases)}}
            reason = "llm_returned_no_cases" + (f":{problem}" if problem else "")
        except Exception as exc:
            # Reported, not swallowed: a malformed response is the single most
            # useful thing to know when the planner produces nothing.
            # detail 以前算出来就没再被用过——注释说「会被报告」，但 reason 里没有它，
            # 于是「模型答了段没法解析的东西」和「模型什么都没答」在账本上长得一样。
            detail = (clean_json[:200] if isinstance(clean_json, str) else "")
            reason = f"llm_output_unusable:{type(exc).__name__}"
            said = _describe_exception(exc)
            if said:
                reason = f"{reason}({said})"
            if detail:
                reason = f"{reason}(正文前 200 字: {detail})"

    # 阶段1 回退路径：纯正则解析
    cases = []
    for i, ac in enumerate(acceptance, 1):
        p = _parse_acceptance(ac)
        if p is None: continue
        cases.append({"id": f"T{i}", "given": p["given"], "when": p["when"], "then": p["then"], "type": case_type})
    
    if not cases:
        return _emit_minimal(task_id, reason or "acceptance_criteria_named_no_callable")
        
    return {"test_cases": {"task_id": task_id, **_language_fields(fields), "entrypoints": _called_names(cases), "cases": cases, **_case_contract(cases)}}
