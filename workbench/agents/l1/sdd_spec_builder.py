import hashlib
import json, re
from typing import Any

from runtime.executor import clock_ports
from runtime.lang import normalize_language

# 需求里出现这些词，说的就是时序行为。契约里没有时钟，用例只能在 0 个时钟沿上比较
# 输出——实测有一次「带使能的 8 位寄存器」被推导成 enable/data/output 三个端口，
# 仿真一条用例都跑不了（passed=0/0），门禁判 A4 失败。失败是诚实的，但根因是契约。
# 只收「不可能是单向」的词：「总线」不收，因为单向总线也用这个词，重试反而会凭空
# 加出一个 inout 端口。三态总线的实测结果是端口里根本没有 inout（data/enable/bus_state），
# 于是测试台没东西可驱动，仿真一条用例都跑不了。
_BIDIRECTIONAL_HINTS = (
    "双向", "inout", "三态", "tri-state", "tristate", "bidirectional", "收发",
)
_SEQUENTIAL_HINTS = (
    "时钟", "clock", "clk", "沿", "edge", "时序", "sequential", "寄存器", "register",
    "触发", "flip-flop", "计数器", "counter", "状态机", "state machine", "fsm",
    "移位", "shift", "回绕", "wrap",
)
DEFAULT_LANGUAGE = "python"
DEFAULT_VERSION = "3.11"
DEFAULT_TEST_FRAMEWORK = "pytest"
MODULE_NAME_DEFAULT = "green_code"

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(md: Any) -> str:
    if isinstance(md, str):
        m = re.search(r"<!--\s*task_id:\s*([^\s>]+)\s*-->", md)
        if m: return m.group(1)
    try: return _hash_id(md)
    except Exception: return "unknown"

def _extract_section(md: str, name: str):
    lines = md.split("\n")
    out, in_sec = [], False
    for line in lines:
        if line.strip() == f"## {name}": in_sec = True; continue
        if in_sec and line.startswith("## "): break
        if in_sec: out.append(line)
    return out

def _extract_items(md: str, section: str):
    items = []
    for line in _extract_section(md, section):
        m = re.match(r"^- (?:\w+\d+:\s*)?(.+)$", line.strip())
        if m:
            c = m.group(1).strip()
            if c and c != "无": items.append(c)
    return items

def _extract_jtbd(md: str) -> str:
    for line in _extract_section(md, "Job-to-be-done"):
        line = line.strip()
        if line and not line.startswith("#"): return line
    return "未提供"

def _group_by_function(items):
    # Items are free-form prose; the LLM path emits Chinese, so the call
    # pattern is not anchored at offset 0 and the name must look like an
    # identifier. re.match(r"(\w+)\s*\(", ...) never matched "实现 add(a, b)…"
    # because \w+ consumed the CJK run and no "(" followed it.
    groups = {}
    for it in items:
        m = re.search(r"([A-Za-z_]\w*)\s*\(", it)
        if m:
            groups.setdefault(m.group(1), []).append(it)
    # Unnamed items are dropped so one resolvable interface does not get diluted
    # into an extra bogus "unknown" interface that poisons the entrypoint.
    if not groups:
        groups["unknown"] = list(items)
    return groups

def _infer_params(item: str) -> int:
    m = re.search(r"\(([^)]*)\)", item)
    if not m: return 0
    args = m.group(1).strip()
    return 0 if not args else len([a for a in args.split(",") if a.strip()])

def _build_interface(name, criteria):
    arity = max((_infer_params(c) for c in criteria), default=0)
    params = [chr(ord("a") + i) for i in range(arity)]
    return {"name": name, "input": [f"{p}: any" for p in params], "output": "any", "types_inferred": True, "acceptance_criteria": list(criteria)}

_UI_KEYWORDS = ["ui", "页面", "按钮", "界面"]
_CLI_KEYWORDS = ["cli", "命令行", "终端", "shell", "bash"]
_FUNCTION_KEYWORDS = ["函数", "function"]

def _decide_mode(items):
    text = " ".join(items).lower()
    if any(k in text for k in _UI_KEYWORDS): return "ui"
    # An identifier call or an explicit "function" phrase means a function task.
    # Checked before CLI because 参数 ("parameter") is an ordinary programming
    # word, not evidence of a command line, and it used to misclassify every
    # arithmetic task as cli.
    if re.search(r"[A-Za-z_]\w*\s*\(", text) or any(k in text for k in _FUNCTION_KEYWORDS):
        return "function"
    if any(k in text for k in _CLI_KEYWORDS): return "cli"
    return "function"

