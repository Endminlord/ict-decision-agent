"""Injected, synchronous main-chain scheduler; Python standard library only.

Agent contract: agents[name](context) -> dict. Adapters for concrete Agent
signatures belong to the caller. Each callback receives user_input, timestamp,
state, llm_client, stage, agent and artifacts keyed by upstream Agent name.

Gate assertions are a mapping of IDs to "pass"/"fail", or a list of
{"id": ..., "result": ...} records. Reviewer may wrap its report in
"review_report"; e2e-validator returns a structured report directly.
B5 is mandatory for execution={"stub": False, ...}; otherwise it is not_run.
Optional state["sidepaths"] maps stages to lists of injected L2/L3/L4 names.
Non-gate Agent exceptions only append degradation records; they never stop
the main chain. Gate exceptions are fail-closed and produce rollback signals.
Optional store is a callable accepting an archive record. Its failure is
recorded, never used as a third hard gate. This module performs no retries,
circuit breaking, fallback execution, Agent construction or subprocess work.
"""

from __future__ import annotations

import inspect
import re
from time import monotonic
from typing import Any, Callable, Mapping, Sequence


MAIN_CHAIN = ("S0", "S1", "S2", "S3", "S5", "S6", "S7")
STAGE_AGENTS = {
    "S0": ("orchestrator",),
    "S1": ("hermes",),
    "S2": ("prd_builder", "req_writer"),
    "S3": ("sdd_spec_builder", "trd_adr_generator"),
    "S5": ("tdd_planner", "tdd_coder", "tdd_reviewer"),
    "S6": ("e2e_validator",),
    "S7": (),
}
AGENT_NUMBERS = {
    name: number
    for number, name in enumerate(
        (name for stage in MAIN_CHAIN for name in STAGE_AGENTS[stage]), 1
    )
}
AGENT_ORDER = tuple(
    name for stage in MAIN_CHAIN for name in STAGE_AGENTS[stage]
)
# 一场运行**走到哪一步为止**，是用户选的，不是链子猜的。
#
# 三个模式都是 MAIN_CHAIN 的**前缀**，这不是巧合而是纪律：模式只决定「到哪儿停」，
# 不决定「怎么走」。一旦允许模式改变顺序或跳过中间阶段，下游就会在缺输入的情况下
# 跑起来——而那种失败看起来像模型不行，其实是编排错了。
#
# 为什么叫 stages 而不是重用一个 mode 字段：传输模式（demo/live，跑不跑真模型）
# 和深度模式（ask/plan/code，跑到哪一步）是两个轴。两个轴共用一个名字，
# 迟早会有人以为「live 就是 code」。
MODE_STAGES = {
    # 只问到「这需求到底要什么」为止：出需求，不写代码、不验证。
    "ask": ("S0", "S1", "S2"),
    # 再问到「打算怎么做」：多一份技术规格。仍然不写代码。
    "plan": ("S0", "S1", "S2", "S3"),
    # 走完整条链，包括实现、端到端和归档。
    "code": MAIN_CHAIN,
}
MODES = tuple(MODE_STAGES)
# 界面上那句人话。放在内核里，是因为「ask 到底跑到哪」和「ask 叫什么」必须一起改——
# 分开放，迟早会出现一个写着「只出需求」、实际跑到规格的模式。
MODE_LABELS = {
    "ask": "问一问（到需求为止）",
    "plan": "先计划（到技术规格为止）",
    "code": "写代码（走完整条链）",
}


def stages_for_mode(mode: str) -> tuple:
    """这个模式要跑哪些阶段。认不出来的模式直接拒绝，不猜一个默认的。"""
    if mode not in MODE_STAGES:
        raise ValueError("unknown mode: " + repr(mode))
    return MODE_STAGES[mode]


REVIEW_ASSERTIONS = ("A1", "A2", "A3")
E2E_ASSERTIONS = ("B1", "B2", "B3", "B4", "B5", "B6")
ROLLBACK_TARGETS = {9: (8,), 10: (7, 8)}
ROLLBACK_STATUSES = {9: "rollback_9_to_8", 10: "rollback_10_to_7_or_8"}

# Each L1 Agent must return the field its downstream consumers read. An Agent
# that returns an empty or partial dict is a structural failure, not a
# degradation: continuing would let a later stage "pass" on missing input.
REQUIRED_OUTPUTS = {
    "orchestrator": "task_dag",
    "hermes": "preference_pack",
    "prd_builder": "prd",
    "req_writer": "requirements_md",
    "sdd_spec_builder": "spec",
    "trd_adr_generator": "trd",
    "tdd_planner": "test_cases",
}


def required_outputs_for(workflow: Any) -> dict[str, str]:
    """Which result key each Agent must return: the default table, overlaid.

    ``workflow`` is a workflow declaration (a `workflows/*.json` entry — the same
    shape `tools/gate2.py::judge` receives). Its optional ``required_outputs``
    maps an Agent name to the key that Agent's result must carry, so a workflow
    whose Agent returns a differently named field can say so, instead of the
    chain hardcoding one answer for every workflow.

    Absent, malformed or partly unreadable declarations leave the default table
    exactly as it is: this is a knob a declaration may turn, not a gate it can
    open. A declaration that says nothing must run byte-for-byte as before, and
    one that says something unreadable must not quietly drop a structural check
    — there is deliberately no way to *remove* a requirement, only to rename it.
    """
    table = dict(REQUIRED_OUTPUTS)
    if not isinstance(workflow, Mapping):
        return table
    declared = workflow.get("required_outputs")
    if not isinstance(declared, Mapping):
        return table
    for agent_name, key in declared.items():
        if isinstance(agent_name, str) and agent_name and isinstance(key, str) and key:
            table[agent_name] = key
    return table


def dispatch_plan(stages: Sequence[str], sidepaths: Any = None) -> tuple:
    """这一场运行的调度计划：按 (阶段, Agent) 排好的完整顺序（11-B 第 14 项 B1）。

    为什么要有这张表：`span_id` 以前是「这是第几次调用」的函数 —— 一个共享的计数器，
    谁先被调用谁拿走下一个数。那个值是**执行历史**的函数，不是**这一场是什么**的
    函数：多挂一条 sidepath、少跑一站、回滚重来，后面每个 Agent 的 id 都会跟着挪，
    于是同一条 fixture 两次跑出来的事件对不上（门禁一要的是逐字节相等）。

    `stages` 传**模式的完整前缀**，不传续跑收窄后的阶段：这样同一个 Agent 在「从头跑」
    和「接着跑」里拿到同一个位置。计划里的位置只由 (阶段, Agent) 决定 —— 与谁先跑完、
    与调用顺序都无关，这也是它敢在将来的并发派发下继续用的原因。

    sidepath 的读法**故意和 `_run_stage` 一致**：读不动、不是列表、名字不是非空字符串、
    或者名字本身是 L1 Agent（`_run_stage` 会拒绝重挂），都不进计划 —— 那些调用不会发生。
    """
    plan: list = []
    seen: set = set()
    mounts = sidepaths if isinstance(sidepaths, Mapping) else {}
    for stage in stages:
        names = list(STAGE_AGENTS.get(stage, ()))
        mounted = mounts.get(stage, ())
        if isinstance(mounted, (list, tuple)):
            names += [name for name in mounted
                      if isinstance(name, str) and name and name not in AGENT_NUMBERS]
        for agent_name in names:
            # 同一条 sidepath 挂两次在 `_run_stage` 里会跑两次，但**位置只占一个**：
            # 第二次调用由 `_invoke_agent` 的「让开计划长度」处理。
            if (stage, agent_name) in seen:
                continue
            seen.add((stage, agent_name))
            plan.append((stage, agent_name))
    return tuple(plan)


def order_events(events: Sequence[Mapping]) -> list:
    """事件的**逻辑**顺序：(第几遍, 计划里的位置)（11-B 第 14 项 B1）。

    `stage_events` 以前是**追加**出来的：谁先跑完谁先落。顺序派发时它恰好等于计划
    顺序，所以看不出问题；可一旦派发顺序变了（将来的并发、乱序挂上的 sidepath），
    同一个输入的两次运行就会写出两份不同的事件流 —— 而门禁一比的就是这两份。
    所以顺序由计划给，不由「谁先完成」给。

    同一 (遍, 位置) 的事件保持它们原来的先后（`sorted` 是稳定的）；认不出位置的
    排在后面（位置解析不出来时按 0 算，`span_id` 的末段就是计划位置）。
    """
    def key(event: Mapping) -> tuple:
        try:
            attempt = int(event.get("attempt") or 0)
        except (TypeError, ValueError):
            attempt = 0
        tail = str(event.get("span_id") or "").rsplit(".", 1)[-1]
        return (attempt, int(tail) if tail.isdigit() else 0)

    return sorted(events, key=key)


class InvalidAgentOutput(RuntimeError):
    """An Agent returned a structurally unusable result."""


class AssertionEvidenceMismatch(RuntimeError):
    """VERIFY.md 的断言与 Agent 给出的数据不是同一份 —— fail loud，不挑一个信。

    今天两条路各走各的：`report["assertions"]`（数据）与 `verify_md`（markdown）。
    数据说 pass、markdown 说 fail 时没有任何东西会报（research/222 §6 第 1 条）。
    """


def _read_assertions(report: dict) -> dict:
    """Reject malformed/duplicate assertion records rather than guessing."""
    raw = report.get("assertions", {})
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, list):
        raise ValueError("assertions must be a mapping or a list")
    assertions = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("invalid assertion record")
        identifier = item["id"]
        if identifier in assertions:
            raise ValueError("duplicate assertion: " + identifier)
        assertions[identifier] = item.get("result")
    return assertions


def _e2e_assertion_evidence(report: Mapping) -> tuple:
    """把 e2e 报告的两条证据面放在一起判，返回 `(source, issues)`。

    * `data+markdown` —— 两面都在且逐条相等（正常态）
    * `markdown-only` —— Agent 没给数据，markdown 是唯一源；**这件事被记下来**，
      不静默退回（research/222 §6 第 2 条）
    * `data-only` —— 没有 markdown 可对；同样是降级态，不是等价
    * `none` —— 两面都没有，gate 照旧会判 fail

    `issues` 非空 ⇒ 两面**都在但不一致** ⇒ 调用方必须 fail loud。
    """
    from tools.verify_md_schema import equivalence  # 与 agents/bootstrap 同一个解析器

    data = report.get("assertions")
    text = report.get("verify_md")
    if not isinstance(text, str) or not text.strip():
        return ("data-only" if data else "none"), []
    verdict = equivalence(data, text)
    if not verdict["ok"]:
        return "mismatch", list(verdict["issues"])
    return ("markdown-only" if verdict["source"] == "markdown-only" else "data+markdown"), []


