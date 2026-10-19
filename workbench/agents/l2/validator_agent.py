import json, ast
from typing import Any

def validator_agent(data: Any, llm=None) -> dict:
    if not isinstance(data, dict):
        return {"validator": {"valid": False, "errors": ["input_not_dict"], "stub": True}}
    code_str = data.get("code", "")
    errors = []
    if isinstance(code_str, str) and code_str.strip():
        try:
            ast.parse(code_str)
        except SyntaxError as e:
            errors.append(f"syntax_error: {e}")
    if not errors and llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": '验证以下数据是否符合规范，返回 JSON: {"valid": bool, "errors": [...]}' + '\n输入数据：' + json.dumps(data, ensure_ascii=False, default=str)}], max_tokens=256)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            errors = parsed.get("errors", [])
            valid = parsed.get("valid", True) and len(errors) == 0
        except Exception:
            pass
    if not errors and code_str and not isinstance(code_str, str):
        errors.append("code_not_string")
    return {"validator": {"valid": len(errors) == 0, "errors": errors, "stub": False}}