def _emit_minimal_spec(task_id: str) -> dict:
    return {"spec": {"task_id": task_id, "architecture": "single_module", "tech_stack": {"language": DEFAULT_LANGUAGE, "version": DEFAULT_VERSION, "framework": None, "test_framework": DEFAULT_TEST_FRAMEWORK}, "data_storage": "none", "interfaces": [], "verification": {"mode": "function", "entrypoint": "green_code.py:main"}}}

_HDL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_derived_contract(text: Any) -> dict | None:
    """`_parse_derived_contract_with_reason`, for callers that only want the result."""
    return _parse_derived_contract_with_reason(text)[0]


def _parse_derived_contract_with_reason(text: Any) -> tuple[dict | None, str]:
    """Pull `{module, ports}` out of a model response, or say why it could not.

    Validated rather than trusted: a module name that is not a legal Verilog
    identifier, or a port without a direction and width, would produce a
    testbench that cannot compile — and the failure would look like the design's
    fault rather than the contract's.

    The reason is carried out because the caller used to report "the spec has no
    interface": true, and silent about the model response that caused it. A
    parameterised-adder request died there with nothing to look at.
    """
    if not isinstance(text, str) or not text.strip():
        return None, "model_returned_nothing"
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, "model_output_contains_no_json_object"
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return None, "model_output_is_not_valid_json"
    if not isinstance(parsed, dict):
        return None, "model_output_is_not_an_object"
    module = parsed.get("module")
    if not isinstance(module, str) or not _HDL_IDENTIFIER.match(module):
        return None, "module_name_missing_or_not_a_verilog_identifier"
    ports = []
    rejected = []
    for port in parsed.get("ports") or []:
        if not isinstance(port, dict):
            rejected.append("a port that is not an object")
            continue
        name = port.get("name")
        direction = str(port.get("direction") or "").lower()
        width = port.get("width")
        if not isinstance(name, str) or not _HDL_IDENTIFIER.match(name):
            rejected.append(f"name {name!r} is not a Verilog identifier")
            continue
        if direction not in ("input", "output", "inout"):
            rejected.append(f"{name}: direction {direction!r} is not input/output/inout")
            continue
        try:
            width = int(width)
        except (TypeError, ValueError):
            # A parameterised requirement invites `"width": "WIDTH"`; without this
            # sentence the run only said "no port survived validation".
            rejected.append(f"{name}: width {port.get('width')!r} is not an integer")
            continue
        if width < 1 or width > 64:
            rejected.append(f"{name}: width {width} is outside 1..64")
            continue
        ports.append({"name": name, "direction": direction, "width": width})
    if not ports:
        return None, "no_port_survived_validation" + (
            f":{rejected[0]}" if rejected else "")
    if not any(p["direction"] == "input" for p in ports):
        return None, "contract_has_no_input_port"
    # A bidirectional port is checkable too: the bench connects it as a wire and
    # compares it when a case states a value, so an interface whose only driven
    # ports are `inout` (a bus transceiver) is not a contract with nothing to
    # verify. Requiring a plain `output` rejected exactly that design.
    if not any(p["direction"] in ("output", "inout") for p in ports):
        return None, "contract_has_nothing_the_design_drives"
    # The behaviour the requirement describes. Without it the planner only knows
    # the port list, and cases can only relate ports to each other — a request for
    # a rotating shift register was "verified" by a design that just loads its
    # input, because "每个时钟沿左移一位" never reached the planner.
    criteria = []
    raw_criteria = parsed.get("acceptance_criteria")
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if isinstance(item, str) and item.strip():
                criteria.append(item.strip())
    return {"module": module, "ports": ports, "acceptance_criteria": criteria[:6]}, ""


# A requirement doc that says "pure combinational logic, no clock or registers" is
# describing a combinational design, and a plain substring search read that as a
# sequential one — it refused the 1-bit full adder because the doc mentioned the
# clock in order to say there wasn't one.
_NEGATIONS = ("不", "无", "非", "没", "未")
_COMBINATIONAL_HINTS = ("组合逻辑", "组合电路", "纯组合", "combinational", "无时序")


