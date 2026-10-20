"""把提示词里的 `@路径` 找出来，读成一段能递给模型的正文。

**为什么要有它**：`runtime/code_index.py` 做的是**猜**——按词面找「像这次需求」的
代码，找不着就不给。@引用是**说**：人指着某个文件说「照这个来」。猜错会给人错的
例子（W10 的三道闸门就是被这件事逼出来的），而人指的那一个不会错。所以这两件事
不合并，一个负责猜、一个负责听；同时命中时，**听来的压过猜来的**。

**它不是读文件的口子**：能读的只有仓库里的普通文本文件，路径解析后必须还在仓库
里面，超长/二进制/不存在都如实说，不假装读到了一段空正文。「人指的那个文件不
存在」是用户需要知道的信息，不是可以吞掉的错误。

只用标准库——`requirements.txt` 写着主链（`runtime/`、`agents/`、`run.py`、
`bootstrap.py`、`llm_client.py`）不引第三方依赖。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 一句话里最多认几个引用。多过这个数的，多出来的不是上下文，是噪声——而且
# 每一段都要进模型的开销里。没读的那几个会写进 `note`，不装作没看见。
REF_LIMIT = 3
# 单个引用最多取多少字符。超了要截断并说明截了多少。
REF_CHARS = 2000
# 只写一个行号（`@f.py:30`）时，前后各读多少行。一「处」从来不止一行。
REF_WINDOW = 15
# 超过这个大小的文件不读。读它不是为了备份，是为了给模型看一眼写法。
MAX_FILE_BYTES = 256 * 1024

# `@` 前面必须是行首或空白：邮箱、`foo@bar`、装饰器都不算引用。
_REF = re.compile(r"(?:^|(?<=\s))@([^\s@]+)")
# `runtime/memory.py:30` / `runtime/memory.py:30-60` / `runtime/memory.py`
_SPAN = re.compile(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")
# 中文标点。中文句子没有空格，`照 @runtime/memory.py，然后` 里的逗号会被
# `[^\s@]+` 一起吞进来——真踩到了：引用变成 `runtime/memory.py，然后`，
# 然后报「仓库里没有这个文件」。
_STOP = "，。、；：？！（）「」『』【】《》〈〉…—～·"
# 句末的标点。`@foo.py.` 里的句号不是文件名的一部分。
_TAIL = ".,;:!?)]}\"'"


def _trim(spec: str) -> str:
    """把粘在引用尾巴上的标点削掉。"""
    for index, char in enumerate(spec):
        if char in _STOP:
            spec = spec[:index]
            break
    while spec and spec[-1] in _TAIL:
        cut = spec[:-1]
        # `v1.2` 里的点是名字的一部分，句末的句号不是。分不出来的时候不动它：
        # 去掉之后还剩下另一个点（也就是还像个带后缀的文件名）才削。
        if spec[-1] == "." and "." not in cut:
            break
        spec = cut
    return spec


def find_refs(text: Any) -> list[str]:
    """按出现顺序找出 `@引用` 的原文（不含 `@`），重复的只留第一次。

    一个引用至少要**像个文件**：带斜杠或带点。`@某人`、`@todo`、`@2026` 不是
    引用——把随手写的 `@` 当成路径去读，会读出一堆「没有这个文件」的噪声，
    而噪声会让人不再相信真正的「没有这个文件」。
    """
    if not isinstance(text, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _REF.finditer(text):
        spec = _trim(match.group(1))
        if "/" not in spec and "." not in spec:
            continue
        if spec in seen:
            continue
        seen.add(spec)
        out.append(spec)
    return out


def split_spec(spec: str) -> tuple[str, int | None, int | None]:
    """把 `runtime/memory.py:30-60` 拆成路径与行范围。

    行号是**可选**的：写 `@runtime/memory.py` 是「照这个文件」，写
    `@runtime/memory.py:30` 是「照这一个函数」。后者更准，所以支持它。
    """
    match = _SPAN.match(spec)
    if match is None:                      # pragma: no cover - 正则收得住所有串
        return spec, None, None
    path = match.group("path")
    start = match.group("start")
    end = match.group("end")
    first = int(start) if start else None
    last = int(end) if end else first
    if first is not None and last is not None and last < first:
        first, last = last, first
    return path, first, last


def _inside(target: Path, base: Path) -> bool:
    """路径解析之后还在不在仓库里。

    `..` 和软链接都要过这一关：`@../../etc/passwd` 是一句提示词，不是一张
    通行证。解析之后再比，是为了让「绕一圈回到仓库里」也照样算在里面。
    """
    try:
        return target.is_relative_to(base)
    except (OSError, ValueError):          # pragma: no cover - 解析失败就是不在里面
        return False


def resolve_ref(spec: str, *, root: Any = ".", chars: int = REF_CHARS) -> dict:
    """把一个引用读成正文。读不到也返回一条记录，`ok` 是 `False`、`note` 说为什么。"""
    base = Path(root).resolve()
    path_part, first, last = split_spec(spec)
    record: dict[str, Any] = {
        "spec": spec, "path": path_part, "start": first, "end": last,
        "ok": False, "lines": 0, "chars": 0, "text": "", "note": "",
    }
    target = (base / path_part).resolve()
    if not _inside(target, base):
        record["note"] = "这个路径解析之后跑到仓库外面去了，没读"
        return record
    if not target.is_file():
        record["note"] = "仓库里没有这个文件" if not target.exists() else "这个路径不是文件"
        return record
    try:
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            record["note"] = f"这个文件 {size} 字节，超过 {MAX_FILE_BYTES} 的上限，没读"
            return record
        raw = target.read_bytes()
    except OSError as exc:
        record["note"] = f"读不出来：{type(exc).__name__}: {exc}"
        return record
    if b"\x00" in raw[:8192]:
        record["note"] = "看着不是文本文件，没读"
        return record
    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()
    window = None
    if first is not None:
        # 行号从 1 数起，越界的那一头按文件边界收——不报错，但说清收窄到哪儿。
        begin = max(1, first)
        finish = min(len(all_lines), last if last is not None else first)
        if begin > len(all_lines):
            record["note"] = f"这个文件只有 {len(all_lines)} 行，{first} 行在外面"
            return record
        if first == last:
            # **只写一个行号时读它前后各一段**。写 `@f.py:30` 的人要的是「这一处」，
            # 而一「处」从来不止一行——真按一行给，递过去的就是一个孤零零的
            # `def tokens(text):`。给了多少写在 note 里，不装作就是第 30 行。
            window = REF_WINDOW
            begin = max(1, first - REF_WINDOW)
            finish = min(len(all_lines), first + REF_WINDOW)
        picked = all_lines[begin - 1:finish]
    else:
        picked = all_lines
    body = "\n".join(picked)
    if window is not None:
        where = f"第 {first} 行前后各 {window} 行（取到第 {begin}–{finish} 行）"
    elif first is not None:
        where = f"第 {first}–{last} 行"
    else:
        where = f"整个文件，{len(picked)} 行"
    cut = len(body) - chars
    note = where
    if cut > 0:
        body = body[:chars]
        note = f"{where}；太长，只取了前 {chars} 个字符，还有 {cut} 个没给"
    record.update({
        "ok": True,
        "lines": len(picked),
        "chars": len(body),
        "text": body,
        "note": note,
    })
    return record


def collect(text: Any, *, root: Any = ".", limit: int = REF_LIMIT) -> dict:
    """把一句话里的引用全读出来。

    `prompt` 原样留着——**不改用户说的话**。工作单里记的 `user_input` 必须是
    人真正写下的那一句；引用读出来的正文是**另外一笔**，分开记，才能复核
    「它当时看到的到底是哪一段」。
    """
    specs = find_refs(text)
    taken, dropped = specs[:limit], specs[limit:]
    refs = [resolve_ref(spec, root=root) for spec in taken]
    notes = [f"{ref['spec']}：{ref['note']}" for ref in refs if not ref["ok"]]
    if dropped:
        notes.append(
            f"一句话里最多认 {limit} 个引用，后面 {len(dropped)} 个没读："
            + "、".join(dropped)
        )
    return {
        "specs": specs,
        "refs": refs,
        "missing": [ref["spec"] for ref in refs if not ref["ok"]],
        "note": "；".join(notes),
    }


def as_block(refs: list[dict]) -> str:
    """把读到的引用拼成一段能塞进提示词的正文。一段都没读到就返回空串。"""
    usable = [ref for ref in refs if ref.get("ok")]
    if not usable:
        return ""
    parts = [
        "下面是人指名要看的代码。它比按词面猜出来的例子更该信：照着它的写法来，"
        "别改它的逻辑。",
    ]
    for ref in usable:
        where = ref["spec"]
        parts.append(f"--- {where} ---\n{ref['text']}")
    return "\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    """`python3 -m runtime.context_refs "照 @runtime/memory.py:30 写一个切词函数"`"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    root = "."
    rest: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == "--root" and index + 1 < len(args):
            root = args[index + 1]
            index += 2
            continue
        rest.append(args[index])
        index += 1
    if not rest:
        print("用法：python3 -m runtime.context_refs [--root 目录] 一句话")
        return 2
    result = collect(" ".join(rest), root=root)
    for ref in result["refs"]:
        mark = "✓" if ref["ok"] else "✗"
        print(f"{mark} @{ref['spec']}  {ref['note']}")
    if result["note"]:
        print(result["note"])
    return 0 if not result["missing"] else 1


if __name__ == "__main__":                 # pragma: no cover
    raise SystemExit(main())
