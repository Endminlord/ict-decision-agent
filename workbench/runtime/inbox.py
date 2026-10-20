"""收件箱消费者：把「排队等醒」的动作变成可处置、可执行、有回执的队列。

判据（`FINAL-方案.md` §11.5，逐字）：

    链内：只做「允许」档动作 → 遇「排队」档 ⇒ 产出 pending_action 并在此收束
          ↓ 落 state/inbox/<date>.jsonl
    链外（独立 run）：另一个 run 读收件箱 → 执行 → 回执
    UI：genui 持久面板 = 审批收件箱，用户醒来一次批

这个模块是那条「链外」的路。生产者**已经存在**：`runtime/safety_tiers.gate()` 在
``enforce`` 模式下把 `pending_action` 追加进 `SAFETY_PENDING_DIR`（`runtime/execution.py:42`
与 `runtime/capability_bus.py:42` 是两个收口）。11-A ③ 已经有了只读渲染面
（`paused-inbox.py`，读 `state/runs/*.json`）。缺的是**消费者**：谁决定、谁执行、
执行几次、怎么回执 —— 就是本模块。

三条不变量：

* **只追加**（append-only）。已有行永不改写：决定、认领、回执都是新行。审计轨迹不会
  因为一次崩溃丢掉历史，两个进程同时写也不会互相覆盖。
* **执行至多一次**。执行前必须**认领**（`claim`），回执必须挂在认领上。没有认领的
  回执 = 伪造（拒绝写入）；超过 `CLAIM_TTL_SECONDS` 仍无回执的认领 = `stale`
  （允许另一个人重新认领，而不是永远卡住）。
* **拒绝是终态**。`deny` 之后不再执行；`approve` 之后仍可 `deny`（只要还没执行）。
  未知的决定词**不是**默认批准 —— 它报错、动作保持 `pending_human`。

它故意不做的事：不自己决定要不要批准（那是人的事）；不执行「动作」（`kind=action`）
—— 只有命令（`action_kind=command`）有本机执行器（`runtime.execution.run_trusted`，
它自己还会再过一次三档门），其余执行器由调用方注入（第 13 项那条独立 run 就是这样一个
调用方）。没有执行器时**拒绝并且不写任何东西**，动作留在原地等人。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from runtime import safety_tiers

# 与 runtime/safety_tiers.py:PENDING_ENV 同一个环境变量：生产者与消费者必须看同一个目录。
INBOX_ENV = safety_tiers.PENDING_ENV
DEFAULT_DIR = "state/inbox"

KIND_PENDING = "pending_action"
KIND_DECISION = "decision"
KIND_CLAIM = "claim"
KIND_RECEIPT = "receipt"
KINDS = (KIND_PENDING, KIND_DECISION, KIND_CLAIM, KIND_RECEIPT)

APPROVE = "approve"
DENY = "deny"
DECISIONS = (APPROVE, DENY)

# 认领多久算失效。15 分钟：比一次真正的执行（本地命令上限 30s）长得多，又短到
# 「消费者崩了」不会让动作卡到明天。
CLAIM_TTL_SECONDS = 900.0

# 终态：到了这里就不再改变（除了 deny 之后的 receipt 会被标成不一致）。
STATE_PENDING = "pending_human"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"
STATE_CLAIMED = "claimed"
STATE_STALE = "stale"
STATE_EXECUTED = "executed"
STATE_FAILED = "failed"
STATES = (STATE_PENDING, STATE_APPROVED, STATE_DENIED, STATE_CLAIMED,
          STATE_STALE, STATE_EXECUTED, STATE_FAILED)

# 身份字段：id 只由这些算出来，别的字段（why / criteria / isolation）改了不影响身份。
IDENTITY_FIELDS = ("kind", "at", "action", "action_kind", "tier", "caller",
                   "run_id", "workflow_id", "step_id")


def default_dir(state_dir: str | os.PathLike | None = None) -> Path:
    """环境变量优先，其次 ``<state_dir>/inbox``，最后 ``state/inbox``。

    环境变量优先是刻意的：生产者写哪里由它决定（`safety_tiers.gate`），消费者必须
    跟着它走；`state_dir` 只是本机默认值。
    """
    target = os.environ.get(INBOX_ENV)
    if target and target.strip():
        return Path(target)
    if state_dir is not None:
        return Path(state_dir) / "inbox"
    return Path(DEFAULT_DIR)


def pending_id(record: Mapping[str, Any]) -> str:
    """内容寻址的 id：同一件事被投递两次得到同一个 id，于是能被折叠成一条。

    12 位十六进制（48 bit）。这不是密码学强度，是「同一天里不撞」的强度：真正的
    去重靠身份字段本身相等，id 只是它的短名。
    """
    material = {key: record.get(key) for key in IDENTITY_FIELDS if key in record}
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def _records_path(directory: str | os.PathLike, when: float | None = None) -> Path:
    """复用生产者的命名：``<dir>/<date>.jsonl``（见 safety_tiers.pending_path）。"""
    return safety_tiers.pending_path(directory, when)


def append(directory: str | os.PathLike, record: Mapping[str, Any],
           *, strict: bool = True, now: float | None = None) -> bool:
    """追加一行。``strict`` 时写不进去会抛（决定/认领/回执必须知道有没有落地）。"""
    path = _records_path(directory, now)
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except OSError as exc:
        if strict:
            raise
        del exc
        return False


def iter_records(directory: str | os.PathLike) -> tuple[list[dict], list[str]]:
    """读目录下所有 ``*.jsonl``（按文件名排序）。返回 ``(记录, 问题)``。

    坏行**不吞**：如实记一条问题（含文件与行号），并且**继续读后面的行**。只坏在
    最后一行的（写入被截断）额外标 ``torn_tail`` —— 这两种情况要分得开：中间坏行是
    真损坏，末尾坏行是崩溃的痕迹。
    """
    root = Path(directory)
    records: list[dict] = []
    problems: list[str] = []
    if not root.exists():
        return records, problems
    for path in sorted(root.glob("*.jsonl")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path.name}: unreadable: {type(exc).__name__}: {exc}")
            continue
        lines = raw.splitlines()
        for index, line in enumerate(lines, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except ValueError as exc:
                tail = index == len(lines) and not raw.endswith("\n")
                suffix = " (torn_tail)" if tail else ""
                problems.append(f"{path.name}:{index}: unparsable{suffix}: {exc}")
                continue
            if not isinstance(value, dict):
                problems.append(f"{path.name}:{index}: not an object")
                continue
            records.append(value)
    return records, problems


def fold(records: Sequence[Mapping[str, Any]], *, now: float | None = None,
         ttl: float = CLAIM_TTL_SECONDS) -> tuple[dict[str, dict], list[str]]:
    """把追加流折叠成「每个动作现在处于什么状态」。返回 ``(表, 孤儿)``。

    ``deny`` 胜过 ``approve``（顺序无关）：一条 deny 之后，后来的 approve 只被记成
    问题 ``approve_after_deny``，状态仍是 ``denied``。回执是唯一的终态。

    「孤儿」= 指向不存在的 ``pending_action`` 的决定/认领/回执。它们**不会**造出条目：
    没有生产者的记录不能凭空变成一个可执行的动作。
    """
    stamp = time.time() if now is None else now
    table: dict[str, dict] = {}
    orphans: list[str] = []
    # 两遍：先把所有 pending_action 建出来，再应用决定/认领/回执。
    # 为什么不能一遍过：记录是按**文件名（日期）**排序读的，而决定可能落在另一天的
    # 文件里（跨零点、两个进程时钟不同步、测试注入的 now）。一遍过会让一条完全合法的
    # 决定变成「孤儿」，仅仅因为它的文件排在前面。
    pending_records = [r for r in records if r.get("kind") == KIND_PENDING]
    follow_ups = [r for r in records if r.get("kind") != KIND_PENDING]
    for record in pending_records:
        ident = pending_id(record)
        entry = table.get(ident)
        if entry is None:
            entry = {"id": ident, "record": dict(record), "decision": None,
                     "decisions": [], "claim": None, "claims": [],
                     "receipt": None, "problems": [], "duplicates": 0}
            table[ident] = entry
        else:
            entry["duplicates"] += 1
    for record in follow_ups:
        kind = record.get("kind")
        ident = record.get("id")
        if not isinstance(ident, str) or ident not in table:
            orphans.append(f"{kind}:{ident}")
            continue
        entry = table[ident]
        if kind == KIND_DECISION:
            entry["decisions"].append(dict(record))
            if entry["decision"] != DENY:
                entry["decision"] = record.get("decision")
        elif kind == KIND_CLAIM:
            entry["claims"].append(dict(record))
            entry["claim"] = dict(record)
        elif kind == KIND_RECEIPT:
            if entry["receipt"] is None:
                entry["receipt"] = dict(record)
            else:
                entry["problems"].append("multiple_receipts")

    for entry in table.values():
        _resolve_state(entry, now=stamp, ttl=ttl)
    return table, orphans


def _resolve_state(entry: dict, *, now: float, ttl: float) -> None:
    decisions = entry["decisions"]
    claim = entry["claim"]
    receipt = entry["receipt"]
    first_deny = next((i for i, d in enumerate(decisions)
                       if (d or {}).get("decision") == DENY), None)
    first_approve = next((i for i, d in enumerate(decisions)
                          if (d or {}).get("decision") == APPROVE), None)
    denied = first_deny is not None
    approved = first_approve is not None
    # deny 胜过 approve，与顺序无关：后来的 approve 只被记成问题，不改状态。
    if denied and approved and first_approve > first_deny:
        entry["problems"].append("approve_after_deny")

    fresh = bool(claim) and (now - float(claim.get("at") or 0.0)) <= ttl
    if len({c.get("by") for c in entry["claims"] if isinstance(c, dict)
            and (now - float(c.get("at") or 0.0)) <= ttl}) > 1:
        entry["problems"].append("double_claim")
    if denied and claim and not receipt:
        entry["problems"].append("deny_after_claim")

    if receipt is not None:
        if not claim:
            entry["problems"].append("receipt_without_claim")
        if denied:
            entry["problems"].append("receipt_on_denied")
        entry["state"] = STATE_EXECUTED if receipt.get("ok") else STATE_FAILED
    elif denied:
        entry["state"] = STATE_DENIED
    elif claim and fresh:
        entry["state"] = STATE_CLAIMED
    elif claim:
        entry["state"] = STATE_STALE
    elif approved:
        entry["state"] = STATE_APPROVED
    else:
        entry["state"] = STATE_PENDING
    entry["claim_fresh"] = fresh


def load(directory: str | os.PathLike, *, now: float | None = None,
         ttl: float = CLAIM_TTL_SECONDS) -> tuple[dict[str, dict], list[str]]:
    """读 + 折叠，一次拿全（CLI 与判据都走这里）。"""
    records, problems = iter_records(directory)
    table, orphans = fold(records, now=now, ttl=ttl)
    if orphans:
        problems.append("orphan_records: " + ", ".join(sorted(orphans)))
    return table, problems


def resolve(table: Mapping[str, dict], prefix: str) -> str:
    """把用户给的 id 前缀解析成完整 id。不唯一 ⇒ 报错并列出候选（绝不猜）。"""
    if prefix in table:
        return prefix
    matches = sorted(k for k in table if k.startswith(prefix))
    if not matches:
        raise KeyError(f"没有这个动作：{prefix}")
    if len(matches) > 1:
        raise KeyError(f"前缀不唯一（{len(matches)} 个）：{', '.join(matches)}")
    return matches[0]


def decide(directory: str | os.PathLike, action_id: str, decision: str, *,
           by: str | None = None, note: str | None = None,
           now: float | None = None, ttl: float = CLAIM_TTL_SECONDS) -> dict:
    """记一条决定。未知决定词 ⇒ ``ValueError``（动作保持 pending，不默认批准）。"""
    if decision not in DECISIONS:
        raise ValueError(f"未知决定：{decision!r}（只有 {DECISIONS}）")
    stamp = time.time() if now is None else now
    table, _ = load(directory, now=stamp, ttl=ttl)
    ident = resolve(table, action_id)
    entry = table[ident]
    state = entry["state"]
    if state in (STATE_EXECUTED, STATE_FAILED):
        raise RuntimeError(f"已经执行过了（{state}），不再改决定：{ident}")
    seen = [(d or {}).get("decision") for d in entry["decisions"]]
    if decision in seen:
        return entry          # 幂等：同样的决定不写第二行
    record = {"kind": KIND_DECISION, "id": ident, "at": stamp, "at_iso": _iso(stamp),
              "decision": decision, "by": by, "note": note}
    append(directory, record, now=stamp)
    table, _ = load(directory, now=stamp, ttl=ttl)
    return table[ident]


def claim(directory: str | os.PathLike, action_id: str, *, by: str | None = None,
          now: float | None = None, ttl: float = CLAIM_TTL_SECONDS) -> dict:
    """认领一个**已批准**（或认领已失效）的动作。未批准 ⇒ ``RuntimeError``。

    这是「执行至多一次」的那一步：认领先落盘，执行后落回执。崩在中间的状态是
    ``stale``，别人可以重新认领 —— 卡住不等于永久占用。
    """
    stamp = time.time() if now is None else now
    table, _ = load(directory, now=stamp, ttl=ttl)
    ident = resolve(table, action_id)
    entry = table[ident]
    state = entry["state"]
    if state in (STATE_EXECUTED, STATE_FAILED):
        raise RuntimeError(f"已经执行过了（{state}），不重复认领：{ident}")
    if state in (STATE_PENDING, STATE_DENIED):
        raise RuntimeError(f"还没被批准（{state}），不能认领：{ident}")
    if state == STATE_CLAIMED:
        raise RuntimeError(
            f"已被 {entry['claim'].get('by')} 认领且未失效（{state}）：{ident}")
    record = {"kind": KIND_CLAIM, "id": ident, "at": stamp, "at_iso": _iso(stamp), "by": by}
    append(directory, record, now=stamp)
    table, _ = load(directory, now=stamp, ttl=ttl)
    return table[ident]


def receipt(directory: str | os.PathLike, action_id: str, *, ok: bool,
            detail: str | None = None, artifacts: Sequence[str] | None = None,
            by: str | None = None, now: float | None = None,
            ttl: float = CLAIM_TTL_SECONDS) -> dict:
    """记一条回执。没有认领就拒绝 —— 没有认领的回执是伪造。"""
    stamp = time.time() if now is None else now
    table, _ = load(directory, now=stamp, ttl=ttl)
    ident = resolve(table, action_id)
    entry = table[ident]
    if entry["receipt"] is not None:
        raise RuntimeError(f"已经有回执了，不写第二条：{ident}")
    if not entry["claim"]:
        raise RuntimeError(f"没有认领就没有回执：{ident}")
    record = {"kind": KIND_RECEIPT, "id": ident, "at": stamp, "at_iso": _iso(stamp),
              "ok": bool(ok), "detail": detail, "by": by,
              "artifacts": list(artifacts or [])}
    append(directory, record, now=stamp)
    table, _ = load(directory, now=stamp, ttl=ttl)
    return table[ident]


def execute_once(directory: str | os.PathLike, action_id: str,
                 executor: Callable[[dict], Any], *, by: str | None = None,
                 now: float | None = None, ttl: float = CLAIM_TTL_SECONDS) -> dict:
    """认领 → 执行 → 回执。执行器抛异常 ⇒ 回执 ``ok=False``（不是没有回执）。

    ``executor`` 收到的是那条 ``pending_action`` 记录；返回什么都可以（会进回执的
    ``detail``）。执行前**再过一次三档门**：如果这条动作今天分类成「禁止」，就拒绝
    执行（纵深防御 —— 批准时它是「排队」，不等于永远都该放行）。
    """
    stamp = time.time() if now is None else now
    table, _ = load(directory, now=stamp, ttl=ttl)
    ident = resolve(table, action_id)
    entry = table[ident]
    _preflight(entry["record"])
    entry = claim(directory, ident, by=by, now=stamp, ttl=ttl)
    try:
        outcome = executor(entry["record"])
        ok, detail = True, outcome if isinstance(outcome, str) else json.dumps(
            outcome, ensure_ascii=False, sort_keys=True, default=str)
    except Exception as exc:  # 执行失败也是回执：绝不能变成「没发生」
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return receipt(directory, ident, ok=ok, detail=detail, by=by, now=now, ttl=ttl)


def _preflight(record: Mapping[str, Any]) -> dict:
    """执行前的第二道门。分类成「禁止」⇒ 拒绝（``PermissionError``）。"""
    action = record.get("action")
    if not isinstance(action, str) or not action.strip():
        raise PermissionError("这条记录没有可执行的 action")
    if record.get("action_kind") == "command":
        verdict = safety_tiers.classify_command(action.split())
    else:
        verdict = safety_tiers.classify_action(action)
    if verdict.get("tier") == safety_tiers.TIER_FORBID:
        raise PermissionError(f"执行前复检为禁止档：{verdict.get('why')}")
    return verdict


def summary(directory: str | os.PathLike, *, now: float | None = None,
            ttl: float = CLAIM_TTL_SECONDS) -> dict:
    """给 UI / 判据用的一张小表。``open`` = 还需要人或有东西在飞。"""
    stamp = time.time() if now is None else now
    table, problems = load(directory, now=stamp, ttl=ttl)
    counts = {state: 0 for state in STATES}
    for entry in table.values():
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    open_states = (STATE_PENDING, STATE_APPROVED, STATE_CLAIMED, STATE_STALE)
    open_entries = [e for e in table.values() if e["state"] in open_states]
    oldest = min((float(e["record"].get("at") or 0.0) for e in open_entries), default=None)
    return {
        "dir": str(directory),
        "counts": counts,
        "total": len(table),
        "open": len(open_entries),
        "oldest_open_at": oldest,
        "oldest_open_age_hours": round((stamp - oldest) / 3600, 1) if oldest else None,
        "problems": list(problems) + [f"{e['id']}: {p}" for e in table.values()
                                      for p in e["problems"]],
    }


def describe(entry: Mapping[str, Any]) -> str:
    """一行摘要（列表与判据共用，避免两处各写一份）。"""
    record = entry.get("record") or {}
    action = str(record.get("action") or "")
    if len(action) > 60:
        action = action[:57] + "..."
    bits = [f"{entry.get('id')}", f"[{entry.get('state')}]", f"tier={record.get('tier')}"]
    if record.get("caller"):
        bits.append(f"caller={record['caller']}")
    if record.get("run_id"):
        bits.append(f"run={record['run_id']}")
    bits.append(action)
    return " ".join(bits)


def _selftest() -> int:
    """无副作用的对照跑：证明折叠与三条不变量不是空转。"""
    import tempfile

    base = {"kind": KIND_PENDING, "at": 1000.0, "action": "覆盖 state/x.json",
            "tier": "queue", "caller": "t1"}
    ident = pending_id(base)
    assert len(ident) == 12 and pending_id(dict(base)) == ident
    assert pending_id({**base, "why": ["x"]}) == ident, "身份字段外的变化不该改 id"

    with tempfile.TemporaryDirectory() as tmp:
        append(tmp, base, now=1000.0)
        assert summary(tmp, now=1000.0)["counts"][STATE_PENDING] == 1
        assert summary(tmp, now=1000.0)["total"] == 1
        try:
            claim(tmp, ident, now=1000.0)
            raise AssertionError("未批准就认领成功")
        except RuntimeError:
            pass
        decide(tmp, ident, APPROVE, by="me", now=1001.0)
        assert summary(tmp, now=1001.0)["counts"][STATE_APPROVED] == 1
        claim(tmp, ident, by="w", now=1002.0)
        assert summary(tmp, now=1002.0)["counts"][STATE_CLAIMED] == 1
        try:
            claim(tmp, ident, by="w2", now=1003.0)
            raise AssertionError("重复认领成功")
        except RuntimeError:
            pass
        receipt(tmp, ident, ok=True, detail="done", now=1004.0)
        assert summary(tmp, now=1004.0)["counts"][STATE_EXECUTED] == 1
        try:
            receipt(tmp, ident, ok=True, now=1005.0)
            raise AssertionError("第二条回执写进去了")
        except RuntimeError:
            pass
        # 认领失效：另一个人可以重新认领
        ident2 = pending_id({**base, "at": 2000.0})
        append(tmp, {**base, "at": 2000.0}, now=2000.0)
        decide(tmp, ident2, APPROVE, now=2001.0)
        claim(tmp, ident2, by="w", now=2002.0)
        assert summary(tmp, now=2003.0)["counts"][STATE_CLAIMED] == 1
        assert summary(tmp, now=2002.0 + CLAIM_TTL_SECONDS + 1)["counts"][STATE_STALE] == 1
        claim(tmp, ident2, by="w2", now=2002.0 + CLAIM_TTL_SECONDS + 1)
        # 未知决定词不是批准
        ident3 = pending_id({**base, "at": 3000.0})
        append(tmp, {**base, "at": 3000.0}, now=3000.0)
        try:
            decide(tmp, ident3, "yes", now=3001.0)
            raise AssertionError("未知决定词被接受")
        except ValueError:
            pass
        assert summary(tmp, now=3001.0)["counts"][STATE_PENDING] == 1
        # 拒绝是终态，且 approve 之后仍可 deny
        decide(tmp, ident3, DENY, by="me", now=3002.0)
        try:
            claim(tmp, ident3, now=3003.0)
            raise AssertionError("被拒绝的动作被认领")
        except RuntimeError:
            pass
    print("inbox 自测通过：折叠 / 至多一次 / 拒绝是终态 / 未知决定词不批准")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
