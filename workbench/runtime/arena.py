"""模型竞技场：同一句话，几个候选各答一遍，摆在一起看。

内核不认厂商、不联网、不发调用——``ask`` 由调用方注入（和
``runtime/model_profiler.py`` 里的 ``profile`` 同一条边界）。它只做三件事：把
同一条需求分别问出去、把每次的回答连同「是谁答的、花了多久、有没有答上来」
记成一条可复核的记录、再把这几条记录摆在一起说清「一致在哪、差在哪」。

三条纪律
--------
- **一条路失败不影响其它条**：每问一条都单独兜住异常，失败的原因原样留在记录
  里。少一条记录和「这一条答错了」是两件事。
- **「答了个空」和「没答上来」分得开**：前者是模型答了、内容是空的；后者是调用
  本身就没跑成（超时、鉴权、连不上）。混成一个「失败」，读的人就会把「我不知道」
  读成「它不行」。
- **不排名次、不打分**：这里只说清谁答了什么、谁跟谁一致、差在哪。谁更好得人来
  读；要给机器分，用 ``runtime/model_profiler.py`` 那五道题——那是另一件事，
  出题、判分、留分数，都有据可查。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

#: 一条记录的三种状态。第三种不是「失败」的另一种说法：它没答上来。
ANSWERED = "answered"
EMPTY = "empty"
FAILED = "failed"

#: 命令行预览里每条回答最多留多少字。界面上不截——截在命令行，是因为
#: 终端里几十行正文会把「差在哪」挤出屏幕。
PREVIEW_CHARS = 160

DISCLAIMER = (
    "这里只说清了谁答了什么、谁跟谁一致、差在哪；谁更好得人来读。"
    "要给机器分，用「检测」那五道题（runtime/model_profiler.py）。"
)


@dataclass(frozen=True)
class Contestant:
    """一个候选：哪个出口、哪个模型。这两个字符串本模块都不解释。"""

    provider: str
    model: str

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else self.model


def parse_contestants(items: Iterable[Any], *, default_provider: str = "") -> list[Contestant]:
    """把界面/调用方给的东西读成候选列表。

    认两种写法：``"出口/模型"`` 和 ``{"provider": ..., "model": ...}``。只写了
    模型名时用 ``default_provider`` 补出口；补不上就报错——猜一个出口，等于替
    用户做了一个他没做的选择。
    """
    if isinstance(items, (str, bytes, Mapping)):
        raise TypeError("items must be a list of candidates, not a single one")
    parsed: list[Contestant] = []
    for index, item in enumerate(list(items or []), start=1):
        if isinstance(item, Mapping):
            provider = str(item.get("provider") or default_provider or "").strip()
            model = str(item.get("model") or "").strip()
        elif isinstance(item, str):
            text = item.strip()
            if "/" in text:
                head, _, tail = text.partition("/")
                provider, model = head.strip(), tail.strip()
            else:
                provider, model = str(default_provider or "").strip(), text
        else:
            raise ValueError(f"第 {index} 个候选读不出来：{item!r}")
        if not model:
            raise ValueError(f"第 {index} 个候选没写模型名：{item!r}")
        if not provider:
            raise ValueError(f"候选 {model!r} 没写出口——不猜它在哪")
        parsed.append(Contestant(provider=provider, model=model))
    return parsed


def _text_of(answer: Any) -> str:
    """把一次调用的返回读成文本。

    ``ask`` 可以回字符串，也可以回 ``{"content": ...}``（和 profiler 的 ``call``
    同一套形状）。别的形状一律当「答了个空」——不替它编内容。
    """
    if isinstance(answer, str):
        return answer
    if isinstance(answer, Mapping):
        for key in ("content", "text", "answer"):
            value = answer.get(key)
            if isinstance(value, str):
                return value
    return ""


def _ms(seconds: float) -> int:
    return max(0, int(round(float(seconds) * 1000)))


def _unique(contestants: Sequence[Contestant]) -> tuple[list[Contestant], list[str]]:
    """同一个候选只问一次；被去掉的记下来，别让人以为问了两遍。"""
    seen: set[str] = set()
    unique: list[Contestant] = []
    dropped: list[str] = []
    for contestant in contestants:
        if contestant.name in seen:
            dropped.append(contestant.name)
            continue
        seen.add(contestant.name)
        unique.append(contestant)
    return unique, dropped


def _record(contestant: Contestant, *, status: str, text: str,
            elapsed_ms: int, error: str) -> dict:
    return {
        "name": contestant.name,
        "provider": contestant.provider,
        "model": contestant.model,
        "status": status,
        "text": text,
        "chars": len(text),
        "elapsed_ms": elapsed_ms,
        "error": error,
        "tags": [],
    }


def run_arena(prompt: str, contestants: Iterable[Contestant], *,
              ask: Callable[[Contestant, str], Any],
              clock: Callable[[], float] = time.monotonic) -> dict:
    """同一条需求，逐个候选问一遍，回来一份可复核的并排记录。

    ``ask(contestant, prompt)`` 由调用方注入：本模块既不认识网关也不认识出口，
    超时、鉴权、重试都留在那一侧。它回字符串（或 ``{"content": ...}``）就是答上
    来了，抛异常就是没答上来——两种都会变成记录里的一行。
    """
    question = str(prompt or "").strip()
    if not question:
        raise ValueError("没有要问的话：空的需求问谁都是白问")
    if not callable(ask):
        raise TypeError("ask must be callable")

    unique, dropped = _unique(list(contestants or []))
    if not unique:
        raise ValueError("至少要有一个候选：没有候选就没有对比")

    records = []
    for contestant in unique:
        started = clock()
        try:
            answer = ask(contestant, question)
        except Exception as exc:  # noqa: BLE001 - 调用失败是一种结果，不是崩溃
            record = _record(contestant, status=FAILED, text="",
                             elapsed_ms=_ms(clock() - started),
                             error=f"{type(exc).__name__}: {exc}")
        else:
            text = _text_of(answer)
            record = _record(contestant, status=ANSWERED if text.strip() else EMPTY,
                             text=text, elapsed_ms=_ms(clock() - started), error="")
        records.append(record)

    summary = compare(records)
    for record in records:
        record["tags"] = list(summary["tags"].get(record["name"], []))
    if dropped:
        summary["notes"].append("重复的候选只问了一次：" + "、".join(dropped))

    return {
        "prompt": str(prompt),
        "records": records,
        "summary": summary,
        "disclaimer": DISCLAIMER,
    }


def compare(records: Sequence[Mapping[str, Any]]) -> dict:
    """把几条记录摆在一起：一致在哪、差在哪。

    只报**看得见的东西**：谁答了、答了几字、花了多久、从哪个字开始分叉。不报
    「谁更好」——那是读的人下的判断；机器要下判断，得另外出题判分。
    """
    answered = [r for r in records if r.get("status") == ANSWERED]
    empty = [r for r in records if r.get("status") == EMPTY]
    failed = [r for r in records if r.get("status") == FAILED]

    notes: list[str] = []
    tags: dict[str, list[str]] = {}

    def tag(record: Mapping[str, Any], label: str) -> None:
        tags.setdefault(str(record.get("name") or ""), []).append(label)

    for record in failed:
        tag(record, "没答上来")
        reason = str(record.get("error") or "").strip() or "（没说原因）"
        notes.append(f"{record.get('name')} 没答上来：{reason}")
    for record in empty:
        tag(record, "答了个空")
        # 「0 字」不能写死：空答有两种，一种一个字都没回，一种回了一串空白。后者
        # 得说出来是几个字——不然「答了个空」看着像它什么都没收到。
        chars = int(record.get("chars") or 0)
        what = f"回的 {chars} 个字全是空白" if chars else "一个字都没回"
        notes.append(
            f"{record.get('name')} 答了个空："
            f"{int(record.get('elapsed_ms') or 0)} ms 就回了，{what}"
        )

    texts = [str(r.get("text") or "") for r in answered]
    same_text = len(texts) >= 2 and len(set(texts)) == 1
    normalized = [" ".join(text.split()).lower() for text in texts]
    same_normalized = len(normalized) >= 2 and len(set(normalized)) == 1
    fork_at = _fork(texts)

    length = latency = None
    if len(answered) >= 2:
        longest = max(answered, key=lambda r: int(r.get("chars") or 0))
        shortest = min(answered, key=lambda r: int(r.get("chars") or 0))
        fastest = min(answered, key=lambda r: int(r.get("elapsed_ms") or 0))
        slowest = max(answered, key=lambda r: int(r.get("elapsed_ms") or 0))
        length = {
            "longest": str(longest.get("name") or ""),
            "shortest": str(shortest.get("name") or ""),
            "max": int(longest.get("chars") or 0),
            "min": int(shortest.get("chars") or 0),
        }
        latency = {
            "fastest": str(fastest.get("name") or ""),
            "slowest": str(slowest.get("name") or ""),
            "min_ms": int(fastest.get("elapsed_ms") or 0),
            "max_ms": int(slowest.get("elapsed_ms") or 0),
        }
        if length["max"] != length["min"]:
            notes.append(
                f"字数：{length['longest']} 最长 {length['max']} 字，"
                f"{length['shortest']} 最短 {length['min']} 字"
            )
            tag(longest, "最长")
            tag(shortest, "最短")
        if latency["max_ms"] != latency["min_ms"]:
            notes.append(
                f"耗时：{latency['fastest']} 最快 {latency['min_ms']} ms，"
                f"{latency['slowest']} 最慢 {latency['max_ms']} ms"
            )
            tag(fastest, "最快")
            tag(slowest, "最慢")
        if same_text:
            for record in answered:
                tag(record, "逐字一致")
        elif same_normalized:
            for record in answered:
                tag(record, "只差空白和大小写")
        if fork_at is not None:
            notes.append(
                f"从第 {fork_at + 1} 个字符起开始不一样（前 {fork_at} 个字符是一样的）"
            )

    if len(answered) >= 2:
        status = "comparable"
        if same_text:
            headline = f"{len(answered)} 条都答上来了，逐字一致"
        elif same_normalized:
            headline = f"{len(answered)} 条都答上来了，去掉空白和大小写后一致"
        else:
            headline = f"{len(answered)} 条都答上来了，说法不一样"
    elif len(answered) == 1:
        status = "only_one"
        headline = "只有一条答上来了——这不算对比，先把没答上来的原因看清楚"
        tag(answered[0], "唯一答上来的")
    else:
        status = "none"
        parts = []
        if failed:
            parts.append(f"{len(failed)} 条调用没跑成")
        if empty:
            parts.append(f"{len(empty)} 条答了个空")
        headline = "一条都没答上来" + (f"（{'、'.join(parts)}）" if parts else "")

    return {
        "status": status,
        "headline": headline,
        "answered": len(answered),
        "empty": len(empty),
        "failed": len(failed),
        "same_text": bool(same_text),
        "same_normalized": bool(same_normalized),
        "fork_at": fork_at,
        "length": length,
        "latency": latency,
        "tags": tags,
        "notes": notes,
        "failures": [
            {"name": str(r.get("name") or ""), "error": str(r.get("error") or "")}
            for r in failed
        ],
    }


def _fork(texts: Sequence[str]) -> int | None:
    """所有回答都一模一样就返回 None；否则返回第一个「不是所有回答都相同」的位置。

    只报分叉点，不报谁对：三条里两条一样、一条不一样，分叉点仍然说明「从这儿
    开始就不是同一句话了」。
    """
    if len(texts) < 2:
        return None
    shortest = min(len(text) for text in texts)
    for index in range(shortest):
        if len({text[index] for text in texts}) > 1:
            return index
    if len({len(text) for text in texts}) > 1:
        return shortest
    return None


def format_report(report: Mapping[str, Any]) -> str:
    """给命令行看的并排文本。界面另有自己的排法，用不着这个。"""
    records = list(report.get("records") or [])
    summary = report.get("summary") or {}
    lines = [f"同一条需求，{len(records)} 个候选：", ""]
    for record in records:
        status = record.get("status")
        elapsed = int(record.get("elapsed_ms") or 0)
        if status == ANSWERED:
            head = f"{record.get('name')}  答了 {int(record.get('chars') or 0)} 字 / {elapsed} ms"
        elif status == EMPTY:
            head = f"{record.get('name')}  答了个空 / {elapsed} ms"
        else:
            head = f"{record.get('name')}  没答上来 / {elapsed} ms"
        labels = [str(label) for label in (record.get("tags") or [])]
        if labels:
            head += "  [" + " ".join(labels) + "]"
        lines.append(head)
        text = str(record.get("text") or "")
        if status == FAILED:
            lines.append("  失败原因：" + (str(record.get("error") or "").strip() or "（没说原因）"))
        elif text:
            body = text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"
            for line in body.splitlines() or [""]:
                lines.append("  " + line)
    lines += ["", "一致在哪、差在哪", "  " + str(summary.get("headline") or "")]
    for note in summary.get("notes") or []:
        lines.append(f"  - {note}")
    lines += ["", str(report.get("disclaimer") or "")]
    return "\n".join(lines).rstrip() + "\n"
