"""红队：把契约自己的字面量往旁边推一格，看生成物会不会当场塌。

蓝队写用例时想的是「证明它行」，红队想的是「证明它不行」——同一个脑子想不出这两种
东西。所以这一步不生成新用例，而是**改写已有的**：契约里写 ``is_leap_year(2024)``，
红队就去问 ``is_leap_year(2025)``；契约里写 ``reverse('hello')``，红队就去问
``reverse('hellox')`` 和 ``reverse('')``。

判据只有一条：**合法的邻居输入不许把它弄塌**。

类型错（把 None 喂给整数参数）**不算**——那种输入本来就该抛，把它算成缺陷等于自己
制造假警报，而假警报比不报还坏：它会让人学会忽略红队。所以那些单独记在 ``weak``
里，只报不判。这条线是「不许喊狼来了」和「不许放过真问题」之间的那一条。

为什么值得有这一步：契约用例是**照着需求写的**，而需求写的是正常路径。真实世界里
让代码塌掉的往往不是需求里的那个数，而是它旁边那个。
"""

from __future__ import annotations

from typing import Any

from runtime.executor import _parse_given, execute_code

# 合法邻居：这些输入在契约描述的问题域内，弄塌了就是缺陷。
#
# 名字就是 `neighbours()` 真会产出的类别名。早先这里写的是几个大类名（step/empty/
# double），而实际产出的是 step_up / append / drop_last……两边对不上，默认策略下
# **一个邻居都不会被选中**——而红队「跑过了、没发现问题」看上去和「什么都没试」一样。
NEIGHBOUR_FAMILIES = (
    "step_up", "step_down", "negate", "append", "prepend", "double", "empty",
    "grow", "drop_last",
)
# 越界输入：只记录，不判。见模块开头那段。这几个名字带后缀，策略里写前缀。
WEAK_FAMILIES = ("wrong_type",)

DEFAULT_LIMIT = 24
_MAX_ERROR = 120


def _strong_neighbours(value: Any) -> list[tuple[str, Any]]:
    """合法邻居：还在问题域里，只是挪了一格。"""
    out: list[tuple[str, Any]] = []
    if isinstance(value, bool):
        # bool 是 int 的子类，但 True+1 这种邻居没有意义。
        return out
    if isinstance(value, int):
        out.append(("step_up", value + 1))
        out.append(("step_down", value - 1))
        out.append(("negate", -value))
    elif isinstance(value, float):
        out.append(("step_up", value + 1.0))
        out.append(("negate", -value))
    elif isinstance(value, str):
        out.append(("append", value + "x"))
        out.append(("prepend", "x" + value))
        out.append(("double", value * 2))
        if value:
            out.append(("empty", ""))
    elif isinstance(value, list):
        out.append(("grow", value + value))
        if value:
            out.append(("drop_last", value[:-1]))
            out.append(("empty", []))
    return out


def _weak_neighbours(value: Any) -> list[tuple[str, Any]]:
    """越界输入：只记录。它们本来就该抛，不构成缺陷。"""
    if isinstance(value, (bool, str)):
        return []
    if isinstance(value, (int, float)):
        return [("wrong_type_str", "not-a-number"), ("wrong_type_none", None)]
    if isinstance(value, list):
        return [("wrong_type_none", None)]
    return []


def neighbours(value: Any) -> list[tuple[str, Any, bool]]:
    """一个值旁边的输入。第三项为 True 表示「合法邻居，弄塌了算缺陷」。"""
    return (
        [(family, item, True) for family, item in _strong_neighbours(value)]
        + [(family, item, False) for family, item in _weak_neighbours(value)]
    )


def _resolve_entrypoint(spec_verification: dict, source: str) -> str:
    """入口函数名：``file.py:func`` 取冒号后面的，``add(1,2)`` 取函数名。"""
    entrypoint = spec_verification.get("entrypoint")
    if isinstance(entrypoint, str) and entrypoint.strip():
        if ":" in entrypoint:
            return entrypoint.split(":")[-1].strip()
        head = entrypoint.split("(")[0].strip()
        if not head.endswith(".py"):
            return head
        # 文件形式的入口，落到下面按用例推。
    # 没有入口（复核器手上就只有用例）：从 when 里取函数名。
    for case in spec_verification.get("cases") or []:
        when = str((case or {}).get("when") or "")
        if "(" in when:
            return when.split("(")[0].strip()
    raise ValueError("no entrypoint and no case names a function")


def _selected(family: str, chosen: Any) -> bool:
    """这个邻居类别在不在策略里。

    越界那几类的名字带后缀（``wrong_type_str`` / ``wrong_type_none``），而策略里写的
    是它们共同的前缀 ``wrong_type``，所以这里按前缀认。
    """
    if family in chosen:
        return True
    return any(family.startswith(weak) for weak in WEAK_FAMILIES if weak in chosen)


def _trials(spec_verification: dict, *, chosen: Any, limit: int) -> list[dict]:
    """每个用例、每个参数、每个邻居，一次只动一个参数。"""
    cases = spec_verification.get("cases")
    if not isinstance(cases, list):
        return []
    out: list[dict] = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        try:
            args = _parse_given(case.get("given", ""))
        except Exception:
            # 解析不了的用例不归红队管：A3/A4 已经在管它了。
            continue
        for index, value in enumerate(args):
            for family, mutant, strong in neighbours(value):
                if not _selected(family, chosen):
                    continue
                if len(out) >= limit:
                    return out
                trial_args = list(args)
                trial_args[index] = mutant
                out.append({
                    "label": f"{case.get('id') or '?'}:arg{index}:{family}",
                    "args": trial_args,
                    "family": family,
                    "strong": strong,
                    "case_id": case.get("id"),
                })
    return out


