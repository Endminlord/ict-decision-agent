import json, subprocess, tempfile, os, ast
from typing import Any

def code_smith(code: Any, llm=None) -> dict:
    if not isinstance(code, dict):
        return {"code_smith": {"refactored": "", "stub": True}}
    code_str = code.get("code", "")
    if not isinstance(code_str, str) or not code_str.strip():
        return {"code_smith": {"refactored": "", "stub": True}}

    refactored = code_str
    improvements = []
    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": '优化以下代码，返回 JSON: {"refactored": "...", "improvements": [...]}' + '\n输入数据：' + json.dumps(code, ensure_ascii=False, default=str)}], max_tokens=512)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            refactored = parsed.get("refactored", code_str)
            improvements = parsed.get("improvements", [])
        except Exception:
            pass

    if isinstance(refactored, str) and refactored.strip():
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "refactored.py")
            with open(file_path, "w") as f:
                f.write(refactored)
            result = subprocess.run(
                ["python3", "-m", "py_compile", file_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                improvements.append("refactor_introduced_syntax_error")
                try:
                    tree = ast.parse(code_str)
                    if tree:
                        refactored = code_str
                        improvements = ["refactor_rejected_unsafe"]
                except SyntaxError:
                    pass

    return {"code_smith": {"refactored": refactored, "improvements": improvements, "stub": False}}
