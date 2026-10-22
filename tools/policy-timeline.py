#!/usr/bin/env python3
"""把 policy-diffusion.py --json 的输出渲染成一张单文件 HTML 时间轴。

用法:
  python3 tools/policy-timeline.py --topic "优化营商环境条例" --out evidence/policy-timeline.html
  python3 tools/policy-timeline.py --from-json /tmp/diff.json --out /tmp/t.html
"""
import argparse, json, subprocess, sys, html
from datetime import date
from pathlib import Path

PROVINCE_OF = {
    "安庆市":"安徽","芜湖市":"安徽","铜陵市":"安徽","马鞍山市":"安徽","宣城市":"安徽",
    "六安市":"安徽","阜阳市":"安徽","池州市":"安徽","黄山市":"安徽","合肥市":"安徽",
    "石家庄市":"河北","唐山市":"河北","保定市":"河北",
    "苏州市":"江苏","南京市":"江苏","无锡市":"江苏","常州市":"江苏","南通市":"江苏",
    "杭州市":"浙江","宁波市":"浙江","温州市":"浙江",
    "广州市":"广东","深圳市":"广东","佛山市":"广东","东莞市":"广东",
    "成都市":"四川","绵阳市":"四川",
    "武汉市":"湖北","宜昌市":"湖北",
    "长沙市":"湖南","郑州市":"河南","济南市":"山东","青岛市":"山东",
    "福州市":"福建","厦门市":"福建","南昌市":"江西","太原市":"山西",
    "西安市":"陕西","昆明市":"云南","贵阳市":"贵州","南宁市":"广西",
    "兰州市":"甘肃","西宁市":"青海","银川市":"宁夏","乌鲁木齐市":"新疆",
    "沈阳市":"辽宁","大连市":"辽宁","长春市":"吉林","哈尔滨市":"黑龙江",
    "呼和浩特市":"内蒙古","拉萨市":"西藏","海口市":"海南",
}
LEVEL_KEY = {"国务院":"national", "省级":"provincial", "市级":"municipal"}
LEVEL_COLORS = {"国务院": "#1f6feb", "省级": "#2ea043", "市级": "#8b949e"}
DENSE_COLOR = "#f0883e"

def load(args):
    if args.from_json:
        return json.load(open(args.from_json, encoding="utf-8"))
    here = Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, str(here / "policy-diffusion.py"),
                        "--topic", args.topic, "--json"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        sys.exit(f"policy-diffusion.py 失败: {r.stderr[:400]}")
    return json.loads(r.stdout)

def parse_year(s):
    try:
        return int(str(s)[:4])
    except Exception:
        return None

def flatten(data):
    out = []
    for level, key in LEVEL_KEY.items():
        for item in (data.get("groups", {}) or {}).get(key, []) or []:
            who = item.get("who", "")
            out.append({
                "name": item.get("title") or who,
                "who": who,
                "level": level,
                "date": (item.get("date") or "")[:10],
                "chunk": item.get("chunk_id", ""),
                "province": PROVINCE_OF.get(who, ""),
            })
    out.sort(key=lambda x: x["date"] or "9999")
    return out

def mark_dense(rows, min_count=3, max_months=12):
    """同省 >= min_count 个市级版本且首尾跨度 <= max_months 个月 → 密集区。"""
    by_prov = {}
    for r in rows:
        if r["level"] == "市级" and r["province"]:
            by_prov.setdefault(r["province"], []).append(r)
    zones = []
    for prov, items in by_prov.items():
        items = [i for i in items if i["date"]]
        if len(items) < min_count:
            continue
        items.sort(key=lambda x: x["date"])
        y0, m0 = int(items[0]["date"][:4]), int(items[0]["date"][5:7])
        y1, m1 = int(items[-1]["date"][:4]), int(items[-1]["date"][5:7])
        if (y1 - y0) * 12 + (m1 - m0) <= max_months:
            zones.append((prov, items))
            for i in items:
                i["dense"] = True
    return zones

