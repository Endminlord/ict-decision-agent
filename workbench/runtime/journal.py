"""Run journal: append-only checkpoints that can actually be resumed from.

11-B 第 6 项（`research/197-workbench-effort.md` 逐项表第 6 行）。同型先例是
`runtime/audit.py`（append-only + `**fields` + 落盘），但 journal 多一件 audit 不需要的
事：**读回来要能把 `Chain.run(resume=…)` 的入参重新拼出来** —— 也就是复原
`chain.py:191 run()` / `:298 _run_stage()` 那个 context 的形状。所以：

* **每个阶段边界一条 `stage_end`**，里面记这一站之后**全部**产物的内容指纹（`sha256` +
  字节数），产物本体按内容寻址写进 `<run>.blobs/<sha256>.json`（同一个产物在后续每一站
  都会重复出现 ⇒ 内容寻址天然去重，不是优化，是**不这么写就会平方级膨胀**）。
* **`resume_payload()` 只在能完整复原时才返回 `ok=True`**：产物 blob 缺一个、或者日志
  中间有解不开的行，都**拒绝**续跑，不拿「少了几件产物」的上下文去接。
  「未知」不许当「通过」——这是 2026-10-07 那轮复核里反复出现的同一类错。
* **记账永远不抛**（`audit.py` 的原话：一个能把运行干掉的审计日志最后会被关掉，
  然后就没有审计日志了）。写失败记进 `write_error` 和 `field_*`，不打断链。

**不声称**：不是防篡改的（它记录发生过什么，不防事后改文件）；不是 WAL（没有 fsync
保证，崩溃可能丢掉最后一条半行 —— 那种情况 `read()` 会**报出来**并容忍，见下）。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["Journal", "open_journal", "read", "resume_payload", "artifact_blob"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact_blob(value: Any) -> tuple[str, int]:
    """产物的内容指纹与字节数。规范化 JSON（`sort_keys`）⇒ 键序不影响指纹。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


class Journal:
    """Append-only JSONL checkpoints. In memory always; on disk when a path is given."""

    def __init__(self, path: Any = None, *, blob_dir: Any = None) -> None:
        self.path = str(path) if path else None
        self.blob_dir = str(blob_dir) if blob_dir else (
            os.path.splitext(self.path)[0] + ".blobs" if self.path else None
        )
        self.entries: list[dict] = []
        self.write_error: str | None = None
        self._seq = 0
        self._run_id: str | None = None
        self._dirs_ready = False

    def _ensure_dirs(self) -> None:
        """目录按需建。

        刻意**不在 `__init__` 里建**：一个只读的检查器（比如
        `tools/validate_workflows.py` 校验 `checkpoint.journal` 的路径模板）会调
        `open_journal()` 拿路径，而校验不该在盘上留下目录。「absent by default」
        也包括「不写就别动文件系统」。
        """
        if self._dirs_ready or not self.path:
            return
        self._dirs_ready = True
        for target in (os.path.dirname(os.path.abspath(self.path)), self.blob_dir):
            if not target:
                continue
            try:
                os.makedirs(target, exist_ok=True)
            except OSError as exc:
                self.write_error = f"{type(exc).__name__}: {exc}"

    # --- 写 ---------------------------------------------------------------
    def record(self, kind: str, **fields: Any) -> dict:
        """Append one entry. Returns it; never raises."""
        if not isinstance(kind, str) or not kind.strip():
            kind = "unnamed_event"
        self._seq += 1
        entry = {"seq": self._seq, "at": _now(), "kind": kind}
        for key, value in fields.items():
            # 信封键被命名空间化，一条记录不能改写自己的序号/时刻/类型。
            entry[f"field_{key}" if key in ("seq", "at", "kind") else key] = value
        self.entries.append(entry)
        if self.path:
            self._ensure_dirs()
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            except Exception as exc:  # 记账不许把被记的活干掉
                self.write_error = f"{type(exc).__name__}: {exc}"
                entry["write_error"] = self.write_error
        return dict(entry)

    def _store_blobs(self, artifacts: dict) -> dict:
        """产物本体按内容寻址落盘，返回 `{name: {sha256, bytes, keys}}` 摘要。"""
        summary: dict[str, dict] = {}
        for name, value in artifacts.items():
            digest, size = artifact_blob(value)
            summary[str(name)] = {"sha256": digest, "bytes": size,
                                  "keys": sorted(value) if isinstance(value, dict) else []}
            if not self.blob_dir:
                continue
            self._ensure_dirs()
            target = os.path.join(self.blob_dir, f"{digest}.json")
            if os.path.exists(target):     # 内容寻址 ⇒ 已经写过就是同一份
                continue
            try:
                tmp = f"{target}.tmp{os.getpid()}"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, sort_keys=True, default=str)
                os.replace(tmp, target)
            except Exception as exc:
                self.write_error = f"{type(exc).__name__}: {exc}"
        return summary

    def run_start(self, *, run_id: str, user_input: Any, timestamp: Any, mode: str,
                  stages: Any, resume_start: Any = None, trace: Any = None) -> dict:
        self._run_id = run_id
        text = user_input if isinstance(user_input, str) else json.dumps(user_input, default=str)
        return self.record(
            "run_start", run_id=run_id, timestamp=timestamp, mode=mode,
            stages=list(stages or ()), resume_start=resume_start, trace=dict(trace or {}),
            user_input_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            user_input_chars=len(text),
        )

    def stage_end(self, stage: str, context: dict, *, status: str | None = None) -> dict:
        artifacts = context.get("artifacts") or {}
        return self.record(
            "stage_end", run_id=self._run_id, stage=stage, status=status or context.get("status"),
            completed_stages=list(context.get("completed_stages") or []),
            artifacts=self._store_blobs(dict(artifacts)),
            gates=dict(context.get("gates") or {}),
            degraded=list(context.get("degraded") or []),
            errors=list(context.get("errors") or []),
            verification_level=context.get("verification_level"),
            rollback_signal=context.get("rollback_signal"),
        )

    def run_end(self, result: dict, context: dict) -> dict:
        return self.record(
            "run_end", run_id=self._run_id, status=result.get("status"),
            reason=result.get("reason"), completed_stages=list(result.get("completed_stages") or []),
            verification_level=result.get("verification_level"),
            elapsed_ms=result.get("elapsed_ms"), write_error=self.write_error,
        )


