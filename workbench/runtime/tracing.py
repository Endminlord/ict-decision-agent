"""Trace summarisation.

``run_chain`` returns a flat ``stage_events`` list covering every dispatch
attempt. That is the right storage shape (nothing is lost, order is preserved)
but the wrong reading shape: after two dispatches the flat list mixes three
attempts together and the failure that caused each retry is easy to miss.

This module groups the flat list by attempt so a reader can see, per pass, what
ran, what failed and why.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def summarize(events: Iterable[Any]) -> dict:
    """Group a flat event list by dispatch attempt.

    Malformed entries are counted rather than raising: a trace is diagnostic
    output, and refusing to summarise it because one entry is odd would hide the
    rest of the evidence.
    """
    grouped: dict[int, list[dict]] = {}
    malformed = 0
    trace_ids: set[str] = set()

    for event in events or ():
        if not isinstance(event, Mapping):
            malformed += 1
            continue
        try:
            attempt = int(event.get("attempt", 0))
        except (TypeError, ValueError):
            attempt = 0
        trace_id = event.get("trace_id")
        if isinstance(trace_id, str):
            trace_ids.add(trace_id)
        grouped.setdefault(attempt, []).append(dict(event))

    attempts = []
    for attempt in sorted(grouped):
        entries = grouped[attempt]
        errors = [
            {
                "span_id": e.get("span_id"),
                "agent": e.get("agent"),
                "reason": e.get("reason"),
            }
            for e in entries
            if e.get("status") == "error"
        ]
        elapsed = [e.get("elapsed_ms") for e in entries if isinstance(e.get("elapsed_ms"), (int, float))]
        attempts.append({
            "attempt": attempt,
            "events": len(entries),
            "agents": [e.get("agent") for e in entries],
            "errors": errors,
            "error_count": len(errors),
            "elapsed_ms": round(sum(elapsed), 3) if elapsed else 0.0,
        })

    return {
        "trace_id": next(iter(trace_ids)) if len(trace_ids) == 1 else None,
        "trace_ids": sorted(trace_ids),
        "attempt_count": len(attempts),
        "event_count": sum(a["events"] for a in attempts),
        "error_count": sum(a["error_count"] for a in attempts),
        "total_elapsed_ms": round(sum(a["elapsed_ms"] for a in attempts), 3),
        "attempts": attempts,
        "malformed_events": malformed,
    }


def first_failure(summary: Mapping[str, Any]) -> dict | None:
    """Return the earliest recorded failure, or None when the trace is clean."""
    for attempt in summary.get("attempts") or ():
        for error in attempt.get("errors") or ():
            return {"attempt": attempt.get("attempt"), **error}
    return None

def agent_times(events: Iterable[Any], *, limit: int | None = None) -> dict:
    """每个 Agent 花了多少时间——按总耗时从多到少排。

    `summarize()` 回答的是「这一遍里出了什么事」，回答不了「谁最慢」：一场运行
    十几分钟里，有的 Agent 花 95 秒、有的花 0.4 毫秒（确定性规则，压根不问模型），
    摊在事件流里看不出来。

    三条纪律：
    - **只统计真花过时间的那些**。没有 `elapsed_ms` 的事件（比如「阶段开始了」）
      算进 `skipped`，不算进 `0`——「没记时间」和「花了 0 毫秒」不是一件事。
    - **失败的那些也要在**。跑得慢不一定是坏事，报错的那个 Agent 常常是耗时最多
      的那个；把它从榜上拿掉，榜就成了「顺利的 Agent 排行榜」。
    - **`calls` 是次数不是遍数**。重试过的运行里同一个 Agent 会出现多次，平均
      耗时分母是次数。
    """
    totals: dict[str, dict] = {}
    spans = 0
    skipped = 0
    for event in events or ():
        if not isinstance(event, Mapping):
            skipped += 1
            continue
        agent = event.get("agent")
        elapsed = event.get("elapsed_ms")
        if not isinstance(agent, str) or not agent:
            skipped += 1
            continue
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
            skipped += 1
            continue
        spans += 1
        row = totals.setdefault(
            agent, {"agent": agent, "calls": 0, "total_ms": 0.0, "max_ms": 0.0,
                    "min_ms": float(elapsed), "errors": 0},
        )
        row["calls"] += 1
        row["total_ms"] += float(elapsed)
        row["max_ms"] = max(row["max_ms"], float(elapsed))
        row["min_ms"] = min(row["min_ms"], float(elapsed))
        if event.get("status") == "error":
            row["errors"] += 1

    rows = []
    for row in totals.values():
        row["total_ms"] = round(row["total_ms"], 3)
        row["max_ms"] = round(row["max_ms"], 3)
        row["min_ms"] = round(row["min_ms"], 3)
        row["avg_ms"] = round(row["total_ms"] / row["calls"], 3)
        rows.append(row)
    # 同样多的时候按名字排，免得两次读数顺序不一样——一个会自己变顺序的榜
    # 没法用来对比。
    rows.sort(key=lambda r: (-r["total_ms"], r["agent"]))
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return {
        "agents": rows,
        "spans": spans,
        "skipped": skipped,
        "total_ms": round(sum(r["total_ms"] for r in rows), 3),
    }
