"""Three-tier safety gate for actions that leave the harness's own process.

判据 (``FINAL-方案.md`` §11.5 / ``research/92-workbench-automation.md:41``，逐字):

    允许（自动执行） = 可逆 ∧ 不出域 ∧ 不花钱 ∧ 不触生产 ∧ 不写外部
    禁止（直接拒绝） = 不可逆 ∧ (出域 ∨ 触生产 ∨ 涉凭据)
    排队（等醒）     = 其余全部

What this module is for: making that decision **legible and fail-closed**. An
action whose attributes are not known is *queued*, never allowed — "I don't know"
must not resolve to "go ahead".

Two things it deliberately does NOT do:

* **it does not hand-enumerate every action in the repository.** A hand-written
  table over 34 agent files is a table nobody can keep true; an entry that is
  wrong in the permissive direction is worse than no entry. Unknown actions and
  unknown commands queue, and the table only records what has actually been
  classified. Growing the table is a separate, reviewable change.
* **it does not block anything by default.** ``SAFETY_TIER_MODE=report`` (the
  default) computes and records the tier; ``enforce`` turns a 排队/禁止 verdict
  into an exception. The *classifier* is fail-closed, the *mode* is opt-in, so
  switching the gate on stays a visible decision instead of a side effect of
  installing this file.

The 允许 tier's "沙箱内执行" requires isolation that is **really applied** —
filesystem *and* network. ``runtime/sandbox.py`` reports that truthfully
(``wrap_command`` returns ``applied``), and the cheap default backend
(``namespace``) does **not** confine the filesystem, so on a host without
``SANDBOX_BACKEND=docker`` most executions queue. That is the honest outcome, not
a bug: see ``sandbox.describe()``.
"""

from __future__ import annotations

import contextvars
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from runtime import sandbox
from runtime.audit import record as audit_record

TIER_ALLOW = "allow"
TIER_QUEUE = "queue"
TIER_FORBID = "forbid"
TIERS = (TIER_ALLOW, TIER_QUEUE, TIER_FORBID)

MODE_ENV = "SAFETY_TIER_MODE"
MODES = ("report", "enforce")
DEFAULT_MODE = "report"

# The seven attributes the three-tier criteria are written in terms of. Every one
# of them is required: a missing attribute queues the action.
ATTRS = (
    "reversible",          # 可逆：动作可以被撤销，且撤销不依赖外部世界
    "outside",             # 出域：接触本机以外的任何东西（含外部网络）
    "costs_money",         # 花钱：任何按量计费的调用
    "touches_production",  # 触生产：改到正在服务的实例/数据
    "writes_external",     # 写外部：向本机以外的系统写（PR、消息、上传）
    "credentials",         # 涉凭据：读或写凭据、代持登录态
    "executes",            # 执行：真的拉起一个子进程/跑代码
)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
LOOPBACK_PORTS_ENV = "SAFETY_LOOPBACK_PORTS"
# The ports the 0f capability catalog registers as reachable local services.
DEFAULT_LOOPBACK_PORTS = (8101,)

# Precise markers only. A bare "token" would match a file named tokenizer.py and
# forbid an innocent command; a gate that fires on the wrong thing gets disabled.
CREDENTIAL_MARKERS = (
    ".credentials.yaml", "credentials.json", ".netrc", "id_rsa", "id_ed25519",
    ".ssh/", ".aws/credentials", ".dsh-backup-creds",
)

PENDING_ENV = "SAFETY_PENDING_DIR"

# 「这是哪一场运行」——给 `gate()` 写下的 `pending_action` 补回链用（11-B 第 13 项）。
_RUN_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "safety_tiers_run_context", default=None
)


def bind_run(*, run_id: str | None = None, workflow_id: str | None = None,
             step_id: str | None = None) -> contextvars.Token:
    """Bind which run is executing, so `gate()`'s records carry a back-reference.

    `gate()` 在很深的地方被调用：capability 派发之前、`Popen` 之前。那里没有 run
    的概念，而收件箱里的 `pending_action` 需要回链（`run_id` / `workflow_id` /
    `step_id`）——没有它，那条动作只能被人读，不能被任何 run 认领回去（11-B 第 13
    项要的正是这条链）。用 contextvar 而不是给每个调用点加参数：参数得穿过
    capability 的实参列表，而那些实参是给能力本身的。

    用完 `reset_run(token)` 还回去。
    """
    return _RUN_CONTEXT.set({"run_id": run_id, "workflow_id": workflow_id,
                             "step_id": step_id})


