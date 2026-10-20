"""Personal Fuse: standard-library-only budget and rollback policy.

With max_retries=10, failures 1-7 request retry, 8-9 warn, 10-11
request rules-only degradation, and 12 pauses for human intervention.
The nominal retry limit is a soft budget; 120% is the hard pause threshold.

States: idle -> armed -> tripped -> cooling -> armed.
Active states may pause; paused may explicitly resume or end.
Cooling is a logical scheduling boundary, not a timer or a sleeping thread.
Only the external runner executes actions, retries Agents or changes models.

handle_rollback() accepts chain.py's rollback_signal. Edge 9->8 is fixed.
For 10->7/8, an explicit target_agent or snapshot['rollback_target'] wins;
otherwise the conservative default is 8. No Agent is instantiated or called.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


ROLLBACK_TARGETS = {9: (8,), 10: (7, 8)}
ROLLBACK_STATUSES = {"rollback_9_to_8", "rollback_10_to_7_or_8"}
STATE_TRANSITIONS = {
    "idle": frozenset({"armed", "paused", "ended"}),
    "armed": frozenset({"tripped", "paused", "ended"}),
    "tripped": frozenset({"cooling", "paused", "ended"}),
    "cooling": frozenset({"armed", "paused", "ended"}),
    "paused": frozenset({"armed", "ended"}),
    "ended": frozenset(),
}


class PersonalFuse:
    def __init__(self, max_retries: int = 10) -> None:
        if type(max_retries) is not int or max_retries <= 0:
            raise ValueError("max_retries must be a positive integer")
        self._max_retries = max_retries
        self._retry_count = 0
        self._total_failures = 0
        self._state = "idle"
        self._rules_only = False
        self._ever_degraded = False
        self._last_action = None
        self._last_reason = None
        self._last_snapshot = {}
        self._transitions = []
        self._exit_result = None

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def state(self) -> str:
        """Read-only view of the state machine: idle/armed/tripped/cooling/paused/ended."""
        return self._state

    @property
    def total_failures(self) -> int:
        return self._total_failures

    def _transition(self, target: str, reason: str) -> None:
        if target == self._state:
            return
        if target not in STATE_TRANSITIONS[self._state]:
            raise RuntimeError(f"invalid fuse transition: {self._state} -> {target}")
        self._transitions.append({
            "from": self._state,
            "to": target,
            "reason": reason,
            "retry_count": self._retry_count,
        })
        self._transitions = self._transitions[-64:]
        self._state = target

    def arm(self) -> dict:
        """Prepare the next attempt; a pause requires explicit resume()."""
        if self._state not in ("idle", "cooling", "armed"):
            raise RuntimeError("only idle, cooling or armed fuses can be armed")
        self._transition("armed", "prepare_attempt")
        return self.get_state()

    def pause(self, reason: str = "manual_pause") -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("pause reason must be a nonempty string")
        if self._state == "ended":
            raise RuntimeError("an ended fuse cannot be paused")
        self._last_action = "pause"
        self._last_reason = reason
        self._transition("paused", reason)
        return self._action_result("pause")

    def resume(self, *, reset_retries: bool = False) -> dict:
        """Human-controlled resume; budget reset must be explicitly requested.

