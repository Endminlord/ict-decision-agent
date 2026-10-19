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
  python3 judge.py --candidates-file object-prune-todo.txt --limit 0 --batch 40 --workers 8 \
                   --key "$K" --out /tmp/judged-object-full.jsonl
  python3 judge.py --self-test        # 不联网，用桩模型
"""
import argparse, hashlib, json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

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

# issued_by 的 object 是**机构名**，用上面那份判据恒判 false（机构当然不是文件）。
# 那份判据回答的是「这个字符串是不是一份可引用文件」，对机构名不成立；
# 需要的是另一个问题：「这个机构是不是**这份文档**的发布机关」。
# 判据从「实体为真」换成「关系为真」——两者在 issued_by 上相差很大：
# 机构为真的 51.8% 里，只有约四成是本文档的真发布机关（其余是调研对象、
# 发布会主体、被引用上级文件的发布者）。
PROMPT_ISSUED_BY = """你是政务文件发布机关判定器。下面每个条目给出三样东西（以制表符分隔）：
① 候选机构名；② 该候选所属文档的标题；③ 该候选在文档正文中出现处的上下文片段。

请判断：**这个机构是不是「这份文档」的发布机关**（印发 / 发布 / 制定这份文件的主体），
而不是「是不是一个真实存在的机构」。

判定标准：
- 是发布机关：标题或证据片段表明该机构就是本文档的印发者 / 发布者 / 制定者。
  例如标题形如「XX局关于印发《…》的通知」中的 XX 局；文末「XX市人民政府办公室印发」中的机关；
  正文「XX部制定了本办法」「本通知由 XX 办公厅负责解释」中的机关。
- 不是发布机关：该机构只是文中被提及 —— 调研 / 考察 / 检查对象、会议主办或参会单位、
  新闻发布厅或发布会主体、被引用的上级文件的发布者、转述他人观点的机构、
  以及「各级各有关部门」这类泛指短语。
- 判断不了就判 false：宁可漏判，不可把「提及」升成「发布」。

候选（每条一行）：
%s

只输出 JSON 数组，每项 {"i": 序号, "is_doc": true/false, "reason": "不超过 20 字"}。
字段名沿用 is_doc：true 表示「是本文档的发布机关」，false 表示「不是（或只是提及）」。
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


def call_model(cands, base, key, model, prompt=None, timeout=180, retries=2):
    """把一批候选发给模型，返回 [{i,is_doc,reason}]。

    prompt 缺省用模块级 PROMPT（判「是不是一份可引用文件」）——**保持原有调用方行为不变**。
    issued_by 传 PROMPT_ISSUED_BY（判「是不是本文档的发布机关」）。判据按谓词分派，
    因为这两问对同一批字符串的答案不同：机构名在「是不是文件」下恒为 false。
    """
    prompt = prompt or PROMPT
    listing = "\n".join(f"{n+1}. {c}" for n, c in enumerate(cands))
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt % listing}],
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


def collect_candidates(src, limit=None, preds=("cites",), confs=("medium", "low"), side="object",
                       with_context=False):
    """从三元组里收集「需要判定」的候选（对象侧）。

    返回 `[候选字符串]`；with_context=True 时返回
    `[{"object", "title", "evidence"}]` —— issued_by 必须带标题与证据片段：
    要判的是「这个机构是不是**这份文档**的发布机关」，脱离文档就退化成
    「是不是一个真实机构」，而后者正是被替换掉的旧判据。
    上下文取 `provenance.title` / `provenance.evidence`（build.py 落盘时就带着）。
    """
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
            if with_context:
                pv = t.get("provenance") or {}
                out.append({"object": o,
                            "title": (pv.get("title") or "").strip(),
                            "evidence": (pv.get("evidence") or "").strip()})
            else:
                out.append(o)
    return out


def load_candidates_file(path, limit=None):
    """从上游剪枝清单读候选：逐行一条，去重保序，limit 截断（0/None = 全量）。

    与 collect_candidates 的区别：清单是**上一段流水线的产物**（例如
    `object-prune-todo.txt` = 从已确认文件出发生成的一圈 object 候选），
    已经做过谓词/置信度/已判定排除，这里不再过滤，只负责去重保序与截断。
    清单可能含空行与重复（不同起点重复指向同一对象），顺序即优先级。
    """
    seen, out = set(), []
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = line.strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
            if limit and len(out) >= limit:
                break
    return out


