import hashlib
import json
from typing import Any

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(spec: Any) -> str:
    if isinstance(spec, dict):
        tid = spec.get("task_id")
        if isinstance(tid, str) and tid:
            return tid
    try:
        return _hash_id(spec)
    except Exception:
        return "unknown"

def _emit_minimal(task_id: str) -> dict:
    return {"trd": {"task_id": task_id, "decisions": [], "tradeoffs": [], "tradeoffs_stub": True},
            "adr": {"task_id": task_id, "context": "未提供", "decisions": [],
                    "alternatives": [], "consequences": [], "alternatives_stub": True}}

def _build_decisions(spec: dict):
    decisions = []
    ts = spec.get("tech_stack", {})
    if isinstance(ts, dict) and ts.get("language"):
        decisions.append({"id": f"D{len(decisions)+1}", "decision": f"使用 {ts.get('language')} {ts.get('version','')}".strip(), "reason": "来自 preference_pack"})
    arch = spec.get("architecture")
    if arch:
        decisions.append({"id": f"D{len(decisions)+1}", "decision": f"{arch} 架构", "reason": "接口数量决定架构粒度"})
    ds = spec.get("data_storage")
    if ds:
        decisions.append({"id": f"D{len(decisions)+1}", "decision": f"数据存储：{ds}", "reason": "Constraints 排除或未提持久化" if ds == "none" else "需求要求持久化"})
    ifaces = spec.get("interfaces", [])
    if isinstance(ifaces, list) and ifaces:
        names = [i.get("name") for i in ifaces if isinstance(i, dict) and i.get("name")]
        if names:
            decisions.append({"id": f"D{len(decisions)+1}", "decision": f"接口设计：{', '.join(names)}", "reason": "来自 SPEC interfaces"})
    return decisions

def _build_consequences(dec: dict):
    text = dec.get("decision", "").lower()
    cons = []
    if "none" in text or "无数据存储" in text: cons.append("无持久化开销")
    if "single_module" in text: cons.append("易测")
    if "layered" in text: cons.append("可扩展")
    if "使用" in dec.get("decision", ""): cons.append("零依赖" if "python" in text else "依赖目标语言")
    return cons or ["无额外后果"]

def _build_context(spec: dict) -> str:
    arch = spec.get("architecture", "unknown")
    ts = spec.get("tech_stack", {})
    lang = ts.get("language", "unknown") if isinstance(ts, dict) else "unknown"
    names = [i.get("name") for i in spec.get("interfaces", []) if isinstance(i, dict) and i.get("name")]
    return f"{arch} 架构，语言 {lang}，接口：{', '.join(names) if names else '无接口'}"

def trd_adr_generator(spec: Any, llm=None) -> dict:
    if not isinstance(spec, dict): return _emit_minimal(_resolve_task_id(spec))
    if not spec.get("architecture") and not spec.get("tech_stack"): return _emit_minimal(_resolve_task_id(spec))
    task_id = _resolve_task_id(spec)
    if llm is not None:
        try:
            prompt = f"""你是一个技术架构师。请根据以下 SPEC 数据，生成技术决策记录。
严格按以下 JSON 格式返回，不要包含 markdown 代码块标记，不要解释：
{{
  "decisions": [{{"id": "D1", "decision": "决策内容", "reason": "理由"}}],
  "context": "系统上下文描述"
}}
SPEC数据：{json.dumps(spec, ensure_ascii=False)}"""
            llm_response = llm.chat(prompt)
            clean_json = (llm_response.get('content', '') if isinstance(llm_response, dict) else llm_response).strip()
            if clean_json.startswith("```json"): clean_json = clean_json[7:]
            if clean_json.endswith("```"): clean_json = clean_json[:-3]
            parsed = json.loads(clean_json.strip())
            decisions = parsed.get("decisions", [])
            context = parsed.get("context", _build_context(spec))
            trd = {"task_id": task_id, "decisions": decisions, "tradeoffs": [], "tradeoffs_stub": True}
            adr = {"task_id": task_id, "context": context, "decisions": [d["decision"] for d in decisions if isinstance(d, dict)], "alternatives": [[] for _ in decisions], "consequences": [_build_consequences(d) for d in decisions if isinstance(d, dict)], "alternatives_stub": True}
            return {"trd": trd, "adr": adr}
        except Exception: pass
    decisions = _build_decisions(spec)
    trd = {"task_id": task_id, "decisions": decisions, "tradeoffs": [], "tradeoffs_stub": True}
    adr = {"task_id": task_id, "context": _build_context(spec), "decisions": [d["decision"] for d in decisions], "alternatives": [[] for _ in decisions], "consequences": [_build_consequences(d) for d in decisions], "alternatives_stub": True}
    return {"trd": trd, "adr": adr}