def render(data, title):
    rows = flatten(data)
    if not rows:
        sys.exit("没有版本数据")
    zones = mark_dense(rows)
    years = [parse_year(r["date"]) for r in rows if parse_year(r["date"])]
    y0, y1 = min(years), max(years)
    span = max(1, y1 - y0)
    levels = ["国务院", "省级", "市级"]
    lane_h, pad_l, pad_t = 92, 110, 110
    W = 1180
    plot_w = W - pad_l - 60
    H = pad_t + lane_h * len(levels) + 80

    def xpos(d):
        y = parse_year(d)
        if y is None:
            return pad_l
        m = int(d[5:7] or 1) if len(d) >= 7 else 1
        return pad_l + ((y - y0) + (m - 1) / 12) / span * plot_w

    dots, labels = [], []
    for i, r in enumerate(rows):
        lv = r["level"]
        cy = pad_t + lane_h * levels.index(lv) + lane_h // 2
        cx = xpos(r["date"])
        col = DENSE_COLOR if r.get("dense") else LEVEL_COLORS[lv]
        rad = 9 if r.get("dense") else 7
        tip = f'{r["who"]}\n{r["date"]}\n{r["chunk"]}'
        dots.append(f'<circle cx="{cx:.1f}" cy="{cy}" r="{rad}" fill="{col}" stroke="#0d1117" stroke-width="2"><title>{html.escape(tip)}</title></circle>')
        dy = -22 if i % 2 == 0 else 30
        short = r["who"][:14] + ("…" if len(r["who"]) > 14 else "")
        labels.append(f'<text x="{cx:.1f}" y="{cy + dy}" fill="#c9d1d9" font-size="11" text-anchor="middle">{html.escape(short)}</text>')

    ticks = []
    for y in range(y0, y1 + 1):
        tx = pad_l + (y - y0) / span * plot_w
        ticks.append(f'<line x1="{tx:.1f}" y1="{pad_t}" x2="{tx:.1f}" y2="{pad_t + lane_h * 3}" stroke="#21262d"/>')
        ticks.append(f'<text x="{tx:.1f}" y="{pad_t + lane_h * 3 + 22}" fill="#8b949e" font-size="12" text-anchor="middle">{y}</text>')

    lanes = []
    for i, lv in enumerate(levels):
        cy = pad_t + lane_h * i
        n = sum(1 for r in rows if r["level"] == lv)
        lanes.append(f'<rect x="0" y="{cy}" width="{W}" height="{lane_h}" fill="{"#0d1117" if i % 2 == 0 else "#11161d"}"/>')
        lanes.append(f'<text x="26" y="{cy + lane_h // 2 + 5}" fill="{LEVEL_COLORS[lv]}" font-size="14" font-weight="600">{lv}</text>')
        lanes.append(f'<text x="26" y="{cy + lane_h // 2 + 25}" fill="#6e7681" font-size="11">{n} 个版本</text>')

    zone_note = ""
    if zones:
        parts = "；".join(f'{p} {len(items)} 个市（{items[0]["date"]} → {items[-1]["date"]}）' for p, items in zones)
        zone_note = f'<text x="{W-60}" y="{pad_t-40}" fill="{DENSE_COLOR}" font-size="13" text-anchor="end" font-weight="600">⚠ 密集区：{html.escape(parts)}</text>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(title)} · 政策扩散时间轴</title>
<style>
  body {{ margin:0; background:#0d1117; color:#c9d1d9; font-family:-apple-system,"Segoe UI","Noto Sans CJK SC",sans-serif; }}
  .wrap {{ max-width:{W}px; margin:0 auto; padding:36px 24px 60px; }}
  h1 {{ font-size:22px; font-weight:600; margin:0 0 6px; }}
  .sub {{ color:#8b949e; font-size:13px; margin-bottom:22px; }}
  svg {{ display:block; background:#0d1117; border:1px solid #21262d; border-radius:10px; }}
  .legend {{ margin-top:16px; display:flex; gap:22px; font-size:12px; color:#8b949e; flex-wrap:wrap; }}
  .legend i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }}
</style></head><body><div class="wrap">
<h1>{html.escape(title)}</h1>
<div class="sub">共 {len(rows)} 个版本 · {y0}–{y1} · 悬停圆点看出处 chunk_id</div>
<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">
  {''.join(lanes)}
  {''.join(ticks)}
  {zone_note}
  {''.join(dots)}
  {''.join(labels)}
</svg>
<div class="legend">
  <span><i style="background:{LEVEL_COLORS['国务院']}"></i>国务院</span>
  <span><i style="background:{LEVEL_COLORS['省级']}"></i>省级</span>
  <span><i style="background:{LEVEL_COLORS['市级']}"></i>市级</span>
  <span><i style="background:{DENSE_COLOR}"></i>密集区（同省 ≥3 个且跨度 ≤12 个月）</span>
</div>
</div></body></html>"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="优化营商环境条例")
    ap.add_argument("--from-json")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data = load(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data, args.topic), encoding="utf-8")
    print(f"写入 {out}  ({out.stat().st_size} B)")

if __name__ == "__main__":
    main()
