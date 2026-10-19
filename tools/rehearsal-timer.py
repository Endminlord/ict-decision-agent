#!/usr/bin/env python3
"""试讲计时器 —— 按答辩稿的逐页时间码记录每页实际耗时。

对应 `待补充清单.md` #34。时间码与 `答辩稿.md` §一 的 12 个 `###` 标题逐条对应，
合计 900 秒 = 15 分钟。

用法：
    python3 tools/rehearsal-timer.py            # 交互试讲：讲完一页按 Enter 翻页
    python3 tools/rehearsal-timer.py --list     # 只看时间表，不计时
    python3 tools/rehearsal-timer.py --start 4  # 从第 4 页开始（中途重来）
    python3 tools/rehearsal-timer.py --auto     # 自动倒计时（无人值守自测环境用）

交互模式：讲的时候屏幕上一直显示「当前页 / 目标 / 已用 / 剩余」，
翻页后打印上一页的偏差，结束时给全表 + 最该压缩的页。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

# 与 答辩稿.md §一 的 12 个时间码逐条对应，不要单独改这里 —— 改答辩稿后同步。
PAGES: list[tuple[str, int]] = [
    ("第 1 页 · 封面（开场）", 50),
    ("第 2 页 · 痛点", 90),
    ("第 3 页 · 我们的答案", 90),
    ("第 4 页 · 正例：答出来的长什么样", 120),
    ("第 5 页 · 负例：什么时候不说", 90),
    ("第 6 页 · 编排深度：MCP 与 Skill", 100),
    ("第 7 页 · 本体三层总览", 60),
    ("第 8 页 · 规则层 + 推理层", 60),
    ("第 9 页 · 模型判定层：双侧过滤", 90),
    ("第 10 页 · 换行业会怎样", 70),
    ("第 11 页 · 能力沉淀", 50),
    ("第 12 页 · 边界与下一步", 30),
]
TOTAL = 900  # 15 分钟


def fmt(sec: float) -> str:
    """秒 → m:ss"""
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def show_list() -> None:
    print(f"试讲时间表（合计 {fmt(sum(t for _, t in PAGES))}）\n")
    acc = 0
    for i, (name, dur) in enumerate(PAGES, 1):
        print(f"  {i:2d}. {name:<34s} {fmt(acc):>5s} → {fmt(acc + dur):>5s}  ({dur:3d}s)")
        acc += dur
    print()
    print("提示：这份分配不是均分 —— 第 4 页正例给 120 秒（要讲完整条链），")
    print("      第 12 页边界只给 30 秒。所以「12 页偏少」不是页数问题，是讲得完讲不完的问题。")


class Clock:
    """后台刷新同一行状态，Enter 时停表。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._line = ""
        self._width = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            print("\r" + self._line.ljust(self._width), end="", flush=True)
            time.sleep(0.2)

    def set(self, line: str) -> None:
        self._width = max(self._width, len(line))
        self._line = line

    def start(self) -> None:
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        self._t.join(timeout=1)
        print("\r" + " " * self._width + "\r", end="", flush=True)


def rehearse(start: int, auto: bool) -> int:
    results: list[tuple[str, int, float]] = []
    clock = Clock()

    print(f"试讲开始 · 共 {len(PAGES)} 页 · 目标 {fmt(TOTAL)}")
    print("（交互模式：讲完一页按 Enter；Ctrl-C 中止并出报告）\n")

    if auto:
        print("--auto 模式：每页按目标时长自动走完，用于确认工具本身可用。\n")

    elapsed_total = 0.0
    clock.start()
    try:
        for i in range(start - 1, len(PAGES)):
            name, target = PAGES[i]
            page_start = time.time()
            elapsed_total = sum(r[2] for r in results)

            if auto:
                for remain in range(target, 0, -1):
                    clock.set(f"  {name}  目标 {fmt(target)} · 剩余 {fmt(remain)}")
                    time.sleep(1)
            else:
                while True:
                    used = time.time() - page_start
                    remain = target - used
                    flag = "  ← 超时" if remain < 0 else ""
                    clock.set(
                        f"  {name}  目标 {fmt(target)} · 已用 {fmt(used)} · "
                        f"剩余 {fmt(max(0, remain))}{flag}"
                    )
                    try:
                        input()
                        break
                    except EOFError:
                        raise KeyboardInterrupt

            used = time.time() - page_start
            results.append((name, target, used))

            if not auto:
                dev = used - target
                mark = "✓" if abs(dev) <= 5 else ("超" if dev > 0 else "省")
                clock.stop()
                print(f"  {mark} {name}  目标 {fmt(target)} · 实际 {fmt(used)} · "
                      f"偏差 {dev:+.0f}s")
                clock.start()
    except KeyboardInterrupt:
        clock.stop()
        print("\n（中止）\n")
        if not results:
            return 130
    finally:
        try:
            clock.stop()
        except Exception:
            pass

    # ── 报告 ──
    print("\n" + "═" * 62)
    print("试讲报告")
    print("═" * 62)
    print(f"{'页':<30s}{'目标':>7s}{'实际':>7s}{'偏差':>8s}")
    print("-" * 62)
    total_used = 0.0
    total_target = 0
    for name, target, used in results:
        total_used += used
        total_target += target
        dev = used - target
        print(f"{name:<30s}{fmt(target):>7s}{fmt(used):>7s}{dev:>+7.0f}s")
    print("-" * 62)
    dev_total = total_used - total_target
    print(f"{'合计':<30s}{fmt(total_target):>7s}{fmt(total_used):>7s}{dev_total:>+7.0f}s")
    print()

    if len(results) == len(PAGES):
        if abs(dev_total) <= 30:
            print(f"✓ 总时长 {fmt(total_used)}，与目标 {fmt(TOTAL)} 相差 {dev_total:+.0f}s —— 可以定稿。")
        elif dev_total > 0:
            print(f"✗ 超时 {dev_total:+.0f}s。最该压缩的三页：")
        else:
            print(f"· 快了 {dev_total:+.0f}s。可以给重点页加时间，或放慢语速。")

        over = sorted(results, key=lambda r: r[2] - r[1], reverse=True)[:3]
        if dev_total > 30:
            for name, target, used in over:
                if used - target > 0:
                    print(f"    {name}  超 {used - target:+.0f}s（目标 {fmt(target)}）")
        under = sorted(results, key=lambda r: r[2] - r[1])[:3]
        if dev_total < -30:
            for name, target, used in under:
                if used - target < 0:
                    print(f"    {name}  省 {used - target:+.0f}s（目标 {fmt(target)}）")
    else:
        print(f"（只讲了 {len(results)}/{len(PAGES)} 页，未出总评）")

    print()
    print("下一步：把偏差最大的页按上面的数字改写答辩稿，再跑一次。")
    print("      「讲不完」通常是那一页写了三件事 —— 删到一件，不是加快语速。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="按答辩稿时间码试讲计时")
    ap.add_argument("--list", action="store_true", help="只打印时间表")
    ap.add_argument("--start", type=int, default=1, help="从第几页开始（默认 1）")
    ap.add_argument("--auto", action="store_true", help="按目标时长自动走完（自测用）")
    a = ap.parse_args()

    if a.list:
        show_list()
        return 0
    if not 1 <= a.start <= len(PAGES):
        print(f"--start 超出范围（1–{len(PAGES)}）", file=sys.stderr)
        return 2
    return rehearse(a.start, a.auto)


if __name__ == "__main__":
    raise SystemExit(main())
