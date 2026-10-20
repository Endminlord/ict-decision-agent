"""幂等双键（11-B 第 12 项）：重复投递与重复劳动都算成功，不记失败。

判据来自 §11.5（`research/195-workbench-chapter.md:154-162`）：

    event_key  = sha256(provider + sourceId + deliveryId)   ← 防重复投递
    intent_key = sha256(需求 + 产物路径)                      ← 防重复劳动
    overlap = "success"  ⇒  skipped(overlap) 视为成功，不是失败

四件事值得先说清：

1. **`skipped` 是成功**。重复投递是**外部世界的行为**，不是任务失败。记成失败会让
   熔断器误判（`runtime/fuse.py` 的 `judge_exit` 只认 completed/failed 那一套）、让账本
   漂移。所以 `SUCCESS_STATUSES` 里必须有它，并由调用方把它变成一个真终态。
2. **只有成功过的键才算「已做过」**。上一次失败不能永久挡住重试 —— 否则一次网络抖动
   就等于把那个需求钉死了。`seen_keys()` 只收 `status ∈ SUCCESS_STATUSES` 的记录。
3. **不知道就不去重**：任一组成部分缺失 ⇒ 那个键是 `None` ⇒「我不能声称这是重复」。
   宁可多跑一次，不可把没做过的事说成做过了。
4. **需求不大小写归一**。`写一个函数` 与 `写一个函数。` 不是一回事，`Write a function`
   与 `write a function` 也可能是有意区分的两次。归一只做空白折叠（首尾 + 连续空白）。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

EVENT_KEY_SPEC = "sha256(provider+sourceId+deliveryId)"
INTENT_KEY_SPEC = "sha256(需求+产物路径)"

OVERLAP_POLICY = "success"
POLICIES = ("success", "report")
SUCCESS_STATUSES = ("completed", "skipped")

KEY_NAMES = ("event_key", "intent_key")
_KNOWN_ARGS = ("provider", "source_id", "delivery_id", "artifact_paths", "seen", "policy")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(value: Any) -> str:
    """None / 非字符串都当「没给」。数字和布尔也当没给 —— 凭据 id 是字符串。"""
    if value is None or isinstance(value, (bool, int, float)):
        return ""
    return str(value).strip()


def normalize_requirement(text: Any) -> str:
    """首尾空白去掉、内部连续空白折叠成一个空格。**不**改大小写。"""
    return " ".join(_clean(text).split())


def normalize_paths(paths: Any) -> tuple[str, ...]:
    """产物路径：接受单个字符串、可迭代、None。去空、去重、排序（顺序不是身份）。"""
    if paths is None:
        return ()
    if isinstance(paths, (str, bytes)):
        candidates: Iterable[Any] = (paths,)
    else:
        try:
            candidates = tuple(paths)
        except TypeError:
            return ()
    out = {_clean(item) for item in candidates}
    out.discard("")
    return tuple(sorted(out))


def event_key(provider: Any = None, source_id: Any = None, delivery_id: Any = None) -> "str | None":
    """防重复投递。三个组成部分缺一个 ⇒ None（不猜、不合并）。"""
    parts = [_clean(provider), _clean(source_id), _clean(delivery_id)]
    if not all(parts):
        return None
    return _digest("\x1f".join(parts))


def intent_key(requirement: Any = None, artifact_paths: Any = None) -> "str | None":
    """防重复劳动。需求为空 ⇒ None；产物路径为空 ⇒ None（见模块开头第 3 条）。"""
    need = normalize_requirement(requirement)
    paths = normalize_paths(artifact_paths)
    if not need or not paths:
        return None
    return _digest(need + "\x1f" + "\x1f".join(paths))


def keys(*, provider: Any = None, source_id: Any = None, delivery_id: Any = None,
         requirement: Any = None, artifact_paths: Any = None) -> dict:
    return {
        "event_key": event_key(provider, source_id, delivery_id),
        "intent_key": intent_key(requirement, artifact_paths),
    }


def is_success(status: Any) -> bool:
    """`skipped` 与 `completed` 一样是成功。别的地方要判「这单成了没有」就问这里。"""
    return isinstance(status, str) and status in SUCCESS_STATUSES


def seen_keys(records: Any) -> dict:
    """把历史记录折成「已做过的键」索引。

    只收 `status ∈ SUCCESS_STATUSES` 的记录：失败的不算做过（要允许重试），
    还在跑的不算（`running` 不是结论）。坏记录跳过，**never raises** —— 一个能把
    判据本身干掉的索引最后会被关掉，然后就没有判据了。
    """
    index: dict = {name: {} for name in KEY_NAMES}
    considered = 0
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        status = record.get("status")
        if not is_success(status):
            continue
        considered += 1
        for name in KEY_NAMES:
            value = record.get(name)
            if isinstance(value, str) and value:
                index[name].setdefault(value, {
                    "status": status,
                    "at": record.get("at") or record.get("finished_at") or "",
                    "run_id": record.get("run_id") or record.get("task_id") or "",
                })
    return index


def check(requirement: Any = None, *, provider: Any = None, source_id: Any = None,
          delivery_id: Any = None, artifact_paths: Any = None, seen: Any = None,
          policy: Any = OVERLAP_POLICY) -> dict:
    """这一次投递/需求是不是已经做过。

    返回一个判决，**不抛**（除了调用方把参数名写错）：
    `duplicate=True` ⇒ 调用方应当把这次运行记成 `skipped`（成功），不是失败。
    `policy="report"` 时照判不误，但 `duplicate=False`、`match` 照给 ——
    这样判据可以先观察一段时间再上闸。
    """
    seen_index = seen_keys(seen) if isinstance(seen, (list, tuple, set)) else (seen or {})
    if not isinstance(seen_index, Mapping):
        seen_index = {}
    new = keys(provider=provider, source_id=source_id, delivery_id=delivery_id,
               requirement=requirement, artifact_paths=artifact_paths)
    active_policy = policy if policy in POLICIES else OVERLAP_POLICY
    match = None
    prior = None
    for name in KEY_NAMES:  # event_key 先判：防重复投递比防重复劳动更硬
        value = new.get(name)
        bucket = seen_index.get(name) if isinstance(seen_index.get(name), Mapping) else {}
        if value and value in bucket:
            match, prior = name, bucket[value]
            break
    return {
        "duplicate": bool(match) and active_policy == "success",
        "match": match,
        "reason": f"overlap:{match}" if match else "",
        "status": "skipped" if (match and active_policy == "success") else None,
        "counts_success": True,          # 见模块开头第 1 条：skipped 是成功
        "policy": active_policy,
        "keys": new,
        "prior": prior,
        "considered_records": sum(len(seen_index.get(n) or {}) for n in KEY_NAMES),
    }


def _selftest() -> int:
    checks = 0

    def expect(name, got, want):
        nonlocal checks
        checks += 1
        if got != want:
            raise AssertionError(f"{name}: 期望 {want!r}，实得 {got!r}")

    k1 = event_key("webhook", "src-1", "d-1")
    expect("event_key 稳定", event_key("webhook", "src-1", "d-1"), k1)
    expect("event_key 与顺序无关", event_key("webhook", "src-1", "d-1") == k1, True)
    expect("event_key 缺 deliveryId ⇒ None", event_key("webhook", "src-1", None), None)
    expect("event_key 空串 ⇒ None", event_key("webhook", "src-1", "  "), None)

    i1 = intent_key("写一个 8 位加法器", ["hw/adder.v"])
    expect("intent_key 空白折叠", intent_key("  写一个   8 位加法器  ", ["hw/adder.v"]), i1)
    expect("intent_key 路径顺序无关", intent_key("写一个 8 位加法器", ["b.v", "a.v"]) ==
           intent_key("写一个 8 位加法器", ["a.v", "b.v"]), True)
    expect("intent_key 需求为空 ⇒ None", intent_key("   ", ["a.v"]), None)
    expect("intent_key 路径为空 ⇒ None", intent_key("写一个加法器", []), None)
    expect("intent_key 需求不同 ⇒ 不同", intent_key("写一个加法器", ["a.v"]) !=
           intent_key("写一个减法器", ["a.v"]), True)
    expect("intent_key 产物不同 ⇒ 不同", intent_key("写一个加法器", ["a.v"]) !=
           intent_key("写一个加法器", ["b.v"]), True)

    rec_ok = {"status": "completed", "event_key": k1, "intent_key": i1, "run_id": "r1"}
    rec_failed = {"status": "failed", "event_key": event_key("webhook", "src-1", "d-2"),
                  "intent_key": i1, "run_id": "r2"}
    rec_skipped = {"status": "skipped", "event_key": event_key("webhook", "src-1", "d-3"),
                   "intent_key": i1, "run_id": "r3"}
    rec_running = {"status": "running", "event_key": event_key("webhook", "src-1", "d-4")}
    index = seen_keys([rec_ok, rec_failed, rec_skipped, rec_running, "junk", None])
    expect("已做过的 event_key 只收成功", sorted(index["event_key"]),
           sorted([k1, rec_skipped["event_key"]]))
    expect("失败不算做过（intent 只剩成功那两条）", i1 in index["intent_key"], True)
    expect("running 不算做过", rec_running["event_key"] in index["event_key"], False)

    v = check("写一个 8 位加法器", provider="webhook", source_id="src-1", delivery_id="d-1",
              artifact_paths=["hw/adder.v"], seen=index)
    expect("重复投递 ⇒ skipped", (v["duplicate"], v["match"], v["status"]),
           (True, "event_key", "skipped"))
    expect("重复投递的理由", v["reason"], "overlap:event_key")

    v2 = check("写一个 8 位加法器", provider="webhook", source_id="src-1", delivery_id="d-9",
               artifact_paths=["hw/adder.v"], seen=index)
    expect("同需求同产物 ⇒ 重复劳动", (v2["duplicate"], v2["match"]), (True, "intent_key"))

    v3 = check("写一个 8 位加法器", provider="webhook", source_id="src-1", delivery_id="d-9",
               artifact_paths=["hw/sub.v"], seen=index)
    expect("同需求不同产物 ⇒ 不重复", (v3["duplicate"], v3["match"]), (False, None))

    v4 = check("写一个 8 位加法器", provider="webhook", source_id="src-1", delivery_id="d-9",
               artifact_paths=["hw/adder.v"], seen=index, policy="report")
    expect("report 模式照判不拦", (v4["duplicate"], v4["match"]), (False, "intent_key"))

    v5 = check("写一个 8 位加法器", provider="webhook", source_id="src-1", delivery_id="d-9",
               artifact_paths=["hw/adder.v"], seen=[rec_failed])
    expect("只有失败过 ⇒ 允许重试", (v5["duplicate"], v5["match"]), (False, None))

    v6 = check("写一个 8 位加法器", artifact_paths=["hw/adder.v"], seen=index)
    expect("没有投递标识 ⇒ 只看需求+产物", (v6["duplicate"], v6["match"]), (True, "intent_key"))
    expect("坏 seen 不抛", check("x", artifact_paths=["a"], seen="nonsense")["duplicate"], False)

    try:
        check("x", artifact_paths=["a"], provider_name="typo")
    except TypeError as exc:
        expect("参数名写错要当场报", "provider_name" in str(exc), True)
    else:
        raise AssertionError("参数名写错没有报")

    expect("skipped 是成功", is_success("skipped"), True)
    expect("failed 不是成功", is_success("failed"), False)
    print(f"PASS {checks} 条对照 · 策略={OVERLAP_POLICY} · skipped 计入成功={is_success('skipped')}")
    return checks


if __name__ == "__main__":
    _selftest()
