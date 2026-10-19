import json
from typing import Any

def reporter_agent(data: Any, llm=None) -> dict:
    if not isinstance(data, dict):
        return {"reporter": {"report": "", "stub": True}}
    data_str = json.dumps(data, ensure_ascii=False)
    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": "根据以下数据生成报告，返回 JSON: {\"report\": \"...\", \"sections\": [...]}" + '\n输入数据：' + json.dumps(data, ensure_ascii=False, default=str)}], max_tokens=512)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            return {"reporter": {"report": parsed.get("report", ""), "sections": parsed.get("sections", []), "stub": False}}
        except Exception:
            pass
    try:
        with open("/tmp/report.md", "w") as fh:
            fh.write(f"# Report\n\n{data_str}\n")
        # 这两条都是兜底：模板拼的报告和「按 N 个字生成的报告」都不是模型写的，
        # `stub` 得说实话。（从前它们都写 False，因为「输入是个 dict」。）
        return {"reporter": {"report": f"# Report\n\n{data_str[:500]}", "sections": ["summary", "details"], "stub": True}}
    except Exception:
        return {"reporter": {"report": f"Report based on {len(data_str)} chars", "stub": True}}