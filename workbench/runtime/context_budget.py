"""Context budget: keep the working set bounded and auditable.

``context["artifacts"]`` accumulates every Agent's output and nothing capped it.
The offline fixture only produces ~4.5 KB, but a real run carries markdown
documents (requirements, spec, TRD) and generated code, and every rollback
dispatch re-carries the whole set into the resumed pass.

The policy is deliberately asymmetric:

* An Agent output is **never dropped** to satisfy the budget. Every L1 artifact
  is read by a downstream stage, so dropping one would break the chain — and a
  budget that can break the chain is worse than no budget.
* The lever is **field-level**. Each artifact declares the fields its consumers
  actually read; everything else (advisories, echoes, debug payloads) is
  expendable and is removed first.
* If an artifact is still oversized after trimming, that is **reported as a
  violation**, not silently truncated. Truncating ``green_code`` or ``test_cases``
  would corrupt data rather than bound it.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# The fields each Agent's downstream consumers actually read. Kept deliberately
# narrow: this is a contract, not a wish list.
ESSENTIAL_FIELDS = {
    "orchestrator": ("task_dag", "context_snapshot"),
    "hermes": ("preference_pack", "memory_index"),
    "prd_builder": ("prd",),
    "req_writer": ("requirements_md",),
    "sdd_spec_builder": ("spec",),
    "trd_adr_generator": ("trd", "adr"),
    "tdd_planner": ("test_cases",),
    "tdd_coder": ("green_code", "test_run_result"),
    "tdd_reviewer": ("review_report",),
    "e2e_validator": ("verify_md", "assertions", "execution", "verdict", "rollback_signal"),
}

# Sized for this project's own documents: V5_IMPLEMENTATION_SPEC.md is 125 KB
# and CHANGELOG.md is 445 KB. The previous 64 KB / 256 KB pair sat *below* a
# single spec, so every real run reported a violation and the report stopped
# discriminating between "big but fine" and "actually broken".
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 512 * 1024


def _size(value: Any) -> int:
    """Byte size of the JSON projection, which is what actually gets persisted."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        return len(repr(value).encode("utf-8", errors="replace"))


class ContextBudget:
    """Bound the artifact working set without ever dropping a required one."""

    def __init__(
        self,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        for name, value in (
            ("max_total_bytes", max_total_bytes),
            ("max_artifact_bytes", max_artifact_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_total_bytes = max_total_bytes
        self.max_artifact_bytes = max_artifact_bytes

    def fit(self, artifacts: Any) -> tuple[dict, dict]:
        """Return ``(artifacts, report)``. No artifact is ever removed."""
        if not isinstance(artifacts, Mapping):
            raise TypeError("artifacts must be a mapping")

        kept: dict[str, Any] = {}
        trimmed_fields: dict[str, list[str]] = {}
        oversized: list[dict] = []

        for name, value in artifacts.items():
            size = _size(value)
            fields = ESSENTIAL_FIELDS.get(name)
            if (
                fields is not None
                and isinstance(value, Mapping)
                and size > self.max_artifact_bytes
            ):
                trimmed = {key: value[key] for key in fields if key in value}
                if trimmed and _size(trimmed) < size:
                    trimmed_fields[name] = sorted(set(value) - set(trimmed))
                    value = trimmed
                    size = _size(value)
            kept[name] = value
            if size > self.max_artifact_bytes:
                oversized.append({"artifact": name, "bytes": size})

        total = sum(_size(value) for value in kept.values())
        report = {
            "max_total_bytes": self.max_total_bytes,
            "max_artifact_bytes": self.max_artifact_bytes,
            "total_bytes": total,
            "over_total_budget": total > self.max_total_bytes,
            "trimmed_fields": trimmed_fields,
            "oversized_artifacts": oversized,
        }
        return kept, report
