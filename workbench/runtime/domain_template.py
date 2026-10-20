"""Declarative domain definition.

A domain is a **declaration**, not behaviour: it names itself, declares the
keywords that route work to it, and carries the prompts it wants prepended.
Keeping the shape here — and the routing in ``registry.py`` — is what lets domain
policy live in ``domains/`` instead of being hardcoded in the kernel.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


class DomainTemplate:
    """A validated domain declaration."""

    def __init__(
        self,
        domain_id: str,
        name: str,
        keywords: Iterable[str] | None = None,
        prompts: Mapping[str, Any] | None = None,
        description: str = "",
        contract: Mapping[str, Any] | None = None,
        adversary: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(domain_id, str) or not domain_id.strip():
            raise ValueError("domain_id must be a nonempty string")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a nonempty string")
        collected = []
        for keyword in keywords or ():
            if not isinstance(keyword, str) or not keyword.strip():
                raise ValueError(f"domain {domain_id!r}: keywords must be nonempty strings")
            collected.append(keyword)
        if prompts is not None and not isinstance(prompts, Mapping):
            raise ValueError(f"domain {domain_id!r}: prompts must be a mapping")
        if contract is not None and not isinstance(contract, Mapping):
            raise ValueError(f"domain {domain_id!r}: contract must be a mapping")
        if adversary is not None and not isinstance(adversary, Mapping):
            raise ValueError(f"domain {domain_id!r}: adversary must be a mapping")

        self.domain_id = domain_id
        self.name = name
        self.keywords = tuple(collected)
        self.prompts = dict(prompts or {})
        # The domain's verification contract, if it has one. Keeping it here —
        # as data — is what lets a domain decide the spec shape instead of the
        # spec builder hardcoding a branch per domain.
        self.contract = dict(contract) if contract else None
        # 红队策略：领域可以说「这些邻居类别对我不适用」。同样是数据——红队自己
        # 不认识任何领域，它只知道哪些类别被允许。ic_design 就是一个现成的例子：
        # verilog 的邻居怎么推还没想清楚，于是那里明确关掉，而不是让它静默地不跑。
        self.adversary = dict(adversary) if adversary else None
        self.description = description or name

    @classmethod
    def from_dict(cls, data: Any) -> "DomainTemplate":
        if not isinstance(data, Mapping):
            raise ValueError("a domain declaration must be a mapping")
        return cls(
            domain_id=data.get("domain_id"),
            name=data.get("name") or data.get("description") or data.get("domain_id"),
            keywords=data.get("keywords") or (),
            prompts=data.get("prompts"),
            description=data.get("description") or "",
            contract=data.get("contract"),
            adversary=data.get("adversary"),
        )

    def hits(self, text: str) -> int:
        """How many of this domain's keywords appear in ``text``."""
        if not isinstance(text, str):
            return 0
        lowered = text.lower()
        return sum(1 for keyword in self.keywords if keyword.lower() in lowered)

    def to_dict(self) -> dict:
        return {
            "domain_id": self.domain_id,
            "name": self.name,
            "description": self.description,
            "keywords": list(self.keywords),
            "prompts": dict(self.prompts),
            "contract": dict(self.contract) if self.contract else None,
            "adversary": dict(self.adversary) if self.adversary else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DomainTemplate({self.domain_id!r}, {len(self.keywords)} keywords)"
