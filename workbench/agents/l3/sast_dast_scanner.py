import json, subprocess, tempfile, os, ast
from typing import Any
from runtime.security_scanner import scan_code

def sast_dast_scanner(code: Any, llm=None) -> dict:
    if not isinstance(code, dict):
        return {"sast_dast_scanner": {"verdict": "fail", "findings": ["input_not_dict"], "stub": True}}
    code_str = code.get("code", "")
    language = code.get("language", "python")
    base = scan_code(code_str, language)

    if isinstance(code_str, str) and code_str.strip() and language == "python":
        try:
            tree = ast.parse(code_str)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func_name = ""
                    if isinstance(node.func, ast.Name):
                        func_name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        func_name = node.func.attr
                    if func_name in ("eval", "exec", "__import__", "compile"):
                        base["findings"].append({"line": getattr(node, "lineno", -1), "function": func_name, "severity": "critical"})
            if any(f.get("severity") == "critical" for f in base["findings"]):
                base["verdict"] = "fail"
        except SyntaxError:
            pass

    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": '对以下代码进行安全审计，返回 JSON: {"findings": [...], "severity": "..."}' + '\n输入数据：' + json.dumps(code, ensure_ascii=False, default=str)}], max_tokens=256)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            base["llm_findings"] = parsed.get("findings", [])
        except Exception:
            pass

    return {"sast_dast_scanner": {"verdict": base["verdict"], "findings": base["findings"], "scanned": base["scanned"], "stub": False}}
