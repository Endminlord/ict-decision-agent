"""Domain routing.

The registry decides which domain a request belongs to by counting keyword hits.
The domains themselves are **declarations that live in ``domains/domains.json``**,
not in this module — that is what keeps the kernel free of domain policy: adding
or changing a domain is a data change, not a code change.

If the file is missing or malformed the registry falls back to the two built-in
domains and records why in ``load_error``. A routing table that cannot be read
should not stop a run; it should say so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from runtime.domain_template import DomainTemplate

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "domains" / "domains.json"

# Used only when domains/domains.json cannot be read. Deliberately policy-free:
# a single fallback domain with no keywords. Carrying the real keyword lists here
# would put domain policy back in the kernel and quietly hide a broken
# declaration file — every request would still route "correctly".
BUILTIN = (
    DomainTemplate("default", "通用任务", (), description="未加载领域声明时的兜底"),
)


class DomainRegistry:
    def __init__(self, path: Any = None) -> None:
        self.domains: dict[str, DomainTemplate] = {}
        self.load_error: str | None = None
        self.source: str | None = None
        self._load(Path(path) if path is not None else DEFAULT_PATH)

    def _load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("domains") if isinstance(data, dict) else data
            if not isinstance(entries, list) or not entries:
                raise ValueError("domains file must contain a nonempty 'domains' list")
            for entry in entries:
                domain = DomainTemplate.from_dict(entry)
                if domain.domain_id in self.domains:
                    raise ValueError(f"duplicate domain_id: {domain.domain_id}")
                self.domains[domain.domain_id] = domain
            self.source = str(path)
        except (OSError, ValueError, TypeError) as exc:
            self.load_error = f"{type(exc).__name__}: {exc}"
            self.domains = {domain.domain_id: domain for domain in BUILTIN}
            self.source = "builtin"

    def register(self, domain_id: str, description: str = "", keywords: Any = None) -> None:
        """Add a domain at runtime, for callers that build one in code."""
        self.domains[domain_id] = DomainTemplate(
            domain_id, description or domain_id, keywords or ()
        )

    def get(self, domain_id: str) -> DomainTemplate | None:
        return self.domains.get(domain_id)

    def list_domains(self) -> list[str]:
        return sorted(self.domains)

    def match(self, user_input: Any) -> dict:
        """Route to the domain with the most keyword hits, defaulting to 'default'."""
        text = user_input if isinstance(user_input, str) else ""
        best_domain = "default"
        max_hits = 0
        for domain_id, domain in self.domains.items():
            hits = domain.hits(text)
            if hits > max_hits:
                max_hits = hits
                best_domain = domain_id
        if best_domain not in self.domains and self.domains:
            best_domain = "default" if "default" in self.domains else next(iter(self.domains))
        matched = self.domains.get(best_domain)
        return {
            "domain_id": best_domain,
            "hits": max_hits,
            "degraded_reason": self.load_error,
            # The declaration travels with the match so downstream stages can act
            # on it. Returning only an id made every domain decorative: it was
            # computed, recorded, and never consulted.
            "domain": matched.to_dict() if matched is not None else None,
        }

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "load_error": self.load_error,
            "domains": {key: domain.to_dict() for key, domain in self.domains.items()},
        }
