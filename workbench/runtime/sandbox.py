"""Sandbox capability detection and command wrapping.

The harness cannot fabricate isolation. What it can do is detect what the host
actually provides, use it, and **report the level truthfully** — a run that
claims to be isolated when it is not is worse than one that admits it is not.

Backends, strongest first:

* ``docker`` — real filesystem isolation. The child sees only its working
  directory; the host home directory is unreachable. Measured at ~36x the cost
  of a bare subprocess (327 ms vs 9 ms for a trivial program), so it is
  **opt-in** via ``SANDBOX_BACKEND=docker`` rather than the default. Needs an
  image with a Python interpreter (``SANDBOX_IMAGE``).
* ``namespace`` — ``unshare -Un`` (user + network namespace). Blocks outbound
  sockets; does **not** confine the filesystem. Cheap (~1 ms), so it is the
  default when available.
* ``bwrap`` / ``firejail`` — used automatically if present. Not installed here.
* ``limits`` — ``RLIMIT_CPU`` / ``AS`` / ``FSIZE`` / ``CORE``. Resource bounding,
  not isolation. Always applied on POSIX.

Detection spawns a process, so the result is cached for the life of the process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Sequence

LIMIT_CPU_SECONDS = 30
LIMIT_ADDRESS_SPACE_BYTES = 1024 ** 3
LIMIT_FILE_SIZE_BYTES = 2 * 1024 * 1024

DOCKER_MEMORY = "1g"
DOCKER_CPUS = "1"
DOCKER_PIDS = "128"
DOCKER_TMPFS = "/tmp:rw,size=64m"

BACKENDS = ("auto", "docker", "namespace", "none")
DEFAULT_IMAGE_CANDIDATES = ("agent-runtime:latest", "python:3.11-slim", "python:3-slim")

_cache: dict | None = None


def resource_limits() -> None:
    """``preexec_fn`` body: bound CPU, memory, file size and core dumps.

    POSIX only. These are limits, not isolation: they stop a runaway child from
    consuming the machine, they do not stop it from reading your files.
    """
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (LIMIT_CPU_SECONDS, LIMIT_CPU_SECONDS))
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (LIMIT_FILE_SIZE_BYTES, LIMIT_FILE_SIZE_BYTES)
    )
    resource.setrlimit(
        resource.RLIMIT_AS, (LIMIT_ADDRESS_SPACE_BYTES, LIMIT_ADDRESS_SPACE_BYTES)
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


# --- probing ---------------------------------------------------------------

def _probe_network() -> tuple[bool, str]:
    """Can an unprivileged user+network namespace be created?"""
    if os.name != "posix" or not shutil.which("unshare"):
        return False, "unshare_not_available"
    try:
        completed = subprocess.run(
            ["unshare", "-Un", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"probe_failed:{type(exc).__name__}"
    if completed.returncode == 0:
        return True, "unshare_user_net_namespace"
    detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
    return False, f"unshare_refused:{detail[:80]}" if detail else "unshare_refused"


def _probe_docker() -> tuple[bool, str | None, str]:
    """Is docker usable, and does an image with Python exist?"""
    if not shutil.which("docker"):
        return False, None, "docker_not_installed"
    requested = os.environ.get("SANDBOX_IMAGE")
    candidates = [requested] if requested else list(DEFAULT_IMAGE_CANDIDATES)
    try:
        for image in candidates:
            completed = subprocess.run(
                ["docker", "image", "inspect", image],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if completed.returncode == 0:
                return True, image, f"image_available:{image}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, f"probe_failed:{type(exc).__name__}"
    return False, None, "no_python_image_present"


def _probe_filesystem_tool() -> tuple[str | None, str]:
    for tool in ("bwrap", "firejail"):
        if shutil.which(tool):
            return tool, f"{tool}_available"
    return None, "no_filesystem_sandbox"


def _resolve_backend(caps: dict, requested: str | None = None) -> str:
    """Pick the backend, honouring an explicit request over the cheap default."""
    choice = (requested or os.environ.get("SANDBOX_BACKEND") or "auto").strip().lower()
    if choice not in BACKENDS:
        choice = "auto"
    if choice == "none":
        return "none"
    if choice == "docker":
        return "docker" if caps.get("docker") else "none"
    if choice == "namespace":
        return "namespace" if caps.get("network") else "none"
    # auto: prefer a real filesystem sandbox if the host ships one, else the
    # cheap namespace path. Docker is deliberately excluded from auto because
    # of its cost; it stays available and is reported.
    if caps.get("bwrap"):
        return "bwrap"
    if caps.get("firejail"):
        return "firejail"
    return "namespace" if caps.get("network") else "none"


def detect(refresh: bool = False) -> dict:
    """Probe (once) and describe what this host can actually enforce."""
    global _cache
    if _cache is not None and not refresh:
        return dict(_cache)

    network, network_detail = _probe_network()
    docker, image, docker_detail = _probe_docker()
    filesystem_tool, filesystem_detail = _probe_filesystem_tool()
    limits = os.name == "posix"

    capabilities = {
        "limits": limits,
        "network": network,
        "docker": docker,
        "image": image,
        "bwrap": filesystem_tool == "bwrap",
        "firejail": filesystem_tool == "firejail",
        "detail": {
            "network": network_detail,
            "docker": docker_detail,
            "filesystem": filesystem_detail,
            "limits": "rlimit_via_preexec_fn" if limits else "not_posix",
        },
    }
    capabilities["backend"] = _resolve_backend(capabilities)
    capabilities["filesystem_isolated"] = capabilities["backend"] in (
        "docker", "bwrap", "firejail",
    )
    capabilities["isolated"] = capabilities["filesystem_isolated"]
    _cache = capabilities
    return dict(capabilities)


def describe(capabilities: dict | None = None) -> str:
    caps = capabilities or detect()
    backend = caps.get("backend", "none")
    parts = [
        f"backend={backend}",
        f"filesystem={'isolated' if caps.get('filesystem_isolated') else 'NOT isolated'}",
        f"network={'blocked' if backend != 'none' or caps.get('network') else 'NOT blocked'}",
        f"limits={'applied' if caps.get('limits') else 'NOT applied'}",
    ]
    if caps.get("docker") and backend != "docker":
        parts.append("(docker filesystem isolation available: SANDBOX_BACKEND=docker)")
    return ", ".join(parts)


# --- wrapping --------------------------------------------------------------

def _python_entrypoint(argv: list[str]) -> list[str]:
    """Map a host interpreter path to one resolvable inside the image.

    The host's ``sys.executable`` is ``/usr/bin/python3``; the image ships only
    ``/usr/local/bin/python3``. Passing the host path through would fail with
    "no such file" inside a perfectly good container.
    """
    if argv and os.path.basename(argv[0]).startswith("python"):
        return ["python3"] + argv[1:]
    return argv


def _docker_argv(command: list[str], workdir: str, image: str) -> list[str]:
    argv = _python_entrypoint(list(command))
    wrapped = [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", DOCKER_TMPFS,
        "-v", f"{workdir}:{workdir}:rw",
        "-w", workdir,
        "--memory", DOCKER_MEMORY,
        "--cpus", DOCKER_CPUS,
        "--pids-limit", DOCKER_PIDS,
    ]
    if hasattr(os, "getuid"):
        wrapped += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return wrapped + [image] + argv


def wrap_command(
    command: Sequence[str],
    *,
    workdir: Any = None,
    capabilities: dict | None = None,
    isolate_network: bool | None = None,
) -> tuple[list[str], dict]:
    """Prefix ``command`` with whatever isolation the host can actually apply.

    Returns ``(argv, applied)``. ``applied`` is what was really put in place, so
    callers can report it instead of assuming.
    """
    if isinstance(command, (str, bytes)):
        raise TypeError("command must be a sequence of arguments, not a string")
    argv = [str(part) for part in command]
    if not argv:
        raise ValueError("command must not be empty")

    caps = capabilities or detect()
    backend = caps.get("backend", "none")
    applied: dict[str, Any] = {
        "backend": backend,
        "network_isolated": False,
        "filesystem_isolated": False,
        "limits_applied": bool(caps.get("limits")),
    }

    if backend == "docker" and workdir is not None:
        applied["filesystem_isolated"] = True
        applied["network_isolated"] = True
        return _docker_argv(argv, str(workdir), caps["image"]), applied
    if backend == "bwrap":
        applied["filesystem_isolated"] = True
        applied["network_isolated"] = True
        return ["bwrap", "--unshare-all", "--die-with-parent", "--"] + argv, applied
    if backend == "firejail":
        applied["filesystem_isolated"] = True
        applied["network_isolated"] = True
        return ["firejail", "--quiet", "--net=none", "--"] + argv, applied

    if isolate_network is None:
        isolate_network = os.environ.get("SANDBOX_NETWORK", "on").lower() != "off"
    if isolate_network and caps.get("network"):
        applied["network_isolated"] = True
        return ["unshare", "-Un"] + argv, applied
    return argv, applied


def preexec_for(capabilities: dict | None = None):
    """The ``preexec_fn`` to pass to ``subprocess.Popen``, or ``None``.

    Not applied under docker: the container enforces its own limits, and a
    ``preexec_fn`` in the host process would bound the docker *client*.
    """
    caps = capabilities or detect()
    if caps.get("backend") == "docker":
        return None
    if not caps.get("limits") or os.name != "posix":
        return None
    return resource_limits
