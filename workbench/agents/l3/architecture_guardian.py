import json, os, tempfile, ast
from typing import Any

def architecture_guardian(spec: Any, llm=None) -> dict:
    if not isinstance(spec, dict):
        return {"architecture_guardian": {"verdict": "unknown", "stub": True}}
    spec_str = json.dumps(spec, ensure_ascii=False)
    verdict = "pass"
    issues = []
    from_model = False
    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": '审查以下架构设计，返回 JSON: {"verdict": "...", "issues": [...]}' + '\n输入数据：' + json.dumps(spec, ensure_ascii=False, default=str)}], max_tokens=256)
            clean = resp["content"].strip()
            if clean.startswith("```"): clean = clean.split("```")[1].strip()
            parsed = json.loads(clean)
            verdict = parsed.get("verdict", "pass")
            issues = parsed.get("issues", [])
            from_model = True
        except Exception:
            pass
    if verdict == "pass" and spec_str:
        try:
            tree = ast.parse(spec_str)
            if not isinstance(tree, ast.Module):
                issues.append("spec_not_valid_python")
        except SyntaxError as e:
            issues.append(f"syntax_error: {e}")
            verdict = "warn"
    with tempfile.TemporaryDirectory() as tmpdir:
        arch_file = os.path.join(tmpdir, "arch_report.json")
        with open(arch_file, "w") as f:
            json.dump({"verdict": verdict, "issues": issues}, f)
    # `stub` 说的是**这个结论是谁下的**：模型答的，还是没人答、默认放行。
    # 从前它恒为 False（因为「输入是个 dict」），于是「审过了」和「没人审」长得一样。
    return {"architecture_guardian": {"verdict": verdict, "issues": issues, "stub": not from_model}}
