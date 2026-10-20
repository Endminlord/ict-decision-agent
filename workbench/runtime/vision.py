"""图片这条路：这张图能不能送进去，以及「送进去了」这句话凭什么说得出口。

这个模块只做判断，不发 HTTP、不读环境、也不认识任何厂商的名字。它做四件事：

* 从**字节**认图（不是从文件名）：什么格式、多少像素、多大；
* 说清这张图能不能走：太大、认不出、格式不在名单里，各有各的说法；
* 给「这条路带不带得动图」一个**能证伪**的答案：一张现画的、答案只有它自己知道的图；
* 把「模型到底看没看见」判成一个结论：看见了、答错了、一个字没回、还是模型自己说没收到。

三条不肯让步的地方：

* **收下了 ≠ 看见了。** 网关把图片字段丢掉的时候照样回 200，正文甚至像模像样——
  实测某个网关对着一张 64×64 的图回了「没有收到任何图片，请上传或提供图片路径。」，
  而且这次请求的 prompt_tokens 是 14522。**200 不是证据，模型说得出图里是什么才是证据。**
* **名字不是证据。** 模型名里带 vision / omni，不等于这条路真的把图转发过去了。
  名单是猜的，探针是量的：拿一张答案已知的图问一句，答得出来才算数。
* **认不出就说认不出。** 只认文件头（magic bytes），认不出返回 ``None``，不拿扩展名猜：
  ``photo.png`` 里装的可能是一段文本，也可能什么都没装。
"""

from __future__ import annotations

import base64
import re
import struct
import zlib

# 图片进请求体要先 base64，体积涨三分之一，所以上限比「文件多大都行」小得多。
MAX_IMAGE_BYTES = 4 * 1024 * 1024
# 一次最多带几张。不是安全边界，是「再多就不像一句话了」。
MAX_IMAGES = 4
# 能送的格式。BMP 认得出来，但不在这个名单里——认得出和送得进是两件事。
IMAGE_KINDS = ("png", "jpeg", "gif", "webp")
_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

# 探针问什么、抄字问什么。探针的问法要短：话越多，模型答错越可能不是因为没看见。
PROBE_QUESTION = "这张图里是什么数字？只回答那个数字，不要解释。"
READ_INSTRUCTION = (
    "把这张图里的文字原样抄下来。只输出正文，不要说明、不要总结、"
    "看不清的地方写 [?]，一个字都看不清就回「看不清」。"
)

# 「模型自己说没收到图」的几种说法。这是被网关丢图时最典型的回话，
# 也是 200 之外唯一能拿到手的证据——所以它要单独判一类，不能混进「答错了」。
_NOT_RECEIVED = (
    "没有收到",
    "未收到",
    "没有收到任何图片",
    "没有图片",
    "无法看到",
    "看不到图片",
    "没有附带",
    "请上传",
    "请提供图片",
)


def _size(n: int) -> str:
    """人话的体积：1023 B / 1.0 KB / 3.7 MB。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _dims(width: object, height: object) -> str:
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return f"{width}×{height}"
    return "尺寸没读出来"


def _png_dims(data: bytes) -> tuple[int | None, int | None]:
    # 8 字节签名 + 4 长度 + 4 "IHDR"，宽高就在 16..24。
    if len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return width, height
    return None, None


def _gif_dims(data: bytes) -> tuple[int | None, int | None]:
    if len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return width, height
    return None, None


def _jpeg_dims(data: bytes) -> tuple[int | None, int | None]:
    """扫 SOF 段。JPEG 没有固定偏移，只能一段一段跳过。"""
    i = 2
    end = len(data)
    while i + 9 < end:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            break
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        # SOF0..SOF3 / SOF5..SOF7 / SOF9..SOF11 / SOF13..SOF15 里带尺寸。
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        if length < 2:
            break
        i += 2 + length
    return None, None


def _webp_dims(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    return None, None


def _bmp_dims(data: bytes) -> tuple[int | None, int | None]:
    if len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return abs(width), abs(height)
    return None, None


def sniff(data: object) -> dict | None:
    """从字节认格式和尺寸。认不出返回 ``None``——不拿扩展名猜。

    返回 ``{"kind", "mime", "width", "height", "bytes"}``；宽高读不出来时是 ``None``，
    不是 ``0``：``0×0`` 是一句量过的结论，``None`` 是「没量出来」。
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return None
    raw = bytes(data)
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        kind, dims = "png", _png_dims(raw)
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        kind, dims = "gif", _gif_dims(raw)
    elif raw[:2] == b"\xff\xd8":
        kind, dims = "jpeg", _jpeg_dims(raw)
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind, dims = "webp", _webp_dims(raw)
    elif raw[:2] == b"BM":
        kind, dims = "bmp", _bmp_dims(raw)
    else:
        return None
    return {
        "kind": kind,
        "mime": _MIME[kind],
        "width": dims[0],
        "height": dims[1],
        "bytes": len(raw),
    }