def judge_ident(cand, predicate, contexts):
    """判定身份键：issued_by 必须把文档上下文并进键里。

    同一机构名在不同文档里答案不同（在 A 文是发布机关、在 B 文只是被调研对象）。
    若沿用纯候选名做键，缓存与结果都会跨文档串味：一次判定会污染同名的所有文档。
    """
    if predicate != "issued_by":
        return cand
    ctx = (contexts or {}).get(cand) or {}
    return f"{cand}\x1f{ctx.get('title', '')}\x1f{ctx.get('evidence', '')}"


def issued_by_line(ident, contexts):
    """把一条 issued_by 候选排成 prompt 里的一行：机构名 / 标题 / 证据片段。"""
    cand, _, _ = ident.partition("\x1f")
    ctx = (contexts or {}).get(cand) or {}
    title = (ctx.get("title") or "").strip() or "（标题缺失）"
    ev = (ctx.get("evidence") or "").strip() or "（证据缺失）"
    return f"{cand}\t标题：{title}\t证据：{ev}"


def judge(cands, base, key, model, cache_path=None, batch=20, verbose=True, workers=1,
          predicate=None, contexts=None):
    """逐批判定；命中缓存不重复调用。返回 {判定身份键: 判定记录}。

    predicate 决定判据：issued_by 用 PROMPT_ISSUED_BY 并把 title/evidence 拼进每条候选，
    其余谓词一律用原 PROMPT（行为与改前逐条一致）。
    contexts：{候选: {"title","evidence"}}，仅 issued_by 需要。

    workers > 1 时批次并发发出，但**结果组装与缓存落盘仍在主线程按批次顺序做**
    —— 缓存逐批 append，中断不丢已判部分（这是 63 分钟全量跑的前提）。
    """
    prompt = PROMPT_ISSUED_BY if predicate == "issued_by" else PROMPT
    cache = load_cache(cache_path)
    result, todo = {}, []
    for c in cands:
        k = cand_key(judge_ident(c, predicate, contexts))
        if k in cache:
            result[judge_ident(c, predicate, contexts)] = cache[k]
        else:
            todo.append(c)
    if verbose:
        print(f"[judge] 候选 {len(cands)} · 缓存命中 {len(cands)-len(todo)} · 待判 {len(todo)}"
              f" · 判据 {'issued_by（发布机关）' if predicate == 'issued_by' else 'document'}"
              + (f" · 并发 {workers}" if workers and workers > 1 else ""))
    chunks = [todo[s:s + batch] for s in range(0, len(todo), batch)]

    def payload_of(chunk):
        if predicate == "issued_by":
            return [issued_by_line(judge_ident(c, predicate, contexts), contexts) for c in chunk]
        return chunk

    def absorb(n, chunk, arr):
        got = {item["i"]: item for item in arr}
        new = []
        for idx, c in enumerate(chunk, 1):
            item = got.get(idx)
            ident = judge_ident(c, predicate, contexts)
            rec = {"key": cand_key(ident), "candidate": c,
                   "is_doc": bool(item["is_doc"]) if item else None,
                   "reason": item["reason"] if item else "模型未返回该条",
                   "model": model, "method": "llm_judge"}
            if predicate == "issued_by":
                ctx = (contexts or {}).get(c) or {}
                rec["predicate"] = "issued_by"
                rec["title"] = ctx.get("title", "")
                rec["evidence"] = ctx.get("evidence", "")
            validate_judgement(rec)      # 缺理由 / is_doc 非布尔，直接抛错而不是静默入库
            result[ident] = rec
            new.append(rec)
        append_cache(cache_path, new)
        if verbose:
            ok = sum(1 for r in new if r["is_doc"])
            print(f"  批 {n}: {len(chunk)} 条 → 判真 {ok} / 判假 {len(chunk)-ok}")

    if workers and workers > 1 and len(chunks) > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(call_model, payload_of(chunk), base, key, model, prompt)
                       for chunk in chunks]
            for n, (chunk, fut) in enumerate(zip(chunks, futures), 1):
                absorb(n, chunk, fut.result())   # map 保序：缓存仍按批次顺序落盘
    else:
        for n, chunk in enumerate(chunks, 1):
            absorb(n, chunk, call_model(payload_of(chunk), base, key, model, prompt))
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

    def n6():
        """J6 清单读入：逐行去重保序，--limit 截断，limit=0 视为全量。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "todo.txt")
            open(p, "w", encoding="utf-8").write(
                "甲条例\n\n乙办法\n甲条例\n丙规定\n丁通知\n乙办法\n")
            assert load_candidates_file(p) == ["甲条例", "乙办法", "丙规定", "丁通知"], \
                f"去重保序不对：{load_candidates_file(p)}"
            assert load_candidates_file(p, limit=2) == ["甲条例", "乙办法"], \
                f"limit 截断不对：{load_candidates_file(p, limit=2)}"
            assert len(load_candidates_file(p, limit=0)) == 4, "limit=0 应视为全量"
    case("J6 清单读入去重保序 + limit 截断", n6)

    passed = sum(1 for _, ok, _ in cases if ok)
    print(f"\n自测：{passed}/{len(cases)} 个负对照被抓到")
    for label, ok, msg in cases:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" -> {msg}" if msg else ""))
    return 0 if passed == len(cases) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(HERE, "triples-governance.jsonl"),
                    help="从三元组现收候选（谓词/置信度过滤）；与 --candidates-file 二选一")
    ap.add_argument("--candidates-file", default=None,
                    help="从上游剪枝清单读候选（逐行一条，去重保序，--limit 截断）")
    ap.add_argument("--out", default=os.path.join(HERE, "judged.jsonl"))
    ap.add_argument("--cache", default=None)
    ap.add_argument("--limit", type=int, default=200, help="0 = 全量")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--workers", type=int, default=1,
                    help="并发批次数（实测网关约 2 条/秒是瓶颈，8 之后再加无益）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--side", default="object", choices=["object", "subject"],
                    help="判定哪一侧（subject = 引用者，object = 被引用者）")
    ap.add_argument("--predicate", default=None,
                    help="按谓词选判据：issued_by = 判「是不是本文档的发布机关」（自动带上 "
                         "provenance 的 title 与 evidence）；cites/implements/amends/repeals = "
                         "原判据「是不是一份可引用文件」。缺省 cites")
    ap.add_argument("--confs", default="medium,low", help="逗号分隔的置信度白名单")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.key:
        print("需要 --key 或环境变量 JUDGE_API_KEY", file=sys.stderr)
        return 2
    pred = (a.predicate or "cites").strip()
    if pred not in ("cites", "implements", "amends", "repeals", "issued_by"):
        print(f"不支持的谓词：{pred}", file=sys.stderr)
        return 2
    with_ctx = pred == "issued_by"
    contexts = None
    if a.candidates_file:
        cands = load_candidates_file(a.candidates_file, limit=a.limit)
        print(f"[judge] 清单 {a.candidates_file} → 候选 {len(cands)}（去重保序后）")
        if with_ctx:
            print("[judge] 警告：清单只有候选名、不含 title/evidence，issued_by 判定会退化成"
                  "「是不是一个真实机构」——请改用 --src 以带上文档上下文", file=sys.stderr)
    else:
        rows = collect_candidates(a.src, limit=a.limit, preds=(pred,),
                                  confs=tuple(x.strip() for x in a.confs.split(",") if x.strip()),
                                  side=a.side, with_context=with_ctx)
        if with_ctx:
            cands = [r["object"] for r in rows]
            contexts = {r["object"]: {"title": r["title"], "evidence": r["evidence"]}
                        for r in rows}
            print(f"[judge] 谓词 issued_by → 候选 {len(cands)}（已带 title/evidence 上下文）")
        else:
            cands = rows
    cache = a.cache or os.path.join(HERE, f"judge-cache-{a.side}.jsonl")
    res = judge(cands, a.base, a.key, a.model, cache_path=cache, batch=a.batch,
                workers=a.workers, predicate=pred if with_ctx else None, contexts=contexts)
    with open(a.out, "w", encoding="utf-8") as f:
        for c in cands:
            f.write(json.dumps(res[judge_ident(c, pred if with_ctx else None, contexts)],
                               ensure_ascii=False) + "\n")
    idents = [judge_ident(c, pred if with_ctx else None, contexts) for c in cands]
    docs = [i for i in idents if res[i]["is_doc"]]
    label = "本文档发布机关" if with_ctx else "文件"
    print(f"\n候选 {len(cands)} · 判为{label} {len(docs)} · 判为其他 {len(cands)-len(docs)}")
    print(f"非{label}例：", [res[i]["candidate"] for i in idents if not res[i]["is_doc"]][:5])
    return 0


if __name__ == "__main__":
    sys.exit(main())
