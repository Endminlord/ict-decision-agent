"""自愈加速器：看这段代码有没有毛病，有的话让模型提一个修法。

**只有「模型给了一段修好的代码、而那段代码能解析」才算修好。**

旧版把 `ast.parse` 成功当成 `healed: True`——「这段代码语法能解析」是**任何**能解析的
代码都有的性质，它不是修好了的证据。于是一个完全没有模型参与的兜底分支，把一个恒真的
判断写成了体检结论：语法错的代码它说没修，语法对的代码它一律说修好了。
`except Exception: pass` 又把「模型挂了」和「模型说没问题」压成同一个答案。

旧版还往 `tempfile.TemporaryDirectory()` 里写了一份 `healing_report.json`——那个目录在
函数**返回之前**就被删了。写了一个任何人都读不到的文件，然后报告里对此一言不发。
现在报告就在返回值里。
"""
import ast
import json
from typing import Any

_PROMPT = (
    '分析以下代码的潜在缺陷并生成修复建议，返回 JSON: '
    '{"healed": bool, "fixes": [...], "code": "修好的代码"}'
)


def _report(
    *,
    healed: bool,
    fixes: Any,
    stub: bool,
    reason: str,
    code: str | None = None,
) -> dict:
    """一份能读的体检报告。`reason` 永远在——修没修成都得说清凭什么。"""
    report = {
        "healed": bool(healed),
        "fixes": list(fixes) if isinstance(fixes, (list, tuple)) else [],
        # `stub` 说的是**这个结论是谁下的**：模型答的 False，内置兜底的 True。
        "stub": bool(stub),
        "reason": reason,
    }
    if code is not None:
        report["code"] = code
    return report


def self_healing_test(code: Any, llm=None) -> dict:
    if not isinstance(code, dict):
        return {"self_healing_test": _report(
            healed=False, fixes=[], stub=True,
            reason="输入不是一个 dict，没有代码可看",
        )}
    code_str = code.get("code", "")
    if not isinstance(code_str, str) or not code_str.strip():
        return {"self_healing_test": _report(
            healed=False, fixes=[], stub=True,
            reason="这段代码是空的，没什么可看的",
        )}
    if llm is None:
        return {"self_healing_test": _report(
            healed=False, fixes=[], stub=True,
            reason="这次没有模型可问，所以没有修法——不是「检查过没问题」",
        )}

    try:
        resp = llm.chat(
            [{"role": "user", "content": _PROMPT + '\n输入数据：'
              + json.dumps(code, ensure_ascii=False, default=str)}],
            max_tokens=256,
        )
        clean = resp["content"].strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1].strip()
        parsed = json.loads(clean)
    except Exception as exc:  # noqa: BLE001 - 模型挂了要说出来，不能咽下去
        return {"self_healing_test": _report(
            healed=False, fixes=[], stub=False,
            reason=f"{type(exc).__name__}: {exc}",
        )}

    if not isinstance(parsed, dict):
        return {"self_healing_test": _report(
            healed=False, fixes=[], stub=False,
            reason="模型没返回一个 JSON 对象",
        )}
    fixes = parsed.get("fixes")
    if not parsed.get("healed"):
        return {"self_healing_test": _report(
            healed=False, fixes=fixes, stub=False,
            reason="模型说这段代码没有需要修的地方",
        )}
    fixed = parsed.get("code")
    if not isinstance(fixed, str) or not fixed.strip():
        return {"self_healing_test": _report(
            healed=False, fixes=fixes, stub=False,
            reason="模型说它修好了，但没给修好的代码——那就没法核对",
        )}
    try:
        ast.parse(fixed)
    except SyntaxError as exc:
        return {"self_healing_test": _report(
            healed=False, fixes=fixes, stub=False,
            reason=f"模型给的修法自己解析不了：{exc}",
        )}
    return {"self_healing_test": _report(
        healed=True, fixes=fixes, stub=False, reason="", code=fixed,
    )}