def reset_run(token: contextvars.Token) -> None:
    """Undo `bind_run`. Never raises: a token from another context is not fatal."""
    try:
        _RUN_CONTEXT.reset(token)
    except (ValueError, LookupError):
        pass


def current_run() -> dict:
    """The bound run, or `{}` when nothing is bound."""
    return dict(_RUN_CONTEXT.get() or {})


class ActionQueued(RuntimeError):
    """排队档：动作没被拒绝，但它不能在这一轮里自动发生。"""

    def __init__(self, verdict: Mapping[str, Any],
                 pending: Mapping[str, Any] | None = None) -> None:
        self.verdict = dict(verdict)
        # 已经写进收件箱的那条记录（11-B 第 13 项）。没有它，链子拿到的「排队」就只剩
        # 一句异常文本：收件箱里躺着一条动作，而**没有任何 run 知道那是自己留下的**，
        # 「独立 run 认领回去」这条链就断了。
        self.pending = dict(pending) if pending is not None else None
        super().__init__(
            "action_queued: " + ", ".join(self.verdict.get("why") or ["unspecified"])
        )


class ActionForbidden(RuntimeError):
    """禁止档：直接拒绝，不是「等醒」。"""

    def __init__(self, verdict: Mapping[str, Any]) -> None:
        self.verdict = dict(verdict)
        super().__init__(
            "action_forbidden: " + ", ".join(self.verdict.get("why") or ["unspecified"])
        )