def check_image(
    data: object,
    *,
    limit: int = MAX_IMAGE_BYTES,
    kinds: tuple[str, ...] = IMAGE_KINDS,
) -> dict:
    """这张图能不能送。空 / 太大 / 认不出 / 格式不在名单里，各说各的话。

    通过时 ``ok=True``，``why`` 是一句人能读的事实（格式、像素、体积）。
    """
    if not isinstance(data, (bytes, bytearray)):
        return {"ok": False, "reason": "not_bytes", "why": "这不是一段字节，没法认。", "bytes": 0}
    size = len(data)
    if size == 0:
        return {"ok": False, "reason": "empty", "why": "这一份是空的：一个字节都没有。", "bytes": 0}
    if size > limit:
        return {
            "ok": False,
            "reason": "too_big",
            "why": f"图太大：{_size(size)}，上限 {_size(limit)}。图片要先 base64 才进得去请求体，"
                   f"体积还要再涨三分之一。",
            "bytes": size,
        }
    facts = sniff(data)
    if facts is None:
        return {
            "ok": False,
            "reason": "unreadable",
            "why": "认不出这是什么图：文件头既不是 PNG、JPEG，也不是 GIF、WebP、BMP。"
                   "改名不算——这里只认字节。",
            "bytes": size,
        }
    if facts["kind"] not in kinds:
        return {
            "ok": False,
            "reason": "unsupported",
            "why": f"这是 {facts['kind'].upper()}，不在能送的名单里（{'、'.join(k.upper() for k in kinds)}）。",
            "bytes": size,
            **{k: facts[k] for k in ("kind", "mime", "width", "height")},
        }
    return {
        "ok": True,
        "reason": "",
        "bytes": size,
        **facts,
        "why": f"{facts['kind'].upper()}，{_dims(facts['width'], facts['height'])}，{_size(size)}。",
    }


def data_url(mime: str, payload: bytes) -> str:
    """拼成 ``data:image/png;base64,…``。这一步是格式转换，不是判断。"""
    encoded = base64.b64encode(bytes(payload)).decode("ascii")
    return f"data:{mime or 'application/octet-stream'};base64,{encoded}"


def content_parts(text: str, images: list[dict] | None = None) -> list[dict]:
    """拼一条 user 消息的 content：先文字，再一张张图。

    ``images`` 里每一项是 ``{"mime": ..., "b64": ...}``（``b64`` 是已编码的字符串）
    或 ``{"mime": ..., "bytes": ...}``。形状就是 ``llm_client`` 原样透传的那个形状，
    所以这里不做任何别的事。
    """
    parts: list[dict] = [{"type": "text", "text": text or ""}]
    for image in images or []:
        if not isinstance(image, dict):
            continue
        mime = str(image.get("mime") or "image/png")
        if isinstance(image.get("b64"), str) and image["b64"]:
            url = f"data:{mime};base64,{image['b64']}"
        elif isinstance(image.get("bytes"), (bytes, bytearray)):
            url = data_url(mime, bytes(image["bytes"]))
        else:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def judge_vision(image: object, carries: object, *, model: str = "", endpoint: str = "") -> dict:
    """这张图 + 这条路 = 现在能不能送。

    ``carries`` 是**量出来的**：``True`` 拿已知的图问过、答对了；``False`` 问过、模型说
    没收到；``None`` 没量过。名字不算数，没量过就写 ``None``——那是「不知道」，不是「不行」。

    返回 ``{"ok", "action", "why"}``（``action`` ∈ ``send`` / ``refuse`` / ``probe``；
    没量过时多一个 ``untested: True``，界面据此不涂色、不下结论）。
    """
    if not isinstance(image, dict) or not image.get("ok"):
        why = image.get("why") if isinstance(image, dict) else "这张图没通过检查。"
        return {"ok": False, "action": "refuse", "why": why or "这张图没通过检查。"}

    where = f"（{model}）" if model else ""
    if endpoint:
        where = f"{where} {endpoint}".strip()
    if carries is True:
        return {
            "ok": True,
            "action": "send",
            "why": f"这条路量过：拿一张答案已知的图问过，模型答对了{where}。图可以送。",
        }
    if carries is False:
        return {
            "ok": False,
            "action": "refuse",
            "why": "这条路带不动图：上一次拿一张答案已知的图去问，模型说它没收到图片。"
                   "请求照样回 200，图没进去——换一条出口，或者把图去掉。",
        }
    return {
        "ok": True,
        "action": "probe",
        "untested": True,
        "why": "这条路带不带得动图还没量过。名字里带 vision / omni 不算证据——"
               "先拿一张答案已知的图问一句，答得出来再送。",
    }


