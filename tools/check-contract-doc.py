#!/usr/bin/env python3
"""三层契约文档 ↔ 制品一致性检查（可复跑判据）

用法：
  python3 tools/check-contract-doc.py [材料包目录]        # 默认 = 本脚本所在目录的上一级
  python3 tools/check-contract-doc.py --self-test         # 自测：构造 5 个负对照，必须全部被抓到

检查项（任一失败 => 退出码 1）：
  A1 03-场景无关契约与迁移设计.md 存在且非空
  A2 每套 MCP 的 L1 工具集合（mcp/*.json）== 对应 L2 声明集合（SKILL.md 的 allowed-tools）
  A3 03 文档覆盖全部 L1 工具名（两套合计）
  A4 03 文档里 L3（本体 schema 契约）的状态标记与 ontology/build.py 的实际存在性双向一致
     （既防「设计稿空喊已实现」，也防「实现之后文档仍标未实现」）
  A5 04-迁移改动清单模板.md 含四项迁移指标（L1/L2/L3 改动行数、迁移工时、复用率）
  A6 每套 mcp/*.json 的 server_sha256 == 对应服务端脚本的实际 sha256
  A7 每套 mcp/*.json 记录了正确的 nexent_tool_id
"""
import hashlib, json, os, re, shutil, sys, tempfile

DOC3 = "03-场景无关契约与迁移设计.md"
DOC4 = "04-迁移改动清单模板.md"

# 每套 = 一个自建 MCP 服务：描述文件 ↔ Skill 模板 ↔ 服务端脚本 ↔ 平台工具 id
MCP_SPECS = [
    {"json": "mcp/workbench-mcp.json", "skill": "skills/workbench-status-report/SKILL.md",
     "server": "mcp/workbench-mcp-server.py", "ids": [34, 35, 36]},
    {"json": "mcp/retrieval-mcp.json", "skill": "skills/governance-policy-trace/SKILL.md",
     "server": "mcp/retrieval-mcp-server.py", "ids": [38, 39, 40]},
]
JSON_MCP = MCP_SPECS[0]["json"]          # 自测用的默认目标


def _allowed_tools(text):
    """从 SKILL.md 的 YAML frontmatter 里取 allowed-tools 列表（不依赖 PyYAML）。"""
    m = re.search(r"^---\s*\n(.*?)\n---\s*\n", text, re.S | re.M)
    if not m:
        return []
    names, in_block = [], False
    for line in m.group(1).splitlines():
        if re.match(r"^allowed-tools:\s*$", line):
            in_block = True
            continue
        if in_block:
            mm = re.match(r"^\s+-\s+(\S+)\s*$", line)
            if mm:
                names.append(mm.group(1))
            elif line.strip() and not line.startswith((" ", "\t")):
                in_block = False
    return names


