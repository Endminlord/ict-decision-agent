"""Evaluation harness: measure quality, and detect when a change makes it worse.

This is deliberately not a test suite. The regression tests under ``tests/``
assert "nothing broke"; an evaluation measures "how good is it" so two revisions
can be compared. Without it there is no way to answer whether a change improved
or degraded generated-code quality.

The harness records per-case outcomes and reduces them to a small metric set.
``compare()`` then diffs two metric sets and classifies each metric as a
regression, an improvement or unchanged. Metrics are directional: more
``completed`` is better, more ``tokens`` is worse.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

# metric -> "higher_is_better" / "lower_is_better"
DIRECTION = {
    "cases": "neutral",
    "completed": "higher",
    "completed_rate": "higher",
    "verified": "higher",
    "verified_rate": "higher",
    "total_retries": "lower",
    "total_dispatches": "lower",
    "total_tokens": "lower",
    # Reported, not gated. Wall-clock on a shared machine is not a quality
    # signal: identical runs here varied 84%, and a run of the same five cases
    # exceeded 2x while every other metric stayed bit-for-bit identical. Raising
    # the tolerance only chases the noise, and a gate that never fires is worth
    # nothing — so this stays diagnostic.
    "total_elapsed_ms": "neutral",
    "usage_complete_rate": "higher",
}

# Relative tolerance for the cost-ish metrics, so noise does not read as a
# regression. The deterministic metrics (tokens, retries, dispatches) are exactly
# reproducible, so 5% is generous for them.
DEFAULT_TOLERANCE = 0.05

# Per-metric bands, for a metric whose noise floor differs from the default.
# Currently empty: the only candidate was wall-clock time, which is now neutral
# (see DIRECTION) because its variance could not be bounded on a shared machine.
METRIC_TOLERANCE: dict[str, float] = {}


class EvalHarness:
    """Accumulates per-case metrics for one evaluation run.

    ``log`` keeps its original three-argument form; extra keyword fields are
    stored alongside so a run can carry verification level, token use and
    dispatch count without breaking existing callers.
    """

    def __init__(self) -> None:
        self.metrics: list[dict] = []

    def log(self, task_id: Any, status: Any, retries: Any, **extra: Any) -> dict:
        entry = {"task_id": task_id, "status": status, "retries": retries}
        entry.update(extra)
        self.metrics.append(entry)
        return entry

    def summary(self) -> dict:
        return summarize(self.metrics)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def summarize(metrics: Iterable[Any]) -> dict:
    """Reduce per-case records to the metric set used for comparison."""
    entries = [m for m in (metrics or ()) if isinstance(m, Mapping)]
    cases = len(entries)

    status_counts: dict[str, int] = {}
    completed = verified = retries = dispatches = tokens = 0
    elapsed = 0.0
    usage_measured = 0

    for entry in entries:
        status = entry.get("status")
        status_counts[str(status)] = status_counts.get(str(status), 0) + 1
        if status == "completed":
            completed += 1
        if entry.get("verification_level") == "contract_pass":
            verified += 1
        for key, total in (("retries", "retries"),):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                retries += int(value)
        value = entry.get("dispatches")
        if isinstance(value, (int, float)):
            dispatches += int(value)
        value = entry.get("total_tokens")
        if isinstance(value, (int, float)):
            tokens += int(value)
            usage_measured += 1
        value = entry.get("elapsed_ms")
        if isinstance(value, (int, float)):
            elapsed += float(value)

    return {
        "cases": cases,
        "completed": completed,
        "completed_rate": _rate(completed, cases),
        "verified": verified,
        "verified_rate": _rate(verified, cases),
        "total_retries": retries,
        "total_dispatches": dispatches,
        "total_tokens": tokens,
        "total_elapsed_ms": round(elapsed, 3),
        "usage_complete_rate": _rate(usage_measured, cases),
        "status_counts": status_counts,
    }


def compare(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict:
    """Diff two summaries. Returns regressions, improvements and a verdict."""
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
        raise TypeError("baseline and current must be mappings")
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be within [0, 1)")

    regressions, improvements, unchanged = [], [], []
    for metric, direction in DIRECTION.items():
        if direction == "neutral" or metric not in baseline or metric not in current:
            continue
        before = baseline[metric]
        after = current[metric]
        if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
            continue

        delta = after - before
        # Timing gets its own band; see METRIC_TOLERANCE.
        band = METRIC_TOLERANCE.get(metric, tolerance)
        if direction == "higher":
            worse = after < before * (1 - band) if before > 0 else after < before
            better = after > before * (1 + band) if before > 0 else after > before
        else:
            worse = after > before * (1 + band) if before > 0 else after > before
            better = after < before * (1 - band) if before > 0 else after < before

        record = {
            "metric": metric,
            "baseline": before,
            "current": after,
            "delta": round(delta, 6),
            "direction": direction,
            "tolerance": band,
        }
        if worse:
            regressions.append(record)
        elif better:
            improvements.append(record)
        else:
            unchanged.append(record)

    return {
        "ok": not regressions,
        "tolerance": tolerance,
        "regressions": regressions,
        "improvements": improvements,
        "unchanged": unchanged,
    }


def render(comparison: Mapping[str, Any]) -> str:
    """Human-readable report for a comparison."""
    lines = ["evaluation comparison", ""]
    if not comparison.get("regressions") and not comparison.get("improvements"):
        lines.append("  no metric changed beyond tolerance")
    for label, key in (("REGRESSION", "regressions"), ("improvement", "improvements")):
        for record in comparison.get(key) or ():
            lines.append(
                f"  {label:<11} {record['metric']:<20} "
                f"{record['baseline']} -> {record['current']} ({record['delta']:+})"
            )
    lines.append("")
    lines.append("  verdict: " + ("PASS" if comparison.get("ok") else "REGRESSION"))
    return "\n".join(lines)
