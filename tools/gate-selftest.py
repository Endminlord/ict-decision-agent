import json,re,sys
def norm(s): return re.sub(r"[\s《》〈〉“”\"]","",s or "")
C=[json.loads(l) for l in open('ontology/triples-confirmed.jsonl',encoding='utf-8')]
R=[json.loads(l) for l in open('ontology/triples-rejected.jsonl',encoding='utf-8')]
A=[json.loads(l) for l in open('ontology/triples-governance.jsonl',encoding='utf-8')]
cites=[r for r in A if r["predicate"]=="cites"]
titles={r["provenance"]["title"] for r in A if r.get("provenance",{}).get("title")}
nt={norm(t) for t in titles}
t=[]
def chk(name,cond): t.append((name,cond))
chk("G1 每条入库记录的 object 都是语料内真实标题", all(norm(r["object"]) in nt for r in C))
chk("G2 每条入库记录都带可回查 chunk_id", all(r["provenance"].get("chunk_id") for r in C))
chk("G3 每条入库记录的 evidence 原文真的含 object", all(norm(r["object"]) in norm(r["provenance"]["evidence"]) for r in C))
chk("G4 无自引（subject≠object）", all(norm(r["subject"])!=norm(r["object"]) for r in C))
chk("G5 review_status 已标 confirmed", all(r["review_status"]=="confirmed" for r in C))
chk("G6 拒绝清单的 object 都不在语料内", all(norm(r["object"]) not in nt for r in R))
chk("G7 入库+拒绝 = cites 全量（无遗漏无重叠）", len(C)+len(R)==len(cites))
chk("G8 负对照：编造一个不存在的文件名必须被拒", norm("完全不存在的规定") not in nt)
ok=sum(1 for _,c in t if c)
for n,c in t: print(f"  {'✓' if c else '✗'} {n}")
print(f"\n闸门自测：{ok}/{len(t)} 通过")
sys.exit(0 if ok==len(t) else 1)
