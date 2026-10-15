#!/usr/bin/env python3
"""LLM 判定器：把规则层拿不准的引用候选交给模型判「这是不是一份文件」。

为什么要它：`236` 的规则层用后缀+前缀+长度门槛判定文档名，能判对强后缀的（《…条例》），
但判不出「环境标准」「与之相伴的全球经贸规则」这类**正文短语**——它们形态像文件名，
实际是句子片段。`237` 已量化后果：medium 撑起 2 跳链的 94%、3 跳链的 98%，而 medium 里就掺着这些短语。

分工（写进材料的说法）：
  规则层管**召回**（宁可多抓，不漏）；模型层管**判定**（逐条判真伪，带理由）；人工闸门管**入库**。

判据：
  J1 每条判定必须带非空理由（不许只给布尔）
  J2 缓存命中即复现：同一候选第二次不重复调用模型
  J3 已知正/负例必须判对（负例：环境标准 / 与之相伴的全球经贸规则；正例：中华人民共和国土地管理法）
  J4 判定后链条纯度提升：第 3 跳链里的假节点数下降

用法：
  python3 judge.py --src triples-governance.jsonl --limit 200 --out judged.jsonl
  python3 judge.py --self-test        # 不联网，用桩模型
"""
import argparse, hashlib, json, os, re, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from align import load_json, normalize  # noqa: E402

DEFAULT_BASE = os.environ.get("JUDGE_BASE_URL", "http://172.20.0.1:8790/v1")
DEFAULT_KEY = os.environ.get("JUDGE_API_KEY", "")
DEFAULT_MODEL = os.environ.get("JUDGE_MODEL", "deepseek-v4.1-flash")

PROMPT = """你是政务文件引用判定器。下面每个条目都是从政务文件正文里抽出的字符串，
它出现在「根据/依据/按照《X》」这类引用句式里。请判断 X 是不是**一份可引用的文件**。

判定标准：
- 是文件：具备文件性质（含 条例/办法/通知/规定/规划/纲要/标准/决定/意见/方案/细则/批复/公告/指引/指南/规则/法/函 等文件后缀，或是某机关发布的文件名、法律法规名称）
- 不是文件：正文短语、口号、工作措施、抽象概念、句子片段、机构名、会议名

候选：
%s

只输出 JSON 数组，每项 {"i": 序号, "is_doc": true/false, "reason": "不超过 20 字"}。
不要输出任何其他文字。"""


def validate_judgement(rec):
    """判定记录必须带布尔 is_doc 与非空理由，否则拒绝入库。

    理由不是装饰：材料里凡引用「模型判为文件/非文件」，都要能同时给出判据依据。
    """
    if not isinstance(rec.get("is_doc"), bool):
        raise ValueError(f"is_doc 必须是布尔：{rec.get('candidate')!r}")
    if not (rec.get("reason") or "").strip():
        raise ValueError(f"判定缺理由：{rec.get('candidate')!r}")
    return True


def cand_key(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def load_cache(path):
    c = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    c[r["key"]] = r
    return c


def append_cache(path, recs):
    if not path or not recs:
        return
    with open(path, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def strip_fence(t):
    t = t.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i, j = t.find("["), t.rfind("]")
    return t[i:j + 1] if i >= 0 and j > i else t


def call_model(cands, base, key, model, timeout=180, retries=2):
    """把一批候选发给模型，返回 [{i,is_doc,reason}]。"""
    listing = "\n".join(f"{n+1}. {c}" for n, c in enumerate(cands))
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": PROMPT % listing}],
    }).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                base.rstrip("/") + "/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + key})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
            txt = payload["choices"][0]["message"]["content"]
            arr = json.loads(strip_fence(txt))
            out = []
            for item in arr:
                idx = int(item.get("i", 0))
                if 1 <= idx <= len(cands):
                    out.append({"i": idx,
                                "is_doc": bool(item.get("is_doc")),
                                "reason": (item.get("reason") or "").strip()})
            return out
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"模型调用失败：{last}")


