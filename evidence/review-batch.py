#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工判例蒸馏 · 批量预处理器（v1）
规则来源：20261101 人工闸门前 80 条判例（队长亲手判）。
只动 medium/low 候选，全部落 batch 文件，规则/数量/来源可复核。
用法：
  python3 review-batch.py --pending P.jsonl --workdir ~/ict-review
产物：
  confirmed-batch.jsonl / rejected-batch.jsonl（批量决定）
  remaining.jsonl（筛剩的真冲突区，重开 review.py 用它）
  batch-report.json（每条规则命中数，全账可查）
"""
import json, re, sys, argparse, os

AP = argparse.ArgumentParser()
AP.add_argument("--pending", required=True)
AP.add_argument("--workdir", required=True)
A = AP.parse_args()
os.makedirs(A.workdir, exist_ok=True)

# ── 规则表（每条都溯源到人工判例批号）──────────────────────────
# 文艺/非文档宾语词尾（判例：新三峡/兴隆场/挺进报/东方红/抓娃娃/人世间/繁花/
# 大江大河/野牦牛/云台谣/朱履曲/铁佛寺/缚鸡行/根据地/刘巧儿）
ART_TAIL = re.compile(r"(影|剧|歌|谣|曲|戏|报|刊|志|专辑|单曲|纪录片|电影|电视剧|微短剧)$")
# 已知非文档宾语全名（判例直录）
ART_NAME = {"东方红","抓娃娃","人世间","繁花","大江大河","新三峡","兴隆场","挺进报",
            "野牯牛","野牦牛","藏羚羊","黑颈鹤","雪豹","金雕","云台谣","朱履曲",
            "铁佛寺","惊蛰","缚鸡行","刘巧儿","根据地","北平无战事","红高粱","白露"}
# 业务单据词（判例：确认单）
DOCISH = re.compile(r"(确认单|登记单|申请表|申报表|准考证|缴款书|通知书单)$")
# 发布会/学习会/新闻稿/答记/解读/专栏 主体 + amends/repeals → r（判例 13/14/15/43/66/69/71…）
CIVIL_SUBJ = re.compile(r"(新闻发布会|记者会|答记者问|学习会|集体学习|讲坛|读书班|"
                        r"专题辅导|宣讲|报道|解读|专栏|访谈|新闻发布会|见面会|专场)$")
STRONG = {"amends", "repeals"}
# 依据动词 + 宾语以「法」字结尾 → a（判例 24/27/36/44/46/62/67/72/75…）
LAW_OBJ = re.compile(r"法$")
DEP_VERB_EV = re.compile(r"根据《|按照《|依照《|依据《|贯彻落实《")
# 泛名简称黑名单（判例：v3 审计「管理办法/实施办法/暂行办法」裸简称会多对多归并污染）——留人工
GENERIC = {"管理办法","实施办法","暂行办法","办法","实施细则","细则","暂行规定","规定",
           "条例","通知","决定","意见","方案","若干措施","特扶办法","暂行条例"}

def ev_text(rec):
    p = rec.get("provenance") or {}
    return p.get("evidence") or rec.get("evidence") or ""

rules = {k: 0 for k in ["art_tail","art_name","docish","civil_strong","dep_law_auto","cb2_inspector","cb2_study_amends","cb2_impl_reg","cb2_art_docish"]}
conf = open(os.path.join(A.workdir,"confirmed-batch.jsonl"),"w",encoding="utf-8")
rej  = open(os.path.join(A.workdir,"rejected-batch.jsonl"),"w",encoding="utf-8")
rem  = open(os.path.join(A.workdir,"remaining.jsonl"),"w",encoding="utf-8")

def rule_of(rec):
    subj = rec.get("subject","")
    obj  = rec.get("object","")
    pred = rec.get("predicate","")
    ev   = ev_text(rec)
    if ART_TAIL.search(obj):
        if re.search(r"(公报|专报|摘报|简报|白皮书)$", obj):
            return None, None  # 真公文，留人工
        return "art_tail", "rej"
    if obj in ART_NAME:
        return "art_name", "rej"
    if DOCISH.search(obj):
        return "docish", "rej"
    if pred in STRONG and CIVIL_SUBJ.search(subj[-18:]):
        return "civil_strong", "rej"
    # ── 第二批规则（20261101 下午 120+ 判例蒸馏）──
    if pred in STRONG and ("统计督察" in subj or "督察组" in subj):
        return "cb2_inspector", "rej"
    if pred in STRONG and re.search(r"(学习|论述|贯彻|辅导报告|宣讲)", subj) and obj == "统计法":
        return "cb2_study_amends", "rej"
    if pred == "implements" and re.search(r"(办法|细则|规定|方案)$", subj):
        titles = set(re.findall(r"《([^》]{2,25})》", ev))
        norm = obj.replace("中华人民共和国", "")
        for t in titles:
            nt = t.replace("中华人民共和国", "")
            if nt == norm or ("制定本办法" in ev and norm in nt):
                return "cb2_impl_reg", "acc"
    if obj in {"柳堡的故事","我的绝密生涯","罗生特","信息表","开具红字增值税专用发票信息表"}:
        return "cb2_art_docish", "rej"
    if pred == "cites" and obj.endswith("法") and obj not in GENERIC:
        titles = set(re.findall(r"《([^》]{2,25})》", ev))
        norm_obj = obj.replace("中华人民共和国", "")
        for t in titles:
            if t.replace("中华人民共和国", "") == norm_obj:
                return "dep_law_auto", "acc"
    return None, None

n_total = 0
with open(A.pending, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        n_total += 1
        rec = json.loads(line)
        rule, act = rule_of(rec)
        if act == "rej":
            rec["batch_rule"] = rule
            rej.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rules[rule] += 1
        elif act == "acc":
            rec["batch_rule"] = rule
            conf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rules[rule] += 1
        else:
            rem.write(line + "\n")
conf.close(); rej.close(); rem.close()

out = {"source": os.path.abspath(A.pending), "total": n_total,
       "rules": rules,
       "remaining": n_total - sum(rules.values())}
json.dump(out, open(os.path.join(A.workdir,"batch-report.json"),"w",encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1))
