import hashlib
import json
import re
from typing import Any

OPERATION_TEMPLATES = {
    "add": {"keywords": ["和", "加法", "相加", "求和"],
            "user_story": {"as": "caller", "want": "传入两个数得到和", "so_that": "复用加法逻辑"},
            "acceptance": ["add(1,2) == 3", "add(-1,1) == 0", "add(0,0) == 0"],
            "scope_in": ["加法函数"]},
    "sub": {"keywords": ["差", "减法", "相减"],
            "user_story": {"as": "caller", "want": "传入两个数得到差", "so_that": "复用减法逻辑"},
            "acceptance": ["sub(3,2) == 1", "sub(0,1) == -1", "sub(0,0) == 0"],
            "scope_in": ["减法函数"]},
    "mul": {"keywords": ["积", "乘法", "相乘"],
            "user_story": {"as": "caller", "want": "传入两个数得到积", "so_that": "复用乘法逻辑"},
            "acceptance": ["mul(2,3) == 6", "mul(-1,2) == -2", "mul(0,5) == 0"],
            "scope_in": ["乘法函数"]},
}
DEFAULT_SCOPE_OUT = ["UI", "网络", "持久化"]

# A bare "和" is the conjunction "and" far more often than it is the sum. The
# template matcher used a plain substring test, so "写一个基类 Shape 和两个
# 子类" was classified as an addition task and the run produced `def add(a, b)`
# — reporting contract_pass for a request about shapes.
#
# These keywords only count when the surrounding text makes them nouns.
_CONTEXTUAL_KEYWORDS = {
    "和": (r"的\s*和", r"之\s*和", r"求\s*和", r"总和", r"之和"),
    "差": (r"的\s*差", r"之\s*差", r"求\s*差", r"差值"),
    "积": (r"的\s*积", r"之\s*积", r"求\s*积", r"乘积"),
}


def _matches_template(template: dict, text: str) -> bool:
    """True when the text names this operation, ignoring grammatical lookalikes."""
    for keyword in template.get("keywords") or []:
        patterns = _CONTEXTUAL_KEYWORDS.get(keyword)
        if patterns is None:
            if keyword in text:
                return True
        elif any(re.search(pattern, text) for pattern in patterns):
            return True
    return False

def _hash_id(obj: Any) -> str:
    return hashlib.sha256(str(obj).encode("utf-8")).hexdigest()[:8]

def _resolve_task_id(td: Any) -> str:
    if isinstance(td, dict):
        tid = td.get("task_id")
        if isinstance(tid, str) and tid:
            return tid
    try:
        return _hash_id(td)
    except Exception:
        return "unknown"

def _resolve_raw_input(td: Any) -> str:
    if isinstance(td, dict):
        raw = td.get("raw_input")
        if isinstance(raw, str) and raw.strip():
            return raw
    return "未提供原始输入"

def _extract_operation(raw_input: str):
    text = raw_input.lower()
    for name, tpl in OPERATION_TEMPLATES.items():
        if _matches_template(tpl, text):
            return {"name": name, **tpl}
    return None

def _emit_minimal_prd(task_id: str) -> dict:
    return {"prd": {"task_id": task_id, "user_stories": [],
                    "acceptance_criteria": [], "scope": {"in": [], "out": []}}}

def _phase_1_prd_builder(preference_pack, task_dag):
    task_id = _resolve_task_id(task_dag)
    raw_input = _resolve_raw_input(task_dag)
    op = _extract_operation(raw_input)
    if op:
        return {"prd": {"task_id": task_id,
                        "user_stories": [dict(op["user_story"])],
                        "acceptance_criteria": list(op["acceptance"]),
                        "scope": {"in": list(op["scope_in"]), "out": list(DEFAULT_SCOPE_OUT)}}}
    return {"prd": {"task_id": task_id,
                    "user_stories": [{"as": "user", "want": raw_input, "so_that": "完成目标"}],
                    "acceptance_criteria": [f"满足：{raw_input}"],
                    "scope": {"in": [raw_input], "out": list(DEFAULT_SCOPE_OUT)}}}

def prd_builder(preference_pack: Any, task_dag: Any, llm=None) -> dict:
    if not isinstance(preference_pack, dict) or not isinstance(task_dag, dict):
        return _emit_minimal_prd(_resolve_task_id(task_dag))
        
    # 阶段2 P0: 强制 LLM 输出结构化 JSON，并适配下游规范
    if llm is not None:
        raw_input = _resolve_raw_input(task_dag)
        task_id = _resolve_task_id(task_dag)
        prompt = f"""你是一个软件需求分析师。请严格按以下 JSON 格式返回，不要包含 markdown 代码块标记，不要解释。
        {{
          "user_stories": [{{"as": "caller", "want": "传入两个数得到和", "so_that": "复用加法逻辑"}}],
          "acceptance_criteria": ["add(1,2) == 3", "add(-1,1) == 0"],
          "scope_in": ["加法函数"]
        }}
        用户输入：{raw_input}"""
        try:
            llm_response = llm.chat(prompt)
            # 清理 LLM 可能带上的 markdown 代码块
            clean_json = (llm_response.get('content', '') if isinstance(llm_response, dict) else llm_response).strip()
            if clean_json.startswith("```json"):
                clean_json = clean_json[7:]
            if clean_json.endswith("```"):
                clean_json = clean_json[:-3]
            clean_json = clean_json.strip()
            parsed = json.loads(clean_json)
            
            # 组装成符合下游链式调用的规范结构
            prd_data = {
                "task_id": task_id,
                "user_stories": parsed.get("user_stories", []),
                "acceptance_criteria": parsed.get("acceptance_criteria", []),
                "scope": {
                    "in": parsed.get("scope_in", [raw_input]),
                    "out": list(DEFAULT_SCOPE_OUT)
                }
            }
            return {"prd": prd_data}
        except Exception:
            # LLM 解析失败，静默回退到阶段1的规则提取，保证系统不崩溃
            pass
        
    return _phase_1_prd_builder(preference_pack, task_dag)
