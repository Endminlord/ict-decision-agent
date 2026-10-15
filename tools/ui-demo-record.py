#!/usr/bin/env python3
"""CDP 驱动 Nexent UI 录屏：选智能体 → 提问 → 按固定间隔连拍 → ffmpeg 合成 mp4。

与 ui-chat-shot.py 的区别：它在回答过程中持续抓帧，产出可播放的过程录像
（提问 → 思考 → 工具调用 → 回答成型），而 ui-chat-shot.py 只在回答稳定后抓一帧。

用法：
  uv run --python 3.10 --with websocket-client python3 ui-demo-record.py \
      --q "问题" --out-dir /tmp/demo-frames --mp4 /tmp/demo.mp4 \
      [--agent "ICT 演示智能体"] [--interval 0.5] [--wait 240] [--fps 8]
"""
import argparse, base64, json, os, subprocess, sys, time, urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222"


def pick_target():
    lst = json.load(urllib.request.urlopen(CDP_HTTP + "/json/list", timeout=10))
    pages = [x for x in lst if x["type"] == "page" and "localhost:3000" in x.get("url", "")]
    if not pages:
        # 注意：新版 Chrome 的 /json/new 要求 PUT，GET 会 405。请让 chrome 启动时直接带 URL。
        raise RuntimeError("未找到 localhost:3000 的 page target；"
                           "请用 --headless=new ... \"http://localhost:3000/zh/newchat\" 启动 chrome")
    return pages[0]


class Page:
    def __init__(self):
        t = pick_target()
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=90,
                                              suppress_origin=True)
        self.i = 0
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Emulation.setDeviceMetricsOverride", width=1500, height=1000,
                  deviceScaleFactor=1, mobile=False)

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def js(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        return r.get("result", {}).get("value")

    def text(self):
        return self.js("document.body.innerText") or ""

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        data = base64.b64decode(r["data"])
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


ASK = r"""
(() => {
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  };
  let box = document.querySelector('textarea[placeholder*="发送"]');
  if (!box) {
    box = [...document.querySelectorAll('textarea')].find(e => e.offsetParent !== null);
  }
  if (!box) return { error: 'no send box', visible: document.body.innerText.slice(0, 200) };
  box.focus();
  setVal(box, QUESTION);
  const kb = (t) => box.dispatchEvent(new KeyboardEvent(t, {
    key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
  }));
  kb('keydown'); kb('keypress'); kb('keyup');
  return { ok: true, val: box.value.slice(0, 40) };
})()
"""

BUSY = ("!!document.querySelector('[class*=stop],[aria-label*=停止],"
        "[class*=loading],[class*=typing]')")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--agent", default="ICT 演示智能体")
    ap.add_argument("--out-dir", required=True, help="帧输出目录（会创建）")
    ap.add_argument("--mp4", default="", help="合成 mp4 路径；留空则只留帧")
    ap.add_argument("--interval", type=float, default=0.5, help="抓帧间隔秒数")
    ap.add_argument("--wait", type=float, default=240.0, help="等回答的上限秒数")
    ap.add_argument("--fps", type=int, default=8, help="合成时的播放帧率")
    ap.add_argument("--warmup", type=float, default=2.0, help="提问前的静场帧秒数")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    for f in os.listdir(args.out_dir):
        if f.startswith("f") and f.endswith(".png"):
            os.remove(os.path.join(args.out_dir, f))

    p = Page()
    print("[1] 导航 newchat", flush=True)
    p.js("location.href='http://localhost:3000/zh/newchat'")
    time.sleep(7)

    clicked = p.js(
        "(()=>{const t=%s;const els=[...document.querySelectorAll('*')]"
        ".filter(e=>e.children.length===0&&e.innerText&&e.innerText.trim()===t);"
        "if(!els.length)return 'not found';let n=els[0];"
        "for(let i=0;i<6&&n;i++){n.click();if(location.href!=='http://localhost:3000/zh/newchat')break;n=n.parentElement;}"
        "return location.href})()" % json.dumps(args.agent))
    print("[2] 点击智能体:", clicked, flush=True)
    time.sleep(6)

    idx = 0

    def grab():
        nonlocal idx
        path = os.path.join(args.out_dir, f"f{idx:05d}.png")
        p.shot(path)
        idx += 1
        return path

    print(f"[3] 静场帧 {args.warmup}s", flush=True)
    t_end = time.time() + args.warmup
    while time.time() < t_end:
        grab()
        time.sleep(args.interval)

    res = p.js(ASK.replace("QUESTION", json.dumps(args.q)))
    print("[4] 提交问题:", json.dumps(res, ensure_ascii=False)[:160], flush=True)

    base = len(p.text() or "")
    t0 = time.time()
    deadline = t0 + args.wait
    last, stable = base, 0
    while time.time() < deadline:
        grab()
        txt = p.text() or ""
        n = len(txt)
        busy = p.js(BUSY)
        if n > base + 60 and n == last and not busy:
            stable += 1
            if stable >= 3:
                print(f"[5] 回答已稳定（{time.time()-t0:.1f}s，{idx} 帧）", flush=True)
                break
        else:
            stable = 0
        last = n
        time.sleep(args.interval)

    # 尾部静场 2 秒，便于观看
    t_end = time.time() + 2.0
    while time.time() < t_end:
        grab()
        time.sleep(args.interval)

    print(f"[6] 共 {idx} 帧 → {args.out_dir}", flush=True)
    if args.mp4:
        cmd = ["ffmpeg", "-y", "-framerate", str(args.fps),
               "-i", os.path.join(args.out_dir, "f%05d.png"),
               "-c:v", "libx264", "-pix_fmt", "yuv420p",
               "-vf", "scale=1500:-2", "-movflags", "+faststart", args.mp4]
        subprocess.run(cmd, check=True, capture_output=True)
        size = os.path.getsize(args.mp4)
        dur = idx / args.fps
        print(f"[7] mp4: {args.mp4} {size} bytes（{idx} 帧 @ {args.fps}fps ≈ {dur:.1f}s）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