def _script(source: str, function_name: str, trials: list[dict]) -> str:
    """一段真实脚本：把生成物 exec 进来，逐个调用，把结果打成 JSON。"""
    import json

    payload = json.dumps(
        [{"label": t["label"], "args": t["args"]} for t in trials],
        ensure_ascii=False,
    )
    return (
        "import json\n"
        "_scope = {'__name__': '__adversary_target__', '__file__': __file__}\n"
        f"exec(compile({source!r}, '<green_code>', 'exec'), _scope)\n"
        f"_parts = {function_name!r}.split('.')\n"
        "_target = _scope[_parts[0]]\n"
        "for _part in _parts[1:]:\n"
        "    _target = getattr(_target, _part)\n"
        "if not callable(_target):\n"
        "    raise TypeError('entrypoint is not callable')\n"
        f"_trials = json.loads({payload!r})\n"
        "_out = []\n"
        "for _trial in _trials:\n"
        "    try:\n"
        "        _target(*_trial['args'])\n"
        "        _out.append({'label': _trial['label'], 'ok': True})\n"
        "    except Exception as _exc:\n"
        "        _out.append({'label': _trial['label'], 'ok': False,\n"
        "                     'error': type(_exc).__name__ + ': ' + str(_exc)[:120]})\n"
        "print(json.dumps(_out, ensure_ascii=False))\n"
    )


def _read_report(stdout: str) -> list[dict] | None:
    """从输出里找最后一行能解析成列表的 JSON。找不到就是没跑成。"""
    import json

    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def challenge(
    spec_verification: Any,
    green_code: Any,
    *,
    families: Any = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """跑一次红队。结论来自真实进程，不由调用方转述。

    ``families`` 是留给领域策略的接口：契约（或它所在的领域）可以给出自己想禁的
    邻居类别，红队据此裁剪。领域策略是数据，红队不认识任何具体领域。
    """
    report: dict[str, Any] = {
        "verdict": "not_run",
        "tried": 0,
        "findings": [],
        "weak": [],
        "reason": "",
        "families": list(NEIGHBOUR_FAMILIES),
    }
    if not isinstance(spec_verification, dict):
        report["reason"] = "spec_verification must be a dict"
        return report
    if spec_verification.get("mode") != "function":
        # CLI 与 ui 形态的邻居怎么推还没想清楚，先如实说没跑。
        report["reason"] = f"mode {spec_verification.get('mode')!r} 不支持红队"
        return report
    if isinstance(green_code, dict):
        source = green_code.get("code", "")
        language = green_code.get("language", "python")
    else:
        source, language = green_code, "python"
    if not isinstance(source, str) or not source.strip():
        report["reason"] = "missing_python_source"
        return report
    if language != "python":
        report["reason"] = f"language {language!r} 不支持红队"
        return report

    if families is None:
        chosen = list(NEIGHBOUR_FAMILIES) + list(WEAK_FAMILIES)
    elif isinstance(families, (list, tuple)) and all(isinstance(f, str) for f in families):
        chosen = list(families)
    else:
        report["reason"] = "families must be a list of strings"
        return report
    report["families"] = chosen

    try:
        function_name = _resolve_entrypoint(spec_verification, source)
        trials = _trials(spec_verification, chosen=chosen, limit=max(1, int(limit)))
    except Exception as exc:  # noqa: BLE001 - 红队跑不起来也是结论的一部分
        report["reason"] = f"adversary_setup_failed:{type(exc).__name__}: {exc}"
        return report
    if not trials:
        report["reason"] = "no_neighbours_derived"
        return report

    result = execute_code(_script(source, function_name, trials), "function")
    if not result.get("executed"):
        report["reason"] = f"adversary_not_executed:{result.get('reason') or result.get('status')}"
        return report
    observed = _read_report(result.get("stdout", ""))
    if observed is None:
        tail = (result.get("stderr") or result.get("stdout") or "").strip()[-_MAX_ERROR:]
        report["reason"] = f"adversary_output_unreadable:{tail}"
        return report

    by_label = {t["label"]: t for t in trials}
    report["tried"] = len(observed)
    for item in observed:
        trial = by_label.get(item.get("label")) or {}
        if item.get("ok"):
            continue
        record = {
            "label": item.get("label"),
            "case_id": trial.get("case_id"),
            "family": trial.get("family"),
            "args": trial.get("args"),
            "error": str(item.get("error") or "")[:_MAX_ERROR],
        }
        if trial.get("strong"):
            report["findings"].append(record)
        else:
            report["weak"].append(record)

    if report["findings"]:
        first = report["findings"][0]
        report["verdict"] = "broken"
        # 原因要点名是谁造成的：只说「红队发现了问题」，读的人无从下手。
        report["reason"] = (
            f"red_team_found_crash:{first['label']}:{first['error']}"
        )
    else:
        report["verdict"] = "survived"
    return report