# ---------------------------------------------------------------- 自证的图
# 探针用的图是**现画**的：白底黑字，画什么由参数决定，答案只有这张图自己知道。
# 用仓库里的一张固定图片当探针，迟早会有人（或某个缓存）记住答案。
_FONT = {
    "0": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    """手写一张 PNG。纯标准库：zlib + struct。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)  # 每行前面一个 filter 字节
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def probe_card(digits: str = "47", *, scale: int = 8, margin: int = 12) -> dict:
    """现画一张「白底黑字、写着 ``digits``」的 PNG，连同它的问题一起给出去。

    图里是什么，只有调用方和这张图知道——所以模型答对了，就是真看见了。
    默认两位数字：猜中的概率从十分之一降到百分之一。
    """
    text = "".join(ch for ch in str(digits) if ch in _FONT) or "47"
    cols = len(text) * 5 + (len(text) - 1)
    rows_px = 7
    width = margin * 2 + cols * scale
    height = margin * 2 + rows_px * scale
    grid = [["0"] * cols for _ in range(rows_px)]
    for index, char in enumerate(text):
        glyph = _FONT[char]
        for y in range(rows_px):
            for x in range(5):
                if glyph[y][x] == "1":
                    grid[y][index * 6 + x] = "1"
    rows = []
    for y in range(height):
        gy = (y - margin) // scale
        line = bytearray()
        for x in range(width):
            gx = (x - margin) // scale
            ink = 0 <= gy < rows_px and 0 <= gx < cols and grid[gy][gx] == "1"
            line += bytes((0, 0, 0) if ink else (255, 255, 255))
        rows.append(bytes(line))
    png = _png(width, height, rows)
    return {
        "digits": text,
        "png": png,
        "mime": "image/png",
        "width": width,
        "height": height,
        "question": PROBE_QUESTION,
        "why": f"这张图是现画的：{width}×{height}，白底黑字，写着「{text}」。"
               f"答案只有它自己知道——所以它答对了，就是真看见了。",
    }


def _flat(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", "", text)


def says_not_received(reply: object) -> bool:
    """模型是不是在说「我没收到图」。

    单独拎出来，是因为这条判断在**任何**看图请求上都要用：探针要用（答没答对），
    普通问答也要用（那句话是回答，还是「图丢了」的证词）。
    """
    got = _flat(reply)
    return bool(got) and any(word in got for word in _NOT_RECEIVED)


def judge_probe(reply: object, digits: str) -> dict:
    """模型对探针图的回答，算不算「看见了」。

    四类结论分开说，因为它们的**下一步不一样**：没回（预算/接口）≠ 说没收到（图被丢了）
    ≠ 答错了（没看见或在猜）。
    """
    want = _flat(digits)
    if not isinstance(reply, str) or not reply.strip():
        return {
            "ok": False,
            "reason": "empty",
            "why": "模型一个字都没回。推理模型的预算不够时就是这样：200、有 usage、content 是空的。",
        }
    got = _flat(reply)
    if says_not_received(reply):
        return {
            "ok": False,
            "reason": "not_received",
            "why": "模型自己说没收到图片——图在这条路上被丢掉了，而请求照样回了 200。"
                   "这就是「收下了」和「看见了」的区别。",
        }
    if want and want in got:
        return {"ok": True, "reason": "seen", "why": f"模型答的是「{reply.strip()[:40]}」——图里正是「{want}」。它真看见了。"}
    return {
        "ok": False,
        "reason": "wrong",
        "why": f"模型答的是「{reply.strip()[:40]}」，图里是「{want}」。没看见，或者只是在猜。",
    }
