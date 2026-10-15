#!/usr/bin/env python3
"""引用链推理：把三元组里的 cites/implements/amends/repeals 当成有向边，
做传递闭包与路径分析，回答「这些文件之间的引用链能连多深、能不能回查」。

与 retrieval MCP 的多跳检索不同：那条路是向量相似度跳转，这条是**显式引用边**跳转。
两条路互为对照——向量检索给「像不像」，本体推理给「谁引用了谁」。

判据：
  R1 闭包包含全部直接边（不做无中生有）
  R2 传递性成立：A→B、B→C 必须推出 A→C
  R3 无自环（A→A 不得出现在闭包里）
  R4 每条闭包边都能给出至少一条路径，且路径上每一环都带得出处（provenance）

用法：
  python3 reason.py --src triples-governance.jsonl --out reason-report.json \
      --chains chains-top.jsonl --starts 2000 --max-depth 4
  python3 reason.py --self-test
"""
import argparse, json, os, sys, time
from collections import deque, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from align import normalize, load_json  # noqa: E402

REF_PREDS = ("cites", "implements", "amends", "repeals")


def load_triples(path, limit=None, preds=REF_PREDS, conf_min=None):
    """读三元组，只保留引用类谓词。返回 (rows, skipped)。"""
    rows, skipped = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            if limit and len(rows) >= limit:
                break
            try:
                t = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if t.get("predicate") not in preds:
                skipped += 1
                continue
            if conf_min and _rank(t.get("confidence")) < _rank(conf_min):
                skipped += 1
                continue
            if not t.get("subject") or not t.get("object"):
                skipped += 1
                continue
            rows.append(t)
    return rows, skipped


def _rank(c):
    return {"high": 3, "medium": 2, "low": 1}.get(c or "", 0)


def build_graph(rows, terms, self_loops_dropped=None):
    """邻接表 {subject: {object: [predicate,...]}} + 边出处索引。

    self_loops_dropped 传 list 时，把自环记进去（调用方用于统计），而不是静默丢弃。
    """
    g = defaultdict(lambda: defaultdict(list))
    prov = {}
    for t in rows:
        s = normalize(t["subject"], terms)
        o = normalize(t["object"], terms)
        if s == o:
            if self_loops_dropped is not None:
                self_loops_dropped.append(t)
            continue
        g[s][o].append(t["predicate"])
        prov[(s, o)] = t
    return dict(g), prov


def closure(g, starts, max_depth=4):
    """从给定起点做有界 BFS，返回 {(a,b): 最短跳数}。"""
    out = {}
    for s in starts:
        seen = {s: 0}
        q = deque([s])
        while q:
            cur = q.popleft()
            d = seen[cur]
            if d >= max_depth:
                continue
            for nxt in g.get(cur, ()):
                if nxt not in seen:
                    seen[nxt] = d + 1
                    q.append(nxt)
        for node, d in seen.items():
            if node != s:
                out[(s, node)] = d
    return out


def top_starts(g, n):
    """按出度排序取前 n 个起点（出度高的最可能长出长链）。"""
    return [k for k, _ in sorted(g.items(), key=lambda kv: -len(kv[1]))[:n]]


def reconstruct(g, a, b, max_depth=6):
    """BFS 找一条 a→b 的最短路径，返回 [a, x, ..., b]；找不到返回 None。"""
    if a == b:
        return [a]
    prev = {a: None}
    q = deque([(a, 0)])
    while q:
        cur, d = q.popleft()
        if d >= max_depth:
            continue
        for nxt in g.get(cur, ()):
            if nxt in prev:
                continue
            prev[nxt] = cur
            if nxt == b:
                path, node = [], nxt
                while node is not None:
                    path.append(node)
                    node = prev[node]
                return list(reversed(path))
            q.append((nxt, d + 1))
    return None


