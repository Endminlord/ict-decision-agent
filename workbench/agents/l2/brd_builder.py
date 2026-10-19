import json
from typing import Any

def brd_builder(task_dag: Any, llm=None) -> dict:
    if not isinstance(task_dag, dict):
        return {"brd": {"title": "", "sections": [], "stub": True}}
    raw = task_dag.get("raw_input", "")
    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": "根据用户需求生成业务需求文档(BRD)。返回 JSON: {\"title\": \"...\", \"sections\": [...]}" + '\n输入数据：' + json.dumps(task_dag, ensure_ascii=False, default=str)}], max_tokens=512)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            return {"brd": {"title": parsed.get("title", ""), "sections": parsed.get("sections", []), "stub": False}}
        except Exception:
            pass
    try:
        with open("/tmp/brd.md", "w") as fh:
            fh.write(f"# BRD\n\n{raw}\n")
        return {"brd": {"title": raw[:50] if raw else "Untitled", "sections": ["requirements", "scope", "timeline"], "stub": False}}
    except Exception:
        return {"brd": {"title": raw[:50], "sections": ["requirements"], "stub": False}}