Without reset_retries, the existing budget remains consumed, so another
failure at/above 120% pauses again. A reset starts a new budget window;
lifetime failure/degradation evidence is retained.
        """
        if type(reset_retries) is not bool:
            raise TypeError("reset_retries must be a bool")
        if self._state != "paused":
            raise RuntimeError("only a paused fuse can be resumed")
        if reset_retries:
            self._retry_count = 0
            self._rules_only = False
        self._last_action = None
        self._last_reason = "human_resume_with_reset" if reset_retries else "human_resume"
        self._transition("armed", self._last_reason)
        return self.get_state()

    def record_failure(
        self, reason: str, snapshot: Mapping[str, Any] | None = None
    ) -> dict:
        """Count one failed attempt and return policy; never execute the action."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("failure reason must be a nonempty string")
        if snapshot is not None and not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a mapping or None")
        if self._state == "ended":
            raise RuntimeError("an ended fuse cannot record failures")
        if self._state == "paused":
            # Repeated polling must not authorize attempts or consume budget.
            return self._action_result("pause")
        if self._state in ("idle", "cooling"):
            self.arm()

        self._retry_count += 1
        self._total_failures += 1
        self._last_reason = reason
        try:
            self._last_snapshot = deepcopy(dict(snapshot)) if snapshot is not None else {}
        except Exception as exc:
            # Diagnostics must not disable the budget policy.
            self._last_snapshot = {
                "snapshot_copy_error": f"{type(exc).__name__}: {exc}"
            }
        self._transition("tripped", reason)

        # Integer comparisons avoid floating-point threshold ambiguity.
        used = self._retry_count * 100
        if used >= self._max_retries * 120:
            action = "pause"
        elif used >= self._max_retries * 100:
            action = "degrade"
        elif used >= self._max_retries * 80:
            action = "warn"
        else:
            action = "retry"
        if used >= self._max_retries * 100:
            self._rules_only = True
            self._ever_degraded = True
        self._last_action = action
        self._transition("paused" if action == "pause" else "cooling", action)
        return self._action_result(action)

    def handle_rollback(
        self,
        rollback_signal: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict:
        """Validate a chain signal and choose a legal target before accounting."""
        if not isinstance(rollback_signal, Mapping):
            raise TypeError("rollback_signal must be a mapping")
        if snapshot is not None and not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a mapping or None")
        source = rollback_signal.get("source_agent")
        if type(source) is not int or source not in ROLLBACK_TARGETS:
            raise ValueError("rollback source must be 9 or 10")
        candidates = rollback_signal.get("candidate_targets")
        if (
            not isinstance(candidates, (list, tuple))
            or any(type(item) is not int for item in candidates)
            or tuple(candidates) != ROLLBACK_TARGETS[source]
        ):
            raise ValueError("rollback candidates do not match the declared edges")
        reason = rollback_signal.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rollback signal requires a nonempty reason")

        captured = dict(snapshot) if snapshot is not None else {}
        target = rollback_signal.get("target_agent")
        if target is None:
            target = captured.get("rollback_target", 8)
        if type(target) is not int or target not in ROLLBACK_TARGETS[source]:
            raise ValueError("selected rollback target is not allowed")
        captured["rollback_signal"] = dict(rollback_signal)
        result = self.record_failure(reason, captured)
        result.update({
            "source_agent": source,
            "candidate_targets": ROLLBACK_TARGETS[source],
            "target_agent": target if result["retry_allowed"] else None,
            "requires_dispatch": result["retry_allowed"],
            "decided_by": "PersonalFuse",
        })
        return result

    def _action_result(self, action: str) -> dict:
        return {
            "action": action,
            "reason": self._last_reason,
            "retry_allowed": action in ("retry", "warn", "degrade"),
            "execution_mode": "rules_only" if self._rules_only else "normal",
            "resource_warning": self._retry_count * 100 >= self._max_retries * 80,
            "requires_human": action == "pause",
            "retry_count": self._retry_count,
            "max_retries": self._max_retries,
            "retry_ratio": self._retry_count / self._max_retries,
            "state": self._state,
        }

    def restore(self, snapshot: Mapping[str, Any]) -> dict:
        """Carry a suspended run's fuse across a process boundary.

        §11.5 的「独立 run」是**另一个进程**。恢复不是「重开一个干净进程、把熔断器
        忘了」——`_rules_only` 一旦忘掉就等于绕过硬熔断（`run.py:107-108` 直接抛），
        而「记成失败会让熔断器误判」那条教训的另一面正是：跨进程也不能把账赖掉。

        只恢复两个计数与两个标志。`state` 一律回到 `idle`：能走到这一步的前提是人
        已经批过，所以不给「上一轮那笔 paused 预算」再暂停一次的机会。`idle` 在
        `STATE_TRANSITIONS` 里从来不是合法**目标**（目标只有 armed/paused/ended），
        所以这里直接赋值、手工补一条迁移记录 —— 恢复不是状态机里的一次迁移，它是
        「另一个进程把同一场运行接回来」。
        """
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a mapping")
        retry_count = snapshot.get("retry_count", 0)
        total_failures = snapshot.get("total_failures", 0)
        for name, value in (("retry_count", retry_count),
                            ("total_failures", total_failures)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        self._retry_count = retry_count
        # 总失败数不可能少于「本场重试过几次」：少算等于把熔断器的账抹掉。
        self._total_failures = max(total_failures, retry_count)
        self._rules_only = bool(
            snapshot.get("rules_only") or snapshot.get("execution_mode") == "rules_only"
        )
        self._ever_degraded = bool(snapshot.get("ever_degraded"))
        self._last_action = "restore"
        self._last_reason = "restored_from_suspension"
        self._last_snapshot = deepcopy(dict(snapshot))
        if self._state != "idle":
            self._transitions.append({
                "from": self._state,
                "to": "idle",
                "reason": "restored_from_suspension",
                "retry_count": self._retry_count,
            })
            self._transitions = self._transitions[-64:]
            self._state = "idle"
        return self.get_state()

    def judge_exit(self, final_status: str) -> dict:
        """Classify completion; rollback and pause are explicitly nonterminal."""
        terminal_modes = {
            "completed": "degraded" if self._ever_degraded else "normal",
            "failed": "failed",
            "cancelled": "cancelled",
            "terminated": "ended",
            "ended": "ended",
            # 重复投递/重复劳动（11-B 第 12 项）。`skipped` 是**成功**：记成失败
            # 会让熔断器误判、让账本漂移（§11.5 逐字「重复投递是外部世界的行为，
            # 不是任务失败」）。放进终态表，它才走得完状态机。
            "skipped": "skipped",
        }
        pending = ROLLBACK_STATUSES | {"paused", "paused_pending_human", "suspended"}
        if not isinstance(final_status, str):
            raise TypeError("final_status must be a string")
        if final_status not in terminal_modes and final_status not in pending:
            raise ValueError("unsupported final_status: " + final_status)
        if self._state == "ended":
            if self._exit_result["final_status"] != final_status:
                raise RuntimeError("cannot change an ended task's final status")
            return deepcopy(self._exit_result)

        if final_status in ("paused", "paused_pending_human"):
            if self._state != "paused":
                self.pause(final_status)
            mode, terminal = "paused", False
        elif final_status == "suspended":
            # 等人，但**不在热路径里等**（§11.5）。它和 `paused` 的区别是承重的：
            # `paused` = 熔断器/闸门把运行按在原地（预算用光了、或者环境不让跑），
            # `suspended` = 运行**好好地停在等人**（排队档动作已写进收件箱，链在
            # 这里收束，醒来的那个人在链外把它接回去）。所以这里不碰 `_state`、
            # 不动 `_retry_count` —— 等人不是失败，也不是熔断。
            mode, terminal = "suspended", False
        elif self._state == "paused" and (
            final_status == "completed" or final_status in ROLLBACK_STATUSES
        ):
            # A completion claim must not silently release a human pause.
            mode, terminal = "paused", False
        elif final_status in ROLLBACK_STATUSES:
            mode, terminal = "rollback_pending", False
        else:
            mode, terminal = terminal_modes[final_status], True
            self._transition("ended", final_status)

        result = {
            "final_status": final_status,
            "exit_mode": mode,
            "terminal": terminal,
            "requires_human": mode in ("paused", "suspended"),
            "retry_count": self._retry_count,
            "total_failures": self._total_failures,
            "ever_degraded": self._ever_degraded,
            "state": self._state,
        }
        if terminal:
            self._exit_result = deepcopy(result)
        return result

    def get_state(self) -> dict:
        """Return detached state; callers cannot mutate the internal history."""
        return {
            "state": self._state,
            "retry_count": self._retry_count,
            "max_retries": self._max_retries,
            "total_failures": self._total_failures,
            "retry_ratio": self._retry_count / self._max_retries,
            "budget_percent": self._retry_count * 100 / self._max_retries,
            "thresholds": {
                "warn": (self._max_retries * 80 + 99) // 100,
                "degrade": self._max_retries,
                "pause": (self._max_retries * 120 + 99) // 100,
            },
            "execution_mode": "rules_only" if self._rules_only else "normal",
            "ever_degraded": self._ever_degraded,
            "last_action": self._last_action,
            "last_reason": self._last_reason,
            "last_snapshot": deepcopy(self._last_snapshot),
            "requires_human": self._state == "paused",
            "terminal": self._state == "ended",
            "transitions": deepcopy(self._transitions),
        }