def analyze(g, clo, prov, top_n=20):
    depth_hist = defaultdict(int)
    for (_, _), d in clo.items():
        depth_hist[d] += 1
    out_deg = {k: len(v) for k, v in g.items()}
    in_deg = defaultdict(int)
    for s, vs in g.items():
        for o in vs:
            in_deg[o] += 1
    # 最长链：按跳数排序，取前 top_n，并重建路径 + 每环出处
    chains = []
    for (a, b), d in sorted(clo.items(), key=lambda kv: -kv[1])[: top_n * 8]:
        path = reconstruct(g, a, b, max_depth=d)
        if not path or len(path) != d + 1:
            continue
        rings = []
        ok = True
        for i in range(len(path) - 1):
            t = prov.get((path[i], path[i + 1]))
            if not t:
                ok = False
                break
            p = t.get("provenance") or {}
            rings.append({
                "from": path[i], "to": path[i + 1], "predicate": t["predicate"],
                "confidence": t.get("confidence"),
                "chunk_id": p.get("chunk_id"), "doc_id": p.get("doc_id"),
                "title": p.get("title"), "date": p.get("date"),
                "evidence": (p.get("evidence") or "")[:120],
            })
        if ok:
            chains.append({"start": a, "end": b, "hops": d, "rings": rings})
        if len(chains) >= top_n:
            break
    return {
        "nodes": len(g),
        "edges": sum(len(v) for v in g.values()),
        "closure_pairs": len(clo),
        "depth_hist": dict(sorted(depth_hist.items())),
        "max_depth_reached": max(depth_hist) if depth_hist else 0,
        "top_out_degree": sorted(out_deg.items(), key=lambda kv: -kv[1])[:10],
        "top_in_degree": sorted(in_deg.items(), key=lambda kv: -kv[1])[:10],
        "isolated": sum(1 for k in g if not g[k]),
        "chains": chains,
    }


def check_judgements(g, clo, rep, prov, starts):
    """R1–R4。返回 (fails, notes)。

    闭包是**有界**的（只从 starts 出发、深度上限 max_depth），所以 R1/R2 只能
    在「起点可达范围」内断言——否则测的是「为什么不做全图闭包」，而不是正确性。
    """
    fails, notes = [], []
    sset = set(starts)
    # R1 起点出发的直接边必须都在闭包里
    missing = [(s, o) for s in sset for o in g.get(s, {}) if (s, o) not in clo]
    if missing:
        fails.append(f"R1 起点出发的直接边漏掉 {len(missing)} 条，例：{missing[:3]}")
    else:
        notes.append(f"R1 {len(sset)} 个起点出发的全部直接边都在闭包内")
    # R2 传递性：随机抽 200 个 (A→B→C) 检查 (A,C)
    import random
    rnd = random.Random(20261013)
    pairs = [(s, o) for s, vs in g.items() for o in vs]
    checked = bad = 0
    for _ in range(400):
        if not pairs:
            break
        s, o = rnd.choice(pairs)
        if s not in sset:          # 起点不在闭包范围内，测不了传递性
            continue
        nxts = list(g.get(o, {}))
        if not nxts:
            continue
        c = rnd.choice(nxts)
        checked += 1
        if (s, c) not in clo:
            bad += 1
    if checked and bad:
        fails.append(f"R2 传递性失败：抽 {checked} 组三元传递，{bad} 组未推出")
    else:
        notes.append(f"R2 传递性成立（抽 {checked} 组 A→B→C 全部推出 A→C）")
    # R3 无自环
    loops = [(a, b) for (a, b) in clo if a == b]
    if loops:
        fails.append(f"R3 闭包含自环 {len(loops)} 条，例：{loops[:3]}")
    else:
        notes.append("R3 闭包无自环")
    # R4 每条闭包边可给路径且每环有出处
    bad_ring = 0
    for c in rep["chains"]:
        for r in c["rings"]:
            if not r.get("chunk_id") or not r.get("evidence"):
                bad_ring += 1
    if rep["chains"] and bad_ring:
        fails.append(f"R4 有 {bad_ring} 环缺出处字段")
    else:
        notes.append(f"R4 最长 {len(rep['chains'])} 条链的每一环都带 chunk_id 与 evidence")
    return fails, notes


def run(src, out_path, chains_path, starts_n, max_depth, limit, conf_min=None):
    terms = load_json(os.path.join(HERE, "terms.governance.zh.json"))
    rows, skipped = load_triples(src, limit=limit, conf_min=conf_min)
    loops = []
    g, prov = build_graph(rows, terms, self_loops_dropped=loops)
    starts = top_starts(g, starts_n) if starts_n else list(g)
    t0 = time.time()
    clo = closure(g, starts, max_depth=max_depth)
    rep = analyze(g, clo, prov)
    rep["elapsed_s"] = round(time.time() - t0, 1)
    rep["input_rows"] = len(rows)
    rep["skipped_rows"] = skipped
    rep["self_loops_dropped"] = len(loops)
    rep["starts_used"] = len(starts)
    rep["max_depth"] = max_depth
    rep["conf_min"] = conf_min
    fails, notes = check_judgements(g, clo, rep, prov, starts)
    rep["judgements"] = {"fails": fails, "notes": notes}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    with open(chains_path, "w", encoding="utf-8") as f:
        for c in rep["chains"]:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    return rep


