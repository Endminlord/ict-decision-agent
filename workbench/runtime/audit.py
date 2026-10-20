"""Audit trail for the things that actually leave the process.

The harness runs generated code, generated test suites and external HDL tools.
Until now none of that was recorded: you could see a run's *result* but not what
it *executed*, which makes "what did this thing actually do on my machine"
unanswerable after the fact.

Two deliberate properties:

* **Recording never raises.** An audit failure must not abort the work it is
  auditing; the failure is captured as its own event instead. An audit log that
  can take down the run gets disabled, and then there is no audit log.
* **Absent by default.** Nothing is written unless a log is configured, so
  importing the harness has no side effects.

The log is append-only JSONL and is **not** tamper-proof: it records what
happened, it does not defend against someone editing the file afterwards.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

__all__ = ["AuditLog", "get_default", "record", "set_default"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """Append-only event log. In memory always; on disk when a path is given."""

    def __init__(self, path: Any = None) -> None:
        self.path = str(path) if path else None
        self._events: list[dict] = []
        self._seq = 0
        self.write_error: str | None = None
        if self.path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            except OSError as exc:
                self.write_error = f"{type(exc).__name__}: {exc}"

    def record(self, kind: Any, **fields: Any) -> dict:
        """Append one event. Returns the stored entry; never raises."""
        if not isinstance(kind, str) or not kind.strip():
            kind = "unnamed_event"
        self._seq += 1
        event = {"seq": self._seq, "at": _now(), "kind": kind}
        for key, value in fields.items():
            # Keys that would collide with the envelope are namespaced, so an
            # event can never rewrite its own sequence number.
            event[f"field_{key}" if key in ("seq", "at", "kind") else key] = value
        self._events.append(event)
        self._persist(event)
        return dict(event)

    def _persist(self, event: dict) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:
            # Record the failure without recursing into another write attempt.
            self.write_error = f"{type(exc).__name__}: {exc}"

    def events(self) -> list[dict]:
        return [dict(event) for event in self._events]

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self._events:
            counts[event["kind"]] = counts.get(event["kind"], 0) + 1
        return counts

    def get_state(self) -> dict:
        return {
            "path": self.path,
            "events": len(self._events),
            "kinds": self.kinds(),
            "write_error": self.write_error,
        }


# --- process-wide default sink --------------------------------------------

_default: AuditLog | None = None


def set_default(log: AuditLog | None) -> None:
    """Install the log that ``record()`` writes to. ``None`` disables auditing."""
    global _default
    if log is not None and not isinstance(log, AuditLog):
        raise TypeError("log must be an AuditLog or None")
    _default = log


def get_default() -> AuditLog | None:
    return _default


def record(kind: Any, **fields: Any) -> dict | None:
    """Record into the default log, or do nothing when auditing is off."""
    log = _default
    if log is None:
        return None
    return log.record(kind, **fields)
