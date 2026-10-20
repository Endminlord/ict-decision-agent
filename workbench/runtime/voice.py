"""语音这条路：谁在听、谁在说，以及说不出来的时候怎么把原因说清楚。

这个模块只做判断，不发 HTTP、不碰麦克风、也不认识任何厂商的名字。浏览器报上来的
「事实」（有没有识别接口、上一次是不是不吭声、挂了几个语音、有没有麦克风）和服务端
量出来的「后端」（配没配 STT / TTS 地址）都从参数进来，出去的只有结论和一句能给人
看的理由。

三条不肯让步的地方：

* **构造函数在，不等于能用。** Chromium 里 ``SpeechRecognition`` 一直都在，但
  ``start()`` 之后既没有 ``onstart`` 也没有 ``onerror``——它连不上识别服务，而且
  **不吭声**。只看 ``typeof`` 就报「可用」，用户按下按钮得到的是一段沉默，然后以为
  是自己不会用。所以「上一次是不是静默失败」是事实的一部分，不是细节。
* **没报的事实不是「没有」。** 没量过就写 ``None``：``False`` 是「量过了，没有」，
  ``None`` 是「不知道」。把后者读成前者，界面就会替用户宣布一件它没查过的事。
* **录下来 ≠ 有地方送。** 麦克风能用、浏览器也能录，但后端一个音频接口都没配的时候，
  那条路只到「录完了，没地方送」为止。这个结论要能单独说出来，而不是等上传失败才暴露。
"""

from __future__ import annotations

import json
import re

# 三个上限。不是安全边界，是「超过这个量就不是一句话了」：
# 语音整理只该处理一段口述，TTS 也不该把一整份日志念出来。
MAX_SPOKEN_CHARS = 4000
MAX_REPLY_CHARS = 6000
# 识别器多久没动静算「它不吭声」。Chromium 的静默失败不会给任何事件，
# 所以这条只能靠时间量出来——它是事实的一部分，见模块开头第二条。
SILENT_WATCHDOG_SECONDS = 6

ENGINE_BROWSER = "browser"
ENGINE_SERVER = "server"
ENGINE_NONE = "none"

_TRUTHY = ("1", "true", "yes", "on")
# 空串**不在**这里：空的是「没报」，不是「报了个否」。把没报读成 False，界面就会
# 说「这台机器没有麦克风」——那是替浏览器编了一个它没说过的结论。（真踩过：
# 请求不带参数时 mic 报成了 false，而不是 None。）
_FALSY = ("0", "false", "no", "off")
_STRIP_PREFIXES = ("整理后：", "整理后:", "整理：", "整理:", "转写：", "转写:")
# 成对的引号。中文的引号**不是同一个字符**（「」是两个字），所以不能拿
# 「首尾相同」当判断——那样只有直引号能剥掉，模型最常加的中文引号一个都剥不掉。
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}


def _tri(value: object) -> bool | None:
    """三态读法：True / False / None（没报、认不出）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSY:
            return False
    return None


def _count(value: object) -> int | None:
    """数出来个数；没报或认不出给 None，不拿 0 冒充「一个都没有」。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def read_facts(raw: object) -> dict:
    """浏览器量出来的事实 → 规范形式。四项：识别接口 / 静默失败 / 语音数 / 麦克风。

    ``silent`` 是「上一次真的按下去之后，它既没开始也没报错」——静默失败本身也是
    一条量过的事实，所以它和「没测过」必须分得开。
    """
    source = raw if isinstance(raw, dict) else {}
    return {
        "recognition": _tri(source.get("recognition")),
        "silent": _tri(source.get("silent")),
        "voices": _count(source.get("voices")),
        "mic": _tri(source.get("mic")),
    }


def _verdict(engine: str, ok: bool, action: str, why: str, **extra: object) -> dict:
    return {"engine": engine, "ok": ok, "action": action, "why": why, **extra}


def judge_stt(facts: dict, backends: dict) -> dict:
    """谁在听。``action`` 是界面该做的事：``recognise`` 用浏览器识别、``record`` 录下来
    发给服务端、``none`` 两条路都不通（``why`` 里就是不通的原因）。"""
    facts = facts or {}
    backends = backends or {}
    server_url = str(backends.get("stt") or "")

    # 麦克风两条路都要。没有就到此为止——否则后面会报「识别不可用」，
    # 而真正的原因是这台机器没有麦。
    if facts.get("mic") is False:
        return _verdict(ENGINE_NONE, False, "none",
                        "这台机器上没有麦克风，说话这条路断在最前面。")

    recognition = facts.get("recognition")
    if recognition is True:
        if facts.get("silent") is True:
            browser_why = (
                "浏览器有识别接口，但它不回话——上一次 start 之后既没有开始事件、"
                "也没有报错。识别服务连不上（多半是浏览器没带那家云端的钥匙）。"
            )
        elif facts.get("silent") is None:
            return _verdict(ENGINE_BROWSER, True, "recognise",
                            "浏览器自带识别接口；它到底回不回话还没测过——按下去等 "
                            f"{SILENT_WATCHDOG_SECONDS} 秒，不吭声就换路。",
                            untested=True)
        else:
            return _verdict(ENGINE_BROWSER, True, "recognise",
                            "浏览器自带的识别接口（上一次真的回话了）。")
    elif recognition is None:
        return _verdict(ENGINE_BROWSER, True, "recognise",
                        "还没量过这个浏览器有没有识别接口——先量再报，不替它宣布。",
                        untested=True)
    else:
        browser_why = "这个浏览器没有识别接口（SpeechRecognition 不存在）。"

    if server_url:
        return _verdict(ENGINE_SERVER, True, "record",
                        f"浏览器这条不行：{browser_why} 录下来发给 {server_url}。")
    return _verdict(ENGINE_NONE, False, "none",
                    f"{browser_why} 服务端也没配音频后端（VOICE_STT_URL 空着），"
                    "录下来没地方送。")


