"""跑完一场，往外喊一声。

这个模块只管一件事：**把「这场跑完了」这件事 POST 到一个 URL 上去**。
它不参与运行本身，也**不许影响运行**——一个通知送不出去，不能让一场
跑完的运行变成「失败」。

几条和别处一样的规矩：

- **送出去了和没送出去，不许长得一样。** 返回的字典里 `sent` 是硬事实
  （只有对方回了 2xx 才是 `True`），`reason` 说清为什么没送到。
- **没配地址不是「送失败」。** `configured: False` 是「没打算送」，
  `configured: True, sent: False` 是「打算送但没送到」——这两件事分开说。
- **默认不把需求原文送出去。** 那是别人的服务器；送过去的是编号和结局，
  想看细节的人拿着 `run_id` 在本地看。要改这个默认，得有人明确说。
- **不让收尾等着网络。** 超时到了就认输、写进记录，而不是让一场跑完的
  运行挂在一个不响应的地址上。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

# 5 秒：一场跑十几分钟的运行，收尾多等 5 秒不算什么；可要是对方永远不回，
# 那就不能等——宁可记录里写一句「没送到」。
DEFAULT_TIMEOUT = 5.0


def mask_url(url: str | None) -> str:
    """把地址里可能是密钥的那一段遮掉。

    Webhook 地址**本身就是凭据**（Slack 那种 `/services/T00/B00/XXXX`，
    最后两段就是密码）。所以它能显示，但不能原样显示：留下
    「往哪儿送」的形状，遮掉「凭什么送」的部分。
    """
    text = str(url or "")
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return "***"
    segments = [s for s in parts.path.split("/") if s]
    base = f"{parts.scheme}://{parts.netloc}"
    if len(segments) >= 2:
        return f"{base}/{segments[0]}/***"
    if segments:
        return f"{base}/***"
    return f"{base}/"


def payload(record: Mapping[str, Any] | None) -> dict:
    """把一条运行记录压成**摘要**。

    不送 `artifacts`（里面可能有几百 KB 的代码），也不送 `user_input`
    （那是人写的话，不该默认离开这台机器）。
    """
    run = record or {}
    result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
    retries = run.get("retries")
    stage = run.get("stage") or result.get("stage")
    verification = result.get("verification_level")
    reason = str(result.get("reason") or run.get("error") or "")[:300]
    bits = [str(run.get("status") or "?")]
    if stage:
        bits.append(str(stage))
    if verification:
        bits.append(str(verification))
    text = f"跑完了一场（{run.get('run_id')}）：{' · '.join(bits)}"
    if reason:
        text = f"{text}\n{reason}"
    return {
        "event": "run_finished",
        "run_id": run.get("run_id"),
        "task_id": result.get("task_id") or run.get("task_id"),
        "status": run.get("status"),
        "stage": stage,
        "verification_level": verification,
        "reason": reason,
        # 深度（ask / plan / code）在运行记录**顶层**，不在链子那份结果里——
        # 只读 result 的话真跑出来是 null（第一版就是这样，量出来的）。
        "stage_mode": result.get("stage_mode") or run.get("stage_mode"),
        # 链子没说重试的事，这里就写 None——**「没说」和「说了没有」不是一回事**。
        "retries": len(retries) if isinstance(retries, list) else None,
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "text": text,
    }


class Notifier:
    """一个地址 + 一次 POST。没有地址就什么也不做，且明说自己什么也没做。"""

    def __init__(self, url: str | None = None, timeout: float = DEFAULT_TIMEOUT):
        self.url = str(url or "").strip()
        self.timeout = float(timeout or DEFAULT_TIMEOUT)

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def describe(self) -> dict:
        """给界面看的：配没配、指向哪儿（遮过）、超时多久。"""
        return {
            "configured": self.configured,
            "url": mask_url(self.url),
            "timeout": self.timeout,
        }

    def send(self, record: Mapping[str, Any] | None) -> dict:
        """送一次。**永远不抛**：通知失败不许把一场跑完的运行带崩。"""
        out: dict[str, Any] = {
            "configured": self.configured,
            "sent": False,
            "status": None,
            "reason": "",
            "url": mask_url(self.url),
        }
        if not self.configured:
            out["reason"] = "没有配通知地址，这一场不往外喊"
            return out
        body = json.dumps(payload(record), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                out["status"] = int(getattr(response, "status", 0) or 0)
                out["sent"] = 200 <= out["status"] < 300
                if not out["sent"]:
                    out["reason"] = f"对方回了 {out['status']}，不算送到"
        except urllib.error.HTTPError as exc:
            out["status"] = int(exc.code)
            out["reason"] = f"对方回了 {exc.code}"
        except Exception as exc:  # 连不上、超时、地址写错——都只是「没送到」
            out["reason"] = f"{type(exc).__name__}: {exc}"
        return out