def check(root):
    fails, notes = [], []

    def fail(item, msg):
        fails.append(f"{item}: {msg}")

    p3 = os.path.join(root, DOC3)
    p4 = os.path.join(root, DOC4)

    # A1
    doc3 = open(p3, encoding="utf-8").read() if os.path.exists(p3) else ""
    if not doc3.strip():
        fail("A1", f"{DOC3} 缺失或为空")

    all_tools = []
    for spec in MCP_SPECS:
        tag = spec["json"]
        pj = os.path.join(root, spec["json"])
        ps = os.path.join(root, spec["skill"])
        pb = os.path.join(root, spec["server"])
        try:
            mcp = json.load(open(pj, encoding="utf-8"))
        except Exception as e:
            fail("A2", f"{tag} 不可解析: {e}")
            continue
        # A2
        tools_l1 = sorted(t["name"] for t in mcp.get("tools", []))
        tools_l2 = sorted(_allowed_tools(open(ps, encoding="utf-8").read())) if os.path.exists(ps) else []
        if tools_l1 != tools_l2:
            fail("A2", f"{tag}: L1 工具集合 {tools_l1} != L2 allowed-tools {tools_l2}")
        else:
            notes.append(f"A2 {tag} L1==L2 {tools_l1}")
        all_tools += tools_l1
        # A6
        if os.path.exists(pb):
            real = hashlib.sha256(open(pb, "rb").read()).hexdigest()
            if mcp.get("server_sha256") != real:
                fail("A6", f"{tag}: server_sha256 记录 {mcp.get('server_sha256')} != 实际 {real}")
            else:
                notes.append(f"A6 {spec['server']} 指纹一致 {real[:16]}…")
        else:
            fail("A6", f"{spec['server']} 缺失")
        # A7
        ids = sorted(t.get("nexent_tool_id") for t in mcp.get("tools", []))
        if ids != sorted(spec["ids"]):
            fail("A7", f"{tag}: nexent_tool_id 记录 {ids} != {sorted(spec['ids'])}")

    # A3
    missing = [t for t in all_tools if t not in doc3]
    if missing:
        fail("A3", f"{DOC3} 未覆盖工具: {missing}")
    # A4 状态标记必须与本体目录的实际存在性一致：两个方向都查——
    # 既防「设计稿空喊已实现」，也防「实现之后文档仍标未实现」。
    onto_build = os.path.join(root, "ontology", "build.py")
    has_impl = os.path.isfile(onto_build)
    l3 = [l for l in doc3.splitlines() if "L3 本体 schema 契约" in l]
    if not l3:
        fail("A4", f"{DOC3} 找不到 L3 行（本体检的诚实标注锚点消失）")
    elif all("未实现" in l for l in l3) and has_impl:
        fail("A4", "L3 行标「未实现」但 ontology/build.py 已存在——文档状态落后于实现")
    elif not any("未实现" in l for l in l3) and not has_impl:
        fail("A4", "L3 行标「已实现」但 ontology/build.py 不存在——不得把本体设计稿升级为已完成")
    else:
        notes.append(f"A4 L3 状态与实现一致（build.py {'存在' if has_impl else '不存在'}）")
    # A5
    doc4 = open(p4, encoding="utf-8").read() if os.path.exists(p4) else ""
    for kw in ("L1 改动", "L2 改动", "L3 改动", "迁移工时", "复用率"):
        if kw not in doc4:
            fail("A5", f"{DOC4} 缺迁移指标关键字: {kw}")
    return fails, notes


def self_test(root):
    cases = [
        ("N1 从 workbench json 删掉一个工具（L1≠L2、A3 漏覆盖）", MCP_SPECS[0]["json"],
         lambda t: t.replace('"workbench_run_demo"', '"workbench_run_demo_X"')),
        ("N2 篡改 workbench server_sha256", MCP_SPECS[0]["json"],
         lambda t: t.replace('"8a25cabfccb1ec9f07a7c16c1e897a5c9e9e25db0331fd1c353eee30c7f4afd6"', '"' + "0" * 64 + '"')),
        ("N3a 文档把 L3 标成「已实现」但实现文件不存在", "ontology/build.py", lambda t: ""),
        ("N3b 实现已存在但文档仍标「未实现」", DOC3,
         # 用正则定位 L3 表格行再改写状态列：不硬编码状态文本，
         # 否则文档措辞一变（如「规则层已实现」→「三层均已实现」）负对照会静默失效。
         lambda t: re.sub(r"^(\| \*\*L3 本体 schema 契约\*\*.*\| )[^|\n]+\|$",
                          r"\1⛔ **未实现**（见 §4，设计稿） |", t, count=1, flags=re.M)),
        ("N4 删除 04 模板", DOC4, lambda t: ""),
        ("N5 从 retrieval json 删掉一个工具（第二套也要被覆盖）", MCP_SPECS[1]["json"],
         lambda t: t.replace('"retrieval_trace_source"', '"retrieval_trace_source_X"')),
    ]
    bad = 0
    for label, rel, mut in cases:
        with tempfile.TemporaryDirectory() as tmp:
            dst = os.path.join(tmp, "pkg")
            shutil.copytree(root, dst)
            p = os.path.join(dst, rel)
            src = open(p, encoding="utf-8").read()
            if rel.endswith("ontology/build.py"):
                os.remove(p)          # 负对照要的是「文件不存在」，不是「空文件」
            else:
                open(p, "w", encoding="utf-8").write(mut(src))
            fails, _ = check(dst)
            hit = bool(fails)
            print(f"  {'PASS' if hit else 'FAIL'}  {label} -> {'抓到' if hit else '漏掉'} {fails[:2]}")
            bad += 0 if hit else 1
    print(f"\n自测：{len(cases) - bad}/{len(cases)} 个负对照被抓到")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.exit(self_test(root))
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fails, notes = check(root)
    for n in notes:
        print(f"  ok  {n}")
    for f in fails:
        print(f"  XX  {f}")
    print(f"\n契约检查：{len(notes)} 通过 / {len(fails)} 失败  (root={root})")
    sys.exit(1 if fails else 0)