def _mentions(hints: Any, text: str) -> bool:
    """Whether any hint occurs somewhere it is not being denied.

    Denials matter here: the hints are the words a requirement uses to say what
    the design must do, and "不包含时钟" uses the same word to say it must not.
    """
    for hint in hints:
        start = 0
        while True:
            index = text.find(hint, start)
            if index < 0:
                break
            window = text[max(0, index - 12):index]
            if not any(negation in window for negation in _NEGATIONS):
                return True
            start = index + len(hint)
    return False


def _describes_sequential(requirements_md: Any) -> bool:
    """Does the requirement describe behaviour that only a clock can produce?"""
    if not isinstance(requirements_md, str):
        return False
    text = requirements_md.lower()
    if any(hint in text for hint in _COMBINATIONAL_HINTS):
        return False
    return _mentions(_SEQUENTIAL_HINTS, text)


def _describes_bidirectional(requirements_md: Any) -> bool:
    """Does the requirement describe a wire the design both drives and reads?"""
    if not isinstance(requirements_md, str):
        return False
    text = requirements_md.lower()
    if any(hint in text for hint in ("单向", "不是双向", "非双向")):
        return False
    return _mentions(_BIDIRECTIONAL_HINTS, text)


_SIGNEDNESS_HINTS = ("有符号", "负数", "补码", "signed", "two's complement")


def _names_signedness(text: Any) -> bool:
    """Whether a piece of text says the numbers are signed."""
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in _SIGNEDNESS_HINTS)


# 模型没有按格式回答时值得再问一次：这不是「需求里缺了东西」，是这一次答坏了。
# 每一条都带着**说清哪里不对**的追问；只问一次，问不出就不再花预算。
_JSON_NOTE = (
    "\n\n注意：上一次的回复不是合法的 JSON。请**只**输出一个 JSON 对象，"
    "不要有任何解释文字，不要用 markdown 代码块包裹。"
)
_RETRY_NOTES = {
    "model_returned_nothing": _JSON_NOTE,
    "model_output_contains_no_json_object": _JSON_NOTE,
    "model_output_is_not_valid_json": _JSON_NOTE,
    "model_output_is_not_an_object": _JSON_NOTE,
    "module_name_missing_or_not_a_verilog_identifier": (
        "\n\n注意：`module` 必须是一个合法的 Verilog 标识符——字母或下划线开头，"
        "只含字母、数字、下划线，例如 `bus_driver_8bit`。上一次的回复里没有这样的名字。"
    ),
    "no_port_survived_validation": (
        "\n\n注意：每个端口都要写成 {\"name\": ..., \"direction\": \"input\"|\"output\"|\"inout\", "
        "\"width\": 整数}。`width` 必须是**整数**（需求给了参数默认值就写那个数字），"
        "上一次的回复里没有端口能通过校验。"
    ),
    "contract_has_no_input_port": (
        "\n\n注意：这个模块至少要有一个 `direction` 为 `input` 的端口。"
    ),
    "contract_has_nothing_the_design_drives": (
        "\n\n注意：这个模块至少要有一个 `direction` 为 `output` 或 `inout` 的端口。"
    ),
}


# 预算用光时**故意不加提示**：实测「请直接给出答案，不要长篇推理」会让这个模型想得
# 更久（同一提示词：不加提示推理 5521 给出 477 字正文，加了那句话推理 8192、正文为空）。
# 有用的是更大预算下的再一次独立抽取。
_REASONING_NOTE = ""


def _ask_again(llm, prompt: str, note: str, max_tokens: int | None = None) -> str | None:
    """One more attempt with the missing piece named, as raw text.

    Raw text, not a parsed contract: the caller re-parses it exactly like the
    first answer. Returning the parsed shape here meant the retry path and the
    success path produced different contracts — the retry's had no `interfaces`
    key, so a run whose second answer was good ended with a spec that declared no
    interface and said nothing about why (the 4-bit counter shape died that way).
    """
    try:
        response = (
            llm.chat(prompt + note, max_tokens=max_tokens)
            if max_tokens is not None else llm.chat(prompt + note)
        )
    except Exception:
        return None
    content = response.get("content") if isinstance(response, dict) else response
    return content if isinstance(content, str) else None


def _parse_or_none(content: str | None) -> dict | None:
    """The parsed contract from a retry, or None when it still is not usable."""
    if not isinstance(content, str):
        return None
    return _parse_derived_contract(content)


