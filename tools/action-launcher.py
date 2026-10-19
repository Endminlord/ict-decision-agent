#!/usr/bin/env python3
"""Action Launcher —— 从一个入口到达所有 agent。

设计见 ~/dsh/research/101-universal-agent-coverage.md §7。
注册表在 tools/agents-registry.json，三种 kind：terminal / open-in-app / url。

用法：
    python3 tools/action-launcher.py list          # 列出全部 + 实时可用性
    python3 tools/action-launcher.py check         # 只测可用性
    python3 tools/action-launcher.py run <id>      # 按 kind 分派
    python3 tools/action-launcher.py --json        # 机器可读

保真等级（level）：
    L-A 原生      同一界面内无缝
    L-B 插件补齐  同屏但带插件徽标
    L-C 终端直通  在侧栏终端跑原生 CLI
    L-D 外部派单  派单 + 异步回收
"""
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REG = Path(__file__).parent / "agents-registry.json"


def probe(kind: str, target: str) -> tuple[bool, str]:
    if kind == "url":
        try:
            with urllib.request.urlopen(target, timeout=2) as r:
                return True, f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            # 401/403 说明服务在跑，只是要认证
            return True, f"HTTP {e.code}（需认证）"
        except Exception as e:
            return False, type(e).__name__
    exe = target.split()[0]
    if shutil.which(exe):
        try:
            v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
            ver = (v.stdout or v.stderr).strip().splitlines()[0][:40] if (v.stdout or v.stderr) else "?"
            return True, ver
        except Exception:
            return True, "已安装"
    return False, "未安装"


def cmd_list(rows, as_json):
    out = []
    for a in rows:
        ok, detail = probe(a["kind"], a["target"])
        out.append({**a, "available": ok, "detail": detail})
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f'{"ID":<14}{"等级":<6}{"状态":<6}{"名称":<26}说明')
    print("-" * 96)
    for a in out:
        mark = "✓" if a["available"] else "✗"
        print(f'{a["id"]:<14}{a["level"]:<6}{mark:<6}{a["label"]:<26}{a["note"]}')
    up = sum(1 for a in out if a["available"])
    print(f"\n{up}/{len(out)} 可用")
    return 0


def cmd_run(rows, agent_id):
    a = next((x for x in rows if x["id"] == agent_id), None)
    if not a:
        print(f"未知 id: {agent_id}；可用：{', '.join(x['id'] for x in rows)}", file=sys.stderr)
        return 2
    ok, detail = probe(a["kind"], a["target"])
    if not ok:
        print(f"[{a['id']}] 不可用：{detail}", file=sys.stderr)
        return 1
    if a["kind"] == "url":
        print(f"[{a['id']}] 打开 {a['target']}")
        # 桌面环境用 explorer.exe / xdg-open；纯 CLI 环境只打印
        opener = shutil.which("explorer.exe") or shutil.which("xdg-open")
        if opener:
            subprocess.run([opener, a["target"]], check=False)
        else:
            print(f"  （无图形环境，请手动访问）")
        return 0
    if a["kind"] == "open-in-app":
        print(f"[{a['id']}] 在 {a['target']} 中打开当前目录")
        subprocess.run([a["target"], "."], check=False)
        return 0
    # terminal kind：打印命令，由调用方在终端执行（launcher 不接管 TUI）
    print(f"[{a['id']}] 终端直通命令：{a['target']}")
    print(f"  （在终端执行；dsh 不记录该会话，产物需手动登记）")
    return 0


def main() -> int:
    args = sys.argv[1:]
    rows = json.loads(REG.read_text(encoding="utf-8"))["agents"]
    if not args or args[0] in ("list", "-l"):
        return cmd_list(rows, as_json=False)
    if args[0] == "--json":
        return cmd_list(rows, as_json=True)
    if args[0] == "check":
        bad = [a for a in rows if not probe(a["kind"], a["target"])[0]]
        for a in bad:
            print(f"✗ {a['id']}: {probe(a['kind'], a['target'])[1]}")
        print(f"\n{len(rows) - len(bad)}/{len(rows)} 可用")
        return 1 if bad else 0
    if args[0] == "run" and len(args) > 1:
        return cmd_run(rows, args[1])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
