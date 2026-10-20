"""Human-in-the-loop approval seam.

Fail-closed by construction. An interface that cannot reach a human must never
approve on the human's behalf: this module used to print a line and return
``"continue"`` unconditionally, which made every gate built on it a no-op — a
pause would be announced and then silently released.

The seam is deliberately synchronous and dependency-free. A caller supplies an
``approver`` callable; without one, every request is denied and recorded.
"""

from __future__ import annotations

from typing import Any, Callable

APPROVE = "approve"
DENY = "deny"

# Only these two are decisions. Anything else — a typo, a missing answer, a
# frontend that never replied — resolves to DENY.
DECISIONS = (APPROVE, DENY)


def _normalize(answer: Any) -> str:
    """Coerce an approver's answer into a decision. Unknown values deny."""
    if answer is True:
        return APPROVE
    if answer is False or answer is None:
        return DENY
    if isinstance(answer, str):
        lowered = answer.strip().lower()
        if lowered in ("approve", "approved", "yes", "allow", "允许", "批准"):
            return APPROVE
        if lowered in ("deny", "denied", "no", "reject", "拒绝"):
            return DENY
    return DENY


class HumanInterface:
    """Records every approval request; denies unless a human explicitly approves.

    ``approver`` is called as ``approver(reason, context)``. It may return a
    bool or a decision string; anything unrecognised is treated as a denial, so
    a broken or half-implemented UI cannot accidentally approve.
    """

    def __init__(self, approver: Callable[..., Any] | None = None) -> None:
        if approver is not None and not callable(approver):
            raise TypeError("approver must be callable or None")
        self._approver = approver
        self.requests: list[dict] = []

    @property
    def has_approver(self) -> bool:
        return self._approver is not None

    def request_approval(self, reason: Any, context: Any = None) -> str:
        """Return ``APPROVE`` or ``DENY``. Never assumes consent."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("approval reason must be a nonempty string")

        decision = DENY
        detail = "no_approver_configured"
        if self._approver is not None:
            try:
                decision = _normalize(self._approver(reason, context))
                detail = "approver_answered"
            except Exception as exc:  # a failing approver must not approve
                decision = DENY
                detail = f"approver_error:{type(exc).__name__}"

        self.requests.append({
            "reason": reason,
            "decision": decision,
            "detail": detail,
            "context_provided": context is not None,
        })
        return decision

    def get_state(self) -> dict:
        """Detached view; callers cannot mutate the audit trail."""
        return {
            "has_approver": self.has_approver,
            "requests": [dict(entry) for entry in self.requests],
            "approved": sum(1 for e in self.requests if e["decision"] == APPROVE),
            "denied": sum(1 for e in self.requests if e["decision"] == DENY),
        }
