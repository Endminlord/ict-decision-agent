#!/usr/bin/env python3
"""QuoteVerification 协议一致性测试套件 v1.0

任何人可以用它验证「自己的引用核验实现」是否符合协议。
本文件不依赖本项目的 lookup.py —— 它只检查一份输出 JSON 是否满足协议约束。

用法：
    python3 conformance.py --impl <命令>        # 对其它实现跑套件
    python3 conformance.py --self-test           # 对本体实现跑套件

实现契约：接受 `{"quote": "...", "strict": false}` 走 stdin 一行 JSON，
输出一行 JSON，字段见 QuoteVerification-1.0.json 的 interface.output。
"""
from __future__ import annotations
import json, subprocess, sys, os

# 每条用例：id / 引用 / 期望（字段 -> 值），描述为什么这条重要
CASES = [
  # ---- 第 1 组：真实性（内容在不在语料里）----
  ("V01", "优化营商环境", {"verified": True},
   "真实存在的短专名必须通过。把真的说成假的是最严重的错误"),
  ("V02", "这句话在语料里根本不存在xyz", {"verified": False},
   "编造内容必须判否"),
  ("V03", "", {"verified": False},
   "空引用不能通过"),

  # ---- 第 2 组：省略与拼接（内容在，但不可当原文）----
  ("V04", "不得把注册资本、资产总额……限制或者排斥潜在投标人", {"verified": False, "partial": True},
   "省略号跨越长距离 = 拼接，不能当连续原文"),

  # ---- 第 3 组：短片段（可查到但证明不了语境）----
  ("V05", "破坏营商环境", {"fragment": True},
   "6 字片段必须被标为 fragment：字面在语料里，但可能把禁止性列举读成事实"),

  # ---- 第 4 组：严格档（同样的输入，strict 改变判定）----
  ("V06", "优化营商环境", {"verified": False}, 
   "严格档下短/泛引用不通过", {"strict": True}),

  # ---- 第 5 组：字段完备性 ----
  ("V07", "优化营商环境", {"specificity": ">0"},
   "必须回传区分度，让调用方自己判断该引用有多泛"),
]


# ---- 内置语料 fixture：协议一致性不能依赖外部数据文件 ----
# 第三方拿到这个脚本就能验证，不需要先跑 build_data.py。
# 语料内容必须是**稳定、可公开、与实现无关**的最小样本。
FIXTURE_ROWS = [
  {"chunk_id": "fx-0001", "doc_id": "fx", "char_offset": 0,
   "title": "示例条例", "date": "2020-01-01", "source": "fixture",
   "text": "第一条 为了持续优化营商环境，不断解放和发展社会生产力。"},
  {"chunk_id": "fx-0002", "doc_id": "fx", "char_offset": 100,
   "title": "示例条例", "date": "2020-01-01", "source": "fixture",
   "text": "第二条 市场主体不得出现无故推诿、拖延、刁难以及违反廉洁纪律、破坏营商环境的行为。"},
  {"chunk_id": "fx-0003", "doc_id": "fx", "char_offset": 200,
   "title": "示例条例", "date": "2020-01-01", "source": "fixture",
   "text": "第三条 深化放管服改革，进一步优化营商环境，激发市场主体活力。"},
  # fx-0004 提供同块内的长跨度否定：用于 V04「拼接」用例。
  # 两段之间隔着 >40 字，分段命中成立但跨度失控 ⇒ 应判 partial。
  {"chunk_id": "fx-0004", "doc_id": "fx", "char_offset": 300,
   "title": "示例条例", "date": "2020-01-01", "source": "fixture",
   "text": "第四条 行政机关不得把注册资本、资产总额、营业收入、从业人员、利润、"
           "纳税额等规模条件作为资格要求或者评审因素，也不得通过设置不合理条件限制"
           "或者排斥潜在投标人，保障各类市场主体公平参与市场竞争。"},
]

def build_fixture(tmpdir: str) -> str:
    import gzip
    path = os.path.join(tmpdir, "chunks.jsonl.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in FIXTURE_ROWS:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path

_FIXTURE = None
REQUIRED_FIELDS = ["verified", "partial", "verbatim", "risk_flags", "specificity", "fragment", "message"]

def run_impl(cmd: str, quote: str, strict: bool) -> dict | None:
    payload = json.dumps({"quote": quote, "strict": strict}, ensure_ascii=False)
    try:
        env = dict(os.environ)
        if _FIXTURE:
            env["ICT_TRACK2_DATA"] = _FIXTURE
        p = subprocess.run(cmd, shell=True, input=payload, capture_output=True,
                           text=True, timeout=60, env=env)
        if p.returncode != 0:
            return {"__error__": f"exit {p.returncode}: {p.stderr[:200]}"}
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}

def check(cid, quote, want, why, opts=None):
    opts = opts or {}
    got = run_impl(CMD, quote, opts.get("strict", False))
    if got is None or "__error__" in got:
        return False, f"{cid} 实现报错: {(got or {}).get('__error__')}"
    missing = [f for f in REQUIRED_FIELDS if f not in got]
    if missing:
        return False, f"{cid} 缺字段 {missing}"
    for k, v in want.items():
        if k == "specificity":
            if not isinstance(got.get(k), (int, float)) or got.get(k) <= 0:
                return False, f"{cid} specificity 应为正数，实为 {got.get(k)}"
        elif got.get(k) != v:
            return False, f"{cid} {k} 期望 {v} 实为 {got.get(k)}（{why}）"
    return True, f"{cid} ok — {why[:40]}"

def main():
    global CMD
    args = sys.argv[1:]
    if "--self-test" in args:
        here = os.path.dirname(os.path.abspath(__file__))
        impl = os.path.join(here, "..", "track2-mcp", "scripts", "verify_cli.py")
        import tempfile
        tmp = tempfile.mkdtemp(prefix="qv_fixture_")
        data = build_fixture(tmp)
        global _FIXTURE
        _FIXTURE = data
        os.environ["ICT_TRACK2_DATA"] = data
        CMD = f"python3 {impl}"
    elif "--impl" in args:
        CMD = args[args.index("--impl") + 1]
    else:
        print(__doc__); return 2
    ok = 0
    for case in CASES:
        passed, msg = check(*case[:4], opts=case[4] if len(case) > 4 else None)
        print(("  ✓ " if passed else "  ✗ ") + msg)
        ok += passed
    print(f"\n一致性：{ok}/{len(CASES)}")
    return 0 if ok == len(CASES) else 1

if __name__ == "__main__":
    sys.exit(main())
