"""R/D 判定器：这条工作单开工前能不能写出机器可校验断言、能不能用已登记的 stage 拆。

判据来自 164 号（双轨制），两条：

R（主判据）＝「开工前能否写出机器可校验断言」。断言分三级——**A 级**可执行
（`exit_code` / `iverilog` / `pytest` / `sha256` / `bytes`）、**B 级**可结构校验
（JSON schema / 必填字段 / 正则 / 字段等于）、**C 级**只能靠人看（自然语言、主观质量、
截图观感）。任一条 C 级则 R 不成立。

D ＝「能否分解为已登记领域的既有 stage」：域要路由得上、stage 要都在主链上、agent 要
登记过且必产键已知、步骤之间的产物依赖要已被声明。

``R ∧ D`` → 生产轨；否则探索轨。**允许偏严**：两类误判的代价不对称（假阳只花一次运行，
假阴只多一次审阅），所以判不出的级别一律当 C 级、判不出的 D 一律当不成立。D 不成立而
R 成立时仍走探索轨，但断言**可以直接复用**（`assertions_reusable`）。

三条纪律：

1. **级别必须能从断言本身推出来**，不接受草稿自己声明的 `level`。草稿同时写了 `kind`
   和 `level` 且不一致时取**更严**的那个，并把分歧记进 `reasons`——仲裁的是机器，不是
   写草稿的人（164 表 1「模型起草、机器仲裁」）。
2. **认不出的 `kind` 一律 C 级**。加级别是改 `KIND_LEVELS` 这张闭合表并升版本号，
   不是让判定器去猜。
3. **不做语义判断、不调模型**：自然语言断言要进 A/B 级，得先由人把它写成 `kind`。
   这是成本纪律——模型判会带来非确定性、成本和缓存问题（197 号第 42 行），而它换来的
   只是少写几个 `kind`。

判据版本号（``TRACK_JUDGE_VERSION``）要写进 `work_orders.jsonl` 的 `track_judge_version`，
判据升级后历史可复评（164 表 1）。本模块**只判定**：不读文件、不写状态、不调模型、
不起子进程。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from runtime.registry import DomainRegistry

TRACK_JUDGE_VERSION = "1.0"

PRODUCTION = "production"
EXPLORATION = "exploration"

# 从松到严。取「更严」＝取下标更大的那个。
LEVELS = ("A", "B", "C")
_STRICTNESS = {level: index for index, level in enumerate(LEVELS)}

# 闭合表。A 级可执行、B 级可结构校验、C 级只能靠人看；表外的 kind 一律 C 级。
KIND_LEVELS = {
    "exit_code": "A",
    "iverilog": "A",
    "pytest": "A",
    "sha256": "A",
    "bytes": "A",
    "json_schema": "B",
    "required_fields": "B",
    "regex": "B",
    "field_equals": "B",
    "prose": "C",
    "screenshot": "C",
    "subjective": "C",
}

# 链上有、但**没有必产键**的 agent（`REQUIRED_OUTPUTS` 里没有它们）。这不是豁免名单，
# 是一张要跟两张表对得上的事实：`tdd_coder` 交的是文件不是键，`tdd_reviewer` /
# `e2e_validator` 交的是门禁报告（链子从 `_run_reviewer_gate` / `_run_e2e_gate` 读）。
# 测试会断言它等于 `STAGE_AGENTS - REQUIRED_OUTPUTS`——两张表一变就红，这里不许手抄两遍。
AGENTS_WITHOUT_REQUIRED_KEY = ("tdd_coder", "tdd_reviewer", "e2e_validator")


def _reason(code: str, detail: str = "") -> dict:
    return {"code": code, "detail": detail}


def level_of(assertion: Any) -> tuple[str, str]:
    """(级别, 为什么是这个级别)。认不出的 kind、缺失的 kind 都是 C 级。"""
    if not isinstance(assertion, Mapping):
        return "C", "断言不是映射，判不出级别"
    kind = assertion.get("kind")
    if kind is None:
        return "C", "没有 kind：级别推不出来，按 C 级算"
    if kind not in KIND_LEVELS:
        return "C", f"kind={kind!r} 不在这张表里，按 C 级算"
    derived = KIND_LEVELS[kind]
    declared = assertion.get("level")
    if declared in LEVELS and _STRICTNESS[declared] > _STRICTNESS[derived]:
        return declared, f"草稿自己声明 {declared} 级，比 kind={kind!r} 推出来的 {derived} 级更严，取更严的"
    if declared in LEVELS and _STRICTNESS[declared] < _STRICTNESS[derived]:
        return derived, f"草稿声明 {declared} 级，但 kind={kind!r} 只能算 {derived} 级（声明不算数）"
    return derived, f"kind={kind!r} → {derived} 级"


def _default_tables() -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """两张现成的表从链子上取，不在这里抄一份。

    延迟导入：`runtime/chain.py` 会 import 30 个 agent，判定器不该因为「有人 import 我」
    就把它们拉起来；等链子接线时也不会变成循环 import。
    """
    from runtime.chain import REQUIRED_OUTPUTS, STAGE_AGENTS

    return STAGE_AGENTS, REQUIRED_OUTPUTS


def _level_table(draft: Any) -> tuple[list[dict], list[dict]]:
    """草稿里每一条的级别，以及 C 级那几条（R 的失败证据）。"""
    graded: list[dict] = []
    if not isinstance(draft, Sequence) or isinstance(draft, (str, bytes)):
        return graded, []
    for index, item in enumerate(draft):
        level, why = level_of(item)
        kind = item.get("kind") if isinstance(item, Mapping) else None
        graded.append({"index": index, "level": level, "kind": kind, "why": why})
    return graded, [entry for entry in graded if entry["level"] == "C"]


def judge_r(draft: Any) -> dict:
    """R：草稿非空且每一条都是 A/B 级。"""
    reasons: list[dict] = []
    if not isinstance(draft, Sequence) or isinstance(draft, (str, bytes)):
        reasons.append(_reason("R_no_draft", "没有断言草稿（或它不是一张列表）"))
        return {"r": False, "levels": [], "c_levels": [], "reasons": reasons}
    graded, c_levels = _level_table(draft)
    if not graded:
        reasons.append(_reason("R_empty_draft", "断言草稿是空的：没有断言就没有可校验的东西"))
        return {"r": False, "levels": graded, "c_levels": c_levels, "reasons": reasons}
    if c_levels:
        reasons.append(
            _reason(
                "R_has_c_level",
                "第 " + "、".join(str(entry["index"]) for entry in c_levels) + " 条是 C 级",
            )
        )
    return {
        "r": not c_levels,
        "levels": graded,
        "c_levels": c_levels,
        "reasons": reasons,
    }


def judge_d(
    steps: Any,
    *,
    text: str = "",
    domain_id: str | None = None,
    registry: Any = None,
    stage_agents: Mapping[str, Any] | None = None,
    required_outputs: Mapping[str, Any] | None = None,
) -> dict:
    """D：域路由得上、stage 都在主链上、agent 登记过、依赖已声明。"""
    reasons: list[dict] = []
    unknown_stages: list[str] = []
    unknown_agents: list[str] = []
    unknown_deps: list[str] = []

    if stage_agents is None or required_outputs is None:
        default_stages, default_outputs = _default_tables()
        stage_agents = default_stages if stage_agents is None else stage_agents
        required_outputs = default_outputs if required_outputs is None else required_outputs

    from runtime.chain import MAIN_CHAIN  # 与 stage_agents 同源，避免抄第二份

    # 第 1 步：域路由。路由到 `default` 且一个关键词都没命中，等于没有领域认领这条工作单。
    routed: dict[str, Any] = {"domain_id": None, "hits": 0, "degraded_reason": None}
    if domain_id is not None:
        routed = {"domain_id": domain_id, "hits": None, "degraded_reason": None}
    else:
        chosen = registry if registry is not None else DomainRegistry()
        matched = chosen.match(text)
        routed = {
            "domain_id": matched.get("domain_id"),
            "hits": matched.get("hits"),
            "degraded_reason": matched.get("degraded_reason"),
        }
        if routed["domain_id"] == "default" and not routed["hits"]:
            routed["domain_id"] = None
    if routed["domain_id"] is None:
        reasons.append(_reason("D_no_domain", "没有任何领域认领这条工作单（default 兜底不算认领）"))

    # 第 2 步：步骤计划。
    plan: list[Mapping[str, Any]] = []
    if isinstance(steps, Sequence) and not isinstance(steps, (str, bytes)):
        plan = [step for step in steps if isinstance(step, Mapping)]
    if not plan:
        reasons.append(_reason("D_no_plan", "没有给出步骤计划，判不出能不能用既有 stage 拆"))

    # 第 3 步：stage 与 agent 都要在链子上登记过。
    registered_agents = {name for names in stage_agents.values() for name in names}
    declared: set[str] = set()
    for index, step in enumerate(plan):
        stage = step.get("stage")
        agent = step.get("agent")
        if stage not in MAIN_CHAIN:
            unknown_stages.append(str(stage))
        if agent not in registered_agents:
            unknown_agents.append(str(agent))
            continue
        # 第 4 步：这一步读的东西，**前面的**步骤得产出过。先查后记，否则一步可以
        # 依赖自己——那种计划在真链子上跑不起来（上游产物还不存在）。
        inputs = step.get("inputs")
        if isinstance(inputs, Sequence) and not isinstance(inputs, (str, bytes)):
            for wanted in inputs:
                if isinstance(wanted, str) and wanted and wanted not in declared:
                    unknown_deps.append(f"第 {index} 步读 {wanted}")
        key = required_outputs.get(agent) if isinstance(required_outputs, Mapping) else None
        if key is not None:
            declared.add(f"{agent}.{key}")
        for produced in step.get("outputs") or ():
            if isinstance(produced, str) and produced:
                declared.add(produced)

    if unknown_stages:
        reasons.append(_reason("D_unknown_stage", "主链上没有这些 stage：" + "、".join(sorted(set(unknown_stages)))))
    if unknown_agents:
        reasons.append(_reason("D_unknown_agent", "没登记过的 agent：" + "、".join(sorted(set(unknown_agents)))))
    if unknown_deps:
        reasons.append(_reason("D_unknown_dep", "依赖没被声明过：" + "、".join(unknown_deps)))

    ok = not (reasons)
    return {
        "d": ok,
        "domain": routed,
        "unknown_stages": sorted(set(unknown_stages)),
        "unknown_agents": sorted(set(unknown_agents)),
        "unknown_deps": unknown_deps,
        "reasons": reasons,
    }


def judge(
    draft: Any,
    *,
    text: str = "",
    steps: Any = None,
    domain_id: str | None = None,
    registry: Any = None,
    stage_agents: Mapping[str, Any] | None = None,
    required_outputs: Mapping[str, Any] | None = None,
) -> dict:
    """判一条工作单走哪条轨。只读、无副作用。"""
    r = judge_r(draft)
    d = judge_d(
        steps,
        text=text,
        domain_id=domain_id,
        registry=registry,
        stage_agents=stage_agents,
        required_outputs=required_outputs,
    )
    track = PRODUCTION if (r["r"] and d["d"]) else EXPLORATION
    reasons = [dict(item, side="R") for item in r["reasons"]] + [
        dict(item, side="D") for item in d["reasons"]
    ]
    if track == PRODUCTION:
        reasons.append(_reason("R_and_D_hold", "R 与 D 都成立：可以走生产轨"))
    return {
        "version": TRACK_JUDGE_VERSION,
        "track": track,
        "r": r["r"],
        "d": d["d"],
        "levels": r["levels"],
        "c_levels": r["c_levels"],
        "domain": d["domain"],
        "unknown_stages": d["unknown_stages"],
        "unknown_agents": d["unknown_agents"],
        "unknown_deps": d["unknown_deps"],
        # D 不成立而 R 成立：仍然走探索轨，但这次的断言下一轮可以直接拿来用。
        "assertions_reusable": bool(r["r"]),
        "reasons": reasons,
    }


def assertion_candidates(result: Any) -> dict:
    """从一次运行的**结果**里抽出下一轮可以当断言的候选（164 表 1 的假阴纠正）。

    只认这次运行真拿到过的、机器能再校验一遍的事实：`execution`（exit_code / 用例结构）、
    `gates.*.assertions`、`verification_level`、产物里带的 `sha256`。**不认** mock 证据：
    `execution.stub` 为真时候选仍然列出来，但 `promotable` 一律为假——门禁二拦的就是
    拿回放当验证，这里不能在判定器上开同一个口子。
    """
    candidates: list[dict] = []
    reasons: list[dict] = []
    stub = False

    if isinstance(result, Mapping):
        execution = result.get("execution")
        if isinstance(execution, Mapping) and execution.get("executed"):
            stub = bool(execution.get("stub"))
            candidates.append(
                {
                    "id": "execution.exit_code",
                    "kind": "exit_code",
                    "value": execution.get("exit_code"),
                    "evidence": "stub" if stub else "run",
                }
            )
            for case in execution.get("case_results") or ():
                if isinstance(case, Mapping) and case.get("status"):
                    # 用例的标识两种写法都收：链子上叫 `case_id`，历史记录里也有 `id`。
                    label = case.get("case_id") or case.get("id")
                    candidates.append(
                        {
                            "id": f"execution.case.{label}",
                            "kind": "field_equals",
                            "value": case.get("status"),
                            "evidence": "stub" if stub else "run",
                        }
                    )
        gates = result.get("gates")
        if isinstance(gates, Mapping):
            for agent, report in sorted(gates.items()):
                if not isinstance(report, Mapping):
                    continue
                for name, verdict in sorted((report.get("assertions") or {}).items()):
                    candidates.append(
                        {
                            "id": f"gates.{agent}.assertions.{name}",
                            "kind": "field_equals",
                            "value": verdict,
                            "evidence": "stub" if stub else "run",
                        }
                    )
        level = result.get("verification_level")
        if isinstance(level, str) and level:
            candidates.append(
                {"id": "verification_level", "kind": "field_equals", "value": level,
                 "evidence": "stub" if stub else "run"}
            )
        artifacts = result.get("artifacts")
        if isinstance(artifacts, Mapping):
            for agent, artifact in sorted(artifacts.items()):
                digest = artifact.get("sha256") if isinstance(artifact, Mapping) else None
                if isinstance(digest, str) and digest:
                    candidates.append(
                        {
                            "id": f"artifacts.{agent}.sha256",
                            "kind": "sha256",
                            "value": digest,
                            "evidence": "stub" if stub else "run",
                        }
                    )

    for candidate in candidates:
        candidate["level"] = level_of(candidate)[0]
    promotable = bool(candidates) and not stub and all(c["level"] != "C" for c in candidates)
    if stub:
        reasons.append(_reason("candidates_from_stub", "这次运行用的是桩/回放，候选不算证据"))
    if not candidates:
        reasons.append(_reason("no_candidates", "这次运行没有留下机器能再校验的事实"))
    elif not promotable and not stub:
        reasons.append(_reason("candidates_have_c_level", "候选里有 C 级：进不了生产轨"))
    return {"candidates": candidates, "promotable": promotable, "stub": stub, "reasons": reasons}
