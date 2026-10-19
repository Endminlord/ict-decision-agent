import json, ast
from typing import Any

def content_processor(content: Any, llm=None) -> dict:
    if not isinstance(content, str):
        return {"content_processor": {"processed": "", "length": 0, "stub": True}}
    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": '处理以下内容，返回结构化 JSON: {"summary": "...", "key_points": [...]}' + '\n输入数据：' + json.dumps(content, ensure_ascii=False, default=str)}], max_tokens=256)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            return {"content_processor": {"processed": json.dumps(parsed, ensure_ascii=False), "length": len(content), "stub": False}}
        except Exception:
            pass
    try:
        tree = ast.parse(content)
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        summary = {"functions": func_names, "classes": class_names, "lines": content.count('\n') + 1}
        return {"content_processor": {"processed": json.dumps(summary, ensure_ascii=False), "length": len(content), "stub": False}}
    except SyntaxError:
        return {"content_processor": {"processed": content[:200], "length": len(content), "stub": False}}
