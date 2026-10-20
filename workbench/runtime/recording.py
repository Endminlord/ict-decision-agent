"""Record terminal runs verbatim, so a later reader can re-read and re-judge them.

Two rules, both inherited from ``runtime/audit.py``:

* **Absent by default.** With no recorder configured nothing is written and no
  directory is created. A reader that only asks where a recording *would* go
  must not leave anything on disk either.
* **Recording never raises.** A run that dies because its recorder was
  unavailable is worse than a run that was not recorded. Failures land in the
  manifest as ``write_error`` and are otherwise swallowed.

What is stored is the **raw** terminal outcome, not a distilled projection.
A projection cannot be re-derived: the replay tool learned this the hard way
(``tools/gate1.py`` originally kept only the distilled records, so readings and
character counts could no longer be recomputed). Distilling stays a separate,
re-runnable step.

Layout::

    <root>/<workflow_id>/<run_id>/result.json     the outcome, verbatim
    <root>/<workflow_id>/<run_id>/MANIFEST.json   how to read it back

``workflow_id`` comes from the outcome when the caller declared one; runs with
no declaration land under ``_unassigned`` — visibly unassigned, not silently
bucketed into some workflow's directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
UNASSIGNED = "_unassigned"
DEFAULT_ENV = "WORKBENCH_FIXTURES_DIR"
RECORDED_BY = "runtime/recording.py"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _safe_id(value: Any, fallback: str = "run") -> str:
    """Filesystem-safe identifier.

    Run ids and workflow ids reach us from traces and callers; a stray ``/`` or
    ``..`` must not be able to place a record outside the configured root.
    """
    text = str(value or "").strip()
    kept = [ch if (ch.isalnum() or ch in "-_.") else "_" for ch in text]
    # 只剥点：`..` 与隐藏文件要挡，但 `_unassigned` 这种自己人用的名字要留得住。
    cleaned = "".join(kept).strip(".")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:120]


def fingerprint(value: Any) -> tuple[str, bytes]:
    """Canonical fingerprint of a JSON-ish value (key order must not matter)."""
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), blob


def _gate_verdicts(outcome: dict) -> dict:
    """Gate name -> verdict, for the manifest's one-line summary."""
    gates = outcome.get("gates") or {}
    if not isinstance(gates, dict):
        return {}
    summary: dict[str, Any] = {}
    for name, value in gates.items():
        if isinstance(value, dict):
            summary[str(name)] = value.get("verdict", value.get("status"))
        else:
            summary[str(name)] = value
    return summary


def _artifact_index(outcome: dict) -> dict:
    """Artifact name -> {sha256, bytes}. Bodies stay in result.json, once."""
    artifacts = outcome.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        return {}
    index: dict[str, Any] = {}
    for name, value in artifacts.items():
        try:
            digest, blob = fingerprint(value)
        except Exception:  # pragma: no cover - default=str covers almost everything
            continue
        index[str(name)] = {"sha256": digest, "bytes": len(blob)}
    return index


class Recorder:
    """Write terminal outcomes under ``root``. Nothing is written until asked."""

    def __init__(self, root: str | os.PathLike[str] | None = None, *, workflow_id: str | None = None):
        self.root = Path(root) if root else None
        self.workflow_id = workflow_id

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def run_dir(self, outcome: dict) -> Path | None:
        """Where this outcome would go. Read-only: creates nothing."""
        if self.root is None:
            return None
        workflow = _safe_id(outcome.get("workflow_id") or self.workflow_id or UNASSIGNED, UNASSIGNED)
        trace = outcome.get("trace") if isinstance(outcome.get("trace"), dict) else {}
        run_id = _safe_id((trace or {}).get("trace_id") or outcome.get("task_id"), "run")
        if not run_id or run_id == "run":
            try:
                run_id = "fp_" + fingerprint(outcome)[0][:16]
            except Exception:
                run_id = "run"
        return self.root / workflow / run_id

    def record(self, outcome: dict) -> dict | None:
        """Store one terminal outcome. Returns the manifest, or None if disabled.

        Never raises: a recorder that can take the run down with it is a
        recorder that will be turned off, and then there is no recording at all.
        """
        if self.root is None:
            return None
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "recorded_by": RECORDED_BY,
            "at": _now(),
        }
        try:
            target = self.run_dir(outcome)
            assert target is not None
            digest, blob = fingerprint(outcome)
            manifest.update(
                {
                    "run_id": target.name,
                    "workflow_id": target.parent.name,
                    "task_id": outcome.get("task_id"),
                    "status": outcome.get("status"),
                    "mode": outcome.get("mode"),
                    "verification_level": outcome.get("verification_level"),
                    "reason": outcome.get("reason"),
                    "completed_stages": list(outcome.get("completed_stages") or []),
                    "resumed_from_agent": outcome.get("resumed_from_agent"),
                    "gates": _gate_verdicts(outcome),
                    "artifacts": _artifact_index(outcome),
                    "result_sha256": digest,
                    "result_bytes": len(blob),
                    "result_file": "result.json",
                    "execution_mode": os.environ.get("EXECUTION_MODE", ""),
                    "python": platform.python_version(),
                    "executable": sys.executable,
                }
            )
            target.mkdir(parents=True, exist_ok=True)
            (target / "result.json").write_bytes(blob)
            (target / "MANIFEST.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            manifest["write_error"] = f"{type(exc).__name__}: {exc}"
        return manifest


# ---------------------------------------------------------------------------
# Process-wide default, mirroring runtime/audit.py
# ---------------------------------------------------------------------------

_DEFAULT: Recorder | None = None


def set_default(recorder: Recorder | None) -> None:
    global _DEFAULT
    _DEFAULT = recorder


def get_default() -> Recorder | None:
    return _DEFAULT


def record(outcome: dict) -> dict | None:
    """Record into the configured recorder. No-op when none is configured."""
    recorder = _DEFAULT
    if recorder is None:
        return None
    return recorder.record(outcome)


def from_env(env: dict | None = None) -> Recorder | None:
    """A recorder from ``WORKBENCH_FIXTURES_DIR``, or None when it is unset."""
    source = os.environ if env is None else env
    root = (source.get(DEFAULT_ENV) or "").strip()
    if not root:
        return None
    return Recorder(root)


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def list_runs(root: str | os.PathLike[str]) -> list[dict]:
    """Every manifest under ``root``, oldest first. Never raises."""
    base = Path(root)
    if not base.is_dir():
        return []
    found: list[dict] = []
    for path in sorted(base.glob("*/*/MANIFEST.json")):
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    found.sort(key=lambda m: (m.get("at") or "", m.get("run_id") or ""))
    return found


def load_run(run_dir: str | os.PathLike[str]) -> dict | None:
    """The verbatim outcome stored in ``run_dir``, or None when unreadable."""
    path = Path(run_dir) / "result.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
