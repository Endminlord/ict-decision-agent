import hashlib
import json
from typing import Any

DEV_KEYWORDS = ["函数", "代码", "实现", "开发", "接口", "api", "bug", "重构", "测试"]
OFFICE_KEYWORDS = ["文档", "邮件", "会议", "纪要", "汇报", "ppt", "总结"]

STAGES = ["S0", "S1", "S2", "S3", "S5", "S6", "S7"]
EDGES = [["S0","S1"],["S1","S2"],["S2","S3"],["S3","S5"],["S5","S6"],["S6","S7"]]
HARD_ROLLBACK_EDGES = [
    {"from": "TDD Reviewer", "from_stage": "S5", "to": "TDD Coder", "to_stage": "S5"},
    {"from": "e2e-validator", "from_stage": "S6", "to": ["TDD Planner","TDD Coder"], "to_stage": "S5"},
]

def _hash_id(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]

def _detect_intent(user_input: str) -> str:
    text = user_input.lower()
    has_dev = any(k in text for k in DEV_KEYWORDS)
    has_office = any(k in text for k in OFFICE_KEYWORDS)
    if has_dev and has_office: return "mixed"
    if has_dev: return "development"
    if has_office: return "office"
    return "unknown"

def _detect_scale(raw_input: str, intent: str) -> str:
    if isinstance(raw_input, str) and len(raw_input) > 200: return "large"
    if intent == "mixed": return "large"
    return "small"

def orchestrate(user_input: str, timestamp: str, registry: Any = None, llm=None, forced_domain: Any = None) -> dict:
    task_id = _hash_id(user_input)
    
    # 默认规则逻辑
    intent = _detect_intent(user_input)
    scale = _detect_scale(user_input, intent)
    
    # 阶段2 P1: 优先用 LLM 进行意图识别
    if llm is not None:
        try:
            prompt = f"""你是一个意图分类器。请分析用户输入，判断意图和规模。
严格按以下 JSON 格式返回，不要包含 markdown 代码块标记，不要解释：
{{"intent": "development", "scale": "small"}}
意图可选值：development (开发代码), office (办公文档), mixed (混合), unknown (未知)
规模可选值：small (短输入), large (长输入或混合)
用户输入：{user_input}"""
            llm_response = llm.chat(prompt)
            clean_json = (llm_response.get('content', '') if isinstance(llm_response, dict) else llm_response).strip()
            if clean_json.startswith("```json"): clean_json = clean_json[7:]
            if clean_json.endswith("```"): clean_json = clean_json[:-3]
            parsed = json.loads(clean_json.strip())
            if parsed.get("intent") in ["development", "office", "mixed", "unknown"]:
                intent = parsed["intent"]
                scale = parsed.get("scale", _detect_scale(user_input, intent))
        except Exception:
            pass # LLM 失败或格式错误，保留规则判定的 intent 和 scale

    domain_id = "default"
    degraded_domain = None
    domain_match = None
    # 领域是「用户点名要的」还是「关键词碰上的」——这两件事在报告里必须分得开。
    # 分不开的话，一次自动识别失败的运行会看起来像用户自己的选择。
    domain_source = "matched"
    if isinstance(forced_domain, str) and forced_domain.strip():
        domain_source = "requested"
        # --domain pins the routing instead of letting keywords decide.
        domain_id = forced_domain.strip()
        if registry is not None and hasattr(registry, "get"):
            declared = registry.get(domain_id)
            if declared is None:
                degraded_domain = f"unknown_domain:{domain_id}"
            else:
                domain_match = {"domain_id": domain_id, "domain": declared.to_dict()}
    elif registry is not None and hasattr(registry, "match"):
        try:
            m = registry.match(user_input)
            if isinstance(m, dict):
                domain_match = m
                domain_id = m.get("domain_id") or "default"
                degraded_domain = m.get("degraded_reason")
        except Exception:
            domain_id = "default"
            degraded_domain = "registry_match_failed"

    task_dag = {
        "task_id": task_id, "domain_id": domain_id, "intent": intent, "scale": scale,
        "stages": list(STAGES), "edges": [list(e) for e in EDGES],
        "hard_rollback_edges": [dict(e) for e in HARD_ROLLBACK_EDGES],
        "l2_parallel": {"S4_research": {"blocking": False}}, "raw_input": user_input,
    }
    task_dag["domain_source"] = domain_source
    if degraded_domain: task_dag["degraded_domain"] = degraded_domain

    domain_config = {"domain_id": domain_id, "namespace": f"domain/{domain_id}"}
    if isinstance(domain_match, dict) and isinstance(domain_match.get("domain"), dict):
        declaration = domain_match["domain"]
        # Carry the declaration, not just the id: downstream stages need the
        # contract and prompts to act on the domain at all.
        for key in ("name", "description", "prompts", "contract"):
            if key in declaration:
                domain_config[key] = declaration[key]

    context_snapshot = {
        "task_id": task_id, "user_prefs": {},
        "domain_config": domain_config,
        "timestamp": timestamp,
    }

    result = {"task_dag": task_dag, "context_snapshot": context_snapshot}
    if intent == "unknown":
        result["rollback_signal"] = {"target": "Claude Cowork 澄清", "auto": True, "emit_signal_only": True, "reason": "intent_unknown"}
    return result