def _derive_contract(requirements_md: str, contract: dict, llm) -> tuple[dict | None, str]:
    """Ask the model for the module name and ports the requirement describes.

    The alternative — a contract written by hand for each design — is what the
    IC domain used to do, and it meant every request that was not a 4-bit adder
    was silently built and tested as one.
    """
    if llm is None:
        return None, "no_model_configured"
    prompt = f"""你在为一个数字 IC 设计任务推导接口契约。
根据下面的需求，给出模块名、端口列表，以及**需求描述的行为**（验收条件）。
严格按以下 JSON 格式返回，不要输出任何其他文字，不要用 markdown 代码块包裹：
{{
  "module": "multiplier_8bit",
  "ports": [
    {{"name": "a", "direction": "input", "width": 8}},
    {{"name": "b", "direction": "input", "width": 8}},
    {{"name": "product", "direction": "output", "width": 16}}
  ],
  "acceptance_criteria": ["product 等于 a 与 b 的乘积", "a=0 或 b=0 时 product=0"]
}}
规则：
1. `module` 必须是合法的 Verilog 标识符（字母/数字/下划线，不以数字开头）
2. 每个端口都要给出 `direction`（`input` / `output` / `inout`）与 `width`（位数，整数）
3. 位宽按需求里的数字确定；未说明时按运算结果的最大范围推导
4. 需求里说「双向」「总线」「inout」「收发」的端口，`direction` 必须是 `inout`，
   例如 {{"name": "io", "direction": "inout", "width": 8}}——写成 output 会让测试台
   去驱动一条本该由设计驱动的线
5. `acceptance_criteria` 是**这个电路该做什么**，2~5 条，每条一句话，必须能由端口和时钟
   周期判断真假。写行为，不要写「模块声明正确」这类套话。需求里提到时钟周期、移位、
   计数、状态、回绕、保持等时序行为时，必须如实写进去——用例只能验证写下来的行为，
   没写下来的行为不会被验证
6. 需求里点名的性质必须写进 `acceptance_criteria`：说「有符号」就要有一条写明「按补码
   解释、负数参与比较」，说「低电平复位」就要写明复位电平，说「参数化位宽」就要写明
   位宽取参数默认值。用例只按验收条件写；**需求里没写进验收条件的性质不会被验证**
7. `width` 必须是**整数**。需求里给了参数默认值（例如 `parameter WIDTH=8`）时写那个数字
   （`8`），不要写参数名——写成参数名的端口会被丢弃
8. 只输出一个 JSON 对象

需求：
{requirements_md}"""
    try:
        response = llm.chat(prompt)
    except Exception as exc:
        return None, f"model_call_failed:{type(exc).__name__}"
    content = response.get("content") if isinstance(response, dict) else response
    if not (isinstance(content, str) and content.strip()):
        # "The model returned nothing" hides a budget problem behind a model
        # problem. ds4.1f is a reasoning model: with the old 1024-token default it
        # spent the whole budget thinking and never wrote the answer.
        from llm_client import describe_empty_answer

        why = "model_returned_nothing" + describe_empty_answer(response)
        if "budget_spent_on_reasoning" not in why:
            return None, why
        # How long it thinks is not fixed — the same prompt took 3400 tokens of
        # reasoning once and 5000 the next — so the budget cannot simply be set high
        # enough. Ask again with double, then with quadruple, and stop at the first
        # answer that arrives.
        base = int(getattr(llm, "max_tokens", 8192) or 8192)
        for budget in (max(base * 2, 16384), max(base * 4, 32768)):
            content = _ask_again(llm, prompt, _REASONING_NOTE, max_tokens=budget)
            if content and content.strip():
                response = {"content": content}
                break
        else:
            return None, why
    derived, why = _parse_derived_contract_with_reason(content)
    if derived is None and why.partition(":")[0] in _RETRY_NOTES:
        # A small local model answers a structured question badly often enough that
        # giving up on the first try wastes the whole request: the parameterised
        # adder died with "the model output is not valid JSON", the bidirectional
        # bus with "the module name is not a Verilog identifier", and nothing was
        # asked again. One more attempt, saying what was wrong with the first.
        content = _ask_again(llm, prompt, _RETRY_NOTES[why.partition(":")[0]])
        if content is not None:
            # Re-parse and carry on through the same validation and normalisation
            # as the first answer, so both paths end in the same contract shape.
            derived, why = _parse_derived_contract_with_reason(content)
    if derived is None:
        return None, why
    # The contract is the only channel from the requirement to the planner: a
    # property the requirement names and the criteria omit is never verified. The
    # signed comparator's four cases were all non-negative because "有符号" reached
    # neither the criteria nor the cases.
    if _names_signedness(requirements_md) and not _names_signedness(
        " ".join(derived["acceptance_criteria"])
    ):
        retry = _parse_or_none(_ask_again(llm, prompt, (
            "\n\n注意：需求里说的是**有符号**数，`acceptance_criteria` 里必须有一条写明"
            "「按补码解释、负数参与比较」，并且给出一个具体的判断例子（例如「a=-1、b=0 时"
            "a 小于 b」）——用例是按验收条件写的，验收条件里没有这条，用例就只会写非负数，"
            "一个按无符号实现的电路会全部通过。"
        )))
        if retry is not None and _names_signedness(
            " ".join(retry["acceptance_criteria"])
        ):
            derived = retry
    # A clocked requirement whose contract has no clock cannot be verified: every
    # case would compare the output after zero edges, which is how a run ends up
    # reporting nothing checked at all. Ask once more, saying what was missing —
    # the model is usually right about everything except the port it left out.
    if _describes_sequential(requirements_md) and not clock_ports(derived["ports"]):
        retry = _parse_or_none(_ask_again(llm, prompt, (
            "\n\n注意：这个需求描述的是**时序**行为（寄存器/计数/状态/移位），端口里必须"
            "包含时钟，例如 {{\"name\": \"clk\", \"direction\": \"input\", \"width\": 1}}"
            "——没有时钟的契约无法验证它描述的行为。"
        )))
        if retry is not None and clock_ports(retry["ports"]):
            derived = retry
        else:
            # A clockless contract for a sequential requirement cannot be
            # verified: every case would compare the output after zero edges.
            return None, ("requirement_describes_sequential_behaviour_but_the_"
                          "contract_has_no_clock")
    # Same shape of defect, same answer: a tri-state bus whose contract has no
    # inout port leaves the bench with nothing it is allowed to drive, and the run
    # reports nothing checked at all.
    if _describes_bidirectional(requirements_md) and not [
        p for p in derived["ports"] if p.get("direction") == "inout"
    ]:
        retry = _parse_or_none(_ask_again(llm, prompt, (
            "\n\n注意：这个需求描述的是**双向/三态总线**，必须有一个 `direction` 为 "
            "`inout` 的端口（例如 {{\"name\": \"io\", \"direction\": \"inout\", "
            "\"width\": 8}}）——写成 input 或 output 会让测试台没法驱动它。"
        )))
        if retry is not None and [
            p for p in retry["ports"] if p.get("direction") == "inout"
        ]:
            derived = retry
        else:
            # Two answers in a row without the port the requirement names. Going
            # on with this contract means verifying a different circuit: the
            # bidirectional-bus shape was "verified" by a contract of
            # enable/data_in/data_out, and nothing in the run said so.
            return None, ("requirement_names_a_bidirectional_port_but_the_"
                          "contract_has_none")
    ports = derived["ports"]
    inputs = [f"{p['name']}: {p['width']}bit" for p in ports if p["direction"] == "input"]
    outputs = [f"{p['name']}: {p['width']}bit" for p in ports if p["direction"] == "output"]
    bidirectional = [f"{p['name']}: {p['width']}bit" for p in ports if p["direction"] == "inout"]
    module = derived["module"]
    # Every field comes from the domain's declaration: this Agent must not carry
    # HDL-specific literals, or a second domain would inherit the wrong defaults.
    derived_contract = {
        "interfaces": [{
            "name": module,
            "input": inputs,
            "output": outputs,
            "bidirectional": bidirectional,
            "ports": ports,
            "acceptance_criteria": list(
                derived.get("acceptance_criteria")
                or contract.get("acceptance_criteria")
                or ["module 声明正确", "包含逻辑赋值", "endmodule 闭合"]
            ),
        }],
        "verification": {
            "mode": contract.get("verification", {}).get("mode") or "cli",
            "entrypoint": f"{module}.{contract.get('language') or 'v'}:{module}",
            "ports": ports,
            "derived": True,
        },
    }
    for key in ("architecture", "language", "version", "test_framework"):
        if contract.get(key):
            derived_contract[key] = contract[key]
    capability = (contract.get("verification") or {}).get("capability")
    if capability:
        derived_contract["verification"]["capability"] = capability
    return derived_contract, ""


