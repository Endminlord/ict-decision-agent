import hashlib
import json
from typing import Any


def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]


def _resolve_task_id(prd: Any) -> str:
    if isinstance(prd, dict):
        tid = prd.get("task_id")
        if isinstance(tid, str) and tid:
            return tid
    try:
        return _hash_id(prd)
    except Exception:
        return "unknown"


def _extract_jtbd(prd: dict) -> str:
    us = prd.get("user_stories", [])
    if isinstance(us, list) and us and isinstance(us[0], dict):
        w = us[0].get("want")
        if isinstance(w, str) and w.strip():
            return w
    return "未提供"


def _extract_functional(prd: dict):
    ac = prd.get("acceptance_criteria", [])
    if isinstance(ac, list) and ac:
        return [str(x) for x in ac if x is not None]
    us = prd.get("user_stories", [])
    if isinstance(us, list):
        return [str(u.get("want")) for u in us
                if isinstance(u, dict) and u.get("want")]
    return []


def _extract_constraints(prd: dict):
    scope = prd.get("scope", {})
    if not isinstance(scope, dict):
        return []
    out = scope.get("out", [])
    return [str(x) for x in out if x is not None] if isinstance(out, list) else []


def _render_md(task_id, jtbd, functional, nf, constraints):
    lines = [f"<!-- task_id: {task_id} -->", "", "# Requirements", "",
             "## Job-to-be-done", jtbd, "", "## Functional"]
    lines += [f"- F{i}: {f}" for i, f in enumerate(functional, 1)] if functional else ["- 无"]
    lines += ["", "## Non-functional"]
    lines += [f"- NF{i}: {x}" for i, x in enumerate(nf, 1)] if nf else ["- 无"]
    lines += ["", "## Constraints"]
    lines += [f"- C{i}: {c}" for i, c in enumerate(constraints, 1)] if constraints else ["- 无"]
    lines.append("")
    return "\n".join(lines)


def req_writer(prd: Any, llm=None) -> dict:
    if not isinstance(prd, dict):
        tid = _resolve_task_id(prd)
        return {"requirements_md": _render_md(tid, "未提供", [], [], [])}
    
    task_id = _resolve_task_id(prd)
    
    # 阶段2 P1: 如果传入了 LLM，尝试用 LLM 生成更完善的需求文档
    if llm is not None:
        try:
            prompt = f"""你是一个资深需求分析师。请根据以下 PRD 数据，生成 JSON 格式的 Requirements。
严格按以下 JSON 格式返回，不要包含 markdown 代码块标记，不要解释：
{{
  "jtbd": "一句话描述核心任务",
  "functional": ["reverse(s) 返回反转后的字符串", "max_of(items) 空列表返回 None"],
  "constraints": ["约束1", "约束2"]
}}
每条 functional **必须包含可调用的函数签名**（函数名加括号与参数），例如
`reverse(s)`。下游据此确定接口名、参数个数与入口点。只写自然语言描述而不写
签名，会让整条链无法生成可执行的测试用例，任务直接失败。
PRD数据：{json.dumps(prd, ensure_ascii=False)}"""
            llm_response = llm.chat(prompt)
            clean_json = (llm_response.get('content', '') if isinstance(llm_response, dict) else llm_response).strip()
            if clean_json.startswith("```json"): clean_json = clean_json[7:]
            if clean_json.endswith("```"): clean_json = clean_json[:-3]
            parsed = json.loads(clean_json.strip())
            
            jtbd = parsed.get("jtbd", _extract_jtbd(prd))
            functional = parsed.get("functional", _extract_functional(prd))
            constraints = parsed.get("constraints", _extract_constraints(prd))
            return {"requirements_md": _render_md(task_id, jtbd, functional, [], constraints)}
        except Exception:
            # LLM 调用失败，静默回退到阶段1的规则提取
            pass

    # 阶段1 纯规则回退路径
    jtbd = _extract_jtbd(prd)
    functional = _extract_functional(prd)
    constraints = _extract_constraints(prd)
    if not functional and jtbd == "未提供":
        return {"requirements_md": _render_md(task_id, "未提供", [], [], [])}
    return {"requirements_md": _render_md(task_id, jtbd, functional, [], constraints)}
