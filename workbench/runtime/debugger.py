"""调试器：在阶段边界停下来问一句——「这一站跑不跑，你说了算」。

判据（写死在这里，服务端和界面都照它读）：

- **断点是「停在哪一站前面」，不是「停在哪一站里面」**：链子的每一站都是一次
  完整的 Agent 调用，停在一半只会留下半个产物，而半个产物比没有产物更难收拾。
- **不设断点就是原来那条路**：没开调试的运行，一个多出来的问题都不问。
- **停住的那一场占着一个跑位**：说清楚，别让「怎么不动了」变成谜。
- **没人管就接着跑**：这不是闸门——运行早就被授权了。断点只是「停下来看一眼」
  的机会；没人来看，就照跑，并且把「没人管」写进日志。
- **认不出来的站名是错的，不是默认值**：拼错的断点如果被悄悄忽略，用户会以为
  自己设上了。走得到走不到是另一件事，那件事只说一句、不当错。

这个模块只做算术和措辞，不读文件、不发请求、不认识任何厂商。
"""

from __future__ import annotations

import json
from typing import Any

from runtime.replay import length_note

# 断点最多几个。站一共就那么几个，这个上限是防手滑贴进来一大段东西。
MAX_BREAKPOINTS = 12

# 停下来之后能做的三件事。服务端拿它校验请求，界面拿它画按钮——所以它是公开的。
ACTIONS = ("step", "continue", "abort")

# 每个动作对应一句人话。`timeout` 不是用户按的，是**没人按**：它也在这一份里，
# 因为日志里那一条要说的是同一件事。
_ACTION_WORDS = {
    "step": "单步",
    "continue": "继续",
    "abort": "中止",
    "timeout": "没人管",
}

_ACTION_HINTS = {
    "step": "放这一站走，下一站再停。",
    "continue": "跑到下一个断点，中间不问。",
    "abort": "这一场就停在这儿，不再往下跑。",
}


