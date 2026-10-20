#!/usr/bin/env python3
"""模型功率检测：五道机器能判的题，和一个只说这五道题的结论。

为什么没有「模型裁判」
----------------------
让另一个模型来打分，等于在一个「结论不许说谎」的系统里请一个会说谎的裁判。
这里五道题全部由**代码**判分：跑一段程序看它打印什么、严格解析一行 JSON、
在长文里找一个代号、对一道有确定答案的概率题。判分方式写进结果的
``judged_by`` 字段，谁都能复核。

为什么住在 runtime/
-------------------
它测的是**能力**，不是身份 —— 不认识任何厂商，输入一段文字、输出一段文字。
厂商名字因此不许出现在本文件里（架构规则 R1）。要问模型就把 ``call`` 传进来，
本模块不 import 网关也不 import 适配器（R2）。

三条纪律
--------
1. **没跑 ≠ 没通过。** 一道题如果连调用都失败了，它的 ``score`` 是 None 而不是
   0，整份结果里 ``tier`` 也可能是 ``NOT_RUN``。把失败算成 0 分，等于把「机器
   坏了」写成「模型不行」。
2. **分数只说这五道题。** 结果里带 ``disclaimer``：这是这台机器、这一次、这五道
   题的分数，不是排行榜，也不主张能预测别的任务。
3. **原始回答一律留下。** 每道题的 ``answer`` 原文进结果，判分理由进 ``why``。
   只给分不给原文的评分表，事后没人能复核。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from runtime.executor import execute_code

__all__ = ["PROBES", "TIERS", "profile", "tier_for", "extract_code", "label_for"]

# 四档。名字与界面上的标签一致（4.11 用它们显示 [MAX]/[HIGH]/[MIDDLE]/[LOW]）。
TIERS = ((90.0, "MAX"), (70.0, "HIGH"), (45.0, "MIDDLE"), (0.0, "LOW"))
NOT_RUN = "NOT_RUN"

# 长文里埋的代号。写死在这里，题目和判分同源，不会各说各话。
BURIED_CODE = "ZX-4471"
_LONG_CONTEXT_PROMPT = (
    "下面是一段项目记录，读完回答问题。\n"
    "----\n"
    "项目于三月立项，四月完成需求评审，五月进入开发。\n"
    "参与人员：甲、乙、丙、丁。评审意见共十四条，其中三条被驳回。\n"
    "构建流水线每天凌晨两点跑一次，产物存放在内部制品库。\n"
    f"本项目的内部代号是 {BURIED_CODE}，只有评审组知道。\n"
    "六月开始联调，七月出第一版验收报告。\n"
    "----\n"
    "问题：本项目的内部代号是什么？只回答代号本身，不要解释。"
)

# 五道题。id 与判分函数一一对应，顺序就是报告顺序。
PROBES = (
    {
        "id": "code_correctness",
        "title": "能不能写出跑得对的代码",
        "question": (
            "写一段 Python 程序，定义一个函数 add_one(n) 返回 n+1，"
            "然后打印 add_one(41) 的结果。只输出代码，不要解释。"
        ),
    },
    {
        "id": "instruction_following",
        "title": "能不能守住格式约束",
        "question": '只输出一行 JSON，内容恰好是 {"ok": true}。不要代码块，不要解释，不要多余字符。',
    },
    {
        "id": "long_context",
        "title": "能不能在长文里找到埋着的那句",
        "question": _LONG_CONTEXT_PROMPT,
    },
    {
        "id": "reasoning",
        "title": "一道有确定答案的推理题",
        "question": (
            "盒子里有 3 个红球和 2 个蓝球，随机取出两个（不放回）。"
            "两个都是红球的概率是多少？用最简分数回答，只给分数。"
        ),
    },
    {
        "id": "stability",
        "title": "同一句话问两遍，答案一样吗",
        "question": "用一句话说明什么是二分查找。",
    },
)


def extract_code(text: str) -> str:
    """从回答里取出代码：有围栏就用围栏里的，没有就把整段当代码。

    只取第一段围栏 —— 模型经常先解释再给代码，取错一段就会把解释当程序跑。
    """
    if not isinstance(text, str):
        return ""
    fenced = re.search(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", text, re.S)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


def _ask(call, question: str) -> tuple[str, str]:
    """问一次。返回 (文本, 错误)。错误非空表示这一问**没跑成**，不是答错了。"""
    try:
        response = call([{"role": "user", "content": question}])
    except Exception as exc:  # noqa: BLE001 - 调用失败是一种结果，不是崩溃
        return "", f"{type(exc).__name__}: {exc}"
    if response is None:
        return "", ""
    if isinstance(response, str):
        return response, ""
    if isinstance(response, dict):
        for key in ("content", "text", "answer"):
            if isinstance(response.get(key), str):
                return response[key], ""
        return "", ""
    return str(response), ""


# --- 五道题的判分 -----------------------------------------------------------

def _judge_code(answer: str) -> tuple[float | None, str]:
    code = extract_code(answer)
    if not code:
        return None, "回答里没有可执行的代码"
    result = execute_code(code, "function")
    if not result.get("executed"):
        return None, f"没跑成：{result.get('reason') or result.get('status')}"
    printed = (result.get("stdout") or "").strip()
    if result.get("success") and printed == "42":
        return 1.0, "程序跑通且打印 42"
    if result.get("success"):
        return 0.5, f"程序跑通了，但打印的是 {printed!r}，不是 42"
    return 0.0, f"程序跑失败了：{(result.get('stderr') or '').strip()[:120]}"


def _judge_json(answer: str) -> tuple[float | None, str]:
    text = (answer or "").strip()
    if not text:
        return None, "空回答"
    try:
        if json.loads(text) == {"ok": True}:
            return 1.0, "恰好一行 JSON，且内容正确"
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.S)
    if fenced:
        try:
            if json.loads(fenced.group(1).strip()) == {"ok": True}:
                return 0.5, "内容对，但套了代码块 —— 违反了「不要代码块」"
        except json.JSONDecodeError:
            pass
    return 0.0, "不是要求的 JSON"


def _judge_long_context(answer: str) -> tuple[float | None, str]:
    text = (answer or "").strip()
    if not text:
        return None, "空回答"
    if BURIED_CODE in text:
        return 1.0, f"找到了埋着的代号 {BURIED_CODE}"
    return 0.0, f"没找到代号（它答的是：{text[:60]!r}）"


def _judge_reasoning(answer: str) -> tuple[float | None, str]:
    text = (answer or "").strip()
    if not text:
        return None, "空回答"
    squeezed = text.replace(" ", "")
    if "3/10" in squeezed:
        return 1.0, "答案正确（3/10）"
    if "0.3" in squeezed or "30%" in squeezed:
        return 0.5, "数值对，但没按要求给最简分数"
    return 0.0, f"答案不对（它答的是：{text[:60]!r}）"


def _judge_stability(first: str, second: str) -> tuple[float | None, str]:
    if first is None or second is None:
        return None, "两问里有一问没跑成"
    if not first.strip() or not second.strip():
        return None, "两问里有空回答"
    if first == second:
        return 1.0, "两次回答逐字相同"
    if first.strip() == second.strip():
        return 0.5, "两次回答只差首尾空白"
    # 措辞不同不等于答案不对：真实模型在温度 0 下也会换一种说法。这一项测的是
    # **逐字重复性**，不是正确性 —— 理由里必须说清楚，否则 0 分会被读成「模型不行」。
    return 0.0, "两次回答逐字不同（措辞不同，意思可能一样；这一项测的是逐字重复性）"


_JUDGES = {
    "code_correctness": _judge_code,
    "instruction_following": _judge_json,
    "long_context": _judge_long_context,
    "reasoning": _judge_reasoning,
}


def tier_for(score: float | None) -> str:
    """分数 → 档位。``None`` 是「没跑」，不是 0 分。"""
    if score is None:
        return NOT_RUN
    for threshold, name in TIERS:
        if score >= threshold:
            return name
    return TIERS[-1][1]


def label_for(report: Any) -> str:
    """把一份检测结果变成下拉框上那个短标签。

    三种情况必须长得不一样，否则用户分不清「没测过」和「测出来不行」：

    * 没有报告（``None``）→ ``""``，**不显示任何标签**。这是本函数存在的
      主要理由：一个从没测过的模型和一个测出来 LOW 的模型，在下拉框里必须
      长得不同。给没测过的补一个 ``[LOW]``，就是拿「我不知道」冒充「我测过」。
    * 测了但一次都没跑成（``tier == "NOT_RUN"``）→ ``[没跑成]``，与「没测过」
      也不同：前者是机器/网络的问题，后者是我什么都没做。
    * 正常测出档位 → ``[HIGH 80]``，带上分数，因为同一档里 71 和 89 不是一回事。
    """
    if not isinstance(report, Mapping):
        return ""
    tier = str(report.get("tier") or "").strip()
    if not tier:
        return ""
    if tier == NOT_RUN:
        return "[没跑成]"
    score = report.get("score")
    if isinstance(score, (int, float)):
        return f"[{tier} {score:g}]"
    return f"[{tier}]"


def profile(call, *, model_id: str = "", probes=None, ask=None) -> dict:
    """跑五道题，给一份能复核的分数。

    参数
    ----
    call
        形如 ``call(messages) -> str | {"content": str}``。由调用方注入，所以本
        模块既不认识网关也不认识 provider。
    model_id
        这次测的是哪个模型 ID（记进结果）。
    probes
        只跑其中几道题（默认全跑）。传了它，``tier`` 会注明是部分测试。
    ask
        覆盖提问函数（测试用），签名同 ``_ask``。

    返回
    ----
    ``{"model_id", "score", "tier", "probes", "judged_by", "partial",
    "disclaimer", "calls"}``。``score`` 为 None 表示五道题一道都没跑成。
    """
    asked = ask or _ask
    wanted = [p for p in PROBES if not probes or p["id"] in probes]
    results: list[dict] = []
    calls = 0

    for spec in wanted:
        answer, error = asked(call, spec["question"])
        calls += 1
        entry = {
            "id": spec["id"],
            "title": spec["title"],
            "question": spec["question"],
            "answer": (answer or "").strip()[:600],
            "judged_by": "machine",
            "score": None,
            "why": "",
            "error": error,
        }
        if error:
            entry["why"] = f"没跑成（这一项不计入 0 分）：{error}"
        elif spec["id"] == "stability":
            second, second_error = asked(call, spec["question"])
            calls += 1
            if second_error:
                entry["why"] = f"第二次问没跑成：{second_error}"
            else:
                entry["second_answer"] = (second or "").strip()[:600]
                entry["score"], entry["why"] = _judge_stability(answer, second)
        else:
            entry["score"], entry["why"] = _JUDGES[spec["id"]](answer)
        results.append(entry)

    scored = [r["score"] for r in results if r["score"] is not None]
    score = round(100.0 * sum(scored) / len(scored), 1) if scored else None
    ran = len(scored)
    return {
        "model_id": model_id,
        "score": score,
        "tier": tier_for(score),
        "probes": results,
        "judged_by": "machine",
        "ran": ran,
        "total": len(results),
        "partial": ran < len(PROBES),
        "disclaimer": (
            "这是这台机器上、这一次、这五道题的分数。它不是排行榜，"
            "也不主张能预测别的任务；判分的是代码，不是另一个模型。"
            + (f" 本次只有 {ran}/{len(results)} 道题跑成了。" if ran < len(results) else "")
        ),
        "calls": calls,
    }