def _spec_from_contract(task_id: str, contract: dict) -> dict:
    """Build a spec from a domain's declared contract.

    The contract is data, so adding a second IC fixture is a change to
    domains/domains.json rather than a new branch in this Agent.
    """
    interfaces = []
    for interface in contract.get("interfaces") or []:
        if not isinstance(interface, dict):
            continue
        interfaces.append({
            "name": interface.get("name"),
            "input": list(interface.get("input") or []),
            "output": list(interface.get("output") or []),
            # A bidirectional port belongs in the interface description too:
            # leaving it out made a bus transceiver look like a module with one
            # input and nothing it drives.
            "bidirectional": list(interface.get("bidirectional") or []),
            "types_inferred": True,
            "acceptance_criteria": list(interface.get("acceptance_criteria") or []),
        })
    verification = contract.get("verification") or {}
    # Carry every declared verification key through, not just the two the old
    # hardcoded branch happened to set. Dropping `capability` here meant the
    # domain declared a verification mechanism that never reached the gate.
    resolved_verification = {
        "mode": verification.get("mode") or "function",
        "entrypoint": verification.get("entrypoint") or "green_code.py:main",
    }
    for key, value in verification.items():
        resolved_verification.setdefault(key, value)
    return {
        "task_id": task_id,
        "architecture": contract.get("architecture") or "single_module",
        "tech_stack": {
            "language": contract.get("language") or DEFAULT_LANGUAGE,
            "version": contract.get("version") or DEFAULT_VERSION,
            "framework": None,
            "test_framework": contract.get("test_framework") or DEFAULT_TEST_FRAMEWORK,
        },
        "data_storage": "none",
        "interfaces": interfaces,
        "verification": resolved_verification,
    }


