"""Budget policy over calls, tokens and money.

Two different moments, deliberately:

* **Calls** are checked *before* the request. Refusing is only useful before the
  spend happens.
* **Tokens and cost** are charged *after* it, because their true size is unknown
  until the response arrives.

A charge never raises. The spend already happened, so the only honest thing it
can do is make the *next* request fail. That is also why ``check_budget`` keeps
returning False once a limit is crossed: a budget that can be talked back under
is not a budget.

``budget_per_workflow`` adds a second, narrower ceiling: one workflow's own
calls are capped even when the chain as a whole still has room. It is opt-in —
without it nothing changes, and ``get_state`` stays byte-identical.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

# 100 was sized for a metered gateway where every call costs money. The default
# route (deepseek-v4.1-flash on workbuddy-intl) is free and unlimited, so the
# call count is no longer the thing that has to be rationed: 100 per chain
# throttled throughput, not spend. The limit is kept as a runaway backstop
# rather than a budget. Point LLM_BUDGET at a metered route to ration again.
DEFAULT_CALL_LIMIT = 1000


def _number(raw: Any, cast):
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return None


class PolicyEngine:
    """Call / token / cost budget for one main chain.

    ``budget_limit`` counts model requests and keeps its original meaning, so
    ``PolicyEngine(100)`` behaves exactly as before. ``token_limit`` and
    ``cost_limit`` are optional; the cost limit only applies once both prices
    are configured, because an unpriced budget would silently never trigger.

    ``budget_per_workflow`` is a narrower ceiling on top of that: a positive
    int gives *every* workflow that many calls, a mapping caps the ids it names.
    The engine serves one chain, so a workflow that runs out of budget is the
    chain that runs out of budget — it stops rather than quietly spending the
    next workflow's room. Without this parameter the workflow id is ignored
    entirely, so an unused feature cannot change a snapshot.
    """

    def __init__(
        self,
        budget_limit: int = DEFAULT_CALL_LIMIT,
        *,
        token_limit: int | None = None,
        cost_limit: float | None = None,
        input_price_per_million: float | None = None,
        output_price_per_million: float | None = None,
        budget_per_workflow: int | Mapping[str, int] | None = None,
    ) -> None:
        if type(budget_limit) is not int or budget_limit <= 0:
            raise ValueError("budget_limit must be a positive integer")
        for name, value in (("token_limit", token_limit), ("cost_limit", cost_limit)):
            if value is not None and (not isinstance(value, (int, float)) or value <= 0):
                raise ValueError(f"{name} must be a positive number or None")
        for name, value in (
            ("input_price_per_million", input_price_per_million),
            ("output_price_per_million", output_price_per_million),
        ):
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise ValueError(f"{name} must be a non-negative number or None")

        if isinstance(budget_per_workflow, Mapping):
            for name, value in budget_per_workflow.items():
                if type(value) is not int or value <= 0:
                    raise ValueError(
                        f"budget_per_workflow[{name!r}] must be a positive integer"
                    )
        elif budget_per_workflow is not None and (
            type(budget_per_workflow) is not int or budget_per_workflow <= 0
        ):
            raise ValueError(
                "budget_per_workflow must be a positive integer, "
                "a mapping of them, or None"
            )

        self.budget_limit = budget_limit
        self.token_limit = token_limit
        self.cost_limit = cost_limit
        self.input_price_per_million = input_price_per_million
        self.output_price_per_million = output_price_per_million
        self.budget_per_workflow = budget_per_workflow

        self.current_spend = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_cost: float | None = None
        self.exhausted_reason: str | None = None
        self.workflow_spend: dict[str, int] = {}
        self.workflow_tokens: dict[str, int] = {}

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "PolicyEngine":
        """Build from the documented LLM_* variables.

        Unknown or malformed values fall back to "unset" rather than raising:
        a typo in an optional budget must not stop the chain from starting.
        """
        env = os.environ if environ is None else environ
        return cls(
            budget_limit=_number(env.get("LLM_BUDGET"), int) or DEFAULT_CALL_LIMIT,
            token_limit=_number(env.get("LLM_TOKEN_BUDGET"), int),
            cost_limit=_number(env.get("LLM_COST_BUDGET_USD"), float),
            input_price_per_million=_number(env.get("LLM_INPUT_PRICE_PER_MILLION"), float),
            output_price_per_million=_number(env.get("LLM_OUTPUT_PRICE_PER_MILLION"), float),
        )

    # -- accounting ---------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def priced(self) -> bool:
        """True when both prices are configured, so cost is a real number."""
        return (
            self.input_price_per_million is not None
            and self.output_price_per_million is not None
        )

    def _workflow_key(self, workflow_id: Any) -> str | None:
        """A workflow id that is not a non-empty string counts as "no workflow".

        A budget is a knob, not a gate: a caller that passes something malformed
        must not turn every model call into a TypeError.
        """
        if isinstance(workflow_id, str) and workflow_id:
            return workflow_id
        return None

    def _workflow_limit(self, key: str) -> int | None:
        """The ceiling for one workflow: a mapping entry, or the shared int."""
        spec = self.budget_per_workflow
        if isinstance(spec, Mapping):
            value = spec.get(key)
            return value if isinstance(value, int) else None
        return spec if isinstance(spec, int) else None

    def check_budget(self, cost: int = 1, workflow_id: Any = None) -> bool:
        """May one more request be issued? Reserves ``cost`` calls when it may.

        ``workflow_id`` opts into the narrower per-workflow ceiling. Passing
        nothing behaves exactly as before that ceiling existed.
        """
        if self.exhausted_reason is not None:
            return False
        if self.current_spend + cost > self.budget_limit:
            self.exhausted_reason = "call_budget_exhausted"
            return False
        key = self._workflow_key(workflow_id) if self.budget_per_workflow is not None else None
        if key is not None:
            limit = self._workflow_limit(key)
            if limit is not None and self.workflow_spend.get(key, 0) + cost > limit:
                self.exhausted_reason = "workflow_budget_exhausted"
                return False
        if self.token_limit is not None and self.total_tokens >= self.token_limit:
            self.exhausted_reason = "token_budget_exhausted"
            return False
        if (
            self.cost_limit is not None
            and self.total_cost is not None
            and self.total_cost >= self.cost_limit
        ):
            self.exhausted_reason = "cost_budget_exhausted"
            return False
        self.current_spend += cost
        if key is not None:
            self.workflow_spend[key] = self.workflow_spend.get(key, 0) + cost
        return True

    def charge(self, usage: Any, workflow_id: Any = None) -> dict:
        """Record observed usage after a request. Never raises.

        ``workflow_id`` only attributes the tokens to that workflow; the
        ceilings themselves stay where they were.
        """
        observed = 0
        if isinstance(usage, Mapping):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if type(prompt) is int and prompt >= 0:
                self.prompt_tokens += prompt
                observed += prompt
            if type(completion) is int and completion >= 0:
                self.completion_tokens += completion
                observed += completion
        key = self._workflow_key(workflow_id) if self.budget_per_workflow is not None else None
        if key is not None:
            self.workflow_tokens[key] = self.workflow_tokens.get(key, 0) + observed

        if self.priced:
            self.total_cost = round(
                (
                    self.prompt_tokens * self.input_price_per_million
                    + self.completion_tokens * self.output_price_per_million
                )
                / 1_000_000,
                8,
            )

        # Cross a limit and the next request is refused, not this one.
        if self.token_limit is not None and self.total_tokens >= self.token_limit:
            self.exhausted_reason = self.exhausted_reason or "token_budget_exhausted"
        if (
            self.cost_limit is not None
            and self.total_cost is not None
            and self.total_cost >= self.cost_limit
        ):
            self.exhausted_reason = self.exhausted_reason or "cost_budget_exhausted"
        return self.get_state()

    def get_state(self) -> dict:
        state = {
            "calls": self.current_spend,
            "call_limit": self.budget_limit,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "token_limit": self.token_limit,
            "total_cost": self.total_cost,
            "cost_limit": self.cost_limit,
            "priced": self.priced,
            "exhausted_reason": self.exhausted_reason,
        }
        if (
            self.budget_per_workflow is not None
            or self.workflow_spend
            or self.workflow_tokens
        ):
            # 只有在真的用了工作流预算时才多出这几个键：没用它时这份快照必须与从前
            # 逐字节一致 —— 它进的是产物。
            state["budget_per_workflow"] = (
                dict(self.budget_per_workflow)
                if isinstance(self.budget_per_workflow, Mapping)
                else self.budget_per_workflow
            )
            state["workflow_spend"] = dict(self.workflow_spend)
            state["workflow_tokens"] = dict(self.workflow_tokens)
        return state
