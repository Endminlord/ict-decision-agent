#!/usr/bin/env python3
"""政策扩散追踪：给定一部法规的名称，找出它在语料里的所有同名/近名版本，
按「国务院 → 省级 → 市级」分层并按时间排序，输出扩散曲线。

用法：
  python3 tools/policy-diffusion.py --topic "优化营商环境条例"
  python3 tools/policy-diffusion.py --topic "优化营商环境条例" --json

数据来源：ES 索引（默认 1-43ce427e8b5447ecad015534f953e084，15,494 块）。
注意：语料是按关键词筛的子集，不是全国政策全文库；结果是「语料里能看到的」。
"""
import argparse, json, re, sys, urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util
spec = importlib.util.spec_from_file_location("core", Path(__file__).parent / "cross-doc-demo.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

PROVINCES = ['北京','上海','天津','重庆','河北','山西','辽宁','吉林','黑龙江','江苏','浙江','安徽',
             '福建','江西','山东','河南','湖北','湖南','广东','海南','四川','贵州','云南','陕西',
             '甘肃','青海','内蒙古','广西','西藏','宁夏','新疆']

def es(path, body):
    req = urllib.request.Request(f"{core.ES}/{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"ApiKey {core.ES_KEY}"},
        method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def collect(topic, size=1000):
    """拉所有提到 topic 的块（字段名是 content，不是 text）。"""
    r = es(f"{core.DEFAULT_INDEX}/_search",
           {"query": {"match_phrase": {"content": topic}},
            "size": size, "_source": ["title", "date", "chunk_id"]})
    return r["hits"]["total"]["value"], [h["_source"] for h in r["hits"]["hits"]]


def classify(title, topic):
    """从 title 判断这条属于哪一级、哪个地区。"""
    m = re.search(r'([\u4e00-\u9fff]{2,8}?(?:省|自治区|市))?' + re.escape(topic), title)
    if not m:
        return None, None
    who = m.group(1)
    if not who:
        return "national", "国务院"
    if who.endswith(("省", "自治区")):
        return "provincial", who
    return "municipal", who


def build(topic):
    total, hits = collect(topic)
    found = {}
    for h in hits:
        level, who = classify(h.get("title", ""), topic)
        if not level:
            continue
        d = h.get("date", "")
        prev = found.get(who)
        if prev is None or (d and (not prev["date"] or d < prev["date"])):
            found[who] = {"level": level, "date": d, "title": h.get("title", ""),
                          "chunk_id": h.get("chunk_id", "")}
    groups = defaultdict(list)
    for who, v in found.items():
        groups[v["level"]].append((v["date"] or "9999", who, v))
    for k in groups:
        groups[k].sort()
    return {"topic": topic, "chunks_scanned": total, "chunks_sampled": len(hits),
            "distinct_versions": len(found), "groups": groups}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = build(args.topic)
    if args.json:
        print(json.dumps({"topic": res["topic"], "chunks_scanned": res["chunks_scanned"],
                          "distinct_versions": res["distinct_versions"],
                          "groups": {k: [{"date": d, "who": w, "title": v["title"]} for d, w, v in vs]
                                     for k, vs in res["groups"].items()}},
                         ensure_ascii=False, indent=2))
        return

    print(f'主题：{res["topic"]}')
    print(f'语料命中：{res["chunks_scanned"]} 块（取样 {res["chunks_sampled"]}）')
    print(f'不同版本：{res["distinct_versions"]} 个\n')
    label = {"national": "国务院", "provincial": "省级", "municipal": "市级"}
    for lv in ("national", "provincial", "municipal"):
        rows = res["groups"].get(lv, [])
        if not rows:
            continue
        print(f'=== {label[lv]}（{len(rows)}）===')
        for d, who, v in rows:
            print(f'  {d}  {who}{res["topic"]}')
        print()
    # 密集区检测：某省内的市级版本 ≥3 且时间跨度 ≤ 400 天
    city = res["groups"].get("municipal", [])
    byprov = defaultdict(list)
    for d, who, v in city:
        for p in PROVINCES:
            if who.startswith(p):
                byprov[p].append(d); break
    for p, ds in byprov.items():
        if len(ds) >= 3:
            ds = sorted(ds)
            print(f'⚠ 密集区：{p} 有 {len(ds)} 个市级版本，{ds[0]} → {ds[-1]}')


if __name__ == "__main__":
    main()