def sdd_spec_builder(
    requirements_md: Any,
    preference_pack: Any,
    llm=None,
    domain_config: Any = None,
) -> dict:
    if not isinstance(requirements_md, str) or not isinstance(preference_pack, dict):
        return _emit_minimal_spec(_resolve_task_id(requirements_md))
    task_id = _resolve_task_id(requirements_md)

    # A declared domain contract decides the spec shape. This used to be a
    # hardcoded "verilog"/"加法器" branch inside this Agent, which meant the
    # domain declaration could not influence anything.
    contract = domain_config.get("contract") if isinstance(domain_config, dict) else None
    if isinstance(contract, dict) and contract:
        # A contract with no fixed `interfaces` means "derive them from the
        # requirement". A contract that lists them keeps them, so a domain can
        # still pin one design deliberately.
        if not contract.get("interfaces"):
            derived, why = _derive_contract(requirements_md, contract, llm)
            if derived is not None:
                return {"spec": _spec_from_contract(task_id, derived)}
            spec = _spec_from_contract(task_id, contract)
            # Say why the contract could not be derived: the next stage only sees
            # "the spec declares no interface", which is the symptom.
            if why:
                spec["contract_derivation_failed"] = why
            return {"spec": spec}
        return {"spec": _spec_from_contract(task_id, contract)}
    functional = _extract_items(requirements_md, "Functional")
    jtbd = _extract_jtbd(requirements_md)
    if not functional and jtbd == "未提供": return _emit_minimal_spec(task_id)
    # 这是**模型输出进 spec 的那个口**：preference_pack 是模型给的，`language` 也是
    # 模型填的。live run 65390e806f81 里它填的是 "chinese" —— 一个不是编程语言的
    # 值，却因为 `or` 只挡 falsy 而进了 spec，一路漏到 tdd_coder 变成一个 stub。
    language = normalize_language(preference_pack.get("language"), DEFAULT_LANGUAGE)
    groups = _group_by_function(functional)
    interfaces = [_build_interface(n, c) for n, c in groups.items()]
    mode = _decide_mode(functional)
    entrypoint = (f"{MODULE_NAME_DEFAULT}.py:{interfaces[0]['name']}" if interfaces else f"{MODULE_NAME_DEFAULT}.py:main")
    return {"spec": {"task_id": task_id, "architecture": "single_module" if len(interfaces) <= 1 else "layered", "tech_stack": {"language": language, "version": DEFAULT_VERSION, "framework": None, "test_framework": DEFAULT_TEST_FRAMEWORK}, "data_storage": "none", "interfaces": interfaces, "verification": {"mode": mode, "entrypoint": entrypoint}}}