def _tokens(value: Any) -> list[str]:
    """界面上那行字 → 一个一个站名。逗号、顿号、分号、空格都算分隔。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        return []
    text = value
    for mark in ("，", "、", "；", ";", "\n", "\t", " "):
        text = text.replace(mark, ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _join(names: Any) -> str:
    return "、".join(str(name) for name in names)


def parse_breakpoints(
    value: Any,
    *,
    known: Any,
    reachable: Any = None,
) -> dict:
    """界面上那行字 → 断点清单。

    `known` 是存在的站名（写错的**拒绝**），`reachable` 是这个模式会走到的站名
    （写了但走不到的不算错，只是不会响——两件事分开说）。大小写不敏感。
    """
    seen = {str(name).upper() for name in known}
    steps = {str(name).upper() for name in (known if reachable is None else reachable)}
    accepted: list[str] = []
    rejected: list[dict] = []
    unreachable: list[str] = []
    for raw in _tokens(value):
        name = raw.upper()
        if name not in seen:
            rejected.append({"raw": raw, "why": "没有这一站"})
            continue
        if name in accepted:
            continue
        if len(accepted) >= MAX_BREAKPOINTS:
            rejected.append({"raw": raw, "why": f"断点最多 {MAX_BREAKPOINTS} 个"})
            continue
        accepted.append(name)
        if name not in steps:
            unreachable.append(name)
    return {
        "breakpoints": accepted,
        "rejected": rejected,
        "unreachable": unreachable,
    }


def plan_note(breakpoints: Any = (), *, step: bool = False, unreachable: Any = ()) -> str:
    """这一次调试打算怎么走，用人话说。"""
    names = [str(name) for name in (breakpoints or [])]
    if step and names:
        said = f"单步走：每一站都停下来问一句；断点 {_join(names)} 也记着。"
    elif step:
        said = "单步走：每一站都停下来问一句。"
    elif names:
        said = f"停在 {_join(names)} 前。"
    else:
        said = "不设断点：一口气跑完。"
    beyond = [str(name) for name in (unreachable or [])]
    if beyond:
        said += f"（{_join(beyond)} 不在这个模式会走到的站里，断点不会响。）"
    return said


def start_state(breakpoints: Any = (), *, step: bool = False) -> dict:
    """一场运行的调试状态。

    `pending` 是「上一站按了单步」留下的欠账；`hits` 是**已经答复过**几次
    （正停在断点上等答复的时候，这一次还没算进去）。
    """
    return {
        "breakpoints": [str(name).upper() for name in (breakpoints or [])],
        "step": bool(step),
        "pending": False,
        # 中止过就是中止过。链子那边确实不会再问（钩子直接返回 False），但状态
        # 自己也得说一致——不然「接着走」这种将来会加的路会从这儿再问一遍。
        "aborted": False,
        "hits": 0,
        "actions": [],
    }


def should_pause(state: Any, stage: str) -> bool:
    """走到这一站前面，要不要停下来问一句。"""
    if not isinstance(state, dict):
        return False
    if state.get("aborted"):
        return False
    if state.get("step") or state.get("pending"):
        return True
    name = str(stage).upper()
    return name in [str(item).upper() for item in state.get("breakpoints") or []]


def apply(state: Any, action: str, stage: str) -> dict:
    """停下来之后按了这一个键，状态变成什么样。**不改原来那份。**"""
    before = state if isinstance(state, dict) else {}
    said = str(action)
    after = {
        "breakpoints": [str(item) for item in before.get("breakpoints") or []],
        "step": bool(before.get("step")),
        "pending": bool(before.get("pending")),
        "aborted": bool(before.get("aborted")),
        "hits": int(before.get("hits") or 0) + 1,
        "actions": [
            {"action": str(row.get("action")), "stage": str(row.get("stage"))}
            for row in (before.get("actions") or [])
            if isinstance(row, dict)
        ],
    }
    if said == "step":
        after["pending"] = True
    elif said in ("continue", "timeout", "abort"):
        # 「继续」的意思是「别再每一站都问了」——开跑时勾的单步也在这一句里。
        # 超时也走这一条：没人管的断点再问一遍，只会把这一场永远卡住。
        # 「中止」也走这一条，并且记下「中止过」：这一场不再问任何一站。
        after["pending"] = False
        after["step"] = False
        if said == "abort":
            after["aborted"] = True
    else:
        after["pending"] = False
    after["actions"].append({"action": said, "stage": str(stage).upper()})
    return after


def summary(state: Any) -> dict:
    """调试状态 → 能上网的那几个字段（字典里的东西不能直接上 JSON）。"""
    if not isinstance(state, dict):
        return {
            "breakpoints": [], "step": False, "pending": False, "aborted": False,
            "hits": 0, "actions": [],
        }
    return {
        "breakpoints": [str(item) for item in state.get("breakpoints") or []],
        "step": bool(state.get("step")),
        "pending": bool(state.get("pending")),
        "aborted": bool(state.get("aborted")),
        "hits": int(state.get("hits") or 0),
        "actions": [
            {"action": str(row.get("action")), "stage": str(row.get("stage"))}
            for row in (state.get("actions") or [])
            if isinstance(row, dict)
        ],
    }


def history_note(actions: Any) -> str:
    """停过几次、每次按了什么。"""
    rows = [row for row in (actions or []) if isinstance(row, dict)]
    if not rows:
        return "还没停过。"
    words = [_ACTION_WORDS.get(str(row.get("action")), str(row.get("action"))) for row in rows]
    return f"停过 {len(rows)} 次：" + "、".join(words) + "。"


def action_list() -> list[dict]:
    """停下来之后能按的几个键，连标签一起给——界面就不用再抄一遍词。"""
    return [
        {"action": name, "label": _ACTION_WORDS[name], "note": _ACTION_HINTS[name]}
        for name in ACTIONS
    ]


def pause_note(stage: str, completed: Any = ()) -> str:
    """停在这一站前面：谁跑完了、谁还没动。"""
    name = str(stage).upper()
    done = [str(item) for item in (completed or [])]
    if not done:
        return f"停在 {name} 前：这一场还没动。"
    return f"停在 {name} 前：{_join(done)} 已经跑完，{name} 还没动。"


def decision_note(action: str, stage: str, *, waited_ms: int | None = None) -> str:
    """停下来之后这一下按的是什么，用人话说。"""
    name = str(stage).upper()
    said = str(action)
    if said == "step":
        return f"单步：放 {name} 走一站，下一站再停。"
    if said == "continue":
        return "继续：跑到下一个断点。"
    if said == "abort":
        return f"中止：这一场停在 {name} 前，不再往下跑。"
    if said == "timeout":
        return f"{waited_note(waited_ms)}没人管：{name} 照跑。"
    return f"认不出来的动作（{said}）：{name} 照跑。"


def waited_note(ms: int | None) -> str:
    """等了多久，用人话说。"""
    if ms is None:
        return "等的时间不明"
    if ms < 1000:
        return "等了不到一秒"
    return "等了 " + length_note(ms)


def hold_note(slots: int) -> str:
    """停住的这一场占着一个跑位——不说的话，「怎么不动了」会变成谜。"""
    try:
        count = int(slots)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return "停住的这一场占着一个跑位。"
    return f"停住的这一场占着一个跑位（一共 {count} 个）。"


def size_note(size: Any) -> str:
    """产物多大，用人话说。"""
    try:
        count = int(size)
    except (TypeError, ValueError):
        return "大小不明"
    if count < 0:
        return "大小不明"
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024.0:.1f} KB"
    return f"{count / (1024.0 * 1024.0):.1f} MB"


def _size(value: Any) -> int | None:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if value is None:
        return 0
    if isinstance(value, (dict, list, tuple)):
        # 一站产出的东西多半是个结构（那份 stage result）。它占多大，按它写成
        # JSON 有多少字节算——那是它落到磁盘上的样子，也是它最后要去的地方。
        # 里面有认不出的东西（写不成 JSON）就说不知道：编一个数比不说更坏。
        try:
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return None
    return None


def produced(artifacts: Any) -> list[dict]:
    """已经产出的东西：谁产的、多大。按链子走的顺序，不排序——顺序就是来路。"""
    if not isinstance(artifacts, dict):
        return []
    rows: list[dict] = []
    for agent, value in artifacts.items():
        count = _size(value)
        rows.append(
            {
                "agent": str(agent),
                "bytes": count,
                "size": size_note(count),
            }
        )
    return rows


def pause_payload(
    stage: str,
    *,
    completed: Any = (),
    artifacts: Any = None,
    state: Any = None,
    slots: int = 0,
) -> dict:
    """停下来那一刻要说的话（服务端照着发事件，界面照着画）。"""
    name = str(stage).upper()
    current = state if isinstance(state, dict) else {}
    answered = [row for row in (current.get("actions") or []) if isinstance(row, dict)]
    return {
        "stage": name,
        "note": pause_note(name, completed),
        "done": [str(item) for item in (completed or [])],
        "produced": produced(artifacts),
        "hold": hold_note(slots),
        "history": history_note(current.get("actions")),
        # 这是这一场第几次停下（正在等的这一次算在内）。
        "count": len(answered) + 1,
        "actions": action_list(),
        "state": summary(current),
    }
