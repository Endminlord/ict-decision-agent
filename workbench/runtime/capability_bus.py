"""Registry of named capabilities a domain can ask for.

A domain declares which verification skill it needs (``verification.capability``
in its contract); the bus maps that name to a callable. This is what keeps a
domain's *policy* in ``domains/`` while its *mechanism* lives in code.

It used to be a dead seam: ``run.py`` put ``capability_bus: None`` into state and
nothing ever read it. The bus is now built by default and consulted by the chain.
"""

from __future__ import annotations

from typing import Any, Callable


class CapabilityBus:
    def __init__(self) -> None:
        self.capabilities: dict[str, Callable[..., Any]] = {}

    def register(self, name: Any, func: Any) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("capability name must be a nonempty string")
        if not callable(func):
            raise TypeError(f"capability {name!r} must be callable")
        self.capabilities[name] = func

    def has(self, name: Any) -> bool:
        return isinstance(name, str) and name in self.capabilities

    def list(self) -> list[str]:
        return sorted(self.capabilities)

    def call(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        if not self.has(name):
            # Reported, not raised: a missing capability degrades the run, it
            # does not crash it.
            return {"error": f"capability '{name}' not found", "available": self.list()}
        # 三档安全执行点（11-B 第 11 项）：派发之前分类。import 放函数里是为了
        # 让本模块的导入不把沙箱探测（会起子进程）也一起拉起来。
        from runtime import safety_tiers

        safety_tiers.gate(f"capability:{name}", kind="action",
                          caller="capability_bus",
                          isolation_state=safety_tiers.isolation())
        return self.capabilities[name](*args, **kwargs)

    def get_state(self) -> dict:
        return {"capabilities": self.list()}


def build_default_bus() -> CapabilityBus:
    """The capabilities this harness ships.

    Registered lazily so importing this module does not pull in the executor,
    the sandbox or the patch machinery.
    """
    from runtime.executor import execute_code, execute_verilog
    from runtime.patch import apply_patch

    bus = CapabilityBus()
    bus.register("execute_code", execute_code)
    bus.register("execute_verilog", execute_verilog)
    bus.register("apply_patch", apply_patch)
    return bus


def capability_for(domain_config: Any) -> str | None:
    """The capability a domain's contract asks for, if any."""
    if not isinstance(domain_config, dict):
        return None
    contract = domain_config.get("contract")
    if not isinstance(contract, dict):
        return None
    verification = contract.get("verification")
    if not isinstance(verification, dict):
        return None
    name = verification.get("capability")
    return name if isinstance(name, str) and name.strip() else None