def mode(explicit: str | None = None) -> str:
    """``report`` (default) or ``enforce``. Unknown values fall back to report."""
    value = (explicit or os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def loopback_ports() -> tuple[int, ...]:
    raw = os.environ.get(LOOPBACK_PORTS_ENV)
    if not raw:
        return DEFAULT_LOOPBACK_PORTS
    ports: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ports.append(int(part))
    return tuple(ports) or DEFAULT_LOOPBACK_PORTS


def isolation(*, applied: Mapping[str, Any] | None = None,
              capabilities: Mapping[str, Any] | None = None,
              refresh: bool = False) -> dict:
    """What isolation is really in place, from either source.

    ``applied`` (what ``sandbox.wrap_command`` actually put in the argv) is
    authoritative; ``capabilities`` (``sandbox.detect()``) is a pre-flight guess.
    The distinction matters: the wrapper is the only thing that knows whether the
    argv it built really isolates the filesystem.
    """
    if applied is not None:
        filesystem = bool(applied.get("filesystem_isolated"))
        network = bool(applied.get("network_isolated"))
        return {"backend": applied.get("backend"), "filesystem": filesystem,
                "network": network, "ok": filesystem and network, "source": "applied"}
    caps = dict(capabilities) if capabilities is not None else sandbox.detect(refresh=refresh)
    backend = caps.get("backend")
    filesystem = bool(caps.get("filesystem_isolated"))
    # Pre-flight: docker always runs with --network none; namespace/bwrap paths
    # isolate the network when the host can and SANDBOX_NETWORK is not off.
    network = filesystem or bool(caps.get("network"))
    return {"backend": backend, "filesystem": filesystem, "network": network,
            "ok": filesystem and network, "source": "capabilities"}


def _verdict(tier: str, why: list[str], criteria: Mapping[str, bool],
             unknown: Iterable[str], action: str | None,
             isolation_state: Mapping[str, Any] | None) -> dict:
    return {"tier": tier, "why": why, "criteria": dict(criteria),
            "unknown": sorted(unknown), "action": action,
            "isolation": dict(isolation_state) if isolation_state else None}


def evaluate(attributes: Mapping[str, Any], *, action: str | None = None,
             isolation_state: Mapping[str, Any] | None = None) -> dict:
    """Classify one action from its attributes. Pure; no I/O, no environment.

    Unknown or absent attributes queue. That is the whole fail-closed story: the
    gate never guesses in the permissive direction.
    """
    if not isinstance(attributes, Mapping):
        raise TypeError("attributes must be a mapping")
    unknown = [name for name in ATTRS if attributes.get(name) is None]
    criteria = {name: bool(attributes.get(name)) for name in ATTRS
                if attributes.get(name) is not None}
    if unknown:
        return _verdict(TIER_QUEUE, [f"attribute_unknown:{n}" for n in unknown],
                        criteria, unknown, action, isolation_state)

    irreversible = not criteria["reversible"]
    forbidden: list[str] = []
    if irreversible and criteria["outside"]:
        forbidden.append("irreversible_and_outside")
    if irreversible and criteria["touches_production"]:
        forbidden.append("irreversible_and_production")
    if irreversible and criteria["credentials"]:
        forbidden.append("irreversible_and_credentials")
    if forbidden:
        return _verdict(TIER_FORBID, forbidden, criteria, [], action, isolation_state)

    blockers: list[str] = []
    if not criteria["reversible"]:
        blockers.append("not_reversible")
    # 允许档 requires all five negatives; 出域/花钱/触生产/写外部/涉凭据 each block it.
    for name in ("outside", "costs_money", "touches_production",
                 "writes_external", "credentials"):
        if criteria[name]:
            blockers.append(name)
    if criteria["executes"]:
        if not (isolation_state or {}).get("ok"):
            blockers.append("execution_without_filesystem_and_network_isolation")
    if blockers:
        return _verdict(TIER_QUEUE, blockers, criteria, [], action, isolation_state)
    return _verdict(TIER_ALLOW, [], criteria, [], action, isolation_state)


# --- the table: what has actually been classified -------------------------
#
# Seeded from the two chokepoints that exist today (CapabilityBus.call and
# run_trusted), not from a sweep of every side effect in the tree. Adding a row
# is a claim that someone looked at that action; an unlisted action queues.

_EXEC = {"reversible": True, "outside": False, "costs_money": False,
         "touches_production": False, "writes_external": False,
         "credentials": False, "executes": True}
_READ = {"reversible": True, "outside": False, "costs_money": False,
         "touches_production": False, "writes_external": False,
         "credentials": False, "executes": False}
_WRITE_NEW = dict(_READ, reversible=True)
_OVERWRITE = dict(_READ, reversible=False)      # 覆盖文件：内容没了，除非有备份
_DESTRUCTIVE = dict(_READ, reversible=False)
_DISPATCH = dict(_EXEC, costs_money=None)       # 调 Agent：花不花钱要看运行模式 ⇒ 未知 ⇒ 排队

ACTION_TABLE: dict[str, dict] = {
    # CapabilityBus capabilities (runtime/capability_bus.py:44-57)
    "capability:execute_code": dict(_EXEC),
    "capability:execute_verilog": dict(_EXEC),
    "capability:apply_patch": dict(_OVERWRITE),
    # Generic verbs a caller can name directly
    "read:summary": dict(_READ),
    "read:file": dict(_READ),
    "write:new_file": dict(_WRITE_NEW),
    "write:overwrite_file": dict(_OVERWRITE),
    "delete:file": dict(_DESTRUCTIVE),
    "delete:production_data": dict(_DESTRUCTIVE, touches_production=True),
    "credentials:read": dict(_READ, credentials=True, reversible=False),
    "credentials:write": dict(_DESTRUCTIVE, credentials=True),
    "network:http_get": dict(_READ),
    "network:http_post": dict(_READ, outside=True, writes_external=True),
    "dispatch:local_agent": dict(_DISPATCH, outside=False),  # 本地也可能花钱 ⇒ 未知 ⇒ 排队
    "dispatch:cloud_agent": dict(_DISPATCH, outside=True, costs_money=True),
    "sandbox:run_command": dict(_EXEC),
}

# Commands run_trusted is allowed to see. Unknown commands queue.
_INTERPRETERS = ("iverilog", "vvp", "python", "python3", "pytest", "node", "npm",
                 "gcc", "cc", "g++", "clang", "make", "cmake", "cargo", "go",
                 "javac", "java", "dotnet", "sh", "bash", "ruby", "perl", "php")
_DESTRUCTIVE_COMMANDS = ("rm", "rmdir", "dd", "mkfs", "shred", "truncate",
                         "chmod", "chown", "chgrp", "mv")
_NETWORK_COMMANDS = ("curl", "wget", "nc", "netcat", "ssh", "scp", "sftp",
                     "rsync", "telnet", "ftp")
_PACKAGE_COMMANDS = ("pip", "pip3", "pnpm", "yarn", "apt", "apt-get", "dpkg",
                     "uv", "gem", "conda", "brew")
_GIT_READ_SUBCOMMANDS = ("status", "diff", "log", "show", "branch", "rev-parse",
                         "ls-files", "describe", "remote", "config", "blame")
_GIT_WRITE_SUBCOMMANDS = ("add", "commit", "checkout", "switch", "merge",
                          "rebase", "reset", "restore", "stash", "tag", "init")
_GIT_OUTSIDE_SUBCOMMANDS = ("push", "fetch", "pull", "clone", "submodule", "remote-update")


def _basename(part: str) -> str:
    return os.path.basename(str(part)).lower()


def _has_credentials(parts: Sequence[str]) -> bool:
    joined = " ".join(str(p) for p in parts).lower()
    return any(marker in joined for marker in CREDENTIAL_MARKERS)


def _host_port(url: str) -> tuple[str, int | None]:
    rest = url.split("://", 1)[1] if "://" in url else url
    rest = rest.split("/", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    if rest.startswith("["):                      # [::1]:8101
        host, _, tail = rest.partition("]")
        host = host + "]"
        port = tail[1:] if tail.startswith(":") else ""
    elif ":" in rest:
        host, _, port = rest.rpartition(":")
    else:
        host, port = rest, ""
    return host.lower(), int(port) if port.isdigit() else None


def classify_command(command: Sequence[str], *, isolation_state: Mapping[str, Any] | None = None) -> dict:
    """Classify a subprocess command by its argv. Unknown commands queue.

    The rules are deliberately coarse and permissive-nowhere: a command whose
    effect this function cannot name is queued, not waved through.
    """
    if isinstance(command, (str, bytes)):
        raise TypeError("command must be a sequence of arguments, not a string")
    parts = [str(p) for p in command]
    if not parts:
        raise ValueError("command must not be empty")
    label = f"command:{_basename(parts[0])}"
    if _has_credentials(parts):
        return evaluate(dict(_READ, credentials=True, reversible=False),
                        action=label, isolation_state=isolation_state)

    name = _basename(parts[0])
    if name in _INTERPRETERS:
        return evaluate(dict(_EXEC), action=label, isolation_state=isolation_state)
    if name in _DESTRUCTIVE_COMMANDS:
        return evaluate(dict(_DESTRUCTIVE), action=label, isolation_state=isolation_state)
    if name in _PACKAGE_COMMANDS:
        # Installs fetch from the network and write into shared trees: 出域 + 写外部.
        return evaluate(dict(_READ, outside=True, writes_external=True),
                        action=label, isolation_state=isolation_state)
    if name in _NETWORK_COMMANDS:
        args = parts[1:]
        urls = [a for a in args if "://" in a or a.lower().startswith(("localhost", "127.0.0.1"))]
        method = "GET"
        for i, arg in enumerate(args):
            if arg in ("-X", "--request") and i + 1 < len(args):
                method = args[i + 1].upper()
            elif arg in ("-d", "--data", "--data-raw", "-F", "--form", "--upload-file", "-T"):
                method = "POST"
        if not urls:
            # A network tool with no URL in the argv: cannot be called 不出域.
            return evaluate(dict(_READ, outside=True), action=label,
                            isolation_state=isolation_state)
        host, port = _host_port(urls[0])
        loopback = host in LOOPBACK_HOSTS and port in loopback_ports()
        if not loopback:
            return evaluate(dict(_READ, outside=True, writes_external=method != "GET"),
                            action=label, isolation_state=isolation_state)
        if method == "GET":
            # 读摘要：回环 + 登记端口 ⇒ 不出域。
            return evaluate(dict(_READ), action=label, isolation_state=isolation_state)
        # 调本地外部 Agent（POST/PUT/…）：不是「读」，排队。
        return evaluate(dict(_DISPATCH, outside=False), action=label,
                        isolation_state=isolation_state)
    if name == "git":
        sub = next((a.lower() for a in parts[1:] if not a.startswith("-")), "")
        if sub in _GIT_OUTSIDE_SUBCOMMANDS:
            # 出域 + 写外部，但推送本身可逆（可再推一次）⇒ 排队；--force 不可逆 ⇒ 禁止。
            force = any(a in ("--force", "-f", "--force-with-lease") for a in parts[1:])
            return evaluate(dict(_READ, outside=True, writes_external=True,
                                 reversible=not force),
                            action=label, isolation_state=isolation_state)
        if sub in _GIT_WRITE_SUBCOMMANDS:
            return evaluate(dict(_OVERWRITE), action=label, isolation_state=isolation_state)
        if sub in _GIT_READ_SUBCOMMANDS:
            return evaluate(dict(_READ), action=label, isolation_state=isolation_state)
        return _verdict(TIER_QUEUE, ["unclassified_command"], {}, ATTRS, label,
                        isolation_state)
    # 认不得 ⇒ 排队（fail-closed）。说「这是不认得的命令」比列出七条未知属性更可读。
    return _verdict(TIER_QUEUE, ["unclassified_command"], {}, ATTRS, label,
                    isolation_state)


def classify_action(name: Any, *, isolation_state: Mapping[str, Any] | None = None,
                    **overrides: Any) -> dict:
    """Classify a named action. Unknown names queue (fail-closed)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("action name must be a nonempty string")
    entry = ACTION_TABLE.get(name)
    if entry is None:
        return _verdict(TIER_QUEUE, ["unclassified_action"], {}, ATTRS, name,
                        isolation_state)
    attributes = dict(entry)
    attributes.update(overrides)
    return evaluate(attributes, action=name, isolation_state=isolation_state)


def gate(action: Any, *, kind: str = "action", mode: str | None = None,
         isolation_state: Mapping[str, Any] | None = None,
         pending_dir: str | os.PathLike | None = None,
         caller: str | None = None,
         run_id: str | None = None, workflow_id: str | None = None,
         step_id: str | None = None, **overrides: Any) -> dict:
    """Classify, record, and (only in enforce mode) raise.

    Returns the verdict in both modes. In ``enforce``: 禁止 raises
    ``ActionForbidden``; 排队 raises ``ActionQueued`` after the pending record
    has been written, so the caller can turn it into a resumable pause.

    ``run_id`` / ``workflow_id`` / ``step_id`` 是给消费者用的**回链**：没有它，收件箱
    里的一条动作只能被人读，不能被任何 run 认领回去（11-B 第 13 项要的正是这条链）。
    不传就不写这三个键，记录形状与不带回链时逐字节一致。
    """
    if kind not in ("action", "command"):
        raise ValueError("kind must be 'action' or 'command'")
    verdict = (classify_action(action, isolation_state=isolation_state, **overrides)
               if kind == "action"
               else classify_command(action, isolation_state=isolation_state))
    record = pending_record(verdict, action=action if isinstance(action, str) else " ".join(action),
                            caller=caller, action_kind=kind, run_id=run_id,
                            workflow_id=workflow_id, step_id=step_id)
    try:
        audit_record("safety_tier", tier=verdict["tier"], action=record["action"],
                     why=list(verdict["why"]), mode=mode or DEFAULT_MODE,
                     isolation=(verdict.get("isolation") or {}).get("backend"))
    except Exception:  # a gate must not fail because its log did
        pass
    active = mode if mode in MODES else globals()["mode"]()
    if active == "enforce":
        if verdict["tier"] == TIER_FORBID:
            raise ActionForbidden(verdict)
        if verdict["tier"] == TIER_QUEUE:
            target = pending_dir if pending_dir is not None else os.environ.get(PENDING_ENV)
            if target:
                append_pending(pending_path(target), record)
            raise ActionQueued(verdict, record)
    return verdict


def pending_record(verdict: Mapping[str, Any], *, action: str,
                   caller: str | None = None, at: float | None = None,
                   action_kind: str = "action",
                   run_id: str | None = None, workflow_id: str | None = None,
                   step_id: str | None = None) -> dict:
    """The ``pending_action`` shape a queued action leaves behind (§11.5).

    ``action_kind``（``action`` / ``command``）是消费者执行前复检要用的：命令走
    ``classify_command``，具名动作走 ``classify_action``，两条路不是一回事。
    """
    stamp = at if at is not None else time.time()
    record = {
        "kind": "pending_action",
        "at": stamp,
        "at_iso": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
        "action_kind": action_kind,
        "action": action,
        "tier": verdict.get("tier"),
        "why": list(verdict.get("why") or []),
        "criteria": dict(verdict.get("criteria") or {}),
        "isolation": verdict.get("isolation"),
        "caller": caller,
        "status": "pending_human",
    }
    # 调用点没给回链时，用绑在当前运行上的（`bind_run`）。显式传参优先：调用方
    # 比 contextvar 更知道这条动作是谁的。
    bound = _RUN_CONTEXT.get() or {}
    for key, value in (("run_id", run_id if run_id is not None else bound.get("run_id")),
                       ("workflow_id", workflow_id if workflow_id is not None else bound.get("workflow_id")),
                       ("step_id", step_id if step_id is not None else bound.get("step_id"))):
        if value is not None:
            record[key] = value
    return record


def pending_path(target: str | os.PathLike, when: float | None = None) -> Path:
    """``<target>/<date>.jsonl`` — one file per day, append-only."""
    root = Path(target)
    if root.suffix == ".jsonl":
        return root
    stamp = datetime.fromtimestamp(when if when is not None else time.time()).strftime("%Y-%m-%d")
    return root / f"{stamp}.jsonl"


def append_pending(path: str | os.PathLike, record: Mapping[str, Any]) -> bool:
    """Append one record. Never raises: a queue that cannot be written is still a
    queue, and losing the record must not turn into losing the refusal."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def get_state() -> dict:
    """Small, detached view for a run report."""
    state = isolation()
    return {"mode": mode(), "loopback_ports": list(loopback_ports()),
            "isolation": state, "known_actions": sorted(ACTION_TABLE),
            "attributes": list(ATTRS)}


def _selftest() -> int:
    failures: list[str] = []
    iso_ok = {"backend": "docker", "filesystem": True, "network": True, "ok": True}
    iso_bad = {"backend": "namespace", "filesystem": False, "network": True, "ok": False}

    def expect(label: str, got: Any, want: Any) -> None:
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    expect("allow:new_file", evaluate(_WRITE_NEW)["tier"], TIER_ALLOW)
    expect("queue:overwrite", evaluate(_OVERWRITE)["tier"], TIER_QUEUE)
    expect("forbid:irreversible_outside", evaluate(dict(_DESTRUCTIVE, outside=True))["tier"], TIER_FORBID)
    expect("forbid:credentials", evaluate(dict(_READ, credentials=True, reversible=False))["tier"], TIER_FORBID)
    expect("queue:unknown_attr", evaluate({"reversible": True})["tier"], TIER_QUEUE)
    expect("queue:exec_without_isolation", evaluate(_EXEC, isolation_state=iso_bad)["tier"], TIER_QUEUE)
    expect("allow:exec_with_isolation", evaluate(_EXEC, isolation_state=iso_ok)["tier"], TIER_ALLOW)
    expect("queue:unclassified_action", classify_action("nope:nothing")["tier"], TIER_QUEUE)
    expect("allow:iverilog_in_sandbox", classify_command(["iverilog", "-o", "a.out", "a.v"], isolation_state=iso_ok)["tier"], TIER_ALLOW)
    expect("queue:iverilog_no_isolation", classify_command(["iverilog", "-o", "a.out", "a.v"], isolation_state=iso_bad)["tier"], TIER_QUEUE)
    expect("allow:loopback_get", classify_command(["curl", "http://127.0.0.1:8101/api/runs"], isolation_state=iso_ok)["tier"], TIER_ALLOW)
    expect("queue:loopback_post", classify_command(["curl", "-X", "POST", "http://127.0.0.1:8101/api/run"], isolation_state=iso_ok)["tier"], TIER_QUEUE)
    expect("queue:external_curl", classify_command(["curl", "https://example.com/x"], isolation_state=iso_ok)["tier"], TIER_QUEUE)
    expect("queue:unregistered_loopback_port", classify_command(["curl", "http://127.0.0.1:9999/x"], isolation_state=iso_ok)["tier"], TIER_QUEUE)
    expect("forbid:ssh_key_read", classify_command(["cat", "/home/x/.ssh/id_rsa"])["tier"], TIER_FORBID)
    expect("queue:unknown_command", classify_command(["mystery-tool", "--go"])["tier"], TIER_QUEUE)
    expect("queue:git_push", classify_command(["git", "push", "origin", "main"])["tier"], TIER_QUEUE)
    expect("allow:git_status", classify_command(["git", "status", "--short"])["tier"], TIER_ALLOW)
    expect("queue:capability_apply_patch", classify_action("capability:apply_patch")["tier"], TIER_QUEUE)
    expect("allow:capability_execute_verilog", classify_action("capability:execute_verilog", isolation_state=iso_ok)["tier"], TIER_ALLOW)

    # enforce mode turns the verdict into an exception; report mode never does.
    try:
        gate("capability:apply_patch", mode="enforce")
        failures.append("enforce:apply_patch did not raise")
    except ActionQueued:
        pass
    try:
        gate("delete:production_data", mode="enforce")
        failures.append("enforce:production_delete did not raise")
    except ActionForbidden:
        pass
    expect("report:never_raises", gate("capability:apply_patch", mode="report")["tier"], TIER_QUEUE)

    if failures:
        for line in failures:
            print(f"FAIL {line}")
        return 1
    print(f"PASS 23 条对照 · 模式={mode()} · 隔离={isolation()['backend']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
