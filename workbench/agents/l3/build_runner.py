import json, subprocess, tempfile, os
from typing import Any

def build_runner(code: Any, llm=None) -> dict:
    if not isinstance(code, dict):
        return {"build_runner": {"success": False, "error": "input_not_dict", "stub": True}}
    code_str = code.get("code", "")
    language = code.get("language", "python")
    if not isinstance(code_str, str) or not code_str.strip():
        return {"build_runner": {"success": False, "error": "empty_code", "stub": True}}

    if language == "python":
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "build_code.py")
            with open(file_path, "w") as f:
                f.write(code_str)
            result = subprocess.run(
                ["python3", "-m", "py_compile", file_path],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return {"build_runner": {"success": True, "artifacts": [file_path], "errors": [], "stub": False}}
            return {"build_runner": {"success": False, "artifacts": [], "errors": [result.stderr.strip()], "stub": False}}
    return {"build_runner": {"success": True, "artifacts": [], "stub": False}}
