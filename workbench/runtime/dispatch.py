"""Rollback dispatch: the consumer that ``chain.py``'s signal never had.

``chain.py`` raises a rollback signal with ``requires_dispatch: True`` when a hard
gate fails, and ``run.py`` asked ``PersonalFuse`` to classify it — but nothing
ever acted on the decision. The run stopped and waited, and the "external
dispatcher" the entry-point docstring referred to did not exist.

This module is that consumer. A dispatch is bounded and explicit:

* it re-enters the chain at the Agent the fuse selected;
* it keeps every artifact produced **before** that point and discards everything
  after it, so a stale downstream result cannot be mistaken for a fresh one;
* it never fabricates a pass — if the resumed pass still fails, that failure is
  reported as-is.

The planner is a pure function so the decision can be tested without running a
chain.
"""

from __future__ import annotations

from typing import Any, Mapping

from runtime.chain import AGENT_NUMBERS, ROLLBACK_TARGETS

NUMBER_TO_AGENT = {number: name for name, number in AGENT_NUMBERS.items()}

# PersonalFuse resolves an unspecified target for a multi-edge rollback to 8.
DEFAULT_TARGET = 8


def is_dispatchable(signal: Any) -> bool:
    """True when the signal is a well-formed rollback the chain can resume from."""
    if not isinstance(signal, Mapping):
        return False
    source = signal.get("source_agent")
    if source not in ROLLBACK_TARGETS:
        return False
    candidates = signal.get("candidate_targets")
    if not isinstance(candidates, (list, tuple)):
        return False
    if tuple(candidates) != ROLLBACK_TARGETS[source]:
        return False
    reason = signal.get("reason")
    return isinstance(reason, str) and bool(reason.strip())


def plan(
    signal: Any,
    *,
    target_agent: Any = None,
    snapshot: Mapping[str, Any] | None = None,
) -> dict | None:
    """Translate a rollback signal into a resume plan.

    Returns ``None`` when the signal is not dispatchable, so a caller can treat
    "cannot resume" and "must not resume" identically.
    """
    if not is_dispatchable(signal):
        return None
    source = signal["source_agent"]
    candidates = ROLLBACK_TARGETS[source]

    target = target_agent
    if target is None:
        target = signal.get("target_agent")
    if target is None and isinstance(snapshot, Mapping):
        target = snapshot.get("rollback_target")
    if target is None:
        target = DEFAULT_TARGET if DEFAULT_TARGET in candidates else candidates[0]

    if target not in candidates:
        raise ValueError(
            f"target_agent {target!r} is not a declared edge for source {source}; "
            f"declared: {candidates}"
        )
    return {
        "start_at": NUMBER_TO_AGENT[target],
        "source_agent": source,
        "target_agent": target,
        "reason": signal.get("reason"),
    }


def resume_arguments(previous: Mapping[str, Any], resume_plan: Mapping[str, Any]) -> dict:
    """Build the ``resume`` payload for a dispatched pass.

    Artifacts from the failed pass are handed over as-is; ``Chain.run`` keeps only
    those produced strictly before ``start_at``.
    """
    if not isinstance(previous, Mapping):
        raise TypeError("previous must be a mapping")
    if not isinstance(resume_plan, Mapping) or "start_at" not in resume_plan:
        raise ValueError("resume_plan must carry start_at")
    artifacts = previous.get("artifacts") or {}
    gates = previous.get("gates") or {}
    if not isinstance(artifacts, Mapping) or not isinstance(gates, Mapping):
        raise TypeError("previous artifacts/gates must be mappings")
    # Only gates that actually passed are carried over. A failed gate belongs to
    # the discarded attempt; carrying it forward would let a resumed pass
    # inherit a verdict it did not earn.
    passed_gates = {
        name: dict(gate)
        for name, gate in gates.items()
        if isinstance(gate, Mapping) and gate.get("verdict") == "pass"
    }
    return {
        "start_at": resume_plan["start_at"],
        "artifacts": dict(artifacts),
        "gates": passed_gates,
    }
