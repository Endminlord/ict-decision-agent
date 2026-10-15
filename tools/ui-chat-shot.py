#!/usr/bin/env python3
"""CDP 驱动 Nexent UI：选智能体 → 提问 → 等回答 → 截图。

用法：
  uv run --python 3.10 --with websocket-client python3 ui-chat-shot.py \
      --q "问题" --shot /mnt/c/temp/chat.png --text-out /tmp/chat.txt [--wait 180]
"""
import argparse, base64, json, sys, time, urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222"


def pick_target():
    lst = json.load(urllib.request.urlopen(CDP_HTTP + "/json/list", timeout=10))
    pages = [x for x in lst if x["type"] == "page" and "localhost:3000" in x.get("url", "")]
    if not pages:
        t = json.load(urllib.request.urlopen(
            CDP_HTTP + "/json/new?http%3A%2F%2Flocalhost%3A3000%2Fzh%2Fnewchat", timeout=10))
        return t
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
  const btns = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null);
  return { ok: true, ph: box.placeholder, val: box.value.slice(0, 40),
           buttons: btns.map(b => (b.innerText || b.getAttribute('aria-label') || '').slice(0, 14)).filter(Boolean).slice(0, 10) };
})()
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", required=True)
    ap.add_argument("--agent", default="ICT 演示智能体")
    ap.add_argument("--shot", default="")
    ap.add_argument("--text-out", default="")
    ap.add_argument("--wait", type=float, default=200.0)
    args = ap.parse_args()

    p = Page()
    print("[1] 导航 newchat", flush=True)
    p.js("location.href='http://localhost:3000/zh/newchat'")
    time.sleep(7)
    print("[2] 列表文本:", (p.text() or "")[:160].replace("\n", " | "), flush=True)

    clicked = p.js(
        "(()=>{const t=%s;const els=[...document.querySelectorAll('*')]"
        ".filter(e=>e.children.length===0&&e.innerText&&e.innerText.trim()===t);"
        "if(!els.length)return 'not found';let n=els[0];"
        "for(let i=0;i<6&&n;i++){n.click();if(location.href!=='http://localhost:3000/zh/newchat')break;n=n.parentElement;}"
        "return location.href})()" % json.dumps(args.agent))
    print("[3] 点击智能体:", clicked, flush=True)
    time.sleep(6)
    print("[4] 对话页 URL:", p.js("location.href"), flush=True)
    print("    可见输入:", p.js(
        "JSON.stringify([...document.querySelectorAll('textarea,input[type=text],[contenteditable=true]')]"
        ".filter(e=>e.offsetParent!==null).map(e=>e.tagName+':'+(e.placeholder||'')))"), flush=True)

    res = p.js(ASK.replace("QUESTION", json.dumps(args.q)))
    print("[5] 提交问题:", json.dumps(res, ensure_ascii=False)[:200], flush=True)

    base = len(p.text() or "")
    deadline = time.time() + args.wait
    last = base
    stable = 0
    while time.time() < deadline:
        time.sleep(6)
        txt = p.text() or ""
        n = len(txt)
        busy = p.js("!!document.querySelector('[class*=stop],[aria-label*=停止],[class*=loading],[class*=typing]')")
        print(f"    +{int(time.time()-(deadline-args.wait))}s len={n} busy={busy}", flush=True)
        if n > base + 60 and n == last and not busy:
            stable += 1
            if stable >= 2:
                print("[6] 回答已稳定", flush=True)
                break
        else:
            stable = 0
        last = n

    txt = p.text() or ""
    if args.shot:
        print("[7] 截图:", args.shot, p.shot(args.shot), "bytes", flush=True)
    if args.text_out:
        with open(args.text_out, "w", encoding="utf-8") as f:
            f.write(txt)
        print("[8] 文本落盘:", args.text_out, len(txt), "字符", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