def self_test():
    """负对照：每个都构造一个「应当被抓到」的缺陷。"""
    terms = load_json(os.path.join(HERE, "terms.governance.zh.json"))
    mk = lambda s, o, p="cites": {"subject": s, "object": o, "predicate": p,
                                 "confidence": "high",
                                 "provenance": {"chunk_id": "c-1", "doc_id": "d-1",
                                                "title": "T", "date": "2020-01-01",
                                                "evidence": "依据《" + o + "》"}}
    cases = []

    def case(label, fn):
        # 每个 case 内部已构造好「应当被抓住」的缺陷（或应当成立的行为），
        # fn() 正常返回 = 预期成立；抛 AssertionError = 缺陷漏网。
        try:
            fn()
            cases.append((label, True, ""))
        except AssertionError as e:
            cases.append((label, False, str(e)))

    # N1 传递性：A→B→C 必须出现 A→C
    def n1():
        g, _ = build_graph([mk("A", "B"), mk("B", "C")], terms)
        c = closure(g, ["A"], 4)
        assert ("A", "C") in c, "A→C 未推出"
    case("N1 三跳传递必须推出 A→C", n1)

    # N2 自环必须被剔除（不能出现在图里）
    def n2():
        loops = []
        g, _ = build_graph([mk("A", "A")], terms, self_loops_dropped=loops)
        assert len(loops) == 1 and "A" not in g, "自环未被剔除"
    case("N2 自环必须被剔除", n2)

    # N3 路径重建必须逐环有序
    def n3():
        g, _ = build_graph([mk("A", "B"), mk("B", "C"), mk("C", "D")], terms)
        p = reconstruct(g, "A", "D", 6)
        assert p == ["A", "B", "C", "D"], f"路径不对：{p}"
    case("N3 路径重建逐环有序", n3)

    # N4 缺出处的环必须被 R4 抓到
    def n4():
        bad = dict(mk("A", "B"))
        bad["provenance"] = {"chunk_id": "", "evidence": ""}
        g, prov = build_graph([bad], terms)
        cl = closure(g, ["A"], 4)
        rep = analyze(g, cl, prov)
        fails, _ = check_judgements(g, cl, rep, prov, ["A"])
        assert any("R4" in f for f in fails), f"缺出处未被抓：{fails}"
    case("N4 环缺出处必须被 R4 抓到", n4)

    # N5 深度上限必须真的生效（不许无限展开）
    def n5():
        rows = [mk("A", "B"), mk("B", "C"), mk("C", "D"), mk("D", "E")]
        g, _ = build_graph(rows, terms)
        c = closure(g, ["A"], max_depth=2)
        assert ("A", "C") in c and ("A", "D") not in c, f"深度上限失效：{sorted(c)}"
    case("N5 max_depth 必须生效", n5)

    passed = sum(1 for _, caught, _ in cases if caught)
    print(f"\n自测：{passed}/{len(cases)} 个负对照被抓到")
    for label, caught, msg in cases:
        print(f"  {'PASS' if caught else 'FAIL'}  {label}" + (f" -> {msg}" if msg else ""))
    return 0 if passed == len(cases) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(HERE, "triples-governance.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "reason-report.json"))
    ap.add_argument("--chains", default=os.path.join(HERE, "chains-top.jsonl"))
    ap.add_argument("--starts", type=int, default=0, help="0 = 全部节点作起点")
    ap.add_argument("--max-depth", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--conf-min", default=None, choices=["high", "medium", "low"],
                    help="只保留不低于该置信度的引用边")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    rep = run(a.src, a.out, a.chains, a.starts, a.max_depth, a.limit, a.conf_min)
    print(json.dumps({k: rep[k] for k in
                      ("nodes", "edges", "closure_pairs", "max_depth_reached",
                       "depth_hist", "self_loops_dropped", "elapsed_s", "judgements")},
                     ensure_ascii=False, indent=1))
    return 0 if not rep["judgements"]["fails"] else 1


if __name__ == "__main__":
    sys.exit(main())
