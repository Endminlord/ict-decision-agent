import hashlib
import json
from typing import Any

from runtime.memory import summarize_for_prompt

DEFAULT_PREFERENCE = {
    "code_style": "pythonic, type hints",
    "doc_template": "google_style",
    "naming": "snake_case",
    "test_framework": "pytest",
    "language": "python",
}
FALLBACK_NAMESPACE = "domain/default"

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_namespace(cs: Any) -> str:
    if not isinstance(cs, dict): return FALLBACK_NAMESPACE
    ns = cs.get("domain_config", {})
    if not isinstance(ns, dict): return FALLBACK_NAMESPACE
    return ns.get("namespace") or FALLBACK_NAMESPACE

def _resolve_task_id(td: Any) -> str:
    if isinstance(td, dict):
        tid = td.get("task_id")
        if isinstance(tid, str) and tid: return tid
        try: return _hash_id(td)
        except Exception: return "unknown"
    return _hash_id(td)


def _recall(memory: Any, raw_input: Any, domain_id: Any, task_id: Any) -> list:
    """从记忆里找同类任务。没装记忆、或者记忆坏了，都只是没有 refs，不是错误。

    **不排除当前 task_id**。`task_id` 是从提示词派生的，所以「同一句话再问一次」
    的两次运行 task_id 相同——而那恰恰是最该被记起来的一条。真正会变成回音壁的是
    「运行一开始就写记忆」，而记忆是在运行**结束**时才写的（`chain._persist`），
    所以按 task_id 排除只会误伤：它把最有用的那条挡在门外，什么也没防住。
    """
    if memory is None:
        return []
    try:
        refs = memory.search(raw_input, domain_id=domain_id)
    except Exception:
        # 记忆是旁路：它坏了只该让这次没有 refs，不该让运行失败。
        return []
    # 同一句话的既往运行，task_id 会和这次一样。标出来，读者才知道那是上一次而不是这一次。
    for ref in refs:
        if isinstance(ref, dict):
            ref["same_task_id"] = ref.get("task_id") == task_id
    return refs


def hermes(task_dag: Any, context_snapshot: Any, llm=None, memory=None) -> dict:
    if not isinstance(task_dag, dict) or not isinstance(context_snapshot, dict):
        tid = _resolve_task_id(task_dag)
        return {
            "preference_pack": {"task_id": tid, **DEFAULT_PREFERENCE, "domain_prefs": {}},
            "memory_index": {"task_id": tid, "refs": [], "namespace": FALLBACK_NAMESPACE},
            "risk_signal": {"task_id": tid, "risks": [], "severity": "none"},
        }

    task_id = _resolve_task_id(task_dag)
    namespace = _resolve_namespace(context_snapshot)
    raw_input = task_dag.get("raw_input", "")
    domain_id = task_dag.get("domain_id")

    # 跨会话记忆：同类任务的既往决定。**只收我们有默认值的键**——记忆里还存着
    # status / verification_level 这类记账字段，它们不是偏好。
    refs = _recall(memory, raw_input, domain_id, task_id)
    if any(ref.get("same_task_id") for ref in refs if isinstance(ref, dict)):
        for ref in refs:
            if isinstance(ref, dict) and ref.get("same_task_id"):
                ref["reason"] = f"{ref.get('reason') or ''} + 同一句需求的既往运行".strip(" +")
    recalled: dict = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        settled = ref.get("settled")
        if not isinstance(settled, dict):
            continue
        for key, value in settled.items():
            if key in DEFAULT_PREFERENCE and value and key not in recalled:
                recalled[key] = value

    # 优先级：本次任务自己给的 > 记忆里的 > 内置默认。
    # 记忆是**建议**，不能覆盖这一次明确说出来的东西。
    domain_prefs: dict = {}
    source = "default"
    note = ""
    if llm is not None:
        try:
            prompt = f"""你是一个用户偏好分析师。请分析用户输入，给出偏好设置和风险信号。
严格按以下 JSON 格式返回，不要包含 markdown 代码块标记，不要解释：
{{
  "domain_prefs": {{"language": "python"}},
  "risks": ["潜在风险1"],
  "severity": "low"
}}
用户输入：{raw_input}"""
            memory_hint = summarize_for_prompt(refs)
            if memory_hint:
                prompt = f"{prompt}\n\n{memory_hint}"
            llm_response = llm.chat(prompt)
            clean_json = (llm_response.get('content', '') if isinstance(llm_response, dict) else llm_response).strip()
            if clean_json.startswith("```json"): clean_json = clean_json[7:]
            if clean_json.endswith("```"): clean_json = clean_json[:-3]
            parsed = json.loads(clean_json.strip())

            domain_prefs = parsed.get("domain_prefs", {})
            risks = parsed.get("risks", [])
            severity = parsed.get("severity", "none")
            source = "llm"
        except Exception as exc:
            # 以前这里是 `pass`：偏好分析失败了，账本上什么也看不出来，
            # 于是「模型答了段没法解析的东西」和「这次本来就没有偏好」长得一样。
            risks = []
            severity = "none"
            note = f"preference_llm_failed:{type(exc).__name__}"
            if str(exc).strip():
                note = f"{note}({str(exc).strip()[:120]})"
    else:
        risks = []
        severity = "none"

    if not isinstance(domain_prefs, dict):
        domain_prefs = {}

    preference_pack = {
        "task_id": task_id,
        **DEFAULT_PREFERENCE,
        **recalled,
        # domain_prefs 放最后：本次任务自己的偏好压过记忆与默认
        "domain_prefs": domain_prefs,
        "preference_source": source,
    }
    if note:
        preference_pack["preference_note"] = note

    return {
        "preference_pack": preference_pack,
        "memory_index": {
            "task_id": task_id,
            "refs": refs,
            "namespace": namespace,
            # 哪些偏好是从记忆里来的——不写出来，这条记忆就等于没生效过
            "recalled_from_memory": sorted(recalled),
            "memory_available": memory is not None,
        },
        "risk_signal": {"task_id": task_id, "risks": risks, "severity": severity},
    }