class Chain:
    def __init__(
        self,
        agents: Mapping[str, Callable[..., dict]],
        *,
        llm_client: Any = None,
        store: Any = None,
        budget: ContextBudget | None = None,
        on_event: Any = None,
        should_stop: Any = None,
        before_stage: Any = None,
        mode: str = "code",
        journal: Any = None,
        dedupe: Any = None,
    ) -> None:
        if not isinstance(agents, Mapping):
            raise TypeError("agents must be an injected mapping")
        # 在构造时校验，不在跑起来之后才发现：一个错字写的模式应该当场被拒，
        # 而不是安静地跑完整条链然后声称自己是 ask。
        stages_for_mode(mode)
        if budget is not None and not isinstance(budget, ContextBudget):
            raise TypeError("budget must be a ContextBudget or None")
        self.agents = dict(agents)
        self.mode = mode
        self.llm_client = llm_client
        self.store = store
        self.budget = budget or ContextBudget()
        # Progress callback. The chain already builds a per-invocation event; a
        # caller that wants live progress (an SSE stream, a log tail) needs to
        # see it as it happens rather than only in the final result.
        self.on_event = on_event if callable(on_event) else None
        # 用户按了「停止」时该问的人。在**阶段边界**上问：一个阶段里的 Agent 调用
        # 可能正卡在一次模型请求里，硬打断它会留下半个产物——而半个产物比没有产物
        # 更难判断，因为下游分不清「还没写」和「写了一半」。
        self.should_stop = should_stop if callable(should_stop) else None
        # 调试器问的人。**同一处边界、同一个理由**：停在一站中间会留下半个产物，
        # 而半个产物比没有产物更难判断。钩子拿到 (站名, 上下文) —— 上下文里有
        # 已经跑完的站和已经产出的东西，「要不要放这一站过去」得看着它们决定。
        # 返回 False = 别跑了（和用户按停止走同一条路，因为**就是**同一件事）。
        self.before_stage = before_stage if callable(before_stage) else None
        # 阶段边界上的 checkpoint（11-B 第 6 项，`runtime/journal.py`）。
        # 默认 None ⇒ 不记任何东西；记账**永远不抛**（一个能把运行干掉的日志
        # 最后会被关掉，然后就没有日志了）。
        self.journal = journal
        # 幂等双键（11-B 第 12 项）。默认 None ⇒ 一次都不判，行为与从前一致。
        # 判据本身是纯函数（`runtime/idempotency.py`），这里只负责把结论变成终态。
        self.dedupe = dedupe

    def run(
        self,
        user_input: str,
        timestamp: str,
        state: dict | None = None,
        *,
        resume: Mapping[str, Any] | None = None,
        trace: Mapping[str, Any] | None = None,
    ) -> dict:
        """Run one pass over the main chain.

        ``resume`` re-enters at a named Agent instead of starting at S0. It must
        carry ``start_at`` (the Agent to begin with) and ``artifacts`` (the
        artifacts produced before that point); artifacts after the resume point
        are discarded so a stale downstream result cannot be mistaken for a
        fresh one.
        """
        if state is not None and not isinstance(state, dict):
            raise TypeError("state must be a dict or None")
        context = {
            "user_input": user_input,
            "timestamp": timestamp,
            "state": dict(state) if state is not None else {},
            "task_id": None,
            "stage": None,
            "status": "running",
            "artifacts": {},
            "gates": {},
            "stage_events": [],
            "completed_stages": [],
            "errors": [],
            "degraded": [],
            "rollback_signal": None,
            "archive": None,
            "archive_persisted": False,
            "verification_level": "not_verified",
            "reason": "",
            "resume_start": None,
            # 这一遍是接着上一遍跑的、从哪个 Agent 接的。与 `resume_start` 的区别：
            # 那个是**待消费**的标记（第一站用掉就置 None），这个是**这一次运行的身份**，
            # 报告里必须留得住 —— 否则「跑完了」和「接着跑完了」长得一模一样。
            "resumed_from_agent": None,
            "trace": dict(trace) if trace else {},
            "span_seq": 0,
            "started_at": monotonic(),
            "mode": self.mode,
        }
        # 工作流声明（可选）：声明点名了某个 Agent 必须返回的键，就用它。
        # 没有声明 ⇒ 与从前逐字节一致（11-B 第 14 项 B3）。
        context["required_outputs"] = required_outputs_for(context["state"].get("workflow"))
        # 模式决定**到哪儿停**，不决定**怎么走**：三个模式都是 MAIN_CHAIN 的前缀，
        # 所以下面那个「阶段必须按序到达」的检查一个字都不用改。
        mode_stages = stages_for_mode(self.mode)
        stages = mode_stages
        # 调度计划（11-B 第 14 项 B1）：`span_id` 的位置由**这一场是什么**决定，
        # 不由**跑到这里为止调用了几次**决定。按模式的完整前缀算，所以「从头跑」
        # 和「接着跑」里同一个 Agent 的位置相同。
        plan = dispatch_plan(mode_stages, context["state"].get("sidepaths"))
        context["span_index"] = {pair: index for index, pair in enumerate(plan, start=1)}
        if resume is not None:
            if not isinstance(resume, Mapping):
                raise TypeError("resume must be a mapping")
            order = AGENT_ORDER
            start_at = resume.get("start_at")
            if start_at not in order:
                raise ValueError("resume.start_at must name a main-chain Agent")
            keep = set(order[: order.index(start_at)])
            supplied = resume.get("artifacts") or {}
            if not isinstance(supplied, Mapping):
                raise TypeError("resume.artifacts must be a mapping")
            # Only artifacts strictly before the resume point survive.
            context["artifacts"] = {k: v for k, v in supplied.items() if k in keep}
            gates = resume.get("gates") or {}
            if not isinstance(gates, Mapping):
                raise TypeError("resume.gates must be a mapping")
            context["gates"] = dict(gates)
            stage = next(s for s in MAIN_CHAIN if start_at in STAGE_AGENTS[s])
            first = MAIN_CHAIN.index(stage)
            # 续跑只在这个模式够得着的范围内算数：一个 ask 运行不该因为续跑点
            # 写在 S5，就凭空多出「S0-S3 都跑过了」这种记录。
            reachable = tuple(s for s in mode_stages if MAIN_CHAIN.index(s) >= first)
            if not reachable:
                raise ValueError(
                    "resume.start_at is beyond what mode "
                    f"{self.mode!r} runs: {start_at}"
                )
            context["completed_stages"] = list(
                s for s in mode_stages if MAIN_CHAIN.index(s) < first
            )
            context["resume_start"] = start_at
            context["resumed_from_agent"] = start_at
            stages = reachable

        if self.journal is not None:
            try:
                self.journal.run_start(
                    run_id=str(context["trace"].get("trace_id") or "run"),
                    user_input=user_input, timestamp=timestamp, mode=self.mode,
                    stages=stages, resume_start=context["resume_start"],
                    trace=context["trace"],
                )
            except Exception:
                pass

        # 幂等双键：重复投递 / 重复劳动在**跑任何一站之前**收口。重复是外部世界的
        # 行为，不是任务失败 —— 所以它落在 `skipped` 上，而 `skipped` 是成功
        # （`runtime/idempotency.SUCCESS_STATUSES`，`fuse.judge_exit` 也认它）。
        if self.dedupe is not None:
            verdict = idempotency.check(user_input, **self.dedupe)
            context["dedupe"] = verdict
            if verdict["duplicate"]:
                context["status"] = "skipped"
                context["reason"] = verdict["reason"]
                context["skip_reason"] = verdict["reason"]
                return self._finish("skipped", context)

        # Agent exceptions are isolated at each invocation, not around the
        # whole chain. A non-gate failure cannot skip the rest of a stage.
        #
        # 回链（11-B 第 13 项）：`gate()` 在很深的地方被调用（capability 派发之前、
        # `Popen` 之前），那里没有「这是哪一场运行」的概念，而收件箱里的
        # `pending_action` 需要 `run_id`/`workflow_id` 才能被这场运行认领回去。
        token = safety_tiers.bind_run(
            run_id=(context["trace"] or {}).get("trace_id"),
            workflow_id=(context["trace"] or {}).get("workflow_id"),
        )
        try:
            context = self._run_stages(stages, context)
        except safety_tiers.ActionQueued as exc:
            # 排队档（§11.5）：闸门在 enforce 模式下**已经把 `pending_action` 写进
            # 收件箱**才抛的这个异常。这一站跑不动了 —— 但这不是失败：链在这里收束，
            # 等人（在链外、另一个 run 里）把那条动作接回去。捕获点选在阶段循环外面
            # 而不是各个 `bus.call` 上：出口只有一个，将来新增的第三条调用路也不会漏。
            context["status"] = "suspended"
            context["reason"] = "action_queued"
            context["pending_action"] = dict(getattr(exc, "pending", None) or {})
            return self._finish("suspended", context)
        finally:
            safety_tiers.reset_run(token)
        return self._finish(context["status"], context)

    def _run_stages(self, stages: Sequence[str], context: dict) -> dict:
        """Run the stages in order; every exit is a `context["status"]` value.

        Split out of `run()` for one reason: the queue tier arrives as an exception
        from deep inside a stage, and there has to be exactly one place that turns
        it into a terminal state. `run()` is that place; this is the loop it wraps.
        """
        for stage in stages:
            if self.should_stop is not None and self.should_stop():
                # 停在阶段边界，并且**如实**叫这个名字：不是 failed（没有东西坏掉），
                # 也不是 completed（没跑完）。把用户的停止说成失败，会让报告里那次
                # 运行看起来像机器的问题。
                context["status"] = "stopped_by_user"
                context["stopped_before"] = stage
                context["reason"] = f"stopped_by_user:before_{stage}"
                return context
            if self.before_stage is not None and self.before_stage(stage, context) is False:
                # 调试器说「这一站别跑了」。状态和上面那条**故意一模一样**：
                # 有人在阶段边界叫它停，就是有人在阶段边界叫它停——区别在谁叫的，
                # 那件事由调试器自己发一条事件说清楚，不靠这里编一个新状态名。
                context["status"] = "stopped_by_user"
                context["stopped_before"] = stage
                context["reason"] = f"stopped_by_user:before_{stage}"
                return context
            context = self._run_stage(stage, context)
            if self.journal is not None:
                try:
                    self.journal.stage_end(stage, context)
                except Exception:
                    pass
            if context["status"] != "running":
                # Any stop (rollback, failed, paused) ends the pass; advancing
                # would run a stage whose inputs were never produced.
                return context
        context["status"] = "completed"
        return context

    def _finish(self, status: str, context: dict) -> dict:
        """唯一的终态出口 —— 日志也在这里收尾（出口有四个，这里只有一个）。"""
        result = self._build_result(status, context)
        if self.journal is not None:
            try:
                self.journal.run_end(result, context)
            except Exception:
                pass
        return result

    def _run_stage(self, stage: str, context: dict) -> dict:
        if stage not in STAGE_AGENTS:
            raise ValueError("unknown stage: " + stage)
        index = len(context["completed_stages"])
        if index >= len(MAIN_CHAIN) or MAIN_CHAIN[index] != stage:
            raise ValueError("out-of-order stage: " + stage)
        if context["status"] != "running":
            raise RuntimeError("cannot advance a stopped chain")
        context["stage"] = stage
        names = STAGE_AGENTS[stage]
        start_at = context.get("resume_start")
        if start_at is not None and start_at in names:
            # A resumed pass skips the Agents the previous pass already produced.
            names = names[names.index(start_at):]
            context["resume_start"] = None
        self._announce({"phase": "started", "stage": stage, "agents": list(names)})
        for agent_name in names:
            if agent_name not in ("tdd_reviewer", "e2e_validator"):
                try:
                    self._invoke_agent(agent_name, context)
                except InvalidAgentOutput as exc:
                    # A structurally invalid Agent output is a hard stop, not a
                    # degradation: downstream stages cannot be trusted.
                    context["status"] = "failed"
                    context["reason"] = str(exc)
                    return context
                except Exception as exc:
                    context["degraded"].append({
                        "component": agent_name,
                        "reason": f"{type(exc).__name__}: {exc}",
                    })
                continue

            # Missing, throwing or malformed hard gates must not pass silently.
            try:
                if agent_name == "tdd_reviewer":
                    gate = self._run_reviewer_gate(context)
                else:
                    gate = self._run_e2e_gate(context)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                if agent_name == "e2e_validator":
                    # A validator that cannot run cannot certify anything.
                    context["errors"].append({
                        "stage": stage,
                        "component": agent_name,
                        "reason": reason,
                    })
                    context["status"] = "failed"
                    context["reason"] = reason
                    return context
                required = REVIEW_ASSERTIONS
                gate = {
                    "verdict": "fail",
                    "assertions": {key: "not_verified" for key in required},
                    "execution_verified": False,
                    "reason": reason,
                }
                context["errors"].append({
                    "stage": stage,
                    "component": agent_name,
                    "reason": reason,
                })
            context["gates"][agent_name] = gate
            if gate["verdict"] != "pass":
                source = AGENT_NUMBERS[agent_name]
                context["rollback_signal"] = self._make_rollback(
                    source, ROLLBACK_TARGETS[source], gate["reason"]
                )
                context["status"] = ROLLBACK_STATUSES[source]
                return context
            if agent_name == "e2e_validator":
                # Both gates passed on measured evidence; the level records that
                # the contract assertions held, not that coverage was measured.
                context["verification_level"] = "contract_pass"
        # Optional sidepaths are explicitly mounted without changing the
        # ten L1 Agents in STAGE_AGENTS. Configuration errors also degrade.
        try:
            mounts = context["state"].get("sidepaths", {})
            if not isinstance(mounts, Mapping):
                raise TypeError("state['sidepaths'] must be a stage mapping")
            sidepaths = mounts.get(stage, ())
            if not isinstance(sidepaths, (list, tuple)):
                raise TypeError("sidepath mounts must be a list or tuple")
        except Exception as exc:
            context["degraded"].append({
                "component": "sidepaths:" + stage,
                "reason": f"{type(exc).__name__}: {exc}",
            })
            sidepaths = ()

        for agent_name in sidepaths:
            try:
                if not isinstance(agent_name, str) or not agent_name:
                    raise TypeError("sidepath Agent name must be a nonempty string")
                if agent_name in AGENT_NUMBERS:
                    raise ValueError("L1 Agents cannot be remounted as sidepaths")
                self._invoke_agent(agent_name, context)
            except Exception as exc:
                context["degraded"].append({
                    "component": agent_name if isinstance(agent_name, str)
                    else "sidepaths:" + stage,
                    "reason": f"{type(exc).__name__}: {exc}",
                })

        if stage == "S7":
            try:
                context["archive"] = self._finalize_archive(context)
            except Exception as exc:
                context["degraded"].append({
                    "component": "archive",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
        context["completed_stages"].append(stage)
        return context

    def _invoke_agent(
        self, agent_name: str, context: dict
    ) -> dict:
        callback = self.agents.get(agent_name)
        started = monotonic()
        trace = context.get("trace") or {}
        # span_id 由**计划里的位置**决定，不由「这是第几次调用」决定（11-B 第 14 项 B1）：
        # 计数器是共享可变状态，任何重排都会把它推着走，同一个 Agent 于是拿到不同的 id。
        index = context.get("span_index") or {}
        pair = (context.get("stage"), agent_name)
        slot = index.get(pair)
        if slot is None:
            # 不在计划里的调用（直接调本函数的调用方）：仍然给唯一 id，而且从计划之后
            # 开始编号，免得撞上计划里的位置。
            context["span_seq"] = context.get("span_seq", 0) + 1
            slot = max(index.values(), default=0) + context["span_seq"]
        else:
            # 同一个 (阶段, Agent) 被调用第二次（重复挂载的 sidepath）时，让开计划长度个
            # 位置：唯一性由算术保证，不由调用顺序保证。
            runs = context.setdefault("span_runs", {})
            runs[pair] = runs.get(pair, 0) + 1
            slot = slot + (runs[pair] - 1) * len(index)
        context["span_seq"] = slot
        trace_id = str(trace.get("trace_id") or "untraced")
        attempt = int(trace.get("attempt", 0))
        # span_id is unique per Agent invocation across the whole logical run,
        # so events from different dispatch attempts stay distinguishable.
        event = {
            "trace_id": trace_id,
            "attempt": attempt,
            "span_id": f"{trace_id}.{attempt}.{slot}",
            "stage": context["stage"],
            "agent": agent_name,
        }
        try:
            if not callable(callback):
                raise RuntimeError("required Agent not mounted: " + agent_name)
            inputs = {
                "user_input": context["user_input"],
                "timestamp": context["timestamp"],
                "state": dict(context["state"]),
                "llm_client": self.llm_client,
                "stage": context["stage"],
                "agent": agent_name,
                "artifacts": dict(context["artifacts"]),
            }
            result = callback(inputs)
            if not isinstance(result, dict):
                raise TypeError(agent_name + " must return a dict")
            if result.get("error"):
                raise RuntimeError(f"{agent_name}: {result['error']}")
            context["artifacts"][agent_name] = result
            if agent_name == "orchestrator":
                dag = result.get("task_dag", {})
                context["task_id"] = (
                    dag.get("task_id") if isinstance(dag, dict) else None
                ) or result.get("task_id")
            table = context.get("required_outputs") or REQUIRED_OUTPUTS
            required = table.get(agent_name)
            if required is not None and not result.get(required):
                raise InvalidAgentOutput("invalid_agent_output")
            if agent_name == "tdd_planner":
                test_cases = result.get("test_cases") or {}
                cases = test_cases.get("cases")
                if not isinstance(cases, list) or not cases:
                    # The planner already records which of the three causes it
                    # was; reporting the bare message threw that away, and the
                    # planner's own docstring says the caller reports it.
                    reason = test_cases.get("stub_reason")
                    raise InvalidAgentOutput(
                        "no_executable_cases" + (f":{reason}" if reason else "")
                    )
            event["status"] = "returned"
            return result
        except Exception as exc:
            event["status"] = "error"
            event["reason"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            event["elapsed_ms"] = round((monotonic() - started) * 1000, 3)
            context["stage_events"].append(event)
            self._announce(event)

    def _announce(self, event: dict) -> None:
        """发一个只给**正在看着这场运行的人**看的事件。

        和 stage_events 分开：那里是证据（哪个 Agent 跑完了、花了多久、什么
        时候），而「某个阶段开始过」证明不了别的——进程半路死了它只能证明
        它开始过。所以它只走这条实时通道，不落进工单。

        监听者坏了不该弄坏它正在看的那场运行：异常吞掉，运行继续。
        """
        if self.on_event is None:
            return
        try:
            self.on_event(dict(event))
        except Exception:
            pass

    def _run_reviewer_gate(self, context: dict) -> dict:
        result = self._invoke_agent("tdd_reviewer", context)
        report = result.get("review_report", result)
        if not isinstance(report, dict):
            raise TypeError("review_report must be a dict")
        assertions = _read_assertions(report)
        failed = [key for key in REVIEW_ASSERTIONS
                  if assertions.get(key) != "pass"]
        passed = (
            report.get("verdict") == "pass"
            and not failed
            and not report.get("rollback_signal")
            and not result.get("rollback_signal")
        )
        reason = "" if passed else str(
            report.get("failure_reason")
            or ("assertions_not_passed:" + ",".join(failed) if failed
                else "reviewer_did_not_pass")
        )
        return {
            "verdict": "pass" if passed else "fail",
            "assertions": {key: assertions.get(key, "not_verified")
                           for key in REVIEW_ASSERTIONS},
            "reason": reason,
        }

    def _run_e2e_gate(self, context: dict) -> dict:
        reviewer = context["gates"].get("tdd_reviewer", {})
        if reviewer.get("verdict") != "pass":
            raise RuntimeError("e2e requires the Reviewer gate to pass")
        report = self._invoke_agent("e2e_validator", context)
        assertions_source, issues = _e2e_assertion_evidence(report)
        if issues:
            # 两面都在但不一致：不挑一个信，也不猜哪个是"对的"。
            raise AssertionEvidenceMismatch(
                "verify_md 与 assertions 不一致: " + "; ".join(issues)
            )
        assertions = _read_assertions(report)
        execution = report.get("execution")
        real_execution = (
            isinstance(execution, dict) and execution.get("stub") is False
        )
        # B6 is checked here rather than in the B5 branch: a spec whose
        # interfaces are not all covered is a failure regardless of whether the
        # one function that was implemented happens to run correctly.
        failed = [key for key in (*E2E_ASSERTIONS[:4], "B6")
                  if assertions.get(key) != "pass"]
        b5 = assertions.get("B5", "not_run")
        if real_execution:
            if b5 != "pass":
                failed.append("B5")
        elif b5 not in ("not_run", "not_verified", "skipped"):
            # A B5 pass without non-stub evidence cannot imply real execution.
            failed.append("B5")
        passed = (
            report.get("verdict") == "pass"
            and not failed
            and not report.get("rollback_signal")
        )
        reason = "" if passed else str(
            report.get("failure_reason")
            or ("assertions_not_passed:" + ",".join(failed) if failed
                else "e2e_validator_did_not_pass")
        )
        return {
            "verdict": "pass" if passed else "fail",
            "assertions": {
                **{key: assertions.get(key, "not_verified")
                   for key in (*E2E_ASSERTIONS[:4], "B6")},
                "B5": b5,
            },
            "execution_verified": passed and real_execution and b5 == "pass",
            "reason": reason,
            # 这份门禁的断言是拿哪一面读出来的：data+markdown / markdown-only /
            # data-only / none。数据缺失不再静默（research/222 §6 第 2 条）。
            "assertions_source": assertions_source,
        }

    def _make_rollback(
        self,
        source_agent: int,
        candidate_targets: tuple[int, ...],
        reason: str,
    ) -> dict:
        if source_agent not in ROLLBACK_TARGETS:
            raise ValueError("unsupported rollback source")
        if candidate_targets != ROLLBACK_TARGETS[source_agent]:
            raise ValueError("rollback targets must match the declared edges")
        return {
            "source_agent": source_agent,
            "candidate_targets": candidate_targets,
            "target_agent": candidate_targets[0]
            if len(candidate_targets) == 1 else None,
            "reason": reason,
            "handled_by": "fuse.py",
            "requires_dispatch": True,
            "auto": False,
        }

    def _finalize_archive(self, context: dict) -> dict:
        for name in ("tdd_reviewer", "e2e_validator"):
            if context["gates"].get(name, {}).get("verdict") != "pass":
                raise RuntimeError("cannot archive success before both gates pass")
        archive = {
            "task_id": context["task_id"],
            "user_input": context["user_input"],
            "timestamp": context["timestamp"],
            "artifacts": dict(context["artifacts"]),
            "gates": dict(context["gates"]),
            "verification_level": context["verification_level"],
        }
        # Persistence deliberately lives in _record, not here: this method only
        # runs at S7, so storing here meant a failed or paused run left no trace.
        return archive

    def _budgeted_artifacts(self, context: dict) -> tuple[dict, dict]:
        """Bound the artifact working set for the returned result.

        Applied on the way out, never during the run: the live chain needs the
        full artifacts to feed downstream stages.
        """
        return self.budget.fit(context["artifacts"])

    def _build_result(
        self, status: str, context: dict
    ) -> dict:
        budgeted = self._budgeted_artifacts(context)
        return {
            "status": status,
            "task_id": context["task_id"],
            "stage": context["stage"],
            # 一场 ask 运行和一场 code 运行，`completed_stages` 会长得不一样——
            # 但那只有读了阶段列表才看得出来。模式要单独记一笔，因为
            # 「跑到 S2 就停了」和「只打算跑到 S2」在报告里必须能分开。
            "mode": context.get("mode", "code"),
            "completed_stages": list(context["completed_stages"]),
            "artifacts": budgeted[0],
            "context_budget": budgeted[1],
            "gates": dict(context["gates"]),
            "rollback_signal": context["rollback_signal"],
            "verification_level": context["verification_level"],
            "reason": context.get("reason", ""),
            "execution": (
                (context["artifacts"].get("tdd_coder") or {}).get("test_run_result")
                if isinstance(context["artifacts"].get("tdd_coder"), dict) else None
            ) or {},
            "verify_md": (
                (context["artifacts"].get("e2e_validator") or {}).get("verify_md")
                if isinstance(context["artifacts"].get("e2e_validator"), dict) else None
            ),
            "archive": context["archive"],
            "archive_persisted": context["archive_persisted"],
            "degraded": list(context["degraded"]),
            "errors": list(context["errors"]),
            "stage_events": order_events(context["stage_events"]),
            "trace": dict(context.get("trace") or {}),
            "resumed_from_agent": context.get("resumed_from_agent"),
            # 重复投递/重复劳动被跳过时，这里说清是被哪个键拦下的（`overlap:event_key`
            # / `overlap:intent_key`），以及判据当时看到了什么。成功但不留痕，
            # 事后就分不清「跳过了」和「根本没跑」。
            "skip_reason": context.get("skip_reason"),
            "dedupe": context.get("dedupe"),
            # 「排队等人」的那条动作要跟着结果出来：链外那条独立 run 靠 `pending_id`
            # 才找得到它该接哪一条。丢了它，异常文本就成了唯一的记录 —— 而异常
            # 文本进不了收件箱，于是「排队」变成一句没人能处置的话。
            "pending_action": dict(context.get("pending_action") or {}),
            "elapsed_ms": round((monotonic() - context["started_at"]) * 1000, 3),
        }


# ---------------------------------------------------------------------------
# Default wiring.
#
# The L1/L2/L3 Agents are imported into this module's namespace so that the
# default mapping resolves them at call time. That keeps the injection seam
# (state["agents"]) intact while letting a caller replace chain.<name> and have
# the default mapping pick the replacement up.
# ---------------------------------------------------------------------------

import os
from uuid import uuid4

from agents.l1.e2e_validator import e2e_validator
from agents.l1.hermes import hermes
from agents.l1.orchestrator import orchestrate
from agents.l1.prd_builder import prd_builder
from agents.l1.req_writer import req_writer
from agents.l1.sdd_spec_builder import sdd_spec_builder
from agents.l1.tdd_coder import tdd_coder
from agents.l1.tdd_planner import tdd_planner
from agents.l1.tdd_reviewer import tdd_reviewer
from agents.l1.trd_adr_generator import trd_adr_generator
from agents.l2.brd_builder import brd_builder
from agents.l2.content_processor import content_processor
from agents.l2.performance_tester import performance_tester
from agents.l2.planner_agent import planner_agent
from agents.l2.reporter_agent import reporter_agent
from agents.l2.resource_scheduler import resource_scheduler
from agents.l2.searcher_agent import searcher_agent
from agents.l2.validator_agent import validator_agent
from agents.l3.architecture_guardian import architecture_guardian
from agents.l3.build_runner import build_runner
from agents.l3.cache_governor import cache_governor
from agents.l3.code_smith import code_smith
from agents.l3.cost_tracker import cost_tracker
from agents.l3.incident_commander import incident_commander
from agents.l3.model_tuner import model_tuner
from agents.l3.observability_agent import observability_agent
from agents.l3.risk_sentinel import risk_sentinel
from agents.l3.sast_dast_scanner import sast_dast_scanner
from agents.l3.self_healing_test import self_healing_test
from agents.l3.test_orchestrator import test_orchestrator

from runtime import idempotency, inbox, recording, safety_tiers
from runtime.audit import record as audit_record
from runtime.capability_bus import build_default_bus, capability_for
from runtime.registry import DomainRegistry
from runtime.context_budget import ContextBudget
from runtime.execution import execution_allowed
from runtime.fuse import PersonalFuse
from runtime.human_interface import APPROVE, DENY
from runtime.telemetry import MeteredLLM


def _llm_adapters() -> tuple:
    from bootstrap import _ChatLLM, _MessagesLLM

    return _ChatLLM, _MessagesLLM


_ARTIFACT_RANK = {name: index for index, name in enumerate(AGENT_ORDER)}


def ordered_artifacts(artifacts: Mapping[str, Any] | None) -> dict:
    """落盘用的产物顺序：按 (阶段, Agent) 排，不按「谁先跑完」排。

    并发之后完成顺序不再是一个稳定的量。顺序若跟着完成顺序走，同一份输入两次运行
    会得到两份字节不同的产物 —— 而抖动的原因看起来像模型不一样，排查方向从一开始
    就错了（V6 判的正是字节）。表里没有的名字排最后、按名字排：不认识的键也要有
    一个确定的落点。
    """
    items = dict(artifacts or {})
    return {
        name: items[name]
        for name in sorted(
            items,
            key=lambda name: (0, _ARTIFACT_RANK[name], "")
            if name in _ARTIFACT_RANK
            else (1, 0, str(name)),
        )
    }


def _artifact_of(inputs: Mapping[str, Any], name: str) -> dict:
    artifacts = inputs.get("artifacts")
    if not isinstance(artifacts, dict):
        return {}
    found = artifacts.get(name)
    return found if isinstance(found, dict) else {}


def _namespace_domain(index: Any) -> Any:
    """把 `domain/coding` 这种 namespace 还原成领域名，取不到就 None。"""
    if not isinstance(index, Mapping):
        return None
    namespace = index.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        return None
    tail = namespace.rsplit("/", 1)[-1]
    return tail if tail and tail != "default" else None


def _memory_entry(outcome: Mapping[str, Any], *, user_input: Any, timestamp: Any) -> dict:
    """从这次运行里抽出**下次能用的**那几样：领域、语言、框架、入口、结果。

    只抽能被复核的字段，不抽自由文本结论——记忆会影响后续决定，
    不该由一段无法追溯的散文来影响。
    """
    artifacts = outcome.get("artifacts") if isinstance(outcome.get("artifacts"), Mapping) else {}
    # `_artifact_of` 收的是「**带** artifacts 这个键的东西」——链子里的 inputs 就长那样。
    # 而这里手上拿的**就是** artifacts 本身。以前直接把 artifacts 递进去，于是它去
    # artifacts 里找 "artifacts" 这个键，永远找不到，五处查询全是 {}。
    #
    # 这个错误不报错、不抛异常、不改变任何流程，只是让记忆里除了「谁问过这句话」
    # 什么都没记下：领域标签没了（`同领域加权` 那半条检索从来没生效过），
    # 语言 / 框架 / 入口没了（`search()` 的 `settled` 恒为空——而「上次这类任务
    # 最后选了什么」正是记忆存在的理由）。外面看 `refs: 1` 还带着一条挺像样的理由，
    # 一切正常。是「把记忆摊开给人看」这件事才让它露出来的：一条都没有领域。
    scope = {"artifacts": artifacts}
    dag = _artifact_of(scope, "orchestrator").get("task_dag")
    dag = dag if isinstance(dag, Mapping) else {}
    prefs = _artifact_of(scope, "hermes").get("preference_pack")
    prefs = prefs if isinstance(prefs, Mapping) else {}
    cases = _artifact_of(scope, "tdd_planner").get("test_cases")
    cases = cases if isinstance(cases, Mapping) else {}
    code = _artifact_of(scope, "tdd_coder").get("green_code")
    code = code if isinstance(code, Mapping) else {}
    domain_prefs = prefs.get("domain_prefs") if isinstance(prefs.get("domain_prefs"), Mapping) else {}

    entry = {
        "task_id": outcome.get("task_id"),
        "timestamp": timestamp,
        "user_input": str(user_input or "")[:400],
        # domain_id 取不到就退回 namespace（"domain/coding" 这种）：
        # 没有领域标签的记忆，只能靠词面匹配，等于少了一半检索能力。
        "domain_id": dag.get("domain_id") or _namespace_domain(
            _artifact_of(scope, "hermes").get("memory_index")
        ),
        "status": outcome.get("status"),
        "verification_level": outcome.get("verification_level"),
        "language": domain_prefs.get("language") or cases.get("language") or code.get("language"),
        "test_framework": domain_prefs.get("test_framework") or cases.get("test_framework"),
        "entrypoint": cases.get("entrypoint") or code.get("entrypoint"),
    }
    if outcome.get("reason"):
        entry["reason"] = str(outcome["reason"])[:200]
    return {key: value for key, value in entry.items() if value not in (None, "")}


# 给写代码的 Agent 看几段**这个仓库里已有的**代码。
#
# 三条规矩，都是被真实结果逼出来的（下面每一条旁边都写着为什么）：只给源码、
# 点到名字才算够格、一条都不够格就什么都不给——**给错例子比不给更糟**：模型会照着
# 一个不相干的文件编出这个仓库根本没有的写法，而报告里只会显示「这次跑了 S5」。
#
# 代价说清楚：这样它只在「需求里点了已有符号的名字」时才给例子（实测 11 条需求
# 里 4 条命中，命中的都是对的）。全新需求一律不给——这是**量出来**的取舍，不是设计。
CODE_HINT_LIMIT = 2
# 提示词里每段代码截多长。够看出「它怎么写」，不够把整个文件塞进上下文。
CODE_HINT_CHARS = 600


def _code_hint(query: Any, *, limit: int = CODE_HINT_LIMIT) -> tuple:
    """`(提示词片段, 出处)`。找不到像的返回 `("", [])`，不返回半个提示词。"""
    text = str(query or "").strip()
    if not text:
        return "", []
    try:
        from runtime.code_index import search_code

        result = search_code(text, limit=max(limit * 3, 6))
    except Exception:
        # 检索失败**不该**让整条链子停：它是加分项，不是必经之路。
        return "", []
    picked: list = []
    for hit in result.get("hits", []):
        # 退休的代码（`attic/`）**不进提示词**。它还找得到（退休 ≠ 删除），但不该被模仿。
        if not isinstance(hit, Mapping) or hit.get("retired"):
            continue
        # 只认**源码**。理由不是「源码更重要」，是「模仿的是这个仓库的写法」——
        # 测试文件里的夹具和桩是为了测试方便写的，不是这个仓库的写法。实测过：
        # 「把罗马数字转成整数」「把摄氏温度转成华氏温度」这种全新需求的头两名
        # 全都是 `tests/` 里的夹具，照它们写只会学到一个不存在的约定。
        if hit.get("zone") != "源码":
            continue
        # 还得**点到名字**才算够格。这是量出来的：中文需求里「写一个函数」「代码」
        # 这类词到处都是，靠词数或分数都拦不住（`runtime/chain.py` 里这段注释自己
        # 就能对上十条）。点到了仓库里某个符号的名字，才说明这次真的在说已有代码。
        if not hit.get("named"):
            continue
        picked.append(hit)
        if len(picked) >= max(1, int(limit)):
            break
    if not picked:
        return "", []
    lines = ["这个仓库里已有的、跟这次像的代码。模仿它的**写法**（怎么切词、怎么报错、"
             "怎么命名），不要照抄它的逻辑："]
    refs: list = []
    for hit in picked:
        snippet = str(hit.get("snippet") or "")[:CODE_HINT_CHARS]
        lines.append(f"\n--- {hit.get('path')}:{hit.get('line')}  {hit.get('qualname')} ---\n{snippet}")
        refs.append({
            "path": hit.get("path"), "line": hit.get("line"), "zone": hit.get("zone"),
            "qualname": hit.get("qualname"), "score": hit.get("score"),
            "matched": hit.get("matched"), "named": hit.get("named"),
            "reason": hit.get("reason"),
        })
    return "\n".join(lines), refs


# 人指名的引用（`@路径`）读出来的正文，递给谁看。
#
# `_code_hint` 上面那一整段是**猜**：按词面找「像这次需求」的代码，找不到就不给。
# @引用是**说**：人指着某个文件说「照这个来」。两件事不合并——猜错会给人错的
# 例子（上面三道闸门就是被这件事逼出来的），而人指的那一个不会错。同时命中时，
# **听来的压过猜来的**。
#
# 递给**只有写代码那一步**（`tdd_coder`）：前面几步看的是需求本身，它们的产物是
# 规格不是代码；把源码塞给写规格的 Agent，等于让它照着实现倒推需求。
def _context_refs(query: Any) -> tuple:
    """`(提示词片段, 引用清单)`。人没指名任何文件时返回 `("", [])`。"""
    text = str(query or "")
    if "@" not in text:
        return "", []
    try:
        from runtime.context_refs import as_block, collect

        found = collect(text)
    except Exception:  # noqa: BLE001 - 和 `_code_hint` 同一条纪律：引用是加分项，
        # 不是必经之路；读文件读挂了不该让整条链子停。
        return "", []
    refs = [ref for ref in (found.get("refs") or []) if isinstance(ref, Mapping)]
    # 记的是**出处**（哪个文件、第几行、给了多少字、为什么没给全），不是正文：
    # 正文由出处加当时的文件复原，存一遍只会存出一个会过期的副本。
    meta = [
        {key: ref.get(key) for key in ("spec", "path", "ok", "lines", "chars", "note")}
        for ref in refs
    ]
    return as_block(refs), meta


def _domain_preamble(inputs: Mapping[str, Any]) -> str | None:
    """The domain's instruction for the Agent currently running, if any.

    Injected once at the client seam so every Agent benefits and no Agent has to
    know domains exist. Without this, ``DomainTemplate.prompts`` was a declared
    field that nothing consumed — the same "declared but unused" shape as the
    other gaps in this codebase.
    """
    snapshot = _artifact_of(inputs, "orchestrator").get("context_snapshot")
    config = snapshot.get("domain_config") if isinstance(snapshot, Mapping) else None
    prompts = config.get("prompts") if isinstance(config, Mapping) else None
    agent = inputs.get("agent")
    if isinstance(prompts, Mapping) and isinstance(agent, str):
        text = prompts.get(agent)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


class _PrefixedChat:
    """Prepends a domain instruction to every prompt it forwards."""

    def __init__(self, inner: Any, preamble: str) -> None:
        self._inner = inner
        self._preamble = preamble

    def chat(self, prompt: Any, model: Any = None, **kwargs: Any) -> Any:
        # A wrapper that changes the signature changes the contract: the budget
        # argument a caller passes must reach the client underneath, or the caller
        # cannot tell "the model had no answer" from "my argument never arrived".
        return self._inner.chat(f"{self._preamble}\n\n{prompt}", model, **kwargs)


class _PrefixedMessages:
    """Prepends a domain instruction to a message list or a bare prompt.

    The list shape matters: ``tdd_coder`` calls its client as
    ``llm([{"role": "user", ...}])``, so a passthrough for lists would silently
    skip the injection for exactly the Agent the domain most needs to steer.
    """

    def __init__(self, inner: Any, preamble: str) -> None:
        self._inner = inner
        self._preamble = preamble

    def __call__(self, messages: Any, **kwargs: Any) -> Any:
        if isinstance(messages, str):
            return self._inner(f"{self._preamble}\n\n{messages}", **kwargs)
        if isinstance(messages, list):
            return self._inner(
                [{"role": "system", "content": self._preamble}] + list(messages), **kwargs
            )
        return self._inner(messages, **kwargs)


def _chat_client(inputs: Mapping[str, Any]) -> Any:
    chat_llm, _ = _llm_adapters()
    client = inputs.get("llm_client")
    if not callable(client):
        return None
    inner = chat_llm(client)
    preamble = _domain_preamble(inputs)
    return _PrefixedChat(inner, preamble) if preamble else inner


def _messages_client(inputs: Mapping[str, Any]) -> Any:
    _, messages_llm = _llm_adapters()
    client = inputs.get("llm_client")
    if not callable(client):
        return None
    inner = messages_llm(client)
    preamble = _domain_preamble(inputs)
    return _PrefixedMessages(inner, preamble) if preamble else inner


def _adapter_orchestrator(inputs: Mapping[str, Any]) -> dict:
    state = inputs.get("state")
    registry = state.get("registry") if isinstance(state, dict) else None
    return orchestrate(
        user_input=inputs.get("user_input", ""),
        timestamp=inputs.get("timestamp", ""),
        registry=registry,
        llm=_chat_client(inputs),
        forced_domain=(state or {}).get("forced_domain") if isinstance(state, dict) else None,
    )


def _adapter_hermes(inputs: Mapping[str, Any]) -> dict:
    upstream = _artifact_of(inputs, "orchestrator")
    # 跨会话记忆从调用方注入的 state 里取（和 store / policy_engine 同一个入口）。
    # 没注入就是 None，hermes 会当「这次没有记忆」，不是错误。
    state = inputs.get("state")
    memory = state.get("memory") if isinstance(state, Mapping) else None
    return hermes(
        task_dag=upstream.get("task_dag"),
        context_snapshot=upstream.get("context_snapshot"),
        llm=_chat_client(inputs),
        memory=memory,
    )


def _adapter_prd_builder(inputs: Mapping[str, Any]) -> dict:
    return prd_builder(
        preference_pack=_artifact_of(inputs, "hermes").get("preference_pack"),
        task_dag=_artifact_of(inputs, "orchestrator").get("task_dag"),
        llm=_chat_client(inputs),
    )


def _adapter_req_writer(inputs: Mapping[str, Any]) -> dict:
    return req_writer(
        prd=_artifact_of(inputs, "prd_builder").get("prd"),
        llm=_chat_client(inputs),
    )


def _adapter_sdd_spec_builder(inputs: Mapping[str, Any]) -> dict:
    snapshot = _artifact_of(inputs, "orchestrator").get("context_snapshot") or {}
    return sdd_spec_builder(
        requirements_md=_artifact_of(inputs, "req_writer").get("requirements_md"),
        preference_pack=_artifact_of(inputs, "hermes").get("preference_pack"),
        llm=_chat_client(inputs),
        domain_config=snapshot.get("domain_config") if isinstance(snapshot, dict) else None,
    )


def _adapter_trd_adr_generator(inputs: Mapping[str, Any]) -> dict:
    return trd_adr_generator(
        spec=_artifact_of(inputs, "sdd_spec_builder").get("spec"),
        llm=_chat_client(inputs),
    )


def _adapter_tdd_planner(inputs: Mapping[str, Any]) -> dict:
    return tdd_planner(
        spec=_artifact_of(inputs, "sdd_spec_builder").get("spec"),
        trd=_artifact_of(inputs, "trd_adr_generator").get("trd"),
        llm=_chat_client(inputs),
    )


def _parse_port_declaration(text: Any, direction: str) -> dict | None:
    """`"a: 4bit"` -> `{"name": "a", "direction": "input", "width": 4}`."""
    match = re.match(r"\s*([A-Za-z_]\w*)\s*:\s*(\d+)\s*bit", str(text))
    if not match:
        return None
    return {"name": match.group(1), "direction": direction, "width": int(match.group(2))}


def _hdl_interface(spec: Any) -> tuple[str | None, list | None]:
    """The module name and its ports, from a declared or derived spec.

    A derived contract carries structured `ports`; a contract written by hand
    declares them as `"a: 4bit"` strings. Both describe the same thing, and the
    testbench generator needs the structured form.
    """
    if not isinstance(spec, Mapping):
        return None, None
    verification = spec.get("verification") or {}
    interfaces = spec.get("interfaces") or []
    interface = interfaces[0] if interfaces and isinstance(interfaces[0], Mapping) else {}

    module = interface.get("name")
    entrypoint = verification.get("entrypoint")
    if isinstance(entrypoint, str) and ":" in entrypoint:
        symbol = entrypoint.partition(":")[2]
        if symbol:
            module = symbol
    if not isinstance(module, str) or not module.strip():
        return None, None

    ports = verification.get("ports")
    if isinstance(ports, list) and ports:
        return module, ports
    declared = []
    for entry in interface.get("input") or []:
        parsed = _parse_port_declaration(entry, "input")
        if parsed:
            declared.append(parsed)
    for entry in interface.get("output") or []:
        parsed = _parse_port_declaration(entry, "output")
        if parsed:
            declared.append(parsed)
    return module, (declared or None)


def _capability_test_run(inputs: Mapping[str, Any], green_code: Mapping[str, Any],
                         test_cases: Mapping[str, Any]) -> dict | None:
    """Run a domain's declared verification capability as the test result.

    The Python test-suite executor cannot run Verilog, so a capability-verified
    domain would fail the reviewer's A4 ("real execution passed") no matter how
    correct its HDL was. The simulation has to happen here, at the coding stage,
    for the reviewer to be able to see it at all.
    """
    spec = _artifact_of(inputs, "sdd_spec_builder").get("spec") or {}
    verification = spec.get("verification") if isinstance(spec, Mapping) else None
    capability = verification.get("capability") if isinstance(verification, Mapping) else None
    if not isinstance(capability, str) or not capability.strip():
        return None
    bus = (inputs.get("state") or {}).get("capability_bus")
    if bus is None or not hasattr(bus, "call"):
        return None

    case_ids = [
        case.get("id") for case in (test_cases.get("cases") or [])
        if isinstance(case, Mapping) and case.get("id")
    ]
    # The capability needs the interface to build a testbench: without the
    # module name and ports there is nothing to compile against, and without the
    # cases there are no expectations — the run would report a pass it never
    # measured.
    module, ports = _hdl_interface(spec)
    cases = test_cases.get("cases") if isinstance(test_cases, Mapping) else None
    # Only an HDL capability takes the interface. Passing the extra keywords to
    # `execute_code` would raise TypeError from inside the call, where a broad
    # handler could not tell it apart from a genuine failure of the simulation.
    extra = _capability_kwargs(bus, capability, module, ports, cases)
    try:
        simulation = bus.call(capability, green_code.get("code") or "", **extra)
    except Exception as exc:
        simulation = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    ok = isinstance(simulation, Mapping) and simulation.get("success") is True
    # Report the counts the simulation actually measured. Collapsing them to
    # 1/0 threw away the evidence: a bench that ran four cases and passed them
    # all was indistinguishable from one that ran a single case.
    measured_passed = simulation.get("passed") if isinstance(simulation, Mapping) else None
    measured_failed = simulation.get("failed") if isinstance(simulation, Mapping) else None
    passed = measured_passed if isinstance(measured_passed, int) and measured_passed >= 0 else (1 if ok else 0)
    failed = measured_failed if isinstance(measured_failed, int) and measured_failed >= 0 else (0 if ok else 1)
    # What the simulation says it ran, not a copy of what the planner wrote. The
    # copy made A3 — planned cases against run cases — compare a list with itself,
    # so a shape that planned five cases and checked two still passed the gate.
    checked = simulation.get("cases_checked") if isinstance(simulation, Mapping) else None
    run_cases = (
        [case_id for case_id in checked if isinstance(case_id, str)]
        if isinstance(checked, list) else case_ids
    )
    dropped = simulation.get("cases_dropped") if isinstance(simulation, Mapping) else None
    return {
        "task_id": test_cases.get("task_id"),
        "executed": True,
        "stub": False,
        "success": bool(ok),
        "passed": passed,
        "failed": failed,
        "cases": run_cases,
        "cases_dropped": [c for c in dropped if isinstance(c, str)] if isinstance(dropped, list) else [],
        "capability": capability,
        "detail": dict(simulation) if isinstance(simulation, Mapping) else {},
        "reason": "" if ok else "capability_execution_failed",
    }


def _adapter_tdd_coder(inputs: Mapping[str, Any]) -> dict:
    from bootstrap import _execute_test_suite

    test_cases = _artifact_of(inputs, "tdd_planner").get("test_cases")
    if not isinstance(test_cases, dict):
        test_cases = {}
    # 「这个仓库里已有的像的代码」按**这次的需求**去找，不是按规格找：规格是机器
    # 从需求推出来的，需求才是人真正说的话。找不到像的，`hint` 就是空字符串。
    # 人指名的压过按词面猜的：人指的那一个不会错，猜的那一个会。人指了路就
    # **不再去猜**——建一次索引、搜一遍仓库，再把结果丢掉，那是白花的钱。
    # 递的是人指的那一段时 `code_index.refs` 是空的，那是**实话**：索引这次一条
    # 都没贡献。`used` 说的是「递了提示」，不是「索引命中」。
    told, told_refs = _context_refs(inputs.get("user_input"))
    if told:
        hint, refs = told, []
    else:
        hint, refs = _code_hint(inputs.get("user_input"))
    result = tdd_coder(test_cases=test_cases, llm=_messages_client(inputs), code_hint=hint, code_refs=refs)
    # tdd_coder never runs its own tests, so the suite is executed here. The
    # Reviewer's A4 gate must see measured results, not a placeholder.
    if isinstance(result, dict) and isinstance(result.get("green_code"), dict):
        capability_run = _capability_test_run(inputs, result["green_code"], test_cases)
        result["test_run_result"] = (
            capability_run
            if capability_run is not None
            else _execute_test_suite(result["green_code"], test_cases)
        )
    if isinstance(result, dict):
        # 这一单照着谁写的（可能一个都没有）。挂进 coder 的产物，由 `_persist`
        # 搬进工单——**只在内存里的证据，服务一重启就没了**。
        result["context_refs"] = told_refs
    return result


def _domain_declaration(inputs: Mapping[str, Any]) -> dict:
    """这个请求落在哪个领域，以及那个领域声明了什么。

    领域声明**跟着匹配结果走**（registry.match 的注释里写着为什么：只传 id 会让每个
    领域都变成装饰品——被算出来、被记下来、从没被用过）。这里顺着那条链把它取出来。
    拿不到就拿不到：红队退回默认策略，而不是让整个复核失败。
    """
    domain = inputs.get("domain")
    if isinstance(domain, Mapping):
        return dict(domain)
    registry = inputs.get("registry")
    domain_id = inputs.get("domain_id")
    if registry is not None and isinstance(domain_id, str) and domain_id:
        try:
            template = registry.get(domain_id)
        except Exception:  # noqa: BLE001 - 领域查不到不该挡住复核
            template = None
        if template is not None:
            try:
                return template.to_dict()
            except Exception:  # noqa: BLE001
                return {}
    return {}


def _adapter_tdd_reviewer(inputs: Mapping[str, Any]) -> dict:
    coder = _artifact_of(inputs, "tdd_coder")
    test_cases = _artifact_of(inputs, "tdd_planner").get("test_cases")
    # 领域的红队策略是数据，跟着契约一起交给红队。契约自己写了的优先——
    # 契约比领域更靠近这次具体的工作。
    policy = _domain_declaration(inputs).get("adversary")
    if isinstance(policy, Mapping) and isinstance(test_cases, Mapping):
        test_cases = dict(test_cases)
        test_cases.setdefault("adversary", dict(policy))
    return tdd_reviewer(
        green_code=coder.get("green_code"),
        test_run_result=coder.get("test_run_result"),
        test_cases=test_cases,
        llm=_messages_client(inputs),
    )


def _verification_with_case_args(verification: Any, test_cases: Any,
                                green_code: Any = None) -> Any:
    """Repair the spec's entrypoint and arguments before the e2e call.

    Two things the spec cannot know:

    * its entrypoint is derived from prose and falls back to `unknown`, which
      names nothing — the generic e2e call then invokes a symbol that does not
      exist and B5 fails on a run whose code and tests are both correct. The
      coder resolved the real symbol, so use it.
    * `args` is empty for a class, so the call would miss its constructor
      arguments. Reuse the ones the planned cases use, which is the same call
      the reviewer's A4 already measured.
    """
    if not isinstance(verification, Mapping):
        return verification
    patched = dict(verification)
    changed = False

    declared = patched.get("entrypoint")
    symbol = str(declared).partition(":")[2] if declared else ""
    if symbol in ("", "unknown") and isinstance(green_code, Mapping):
        resolved = green_code.get("entrypoint")
        if isinstance(resolved, str) and str(resolved).partition(":")[2] not in ("", "unknown"):
            patched["entrypoint"] = resolved
            changed = True

    if patched.get("args") or patched.get("arg_exprs"):
        return patched if changed else verification
    cases = (test_cases or {}).get("cases") if isinstance(test_cases, Mapping) else None
    first = cases[0] if isinstance(cases, list) and cases else None
    if not isinstance(first, Mapping):
        return patched if changed else verification
    try:
        from bootstrap import _given_expressions, _given_namespace

        named = _given_namespace(first.get("given"))
        pending = _given_expressions(first.get("given"))
    except Exception:
        return patched if changed else verification
    if named:
        patched["args"] = list(named.values())
        changed = True
    if pending:
        # Not JSON-serialisable (a lambda, a constructed object): hand the child
        # the source text so it can evaluate them in the generated code's scope.
        patched["arg_exprs"] = list(pending.values())
        patched.pop("args", None)
        changed = True
    return patched if changed else verification


def _capability_kwargs(bus: Any, capability: str, module: Any, ports: Any, cases: Any) -> dict:
    """The interface keywords this capability accepts, if it takes them.

    Only an HDL capability does. Passing them to `execute_code` would raise
    TypeError from inside the call, where a broad handler could not tell it apart
    from a genuine failure of the simulation.
    """
    registered = getattr(bus, "capabilities", {}).get(capability)
    if registered is None:
        return {}
    try:
        parameters = inspect.signature(registered).parameters
    except (TypeError, ValueError):
        return {}
    if all(k in parameters for k in ("module", "ports", "cases")):
        return {"module": module, "ports": ports, "cases": cases}
    return {}


def _capability_runner(inputs: Mapping[str, Any]):
    """Route a contract-declared capability to the bus.

    The IC domain declares ``verification.capability = "execute_verilog"``. Its
    ``mode``/``entrypoint`` describe a *simulation*, not a command, so running
    the entrypoint as a subprocess could never verify it.
    """
    bus = (inputs.get("state") or {}).get("capability_bus")
    if bus is None or not hasattr(bus, "call"):
        return None

    # The e2e stage re-runs the simulation to check it independently, so it
    # needs the same interface the coding stage had. Without it the capability
    # reported "unverifiable" and B5 failed on a design that simulated cleanly.
    spec = _artifact_of(inputs, "sdd_spec_builder").get("spec") or {}
    test_cases = _artifact_of(inputs, "tdd_planner").get("test_cases") or {}
    module, ports = _hdl_interface(spec)
    cases = test_cases.get("cases") if isinstance(test_cases, Mapping) else None

    def runner(capability: str, code: Any) -> dict:
        return bus.call(capability, code, **_capability_kwargs(bus, capability, module, ports, cases))

    return runner


def _adapter_e2e_validator(inputs: Mapping[str, Any]) -> dict:
    from bootstrap import _parse_assertions_md

    spec = _artifact_of(inputs, "sdd_spec_builder").get("spec")
    verification = spec.get("verification") if isinstance(spec, dict) else None
    # The generic e2e call passes `args` from the spec, which is empty for a
    # spec that declares a class: it would call `Rectangle()` and fail on the
    # missing constructor arguments. Reuse the arguments the planned cases use,
    # so B5 exercises the same call the reviewer's A4 already measured.
    verification = _verification_with_case_args(
        verification,
        _artifact_of(inputs, "tdd_planner").get("test_cases"),
        _artifact_of(inputs, "tdd_coder").get("green_code"),
    )
    result = e2e_validator(
        review_report=_artifact_of(inputs, "tdd_reviewer").get("review_report"),
        green_code=_artifact_of(inputs, "tdd_coder").get("green_code"),
        spec_verification=verification,
        test_cases=_artifact_of(inputs, "tdd_planner").get("test_cases"),
        llm=_chat_client(inputs),
        capability_runner=_capability_runner(inputs),
        spec_interfaces=spec.get("interfaces") if isinstance(spec, dict) else None,
        # The e2e stage re-runs the simulation; hand it what the coding stage
        # measured so the two runs can be compared instead of each being believed
        # on its own.
        previous_run=_artifact_of(inputs, "tdd_coder").get("test_run_result"),
    )
    if isinstance(result, dict) and "assertions" not in result:
        recovered = _parse_assertions_md(result.get("verify_md"))
        if recovered:
            result["assertions"] = recovered
    return result


def default_agents() -> dict:
    """The L1 wiring used when the caller injects no agent mapping."""
    return {
        "orchestrator": _adapter_orchestrator,
        "hermes": _adapter_hermes,
        "prd_builder": _adapter_prd_builder,
        "req_writer": _adapter_req_writer,
        "sdd_spec_builder": _adapter_sdd_spec_builder,
        "trd_adr_generator": _adapter_trd_adr_generator,
        "tdd_planner": _adapter_tdd_planner,
        "tdd_coder": _adapter_tdd_coder,
        "tdd_reviewer": _adapter_tdd_reviewer,
        "e2e_validator": _adapter_e2e_validator,
    }


def _failure_reason(result: Mapping[str, Any]) -> str:
    for entry in result.get("degraded") or []:
        if isinstance(entry, dict) and entry.get("component") == "tdd_coder":
            return str(entry.get("reason") or "")
    signal = result.get("rollback_signal")
    if isinstance(signal, Mapping) and signal.get("reason"):
        return str(signal["reason"])
    for entry in result.get("errors") or []:
        if isinstance(entry, Mapping) and entry.get("reason"):
            return str(entry["reason"])
    return str(result.get("reason") or result.get("status") or "")


def _is_empty_payload(payload: Any) -> bool:
    """这份输入里有没有东西可看。

    `{}`、`[]`、`""` 都算空——**空不等于「不合法」**，但一样没什么可看的。
    数字和布尔不算空：`0` 和 `False` 是值，不是「没有值」。
    """
    if payload is None:
        return True
    if isinstance(payload, (str, bytes, bytearray, list, tuple, set, frozenset, dict)):
        return len(payload) == 0
    return False


def _run_l3(
    llm: Any,
    result: Mapping[str, Any],
    fuse_state: Mapping[str, Any],
    usage: Mapping[str, Any],
    state: Mapping[str, Any],
    user_input: str,
) -> dict:
    """Run the non-blocking L2/L3 accelerators over measured context.

    Every accelerator carries its own ``stub`` flag and falls back to a
    deterministic result when the advisory call fails, so nothing here can turn
    a failed run into a passing one. The only accelerator that reaches the
    network runs solely when search is explicitly enabled.
    """
    artifacts = result.get("artifacts") or {}
    dag = (artifacts.get("orchestrator") or {}).get("task_dag") or {}
    spec = (artifacts.get("sdd_spec_builder") or {}).get("spec") or {}
    requirements = (artifacts.get("req_writer") or {}).get("requirements_md") or ""
    test_cases = (artifacts.get("tdd_planner") or {}).get("test_cases") or {}
    green_code = (artifacts.get("tdd_coder") or {}).get("green_code") or {}
    execution = result.get("execution") or {}
    errors = list(result.get("errors") or [])
    completed = list(result.get("completed_stages") or [])

    def guard(fn: Callable[..., Any], payload: Any) -> Any:
        try:
            # 空输入不调模型。
            #
            # 加速器的「输入不合法」判断是 `isinstance(payload, dict)`，而 `{}`
            # 是个**合法的空 dict**——于是一个拿到空规格的规划器会认真地规划
            # 一个空规格的实现方案，并且照价收费。ask 模式压根不生成规格，
            # 这一条就是在那儿被看见的：主链只跑到 S2，跑完之后三个拿 spec 的
            # 加速器（planner / reporter / architecture_guardian）照样各发一次
            # 真实模型调用，问的还是「根据以下技术规格……」——而规格是空的。
            #
            # 传 `llm=None` 而不是跳过整个加速器：每个加速器都有自己那条「没有
            # 模型时怎么办」的路，结果形状不变，只是不再假装看过东西。
            return fn(payload, llm=None if _is_empty_payload(payload) else llm)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    output = {
        "resource": guard(resource_scheduler, {
            "task_dag": dag,
            "cpu_count": os.cpu_count() or 1,
            "max_workers": max(1, len(completed)),
            "completed_stages": completed,
        }),
        "cache": guard(cache_governor, {
            "ttl": 3600,
            "requests": usage.get("calls", 0),
            "hits": 0,
        }),
        "model": guard(model_tuner, {
            "config": {"model": getattr(llm, "model", None) or "unknown"},
            "usage": dict(usage),
        }),
        "incident": guard(incident_commander, {
            "errors": errors,
            "level": "critical" if errors else "info",
        }),
        "cost": guard(cost_tracker, {
            "tokens": dict(usage),
            "pricing": {},
        }),
        "obs": guard(observability_agent, {
            "errors": errors,
            "fuse": dict(fuse_state),
            "execution": dict(execution),
        }),
        "risk": guard(risk_sentinel, {
            "scanned": True,
            "findings": [],
            "verdict": "pass",
        }),
        "planner": guard(planner_agent, spec),
        "content": guard(content_processor, requirements),
        "reporter": guard(reporter_agent, spec),
        "arch": guard(architecture_guardian, spec),
        "healing": guard(self_healing_test, green_code),
        "build": guard(build_runner, green_code),
        "smith": guard(code_smith, green_code),
        "sast": guard(sast_dast_scanner, green_code),
        "validator": guard(validator_agent, test_cases),
        "perf": guard(performance_tester, green_code),
        "test_orch": test_orchestrator(test_cases, green_code=green_code),
    }
    # BRD is only meaningful for large-scope work, matching the declared scale.
    if isinstance(dag, Mapping) and dag.get("scale") == "large":
        output["brd"] = guard(brd_builder, dag)
    # searcher_agent is the only accelerator that reaches the network.
    if state.get("search_enabled"):
        output["research"] = guard(searcher_agent, user_input)
    return output


def _consult_approval(
    interface: Any, reason: str, context: Mapping[str, Any]
) -> dict | None:
    """Ask the approval seam whether a retry is authorised.

    Returns ``None`` when no seam is attached, so the default behaviour is
    unchanged. A seam that raises is treated as a refusal: an approval interface
    that cannot answer must not authorise anything.
    """
    if interface is None:
        return None
    request = getattr(interface, "request_approval", None)
    if not callable(request):
        return {"decision": DENY, "detail": "interface_has_no_request_approval"}
    try:
        decision = request(reason, {
            "status": context.get("status"),
            "task_id": context.get("task_id"),
            "rollback_signal": context.get("rollback_signal"),
        })
    except Exception as exc:
        return {"decision": DENY, "detail": f"interface_error:{type(exc).__name__}"}

    # The seam may have caught an internal failure and answered DENY itself; if
    # so its own audit detail is the useful one, not "interface_answered".
    detail = "interface_answered"
    state_getter = getattr(interface, "get_state", None)
    if callable(state_getter):
        try:
            recorded = state_getter().get("requests") or []
            if recorded:
                detail = recorded[-1].get("detail") or detail
        except Exception:
            pass
    return {"decision": decision, "detail": detail}


def _usage_or_zero() -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_complete": False,
        "budget_exhausted": False,
    }


def run_chain(
    user_input: str,
    timestamp: str,
    state: dict | None = None,
    *,
    agents: Mapping[str, Callable[..., dict]] | None = None,
    llm_client: Any = None,
    max_dispatches: int = 0,
    on_event: Any = None,
    should_stop: Any = None,
    before_stage: Any = None,
    mode: str = "code",
    journal: Any = None,
    dedupe: Any = None,
    resume: Mapping[str, Any] | None = None,
) -> dict:
    """Run the chain to a terminal status, applying the fuse budget.

    Returns the chain result enriched with ``usage``, ``fuse_state``,
    ``execution`` and ``l3_output``. The chain never reports ``completed``
    without both gates passing on measured evidence, and a degraded fuse never
    fabricates a pass.

    ``max_dispatches`` bounds how many rollbacks are re-entered at the Agent
    the fuse selected. Default 0 preserves the historical behaviour of
    stopping at the first rollback and waiting for a human.
    """
    if state is not None and not isinstance(state, dict):
        raise TypeError("state must be a dict or None")
    injected = dict(state) if state is not None else {}
    mounted = agents if agents is not None else injected.get("agents")
    if not mounted:
        mounted = default_agents()
    client = llm_client if llm_client is not None else injected.get("llm_client")
    policy = injected.get("policy_engine")
    # The bus used to be a dead seam: run.py put None in state and nobody read
    # it. Building a real one by default is what makes a domain's declared
    # verification capability reachable.
    if injected.get("capability_bus") is None:
        injected["capability_bus"] = build_default_bus()
    # Same reasoning for the domain registry: without one, --domain can set an
    # id but cannot resolve the declaration behind it, so the contract and the
    # prompts silently do nothing.
    if injected.get("registry") is None:
        injected["registry"] = DomainRegistry()
    fuse = injected.get("fuse")
    if not isinstance(fuse, PersonalFuse):
        fuse = PersonalFuse(int(injected.get("max_retries", 10) or 10))

    def _policy_state(engine: Any) -> dict | None:
        """Expose the budget position so spend is auditable, not implicit."""
        getter = getattr(engine, "get_state", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _persist(outcome: dict) -> None:
        """Write the work order on every terminal path, success or not.

        Storing only at S7 meant the viewer could only ever show successes —
        the same blindness as an evaluation that logs only successes, which this
        codebase already fixed once. A paused run is exactly the evidence you
        most want to look at afterwards.
        """
        # 顺序先定（B2）：这一句必须早于记忆/录制/工单三处落盘 —— 它们读的都是这份
        # outcome，晚一步就会有两条路径写出的字节不一样。
        outcome["artifacts"] = ordered_artifacts(outcome.get("artifacts"))

        # 记忆先写。它不该依赖工作单仓库在不在——「旁路只降级」说的是记忆坏了不影响主链，
        # 不是「没配工作单仓库就不记记忆」。之前挂在 store 后面，等于没配就永远不记。
        memory = injected.get("memory")
        if memory is not None:
            try:
                outcome["memory_recorded"] = bool(
                    memory.record(_memory_entry(outcome, user_input=user_input, timestamp=timestamp))
                )
            except Exception as exc:
                outcome["memory_recorded"] = False
                outcome["memory_error"] = f"{type(exc).__name__}: {exc}"

        # 录制同样先写，理由与记忆一模一样：挂在 store 后面就等于「没配工作单仓库
        # 就永远不录」，而录制的全部意义就是事后能读回来重判。
        recorder = recording.get_default()
        if recorder is not None:
            if injected.get("workflow_id") is not None:
                outcome.setdefault("workflow_id", injected.get("workflow_id"))
            try:
                outcome["recorded"] = recorder.record(outcome) is not None
            except Exception as exc:  # noqa: BLE001 - 记账不许把被记的活干掉
                outcome["recorded"] = False
                outcome["recording_error"] = f"{type(exc).__name__}: {exc}"

        store = injected.get("store")
        if store is None:
            outcome["stored"] = False
            return
        archive = outcome.get("archive")
        if not isinstance(archive, Mapping):
            archive = {
                "task_id": outcome.get("task_id"),
                "user_input": user_input,
                "timestamp": timestamp,
                "artifacts": dict(outcome.get("artifacts") or {}),
                "gates": dict(outcome.get("gates") or {}),
                "verification_level": outcome.get("verification_level"),
            }
        record = dict(archive)
        record["status"] = outcome.get("status")
        # **谁停的**，得跟 `status` 一起落下来。只落一个 `status` 的话，
        # 「人按了停止」和「审批被拒」在工单里长得一样——都是没跑完。
        # 链子一直在往结果里写这个键（`stopped_by_user` / `denied_by_approver`），
        # 但它从来没进过工单：两条真被停掉的运行（`260642c8` / `a804d60d`）里，
        # 顶层和 `result` 里读出来都是 `None`。`server.py` 里那条
        # `order.get("fuse_action") == "stopped_by_user"` 的分支因此从来没亮过。
        action = outcome.get("fuse_action")
        if action:
            record["fuse_action"] = action
        # Which model produced this run. With two regional exits and twenty models
        # behind them, a conclusion that does not say what produced it cannot be
        # read: the same prompt gets different designs from different models, and
        # "the run said contract_pass" means nothing without the who.
        provider = injected.get("provider")
        route = injected.get("llm_route")
        model = route.get("model") if isinstance(route, Mapping) else None
        if provider or model:
            record["model_route"] = {
                key: value for key, value in (("provider", provider), ("model", model))
                if value
            }
        # **谁选的**：上面那行说的是「用了哪个模型」，这里说的是「谁定的、为什么」。
        # 成本路由（2.6）把决策放在这个槽里。没有这个槽就一个字都不写——「这场没有
        # 路由决策」和「路由决策是空的」不该长得一样（和 retries 同一条纪律）。
        choice = injected.get("model_choice")
        if isinstance(choice, Mapping) and choice:
            record["model_choice"] = dict(choice)
        # 这一单**是怎么来的**：新写的，还是从哪一单重放出来的。缺了它，一条重放的
        # 工单和一条新工单在仓库里长得一模一样——而「同一个需求跑两次结果不一样」
        # 正是最需要看出这件事的时候：不一样是正常的（模型不是确定性的），但前提是
        # 你得看得出这两条是同源的重放，而不是两个碰巧很像的不同需求。
        lineage = injected.get("lineage")
        if isinstance(lineage, Mapping) and lineage:
            record["lineage"] = dict(lineage)
        # 这一单**打算跑到哪一步**。没有它，一场 ask 运行和一场跑到 S3 就崩了的
        # code 运行，在工单里剩下的痕迹都是「S3 之后什么都没有」——同一份证据，
        # 两种完全不同的原因。
        record["stage_mode"] = outcome.get("mode") or "code"
        # 「重试过几次、每次是从哪儿接着跑的、重试之后怎么收的」。这些证据只活在
        # 内存里，服务一重启就没了——和 lineage / stage_mode 同一条纪律。
        # 没重试过也写 `[]`：`[]` 说的是「这次没重试」，缺键说的是「这个字段还不存在」，
        # 两件事不该长得一样。
        record["retries"] = [dict(item) for item in (outcome.get("retries") or [])]
        record["retry_note"] = outcome.get("retry_note") or ""
        # 这一单**照着谁写的**。`@路径` 是人指名的，和按词面猜出来的 `code_index`
        # 不是一件事。这里和 `retries` 的写法不一样：`retries` 是链子说没说话
        # （说了才写），这里是 coder **跑到没跑到**——跑到了、没人指路，写 `[]`
        # （「这次没人指路」）；压根没跑到 S5，缺键（「这条记录不知道有这回事」）。
        told = _artifact_of(outcome, "tdd_coder").get("context_refs")
        if isinstance(told, list):
            record["context_refs"] = [
                dict(item) for item in told if isinstance(item, Mapping)
            ]
        if outcome.get("reason"):
            record["reason"] = outcome["reason"]
        try:
            if not callable(store):
                raise TypeError("store must be a callable archive adapter")
            outcome["stored"] = store(record) is not False
        except Exception as exc:
            outcome["stored"] = False
            outcome["store_error"] = f"{type(exc).__name__}: {exc}"


    def _record(outcome: dict) -> dict:
        """Record the outcome for evaluation, on every terminal path.

        Logging only successes would make the evaluation blind to exactly the
        cases it exists to detect.
        """
        # 重试的留痕挂在这唯一的收口上，不挂在各个出口上。出口不止一个
        # （正常完成 / 失败 / 用户停止 / 预算耗尽 / 审批拒绝 / 熔断暂停），
        # 每处各写一遍就是好几个地方会漏——这个仓库已经因为「一条规则两处实现」
        # 反复吃过亏，工单卡和运行卡打架就是这么来的。
        outcome["retries"] = [dict(item) for item in retries]
        outcome["retry_note"] = _retry_note(retries)
        _persist(outcome)
        # How many model calls this run actually made. A run that reached no
        # model is not a run, and reporting that as a plain "failed" sends the
        # operator looking at their prompt instead of their provider config.
        outcome["model_calls"] = int((outcome.get("usage") or {}).get("calls") or 0)
        harness = injected.get("eval_harness")
        if harness is not None and hasattr(harness, "log"):
            usage = outcome.get("usage") or {}
            try:
                harness.log(
                    outcome.get("task_id"),
                    outcome.get("status"),
                    fuse.retry_count,
                    verification_level=outcome.get("verification_level"),
                    dispatches=len(outcome.get("dispatches") or []),
                    total_tokens=usage.get("total_tokens"),
                    elapsed_ms=outcome.get("elapsed_ms"),
                )
            except Exception:
                pass
        return outcome

    # 重试的留痕。`fuse_state.retry_count` 只说「失败过几次」，不说重试的是哪一次、
    # 为什么、重试之后好点没有——于是一场跑了两遍的运行和一个碰巧一次就成的运行，
    # 在报告里长得一模一样。这是这个模块最贵的一种沉默。
    #
    # 它必须声明在**任何** _record 调用之前：_record 是个闭包，读的是外层变量，
    # 而下面那条 `execution_disabled` 早退会在进循环之前就调用它。写晚了就是
    # `NameError: free variable 'retries' referenced before assignment`——踩过。
    retries: list[dict] = []

    if not execution_allowed():
        # Refuse before any model call: an untrusted run must not spend tokens
        # on code it will not be allowed to execute.
        return _record({
            "status": "paused_pending_human",
            "reason": "execution_disabled",
            "verification_level": "not_verified",
            "usage": _usage_or_zero(),
            "fuse_state": fuse.get_state(),
            "execution": {},
            "l3_output": {},
            "verify_md": None,
        })

    from runtime import dispatch as dispatch_module


    if not isinstance(max_dispatches, int) or max_dispatches < 0:
        raise ValueError("max_dispatches must be a non-negative integer")

    # 工作流级预算（B5）：调用方在 injected 里给 `workflow_id` 才生效；不给就与从前一致。
    metered = MeteredLLM(client, policy, workflow_id=injected.get("workflow_id"))
    result: dict = {}
    dispatches: list[dict] = []
    # 首次进入用调用方给的续跑点（`runtime/journal.py` 的 `resume_payload()` 产的就是
    # 这个形状）；重试/回滚路径会在循环里覆盖它 —— 所以这一行不是「置 None」，是「接住」。
    resume: Mapping[str, Any] | None = resume
    # One trace_id for the whole logical run; attempt counts dispatch passes.
    trace_id = uuid4().hex[:12]
    attempt = 0
    trace_events: list[dict] = []
    while True:
        chain = Chain(
            mounted,
            llm_client=metered,
            store=injected.get("store"),
            on_event=on_event,
            should_stop=should_stop,
            before_stage=before_stage,
            mode=mode,
            journal=journal,
            dedupe=dedupe,
        )
        result = chain.run(
            user_input,
            timestamp,
            injected,
            resume=resume,
            trace={"trace_id": trace_id, "attempt": attempt,
                   "workflow_id": injected.get("workflow_id")},
        )
        # Events are accumulated across passes: a dispatch must not erase the
        # record of the attempt that failed.
        trace_events.extend(result.get("stage_events") or [])
        # Stamp every exit path (completed / failed / stopped) so no path can
        # return a partial trace.
        result["trace_id"] = trace_id
        result["stage_events"] = order_events(trace_events)
        # 这一遍是上一遍重试出来的：现在知道它怎么收场了，补到那条记录上。
        # 「重试过了」和「重试之后好点没有」是两个问题，只答第一个等于没答。
        # 补在这里而不是各出口：出口有五个，这里只有一个。
        if retries and not retries[-1]["outcome"]:
            retries[-1]["outcome"] = result["status"]
        if result["status"] == "completed":
            break
        if result["status"] == "skipped":
            # 重复投递/重复劳动（11-B 第 12 项）：**成功且终态**，必须在这里收口。
            # 落进下面的重试逻辑，一次「本来就该跳过」的运行会被重试到预算耗尽、
            # 最后报成 `paused_pending_human` —— 那正是我第一版的行为，也正是
            # §11.5 说的「记成失败会让熔断器误判」。
            skipped = dict(result)
            skipped["chain_status"] = "skipped"
            skipped["fuse_state"] = fuse.get_state()
            skipped["usage"] = metered.snapshot()
            skipped["execution"] = result.get("execution") or {}
            skipped["l3_output"] = result.get("l3_output") or {}
            skipped["dispatches"] = dispatches
            skipped["policy_state"] = _policy_state(getattr(metered, "policy", None))
            skipped["verification_level"] = "not_verified"
            return _record(skipped)
        if result["status"] == "suspended":
            # 排队档（§11.5）：收件箱里已经躺着一条 `pending_action`，链在这里收束
            # 等人。**不能**落进下面的重试逻辑 —— 那会把一次「本来就该等人」的运行
            # 重试到预算耗尽、最后报成 `paused_pending_human`，正是 §11.5 说的
            # 「记成失败会让熔断器误判」；也不在这里等人：等人期间前缀会被压缩/
            # 改写，醒过来时 `cacheRead` 归零（价差 50×）。
            pending = dict(result.get("pending_action") or {})
            written = False
            if not pending:
                # 闸门在 enforce 模式但没配收件箱目录时只抛不写。链子自己补一条，
                # 否则这场运行「挂起了」却没有任何东西可以被认领。
                pending = _queue_pending(
                    injected, fuse=fuse, action="queued_action",
                    why=["queued_action_was_not_recorded"], action_kind="action",
                    run_id=trace_id, step_id=None,
                )
                written = True
            pending.setdefault("pending_id", inbox.pending_id(pending))
            pending.setdefault("inbox", str(_inbox_dir(injected)))
            if not written:
                # 闸门**配了**收件箱目录时会自己写一行；没配时它只抛不写。这里按
                # `pending_id` 查一次再补 —— 同一件事投递两次会被折叠成一条，但白写
                # 一行会让收件箱看起来像被投递了两次。
                table, _ = inbox.load(pending["inbox"])
                if pending["pending_id"] not in table:
                    inbox.append(pending["inbox"], pending, strict=False)
            suspended = _suspended(result, fuse, metered, pending,
                                   result.get("reason") or "action_queued")
            suspended["dispatches"] = dispatches
            suspended["policy_state"] = _policy_state(getattr(metered, "policy", None))
            return _record(suspended)
        if result["status"] == "failed":
            # A structural failure is terminal: retrying cannot repair a broken
            # Agent contract or an empty case set.
            result["usage"] = metered.snapshot()
            result["fuse_state"] = fuse.get_state()
            result["execution"] = result.get("execution") or {}
            result["l3_output"] = {}
            # 上面那句注释说「每条出口都要打戳，不让任何路径返回半截 trace」——
            # policy_state 是唯一漏掉的一个：failed 的运行看不到自己花了多少调用，
            # 而预算恰恰是失败时最该看的东西。别的出口（预算耗尽 / 熔断暂停 / 正常完成）
            # 都有它。
            result["policy_state"] = _policy_state(getattr(metered, "policy", None))
            return _record(result)
        if result["status"] == "stopped_by_user":
            # 用户按了停止。这条必须在重试分支**之前**：回滚会重进这个循环，而
            # 「我按了停止，它又自己跑起来了」是最不该有的一种行为。
            #
            # 这里**不能**走 _stopped()：它把状态写成 paused_pending_human，而那个
            # 状态的意思是「有人在等一个人」。用户停止时没有人在等谁——写成那样，
            # 界面会为一次刚被停掉的运行弹出审批框。
            stopped = dict(result)
            stopped["status"] = "stopped_by_user"
            stopped["chain_status"] = "stopped_by_user"
            stopped["reason"] = result.get("reason", "")
            stopped["fuse_action"] = "stopped_by_user"
            stopped["fuse_state"] = fuse.get_state()
            stopped["usage"] = metered.snapshot()
            stopped["execution"] = result.get("execution") or {}
            stopped["l3_output"] = result.get("l3_output") or {}
            stopped["dispatches"] = dispatches
            stopped["policy_state"] = _policy_state(policy)
            stopped["verification_level"] = "not_verified"
            return _record(stopped)
        reason = _failure_reason(result)
        if metered.budget_exhausted:
            exhausted = _stopped(result, fuse, metered, "budget_exhausted", reason)
            exhausted["dispatches"] = dispatches
            exhausted["policy_state"] = _policy_state(policy)
            return _record(exhausted)

        signal = result.get("rollback_signal")
        if signal is not None:
            # A declared rollback edge goes through the fuse's edge validation,
            # which also resolves the target Agent and records the attempt.
            decision = fuse.handle_rollback(signal, result)
        else:
            decision = fuse.record_failure(reason, result)

        # 审批移出热路径（11-B 第 13 项）：`--approval-mode queue` 时**不在链里等人**。
        # 写一条 `pending_action`（带熔断器快照）然后收束成 `suspended`；醒来的那个人
        # 在链外用 `run.py --consume-inbox` 接着跑。为什么必须移出：审批插在链中间会
        # 打断前缀 —— 人工等待期间前缀可能被压缩/改写，恢复后 `cacheRead` 归零
        # （价差 50×）；移到链外，这个代价是零。
        if injected.get("approval_mode") == "queue":
            pending = _queue_pending(
                injected, fuse=fuse, action=f"retry:{reason}",
                why=["approval_required"], action_kind="approval",
                run_id=trace_id, step_id=None,
            )
            suspended = _suspended(result, fuse, metered, pending, "approval_queued")
            suspended["dispatches"] = dispatches
            suspended["policy_state"] = _policy_state(getattr(metered, "policy", None))
            return _record(suspended)

        # A pause is a decision point, not a dead end. With a seam attached, a
        # retry needs an explicit approval; the decision is recorded either way
        # so the pause stays auditable.
        approval = _consult_approval(injected.get("human_interface"), reason, result)
        if approval is not None:
            audit_record(
                "approval_decided",
                decision=approval["decision"],
                detail=approval["detail"],
                reason=reason,
                task_id=result.get("task_id"),
            )
        if approval is not None and approval["decision"] != APPROVE:
            denied = _stopped(result, fuse, metered, "denied_by_approver", reason)
            denied["dispatches"] = dispatches
            denied["approval"] = approval
            denied["policy_state"] = _policy_state(getattr(metered, "policy", None))
            return _record(denied)

        if (
            decision["action"] == "retry"
            and signal is not None
            and len(dispatches) < max_dispatches
        ):
            dispatch_plan = dispatch_module.plan(
                signal,
                target_agent=decision.get("target_agent"),
                snapshot=result,
            )
            if dispatch_plan is not None:
                resume = dispatch_module.resume_arguments(result, dispatch_plan)
                dispatches.append({
                    "attempt": len(dispatches) + 1,
                    "start_at": dispatch_plan["start_at"],
                    "source_agent": dispatch_plan["source_agent"],
                    "target_agent": dispatch_plan["target_agent"],
                    "reason": dispatch_plan["reason"],
                    "previous_status": result["status"],
                })
                retries.append(_retry_record(
                    len(retries) + 1,
                    result,
                    reason,
                    resumed_from=dispatch_plan["start_at"],
                    resume_agent=dispatch_plan["target_agent"],
                ))
                attempt += 1
                continue

        if decision["action"] == "retry":
            # 没有回滚信号，就没有「从哪一步接着跑」这回事：这一遍是**整条链从 S0
            # 重跑一遍**，不是从失败那一步接着跑。这个代价以前一个字都没人记——
            # 现在它是记录里的 `resumed_from: None`。
            retries.append(_retry_record(len(retries) + 1, result, reason))
            # attempt 是打在每一个 trace 事件上的「这是第几遍」。整条链重跑当然也是
            # 新的一遍：不 +1 的话，两遍的事件会顶着同一个 attempt 混在一起。
            attempt += 1
            continue
        # Both "degrade" and "pause" require a human here: degrading a failed
        # run would report success that the evidence does not support.
        stopped = _stopped(result, fuse, metered, decision["action"], reason)
        stopped["dispatches"] = dispatches
        stopped["approval"] = approval
        stopped["policy_state"] = _policy_state(getattr(metered, "policy", None))
        return _record(stopped)

    usage = metered.snapshot()
    l3_output = _run_l3(metered, result, fuse.get_state(), usage, injected, user_input)
    # Re-snapshot after the accelerators: their advisory calls are real model
    # calls and must appear in the reported usage.
    usage = metered.snapshot()
    result["execution"] = result.get("execution") or {}
    result["l3_output"] = l3_output
    result["usage"] = usage
    result["fuse_state"] = fuse.get_state()
    result["reason"] = ""
    result["dispatches"] = dispatches
    result["policy_state"] = _policy_state(policy)
    # The bus and registry are filled in on a copy of the caller's state, so
    # without this the caller cannot tell what was actually available.
    bus = injected.get("capability_bus")
    result["capabilities"] = (
        bus.get_state() if hasattr(bus, "get_state") else {"capabilities": []}
    )
    registry = injected.get("registry")
    result["domains"] = (
        registry.list_domains() if hasattr(registry, "list_domains") else []
    )

    return _record(result)


def _retry_record(
    number: int,
    result: Mapping[str, Any],
    reason: str,
    *,
    resumed_from: Any = None,
    resume_agent: Any = None,
) -> dict:
    """一次重试的留痕。

    `fuse_state.retry_count` 只说「失败过几次」。它不说重试的是哪一次、为什么、
    重试之后好点没有——于是一场跑了两遍的运行和一个碰巧一次就成的运行，在报告里
    长得一模一样。这条记录就是来拆开这两个的。

    `resumed_from is None` 是**有意思**的那一种：链子没有回滚信号时，重试不是从
    失败那一步接着跑，而是**整条链从 S0 重跑一遍**。多花的时间、多烧的调用都是
    真的，以前一个字都没记。
    """
    return {
        "n": number,
        "at_stage": result.get("stage"),
        "failed_status": result.get("status"),
        "reason": reason,
        # 只有 fuse 判了 retry 才会走到这儿（degrade / pause 都是停下来等人），
        # 所以这里不必再问一次 decision["action"]。
        "action": "retry",
        "resumed_from": resumed_from,
        "resume_agent": resume_agent,
        # 这一遍怎么收场的，等下一遍跑完再补。空字符串 = 还没轮到它。
        "outcome": "",
    }


def _retry_note(retries: Sequence[Mapping[str, Any]]) -> str:
    """把重试记录压成一句人话。没重试过就是空字符串——没事就别说话。"""
    if not retries:
        return ""
    parts = []
    for item in retries:
        if item.get("resumed_from"):
            where = f"从 {item['resumed_from']}（{item.get('resume_agent') or '？'}）接着跑"
        else:
            where = "把整条链从头跑了一遍"
        why = item.get("reason") or "没说原因"
        how = item.get("outcome") or "还没收尾"
        parts.append(f"第 {item.get('n')} 次重试{where}，因为 {why}；那一次最后是 {how}")
    return "；".join(parts) + "。"


def _inbox_dir(injected: Mapping[str, Any]) -> Any:
    """Where queued actions are written. Explicit override first, then the env
    variable / `<state_dir>/inbox` that the producer (`safety_tiers.gate`) uses."""
    explicit = injected.get("inbox_dir")
    if explicit:
        return explicit
    return inbox.default_dir(injected.get("state_dir"))


def _queue_pending(
    injected: Mapping[str, Any],
    *,
    fuse: PersonalFuse,
    action: str,
    why: Sequence[str],
    action_kind: str,
    run_id: str | None,
    step_id: str | None,
) -> dict:
    """Write one `pending_action` for a run that is about to be suspended.

    Returns the record that was written, **including** its content-addressed id and
    the inbox path, so the caller can hand a reader something they can act on.
    """
    verdict = {
        "tier": safety_tiers.TIER_QUEUE,
        "why": list(why),
        "criteria": {},
        "isolation": safety_tiers.isolation(),
    }
    record = safety_tiers.pending_record(
        verdict, action=action, caller="run_chain", action_kind=action_kind,
        run_id=run_id, workflow_id=injected.get("workflow_id"), step_id=step_id,
    )
    # 熔断器快照：醒来的那一刻是**另一个进程**。`_rules_only` 一旦没带过去就等于
    # 绕过硬熔断（`run.py` 里那句 `rules_only mode forbids LLM calls`）。
    record["fuse_state"] = fuse.get_state()
    directory = _inbox_dir(injected)
    # `pending_id` 只由身份字段算出，加这两把「钥匙」不会改 id（见 `inbox.pending_id`）。
    record["pending_id"] = inbox.pending_id(record)
    record["inbox"] = str(directory)
    inbox.append(directory, record, strict=False)
    return record


def _suspended(
    result: Mapping[str, Any],
    fuse: PersonalFuse,
    metered: MeteredLLM,
    pending: Mapping[str, Any],
    reason: str,
) -> dict:
    """A run that needs a human stops here — in the chain, not in the hot path.

    §11.5 逐字：「需要人 ⇒ **不进热路径**：写 `pending_action` 进收件箱，卡上标
    "等醒"，链在此收束（状态 `suspended`，**不是** `failed`）」。和
    `paused_pending_human` 的区别是承重的：那个是熔断器/闸门把运行按在原地，这个
    是运行好好地停在等人 —— 醒来的那个人在链外（另一个 run）把它接回去。
    """
    suspended = dict(result)
    suspended["status"] = "suspended"
    suspended["chain_status"] = result.get("status")
    suspended["reason"] = reason
    suspended["fuse_action"] = "suspended_for_human"
    suspended["fuse_state"] = fuse.get_state()
    suspended["usage"] = metered.snapshot()
    suspended["execution"] = result.get("execution") or {}
    suspended["l3_output"] = {}
    suspended["pending_action"] = dict(pending)
    if suspended.get("verification_level") != "not_verified":
        suspended["verification_level"] = "not_verified"
    return suspended


def _stopped(
    result: Mapping[str, Any],
    fuse: PersonalFuse,
    metered: MeteredLLM,
    action: str,
    reason: str,
) -> dict:
    stopped = dict(result)
    stopped["status"] = "paused_pending_human"
    stopped["chain_status"] = result.get("status")
    stopped["reason"] = reason
    stopped["fuse_action"] = action
    stopped["fuse_state"] = fuse.get_state()
    stopped["usage"] = metered.snapshot()
    stopped["execution"] = result.get("execution") or {}
    stopped["l3_output"] = result.get("l3_output") or {}
    if stopped.get("verification_level") != "not_verified":
        stopped["verification_level"] = "not_verified"
    return stopped
