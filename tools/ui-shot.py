#!/usr/bin/env python3
"""用 CDP 驱动 Windows Chrome（headless）操作 Nexent UI 并截图。

前置：Chrome 已用 --remote-debugging-port=9222 --remote-debugging-address=0.0.0.0 启动。
用法：uv run --python 3.10 --with websocket-client python3 ui-shot.py --help
"""
import argparse, base64, json, sys, time, urllib.parse, urllib.request

import websocket

CDP_HTTP = "http://127.0.0.1:9222"


def http_json(path, method="GET"):
    req = urllib.request.Request(CDP_HTTP + path, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


class Page:
    def __init__(self, url, width=1440, height=1000):
        target = http_json("/json/new?" + urllib.parse.quote(url, safe=""), "PUT")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=120, suppress_origin=True)
        self._id = 0
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=1, mobile=False)

    def send(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            return {"__error__": str(r["exceptionDetails"])[:400]}
        return r.get("result", {}).get("value")

    def text(self):
        return self.js("document.body.innerText") or ""

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        data = base64.b64decode(r["data"])
        with open(path, "wb") as f:
            f.write(data)
        return len(data)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


FILL_AND_LOGIN = r"""
(() => {
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const inputs = [...document.querySelectorAll('input')].filter(i => i.type !== 'hidden');
  const out = { inputs: inputs.map(i => i.type + ':' + (i.placeholder || i.name || '')) };
  if (inputs.length < 2) { out.error = 'less than 2 inputs'; return out; }
  setVal(inputs[0], EMAIL);
  setVal(inputs[1], PASSWORD);
  const btns = [...document.querySelectorAll('button')];
  const login = btns.find(b => /登\s*录|Login|Sign in/i.test(b.innerText));
  out.buttons = btns.map(b => b.innerText.trim()).filter(Boolean).slice(0, 12);
  if (!login) { out.error = 'no login button'; return out; }
  login.click();
  out.clicked = login.innerText.trim();
  return out;
})()
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3000/")
    ap.add_argument("--email", default="ict@example.com")
    ap.add_argument("--password", default="ict-demo-pass")
    ap.add_argument("--wait", type=float, default=6.0, help="登录后等待秒数")
    ap.add_argument("--shot", default="")
    ap.add_argument("--text-out", default="")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    p = Page(args.url)
    time.sleep(4)
    print("[1] 初始页面文本前 200 字:", (p.text() or "")[:200].replace("\n", " | "))
    script = FILL_AND_LOGIN.replace("EMAIL", json.dumps(args.email)).replace(
        "PASSWORD", json.dumps(args.password))
    res = p.js(script)
    print("[2] 填表结果:", json.dumps(res, ensure_ascii=False)[:600])
    time.sleep(args.wait)
    print("[3] 登录后 URL:", p.js("location.href"))
    txt = p.text() or ""
    print("[4] 登录后文本前 300 字:", txt[:300].replace("\n", " | "))
    if args.shot:
        n = p.shot(args.shot)
        print("[5] 截图:", args.shot, n, "bytes")
    if args.text_out:
        with open(args.text_out, "w", encoding="utf-8") as f:
            f.write(txt)
        print("[6] 文本落盘:", args.text_out, len(txt), "字符")
    if not args.keep_open:
        p.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
