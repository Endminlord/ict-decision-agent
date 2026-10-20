"""把一场运行说过的话读成「帧」——回放要的是时间轴，不是一段文本。

判据（写死在这里，界面和服务端都照它读）：

- **时间戳读不出来就是读不出来**：`t` 给 `None`，不拿 `0` 冒充——`0` 的意思是
  「和第一帧同一时刻」，那是另一件事。
- **帧号是它在记录里的位置**，不是「第几秒」：回放要能一步一帧地对回去，
  而「两帧之间隔了多久」是另一列。
- **间隔单独算**：`跑了四分钟` 和 `卡了四分钟`，在一条没有间隔的日志里长得
  一模一样。
- **读不出来的行也占一帧**：记录里混进一个不是字典的东西，回放里就出现一帧
  「这一行读不出来」——把它悄悄跳过，等于替这场运行说了它没说过的话。

这个模块只做算术和措辞，不读文件、不发请求、不认识任何厂商。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 一帧一帧回放的闸门。超过这个数只留前面这些，并且说清楚「截了」——
# 悄悄截断会让最后一帧看着像「这场就跑到这儿」。
MAX_FRAMES = 2000

# 本地时间的写法。时间戳是 UTC 存的（服务端打的），给人看的时候换成这台
# 机器上的钟——日志里最该看得出的一件事是「这条和上一条隔了多久」。
_CLOCK = "%H:%M:%S"


def _parse(value: Any) -> datetime | None:
    """ISO 时间戳 → datetime（带时区）。读不出来给 None。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 没有时区的按 UTC 读——服务端写的就是 UTC。这是**读法**，不是猜测：
        # 猜成本地时间会让同一份日志在两台机器上差几个小时。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ms(value: Any) -> int | None:
    """ISO 时间戳 → epoch 毫秒。读不出来给 None，不给 0。"""
    parsed = _parse(value)
    if parsed is None:
        return None
    return int(round(parsed.timestamp() * 1000))


def clock(value: Any) -> str:
    """这一帧的钟点（这台机器上的本地时间，`HH:MM:SS`）。读不出来给空串。"""
    parsed = _parse(value)
    if parsed is None:
        return ""
    return parsed.astimezone().strftime(_CLOCK)


def _split(ms: int) -> tuple[int, int, int]:
    """毫秒 → (时, 分, 秒)。秒是整数部分，用来拼人话。"""
    total = max(0, int(ms))
    seconds = total // 1000
    return seconds // 3600, (seconds % 3600) // 60, seconds % 60


def length_note(ms: int) -> str:
    """一段时长写成话：`2 分 12 秒` / `4 小时 3 分` / `1.2 秒`。

    只写时长本身，不带「隔了」「离开场」这类前缀——前缀是**那句话**的一部分，
    由摆这句话的地方加（界面上同一列里既有「离开场」也有「离上一帧」，
    调试器那边还有「等了」）。所以它是公开的：同一句话只留一份实现。
    """
    hours, minutes, seconds = _split(ms)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{ms / 1000.0:.1f} 秒"


def gap_note(ms: int | None) -> str:
    """两帧之间隔了多久，用人话说。读不出来就说读不出来。"""
    if ms is None:
        return "时间不明"
    if ms < 0:
        # 时钟倒着走（记录是两台机器拼的、或者谁改了系统时间）。不编一个
        # 「隔了 -3 秒」，直接说这一条的时间不对劲。
        return "时间戳比上一条还早"
    if ms < 1000:
        return "不到一秒"
    return length_note(ms)


def elapsed_note(ms: int | None) -> str:
    """这一帧离**时间轴的起点**多久。"""
    if ms is None:
        return "时间不明"
    if ms == 0:
        # `t == 0` 的意思不是「运行刚开始」，是「第一帧有时间戳的就是它」——
        # 前面那几帧没有时间戳。说成「开场」会把这段空白抹掉。
        return "时间轴的起点"
    if ms < 1000:
        return "不到一秒"
    return length_note(ms)


def span_note(span_ms: int | None) -> str:
    """这场运行前后多久。"""
    if span_ms is None:
        return "时间不明"
    if span_ms < 1000:
        return "前后不到一秒"
    return "前后 " + length_note(span_ms)


def _frame(index: int, event: Any, base: int | None, previous: int | None) -> dict:
    """一条记录 → 一帧。原始字段一个不少地带着，另加回放要用的那几列。"""
    if not isinstance(event, dict):
        return {
            "i": index,
            "type": "",
            "at": "",
            "at_text": "",
            "t": None,
            "since": None,
            "readable": False,
        }
    at = event.get("at")
    stamp = _ms(at)
    frame = dict(event)
    frame["i"] = index
    frame["readable"] = True
    frame["at"] = at if isinstance(at, str) else ""
    frame["at_text"] = clock(at)
    if stamp is None or base is None:
        frame["t"] = None
        frame["since"] = None
        return frame
    frame["t"] = stamp - base
    frame["since"] = None if previous is None else stamp - previous
    return frame


def read_frames(events: Any, *, limit: int = MAX_FRAMES) -> dict:
    """一场运行说过的话 → 帧。

    `t` 是离**第一帧有时间戳的那个**的毫秒数（前面那几帧没有时间戳，`t` 给
    `None`），`since` 是离**上一帧**的毫秒数。第一帧的 `since` 是 `None`
    （它前面没有东西），不是 0——0 的意思是「紧挨着」。
    """
    rows = list(events) if isinstance(events, (list, tuple)) else []
    gate = max(1, int(limit))
    truncated = len(rows) > gate
    rows = rows[:gate]

    stamps = [_ms(row.get("at")) if isinstance(row, dict) else None for row in rows]
    timed = [stamp for stamp in stamps if stamp is not None]
    base = timed[0] if timed else None

    frames: list[dict] = []
    unreadable: list[int] = []
    no_time: list[int] = []
    kinds: dict[str, int] = {}
    previous: int | None = None
    for index, row in enumerate(rows):
        frame = _frame(index, row, base, previous)
        if not frame["readable"]:
            unreadable.append(index)
        elif frame["t"] is None:
            no_time.append(index)
        else:
            previous = stamps[index]
        kind = frame.get("type")
        if isinstance(kind, str) and kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        frames.append(frame)

    span = None
    if len(timed) >= 2:
        span = timed[-1] - timed[0]
    return {
        "frames": frames,
        "count": len(rows),
        "shown": len(frames),
        "truncated": truncated,
        "unreadable": unreadable,
        "no_time": no_time,
        "kinds": kinds,
        "span_ms": span,
        "span_note": span_note(span),
        "started_at": frames[0]["at"] if frames else "",
        "finished_at": frames[-1]["at"] if frames else "",
    }