def open_journal(state_dir: Any, run_id: str) -> Journal:
    """`<state_dir>/journals/<run_id>.jsonl` —— 与 `workflows/ic_design.json` 的
    `checkpoint.journal = "state/journals/<run_id>.jsonl"` 同一个约定。"""
    base = Path(state_dir) / "journals"
    return Journal(base / f"{run_id}.jsonl")


# --- 读 ---------------------------------------------------------------
def read(path: Any) -> tuple[list[dict], dict]:
    """读回一份 journal。返回 `(entries, report)`，**坏行不静默丢**。

    `report` 分两类，因为它们的含义完全不同：
      * `torn_tail`：**最后**一行解不开（多半是崩溃时写了一半）⇒ 容忍，但报出来；
      * `problems`：**中间**有解不开的行 ⇒ 这是**日志被改过或坏了**，续跑必须拒绝。
    """
    entries: list[dict] = []
    problems: list[str] = []
    torn_tail: str | None = None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return [], {"problems": [f"读不到 {path}: {type(exc).__name__}: {exc}"],
                    "torn_tail": None, "lines": 0}
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            if number == len(lines):
                torn_tail = f"第 {number} 行（最后一行）解不开：{exc}"
            else:
                problems.append(f"第 {number} 行解不开：{exc}")
            continue
        if not isinstance(entry, dict):
            problems.append(f"第 {number} 行不是对象")
            continue
        entries.append(entry)
    return entries, {"problems": problems, "torn_tail": torn_tail, "lines": len(lines)}


def resume_payload(path: Any, *, state_dir: Any = None) -> dict:
    """把一份 journal 变成 `Chain.run(resume=…)` 的入参。

    只在**能完整复原**时 `ok=True`：日志中间坏行、没有已完成的阶段、任一产物 blob
    缺失 —— 三条里任何一条都拒绝，并说清是哪一条。
    """
    from runtime.chain import AGENT_ORDER, MAIN_CHAIN, STAGE_AGENTS  # 延迟导入：避免环

    entries, report = read(path)
    if report["problems"]:
        return {"ok": False, "reason": "journal 中间有解不开的行，不拿它续跑",
                "problems": report["problems"], "torn_tail": report["torn_tail"], "resume": None}
    stage_ends = [e for e in entries if e.get("kind") == "stage_end" and e.get("stage")]
    if not stage_ends:
        return {"ok": False, "reason": "没有已完成的阶段", "problems": [],
                "torn_tail": report["torn_tail"], "resume": None}
    done: list[str] = []
    for entry in stage_ends:
        if entry["stage"] not in done:
            done.append(entry["stage"])
    last = stage_ends[-1]
    rest = [s for s in MAIN_CHAIN if s not in done]
    if not rest:
        return {"ok": False, "reason": "所有站都已完成，没有可续的站",
                "problems": [], "torn_tail": report["torn_tail"], "resume": None}
    stage = rest[0]
    agents = tuple(STAGE_AGENTS.get(stage, ()))
    if not agents:
        return {"ok": False, "reason": f"下一站 {stage} 没有可执行的 Agent",
                "problems": [], "torn_tail": report["torn_tail"], "resume": None}
    blob_dir = Path(state_dir) / "journals" / (Path(path).stem + ".blobs") if state_dir else \
        Path(path).parent / (Path(path).stem + ".blobs")
    artifacts: dict[str, Any] = {}
    missing: list[str] = []
    for name, meta in (last.get("artifacts") or {}).items():
        digest = (meta or {}).get("sha256")
        blob = blob_dir / f"{digest}.json"
        try:
            artifacts[name] = json.loads(blob.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(f"{name}（{digest}）")
    if missing:
        return {"ok": False, "reason": "产物 blob 缺失，续跑会把上下文接成残的",
                "missing": missing, "problems": [], "torn_tail": report["torn_tail"],
                "resume": None}
    start_at = agents[0]
    return {
        "ok": True,
        "resume": {"start_at": start_at, "artifacts": artifacts,
                   "gates": dict(last.get("gates") or {})},
        "completed_stages": list(done),
        "next_stage": stage,
        "start_at": start_at,
        "agent_order_index": AGENT_ORDER.index(start_at) if start_at in AGENT_ORDER else None,
        "run_id": last.get("run_id"),
        "problems": [],
        "torn_tail": report["torn_tail"],
    }