def judge_tts(facts: dict, backends: dict) -> dict:
    """谁在说。``action``：``speak`` 用浏览器自带语音、``fetch_audio`` 找服务端要音频、
    ``none`` 念不出来（``why`` 里说清为什么）。"""
    facts = facts or {}
    backends = backends or {}
    server_url = str(backends.get("tts") or "")
    voices = facts.get("voices")

    if voices is None:
        return _verdict(ENGINE_NONE, False, "none",
                        "还不知道这台浏览器挂了几个语音——先量再报。", untested=True)
    if voices > 0:
        return _verdict(ENGINE_BROWSER, True, "speak",
                        f"浏览器自带 {voices} 个语音，直接念。")
    if server_url:
        return _verdict(ENGINE_SERVER, True, "fetch_audio",
                        "浏览器一个语音都没挂（这台机器上没有 speech-dispatcher 之类），"
                        f"改问 {server_url} 要音频。")
    return _verdict(ENGINE_NONE, False, "none",
                    "念不出来：浏览器一个语音都没挂，服务端也没配 VOICE_TTS_URL。")


def route_for(model: str, *, gateway_model: str, gateway_base: str = "") -> dict:
    """这个模型该走哪条出口。

    规则只有一条：**模型是网关自己那份就走网关**（那条额度不要钱），别的照它自己的
    出口走。判断放内核里、地址由调用方从环境里读进来——内核不认厂商，也不读环境。
    """
    model = str(model or "").strip()
    gateway_model = str(gateway_model or "").strip()
    where = f"（{gateway_base}）" if gateway_base else ""
    if model and gateway_model and model == gateway_model:
        return {"route": "gateway", "model": model,
                "why": f"{model} 走网关{where}：这条额度不要钱。"}
    if model:
        return {"route": "provider", "model": model, "why": f"{model} 走它自己的出口。"}
    if gateway_model:
        return {"route": "gateway", "model": gateway_model,
                "why": f"没点名模型，就用网关默认的 {gateway_model}{where}。"}
    return {"route": "none", "model": "", "why": "没点名模型，网关也没设默认模型。"}


TIDY_INSTRUCTION = (
    "下面是一段语音转写出来的话，可能有错别字、断句乱、口语重复。"
    "把它整理成通顺的中文，只做四件事：\n"
    "1. 改错别字、标点和语序；不许添加原文没有的信息，也不许删掉原文有的意思；\n"
    "2. 专业名词、代码、路径、数字照抄，一个字都不许改写；\n"
    "3. 听不清、认不出是什么的地方原样留着，不要猜、不要补；\n"
    "4. 只输出整理后的正文，不要解释、不要加引号、不要复述这些要求。\n"
)


def tidy_prompt(text: str) -> str:
    """整理那段话时真正发出去的提示词。原文放最后，免得它被要求盖住。"""
    return f"{TIDY_INSTRUCTION}\n---\n{str(text or '').strip()}"


def spoken_check(text: str) -> dict:
    """要整理的原文够不够格。空的不发（发了就是拿一次调用换一句空话）。"""
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "reason": "empty", "why": "没有要整理的原文，一次调用都没发。"}
    if len(body) > MAX_SPOKEN_CHARS:
        return {"ok": False, "reason": "too_long",
                "why": f"原文 {len(body)} 字，超过 {MAX_SPOKEN_CHARS} 字——"
                       "这已经不是一段口述了，先自己删一删。"}
    return {"ok": True, "reason": "", "why": ""}


def _unwrap_json(text: str) -> str | None:
    """把「正文被包在 JSON 里」这种情况拆出来；拆不出正文给 None。

    这是真实会遇到的：让它整理一句话，它回 ``{"text": "整理好的话"}``——好答案
    换了层皮。也有只回一个 ``{}`` 的，那不是正文。两种都得认出来，不能原样塞进
    用户的输入框。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, list):
        parts = [item for item in data if isinstance(item, str) and item.strip()]
        return "\n".join(parts).strip() or None
    if isinstance(data, dict):
        for key in ("text", "result", "content", "answer", "整理后", "正文"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def clean_tidy(raw: str) -> dict:
    """模型回的话 → 能直接放进输入框的正文。

    模型很爱加「整理后：」和整段引号，这两样都不是内容。还有一层皮是 JSON——正文
    被包在里面的时候要拆出来，只回一个 ``{}`` 的时候要认得这不是正文。认不出、回空
    的，如实说回空的：输入框一个字都不动，绝不把模型的排版事故当成用户说的话。
    """
    text = str(raw or "").strip()
    if not text:
        return {"ok": False, "text": "", "truncated": False, "reason": "empty",
                "why": "模型一个字都没回，输入框不动。"}
    if text[:1] in ("{", "["):
        unwrapped = _unwrap_json(text)
        if unwrapped is None:
            return {"ok": False, "text": "", "truncated": False, "reason": "not_prose",
                    "why": "模型回的不是正文，是一段 JSON（里面没有正文），输入框不动。"}
        text = unwrapped
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    if len(text) >= 2 and text[-1] == _QUOTE_PAIRS.get(text[0], ""):
        text = text[1:-1].strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > MAX_REPLY_CHARS:
        return {"ok": True, "text": text[:MAX_REPLY_CHARS].rstrip(), "truncated": True,
                "reason": "truncated",
                "why": f"整理后的正文超过 {MAX_REPLY_CHARS} 字，截到这里。"}
    return {"ok": True, "text": text, "truncated": False, "reason": "", "why": ""}
