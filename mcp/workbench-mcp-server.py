#!/usr/bin/env python3
"""工作台 MCP 服务（workbench-mcp-server.py）

把「个人 AI 工作台」的真实能力以**标准 MCP 协议**（streamable-http）暴露给 Nexent 智能体。
三个工具都读/跑真实项目，不返回编造数据：
  1. workbench_task_status()     读 TASK_PLAN_STATUS.md，数 ✅/🟨/⬜
  2. workbench_recent_commits(n) 跑 git log --oneline
  3. workbench_run_demo()        跑 python3 run.py --demo（离线演示链），返回 rc/耗时/链路统计

用法: PORT=8791 python workbench-mcp-server.py   # 默认 0.0.0.0:8791，路径 /mcp
"""
import json
import os
import subprocess
import time
from pathlib import Path

from fastmcp import FastMCP

PROJECT = Path(os.environ.get("WORKBENCH_DIR", "/home/endmin/project-ict"))
PORT = int(os.environ.get("PORT", "8791"))

mcp = FastMCP("workbench")


@mcp.tool
def workbench_task_status() -> dict:
    """读取工作台项目的《终极任务计划 · 状态对账表》，返回已完成/部分完成/待办三项计数与文档元信息。"""
    path = PROJECT / "TASK_PLAN_STATUS.md"
    text = path.read_text(encoding="utf-8", errors="replace")
    st = path.stat()
    return {
        "file": str(path),
        "bytes": st.st_size,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "done": text.count("\u2705"),
        "partial": text.count("\U0001f7e8"),
        "todo": text.count("\u2b1c"),
    }


@mcp.tool
def workbench_recent_commits(limit: int = 5) -> list:
    """返回工作台仓库最近 limit 条提交（git log --oneline），limit 取值 1-20。"""
    n = max(1, min(int(limit), 20))
    out = subprocess.run(
        ["git", "-C", str(PROJECT), "log", "-%d" % n, "--oneline"],
        capture_output=True, text=True, timeout=30,
    )
    return out.stdout.strip().splitlines()


@mcp.tool
def workbench_run_demo() -> dict:
    """在工作台项目里跑一次离线演示链（python3 run.py --demo），返回退出码、耗时与链路统计。"""
    t0 = time.time()
    out = subprocess.run(
        ["python3", "run.py", "--demo"], cwd=str(PROJECT),
        capture_output=True, text=True, timeout=300,
    )
    result = {
        "rc": out.returncode,
        "seconds": round(time.time() - t0, 2),
        "stdout_tail": out.stdout.strip().splitlines()[-3:],
    }
    try:
        start = out.stdout.index("{")
        data = json.loads(out.stdout[start:])
        for key in ("model_calls", "model_mode", "domains", "stored", "memory_recorded"):
            if key in data:
                result[key] = data[key]
    except Exception as exc:  # 解析失败不影响工具可用
        result["parse_error"] = str(exc)
    return result


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=PORT, path="/mcp")