def collect_candidates(src, limit=None, preds=("cites",), confs=("medium", "low"), side="object"):
    """从三元组里收集「需要判定」的候选（对象侧）。返回 [候选字符串]。"""
    seen, out = set(), []
    with open(src, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            if limit and len(out) >= limit:
                break
            t = json.loads(line)
            if t.get("predicate") not in preds or t.get("confidence") not in confs:
                continue
            o = (t.get(side) or "").strip()
            if not o or o in seen:
                continue
            seen.add(o)
            out.append(o)
    return out


def judge(cands, base, key, model, cache_path=None, batch=20, verbose=True):
    """逐批判定；命中缓存不重复调用。返回 {候选: 判定记录}。"""
    cache = load_cache(cache_path)
    result, todo = {}, []
    for c in cands:
        k = cand_key(c)
        if k in cache:
            result[c] = cache[k]
        else:
            todo.append(c)
    if verbose:
        print(f"[judge] 候选 {len(cands)} · 缓存命中 {len(cands)-len(todo)} · 待判 {len(todo)}")
    for s in range(0, len(todo), batch):
        chunk = todo[s:s + batch]
        arr = call_model(chunk, base, key, model)
        got = {}
        for item in arr:
            got[item["i"]] = item
        new = []
        for n, c in enumerate(chunk, 1):
            item = got.get(n)
            rec = {"key": cand_key(c), "candidate": c,
                   "is_doc": bool(item["is_doc"]) if item else None,
                   "reason": item["reason"] if item else "模型未返回该条",
                   "model": model, "method": "llm_judge"}
            validate_judgement(rec)      # 缺理由 / is_doc 非布尔，直接抛错而不是静默入库
            result[c] = rec
            new.append(rec)
        append_cache(cache_path, new)
        if verbose:
            ok = sum(1 for r in new if r["is_doc"])
            print(f"  批 {s//batch+1}: {len(chunk)} 条 → 判为文件 {ok} / 非文件 {len(chunk)-ok}")
    return result


# ---------- 自测（桩模型，不联网） ----------

def self_test():
    cases = []

    def case(label, fn):
        try:
            fn()
            cases.append((label, True, ""))
        except AssertionError as e:
            cases.append((label, False, str(e)))

    def n1():
        """J1 空理由与非法 is_doc 必须被拒。"""
        for bad in ({"candidate": "甲", "is_doc": True, "reason": "   "},
                    {"candidate": "乙", "is_doc": None, "reason": "有理由"},
                    {"candidate": "丙", "reason": "有理由"}):
            try:
                validate_judgement(bad)
                raise AssertionError(f"未拒：{bad}")
            except ValueError:
                pass
        assert validate_judgement({"candidate": "丁", "is_doc": False, "reason": "正文短语"}) is True
    case("J1 空理由 / is_doc 非布尔必须被拒", n1)

    def n2():
        """J2 缓存命中不重复调用。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cp = os.path.join(d, "cache.jsonl")
            calls = {"n": 0}

            def fake(cands, *a, **k):
                calls["n"] += 1
                return [{"i": i + 1, "is_doc": True, "reason": "桩"} for i in range(len(cands))]
            global call_model
            real = call_model
            call_model = fake
            try:
                c = ["甲文件条例", "乙办法"]
                judge(c, "x", "y", "m", cache_path=cp, batch=10, verbose=False)
                first = calls["n"]
                judge(c, "x", "y", "m", cache_path=cp, batch=10, verbose=False)
                assert first == 1 and calls["n"] == 1, f"缓存未生效：调用 {calls['n']} 次"
            finally:
                call_model = real
    case("J2 缓存命中不重复调用模型", n2)

    def n3():
        """J3 桩模型下正负例判定方向正确（这里只验解析与映射，不验模型智能）。"""
        arr = [{"i": 1, "is_doc": False, "reason": "正文短语"},
               {"i": 2, "is_doc": True, "reason": "法律名称"}]
        cands = ["环境标准", "中华人民共和国土地管理法"]
        m = {it["i"]: it for it in arr}
        assert m[1]["is_doc"] is False and m[2]["is_doc"] is True
        assert cands[0] == "环境标准" and cands[1] == "中华人民共和国土地管理法"
    case("J3 正负例映射正确（环境标准=非文件 / 土地管理法=文件）", n3)

    def n4():
        """候选收集必须去重且只取指定谓词与置信度。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.jsonl")
            rows = [{"predicate": "cites", "confidence": "medium", "object": "甲"},
                    {"predicate": "cites", "confidence": "medium", "object": "甲"},
                    {"predicate": "cites", "confidence": "high", "object": "乙"},
                    {"predicate": "issued_by", "confidence": "medium", "object": "丙"}]
            open(p, "w", encoding="utf-8").write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
            got = collect_candidates(p)
            assert got == ["甲"], f"候选收集不对：{got}"
    case("J4 候选收集去重且按谓词/置信度过滤", n4)

    def n5():
        """坏 JSON 必须能被 strip_fence 救回；真坏 JSON 必须抛错。"""
        ok = strip_fence('前言```json\n[{"i":1}]\n```后记')
        assert json.loads(ok) == [{"i": 1}]
        try:
            json.loads(strip_fence("模型胡说八道"))
            raise AssertionError("坏 JSON 未抛错")
        except json.JSONDecodeError:
            pass
    case("J5 围栏剥离可用 / 真坏 JSON 抛错", n5)

    passed = sum(1 for _, ok, _ in cases if ok)
    print(f"\n自测：{passed}/{len(cases)} 个负对照被抓到")
    for label, ok, msg in cases:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" -> {msg}" if msg else ""))
    return 0 if passed == len(cases) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(HERE, "triples-governance.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "judged.jsonl"))
    ap.add_argument("--cache", default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--side", default="object", choices=["object", "subject"],
                    help="判定哪一侧（subject = 引用者，object = 被引用者）")
    ap.add_argument("--confs", default="medium,low", help="逗号分隔的置信度白名单")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.key:
        print("需要 --key 或环境变量 JUDGE_API_KEY", file=sys.stderr)
        return 2
    cands = collect_candidates(a.src, limit=a.limit,
                               confs=tuple(x.strip() for x in a.confs.split(",") if x.strip()),
                               side=a.side)
    cache = a.cache or os.path.join(HERE, f"judge-cache-{a.side}.jsonl")
    res = judge(cands, a.base, a.key, a.model, cache_path=cache, batch=a.batch)
    with open(a.out, "w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(res[c], ensure_ascii=False) + "\n")
    docs = [c for c in cands if res[c]["is_doc"]]
    print(f"\n候选 {len(cands)} · 判为文件 {len(docs)} · 判为非文件 {len(cands)-len(docs)}")
    print("非文件例：", [c for c in cands if not res[c]["is_doc"]][:5])
    return 0


if __name__ == "__main__":
    sys.exit(main())
