import json
from typing import Any

def planner_agent(spec: Any, llm=None) -> dict:
    if not isinstance(spec, dict):
        return {"planner": {"plan": [], "spec_id": str(spec), "stub": True}}
    spec_text = json.dumps(spec, ensure_ascii=False)
    steps = []
    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": "根据以下技术规格，生成实现计划。返回 JSON: {\"steps\": [{...}]}" + '\n输入数据：' + json.dumps(spec, ensure_ascii=False, default=str)}], max_tokens=512)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            steps = parsed.get("steps", [])
        except Exception:
            pass
    if not steps:
        # 兜底计划。`stub` 说的是**这份计划是谁给的**，不是「输入是不是个 dict」——
        # 从前它恒为 False，于是「模型答的」和「内置的三步」看起来一模一样。
        steps = [{"step": "analyze_spec", "order": 1, "tool": "ast_parse"},
                 {"step": "generate_code", "order": 2, "tool": "tdd_coder"},
                 {"step": "verify", "order": 3, "tool": "py_compile"}]
        return {"planner": {"plan": steps, "spec_id": spec.get("task_id", ""), "stub": True}}
    return {"planner": {"plan": steps, "spec_id": spec.get("task_id", ""), "stub": False}}