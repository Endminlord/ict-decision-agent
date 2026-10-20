"""按意思找代码：在这个仓库自己的源码里检索，而不是只能按文件名猜。

要解决的问题是：**记得有个东西处理过「记忆读不出来」，但记不得它在哪个文件里。**
`grep -rn "记忆读不出来"` 找得到（那句话在注释里），`grep -rn memory` 给你四十个文件，
而人真正会问的是「**记忆读不出来这件事是谁管的**」——两头都够不着。

它敢叫「按语义找」，是因为**代码里的语义全在名字和注释里**，而这两样恰好是普通
grep 用不好的地方：

1. **切开标识符**。`_memory_entry` 里没有「memory entry」这个子串，`MemoryStore`
   里也没有。切开以后，「记忆条目」才和 `memory entry` 连得上。
2. **注释是正文，不是装饰**。这个仓库每个函数上面都有一段中文说明「为什么」——
   那是整个仓库里语义最密的地方。搜「读不出来」应该找到 `MemoryStore.entries`，
   因为那段注释就写在它头上。
3. **按稀缺度加权**。`memory` 出现在几十个文件里，`broken_lines` 只出现在两个里。
   命中后者比命中前者说明问题得多——所以 idf 不是排序的装饰，idf 就是排序。

**它不是向量检索。** 没有嵌入模型：`requirements.txt` 写着主链只用标准库
（fastapi/httpx/uvicorn 只服务于可选的 `openai_shim.py`）。词面检索加上面这三件事，
在「找一个自己写过的符号」这件事上够用。它**够不着**的地方明写在下面，不藏着：

- **同义词**。搜「缓存」找不到只写了 `cache` 的地方，反之亦然。中英混写的仓库里
  这是个真缺口。
- **跨语言表述**。搜「把秒数换算成时分秒」不会命中 `divmod`，虽然那就是答案——
  没有嵌入，就没有这种跳跃。
- **不看运行时行为**。它只读文件。一个函数在 `__init__.py` 里被重新导出，索引里
  没有任何记录。

范围边界：**这里只找代码。** 工作单、生成物、快照归 `runtime/search.py` 管
（那是「工作台产出过什么」），跨会话记忆归 `runtime/memory.py` 管。三个搜索回答三个
不同的问题，各说各的 `searched`，谁都不假装自己搜了别人的地方。

三条纪律，和 `search.py` / `memory.py` 一致：

1. **搜了哪儿要说出来**——「没找到」和「没搜那儿」在界面上长得一模一样。
2. **截断了要说出来**——「只有五条」和「只给你五条」是两回事。
3. **没搜的地方也要说出来**——`.git`、`__pycache__`、`node_modules`、`state/`
   都不在范围里，明写出来，免得把「索引里没有」当成「这个仓库里没有」。

`attic/` 是**收进来的**，但每条命中会带 `retired: True`。退休 ≠ 删除：代码还在
那儿，就该找得到；但找到的人必须知道它已经退休了，否则会照着一份死代码去改。
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Mapping

from runtime.memory import tokens as _base_tokens

__all__ = [
    "ZONES",
    "ZONE_WEIGHT",
    "NAME_WEIGHT",
    "MAX_FILE_BYTES",
    "expand_identifiers",
    "tokens",
    "index_repo",
    "search_code",
    "main",
]

#: 只索引这些后缀的文本文件。二进制文件不试着解码——猜错编码只会得到一堆乱码词元，
#: 那种词元比没有更糟：它看起来像命中了什么。
TEXT_SUFFIXES = (
    ".py", ".js", ".mjs", ".cjs", ".ts", ".html", ".css", ".md", ".json",
    ".sh", ".bash", ".yml", ".yaml", ".toml", ".txt", ".ini", ".cfg", ".sql",
)

#: 这些目录不进索引。前三个是缓存/依赖/版本库，后两个是**别的搜索的地盘**：
#: `state/` 是工作单和记忆（`storage.py` / `memory.py` 管），`generated/` 是产物。
#: 另外**点开头的目录一律不搜**（见 `_is_skipped`）：`.git`、`.agent-backups`、
#: `.runs` 都是工具的工作区，不是源码。第一次建索引时 `.agent-backups/` 里的
#: 旧版本把 `self_healing_test` 顶到了第 2 名——那是**同一个函数的三份化石**。
SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache",
    "state", "generated", "dist", "build", ".venv", "venv", ".idea", ".vscode",
})

#: 超过这个大小的文件不读——一个巨大的 CHANGELOG 不该让整次检索卡住。
#: 跳过会被记进 `skipped["太大"]`，不是悄悄忽略。
MAX_FILE_BYTES = 1024 * 1024

SNIPPET_CHARS = 160

#: 命中在**名字**上比命中在正文里更说明问题：「memory」出现在 `_memory_entry` 的
#: 名字里，和它出现在某段注释里，是两件不同分量的事。
NAME_WEIGHT = 2.0

#: **切开标识符是为了扩大召回，不是为了让碎片和整词并列。** 搜 `broken_lines`
#: 时，文档里真有 `broken_lines` 这个词的，应该排在只有 `Broken` 这个类的地方前面——
#: 后者只是碎片撞上了。第一次建索引时 `class Broken:` 霸占了前三名，就是这条没设。
FRAGMENT_WEIGHT = 0.25

#: 五个区。找到了只是第一步，下一步是「它在哪个区」——一份死代码和一份活代码
#: 该引出完全不同的动作。
ZONES = ("源码", "测试", "文档", "退休", "其他")

#: 区的权重。不是价值判断，是**排序用的先验**：搜一个符号名时，人想先看到实现，
#: 然后是它的测试，然后是解释它的文档，最后才是退休掉的旧版本。
ZONE_WEIGHT = {"源码": 1.0, "测试": 0.85, "文档": 0.7, "其他": 0.7, "退休": 0.5}

#: 出现在**半数以上**符号里的词元当停用词：它们排不出任何顺序。中文单字最容易撞上
#: 这条线——「读」「不」「出」出现在两千多个符号里，拿它们排序等于随机。
#: 被停用的词会出现在结果的 `stopped` 里：**没拿什么来搜也要说出来**。
STOP_DF_RATIO = 0.5

_SOURCE_ROOTS = frozenset({
    "runtime", "agents", "adapters", "gateway", "tools", "desktop",
})
_TEST_ROOTS = frozenset({"tests", "evals"})
_RETIRED_ROOTS = frozenset({"attic"})
_DOC_ROOTS = frozenset({"docs"})
_SOURCE_FILES = frozenset({
    "server.py", "run.py", "bootstrap.py", "llm_client.py", "openai_shim.py",
    "netguard.py", "index.html",
})

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _split_identifier(name: str) -> list[str]:
    """把一个标识符切成词：``_memory_entry`` → ``[memory, entry]``。

    下划线按分隔符切，驼峰按大小写边界切。``HTTPError`` 该切成 ``HTTP`` + ``Error``
    而不是 ``HTT`` + ``PError``——所以大写的连串整个留着。切完一律**小写**：
    这个函数是为检索服务的，而检索那边（`runtime/memory.py` 的 `tokens`）也是小写，
    两边大小写不一致的话，「搜 `http_error` 找得到 `HTTPError`」就不成立了。
    """
    parts: list[str] = []
    for chunk in name.split("_"):
        if not chunk:
            continue
        parts.extend(_CAMEL.findall(chunk) or [chunk])
    return [p.lower() for p in parts if p]


def expand_identifiers(text: str) -> str:
    """把文本里每个标识符**原地补一份切开的样子**，其余原样留着。

    只补不换：``_memory_entry`` 自己作为一个词元也留着（搜「memoryentry」时它有用），
    切开的那份是**多加**的。切词本身不在这里做——那件事只有 `memory.tokens` 一份实现。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    extra: list[str] = []
    for match in _IDENT.finditer(text):
        parts = _split_identifier(match.group(0))
        if len(parts) > 1:
            extra.append(" ".join(parts))
    return text if not extra else text + "\n" + "\n".join(extra)


def tokens(text: Any) -> set[str]:
    """代码的词元：先切开标识符，再交给 `memory.tokens` 那一套切。"""
    return _base_tokens(expand_identifiers(text) if isinstance(text, str) else text)


def _raw_tokens(text: Any) -> set[str]:
    """**不切开**标识符时切出来的词元——也就是人原样敲进去的那些词。

    两套都要，是因为它们的分量不一样：整词命中是「这个人真写了这个词」，
    切开后的碎片命中是「大概沾边」。见 `FRAGMENT_WEIGHT`。
    """
    return _base_tokens(text)


def _is_skipped(parts: Any) -> bool:
    """路径里任何一段是点开头的目录、或在 `SKIP_DIRS` 里，就不搜。

    点目录是工具的工作区（`.git`、`.agent-backups`、`.runs`），不是源码。
    """
    for part in parts:
        if part in SKIP_DIRS or part.startswith("."):
            return True
    return False


def _zone_of(rel: str) -> str:
    """一条路径属于哪个区。根目录下的 ``*.md`` 算文档，``attic/`` 算退休。

    区是按**整条路径**判的，不是只看第一段：`desktop/tests/test_desktop.py` 的第一
    段是 `desktop`（源码目录），可它是测试——只认头一段，测试文件就会穿着源码的
    身份被当成「这个仓库的写法」递给写代码的 Agent。
    """
    parts = rel.split("/")
    head = parts[0]
    if head in _RETIRED_ROOTS:
        return "退休"
    if _TEST_ROOTS & set(parts[:-1]) or head in _TEST_ROOTS:
        return "测试"
    if head in _DOC_ROOTS:
        return "文档"
    if head in _SOURCE_ROOTS or rel in _SOURCE_FILES:
        return "源码"
    if "/" not in rel and rel.lower().endswith(".md"):
        return "文档"
    return "其他"


# --- 拆符号 -----------------------------------------------------------------


def _iter_defs(node: ast.AST, prefix: str = "") -> Iterator[tuple[ast.AST, str]]:
    """递归吐出一个模块里的每个函数 / 类，带限定名。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qual = f"{prefix}{child.name}"
            yield child, qual
            yield from _iter_defs(child, prefix=f"{qual}.")


def _own_source(node: ast.AST, lines: list[str]) -> str:
    """一个符号**自己**的源码：内嵌定义的正文挖掉。

    不挖的话，一个类的文档会把它的每个方法都装进去，于是搜一个方法名同时命中
    「这个类」和「这个方法」，两票算在同一个东西上。挖掉以后每个符号只算一次。
    """
    start = max(0, getattr(node, "lineno", 1) - 1)
    end = getattr(node, "end_lineno", None) or len(lines)
    span = list(range(start, end))
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child_start = max(0, getattr(child, "lineno", 1) - 1)
            child_end = getattr(child, "end_lineno", None) or child_start + 1
            span = [i for i in span if not (child_start <= i < child_end)]
    return "\n".join(lines[i] for i in span)


def _python_docs(rel: str, text: str) -> list[dict]:
    """Python 文件按符号拆；语法坏掉就退回整文件一份，并**说出来**。"""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [_file_doc(rel, text, kind="文件", note=f"语法读不动（{exc.msg}），整份当一个文档")]
    lines = text.splitlines()
    out: list[dict] = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        out.append({"kind": "模块", "name": Path(rel).stem, "qualname": Path(rel).stem,
                    "line": 1, "text": module_doc})
    for node, qual in _iter_defs(tree):
        kind = "类" if isinstance(node, ast.ClassDef) else "函数"
        own = _own_source(node, lines)
        doc = ast.get_docstring(node) or ""
        # 文档字符串在 `own` 里已经有一份（它就是源码的一部分），不用再加。
        out.append({"kind": kind, "name": node.name, "qualname": qual,
                    "line": getattr(node, "lineno", 1), "text": own + "\n" + doc})
    if not out:
        out.append(_file_doc(rel, text, kind="文件"))
    return out


def _file_doc(rel: str, text: str, *, kind: str = "文件", note: str = "") -> dict:
    doc = {"kind": kind, "name": Path(rel).name, "qualname": Path(rel).name,
           "line": 1, "text": text}
    if note:
        doc["note"] = note
    return doc


# --- 建索引 -----------------------------------------------------------------


def index_repo(root: Any = ".", *, subdirs: Any = None) -> dict:
    """把一棵源码树扫成索引。**文件读不动就跳过**，但**根目录找不到要抛**。

    「这个根目录不存在」和「这个仓库里一个文本文件都没有」是两件事，而
    `Path.rglob` 对前者是**安静地什么都不吐**——不抛的话，打错一个 `--root`
    得到的是「搜了 0 个文件、什么都没找到」，看起来跟「这个仓库里没有」一模一样。

    ``subdirs`` 给了就只扫这几棵子树（测试里用得上，也让「只搜 runtime/」这种
    问题有答案）。
    """
    base = Path(root)
    # 抛 OSError 而不是返回空索引：调用方（`GET /api/code`、命令行）要能把它
    # 变成一个明确的「这个 root 读不了」，而不是一个体面的空结果。
    if not base.is_dir():
        raise FileNotFoundError(f"root is not a directory: {base}")
    picked = list(subdirs) if subdirs else None
    docs: list[dict] = []
    skipped: dict[str, list[str]] = {"太大": [], "不是文本": [], "读不出来": []}
    files = 0
    line_count = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            continue
        parts = path.relative_to(base).parts[:-1]
        if _is_skipped(parts) or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if picked and not any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in picked):
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped["读不出来"].append(f"{rel}: {type(exc).__name__}")
            continue
        if size > MAX_FILE_BYTES:
            skipped["太大"].append(f"{rel}（{size} 字节）")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # 不是文本就不猜编码。猜错会造出一堆像命中的乱码词元。
            bucket = "不是文本" if isinstance(exc, UnicodeDecodeError) else "读不出来"
            skipped[bucket].append(f"{rel}: {type(exc).__name__}")
            continue
        files += 1
        line_count += text.count("\n") + 1
        zone = _zone_of(rel)
        entries = (_python_docs(rel, text) if path.suffix.lower() == ".py"
                   else [_file_doc(rel, text)])
        for entry in entries:
            body = tokens(entry["text"])
            name_tokens = tokens(entry["name"]) | tokens(entry["qualname"])
            docs.append({
                "path": rel, "line": entry["line"], "kind": entry["kind"],
                "name": entry["name"], "qualname": entry["qualname"],
                "zone": zone, "retired": zone == "退休",
                "text": entry["text"], "tokens": body | name_tokens,
                "name_tokens": name_tokens, "note": entry.get("note", ""),
            })
    df: Counter = Counter()
    for doc in docs:
        df.update(doc["tokens"])
    return {
        "root": str(base), "docs": docs, "df": dict(df), "total": len(docs),
        "scanned": {"文件": files, "符号": len(docs), "行": line_count},
        "skipped": skipped,
        "searched": [str(p) for p in (picked or ["."])],
    }


def _idf(df: int, total: int) -> float:
    """稀缺度。``memory`` 出现在几十个文档里所以几乎不加分，``broken_lines`` 只出现
    在两个里所以它一命中就说话。这就是「按意思找」里排序那半边。"""
    return math.log(1.0 + total / max(1, df))


def search_code(query: Any, *, root: Any = ".", limit: int = 10,
                subdirs: Any = None, index: Mapping[str, Any] | None = None) -> dict:
    """按意思找代码。返回形状和 `runtime/search.py` 对齐。"""
    built = dict(index) if index is not None else index_repo(root, subdirs=subdirs)
    docs = built["docs"]
    total = built["total"]
    df = built["df"]
    base = {
        "query": query,
        "hits": [],
        "counts": {"文件": built["scanned"]["文件"], "符号": total, "命中": 0},
        "searched": built["searched"],
        "scanned": built["scanned"],
        "skipped": built["skipped"],
        "truncated": False,
        "note": "",
    }
    wanted = tokens(query)
    typed = _raw_tokens(query)
    base["tokens"] = sorted(wanted)
    if not wanted:
        # 「这句话里没有一个能拿来找的词」和「有词但一条都没命中」是两件事：
        # 前者要人换句话，后者要人换关键词。分开说。
        base["note"] = ("这句话里没有一个能拿来索引的词（标点、单个字母、纯数字都不算）。"
                        "换个说法再问一次，或者给一个符号名。")
        return base
    # 停用词，两种。都不丢，但**只有当这句话里没别的词可用时才轮到它们**——
    # 一句话全是常见词，仍然该有答案，只是答案得说明自己排得不准。
    #   ① 单个汉字：`memory.tokens` 留单字是为了让「一个字」的查询有得匹配，
    #      但「读」「不」「出」在两千多个符号里都有，拿它们排序等于随机。
    #   ② 出现在半数以上符号里的词：排不出任何顺序。
    common = {t for t in wanted
              if (len(t) == 1 and _CJK.match(t))
              or df.get(t, 0) > total * STOP_DF_RATIO}
    useful = wanted - common
    base["stopped"] = sorted(common) if useful else []
    pre_note = ""
    if not useful:
        pre_note = (f"查询里的词都很常见（{'、'.join(sorted(common)[:6])}）——"
                    "它们出现在半数以上的符号里，所以下面的排序基本没有区分度。"
                    "换一个更具体的说法，或者直接给一个符号名，会准得多。")
    scoring_tokens = useful or wanted
    scored: list[dict] = []
    for doc in docs:
        hits = scoring_tokens & doc["tokens"]
        if not hits:
            continue
        score = 0.0
        detail: list[tuple[str, float, bool]] = []
        for token in hits:
            weight = _idf(df.get(token, 0), total)
            # 碎片（切开标识符才得到的词）不是整词命中：搜 `broken_lines` 的人
            # 要的是那个词，不是叫 `Broken` 的类。见 `FRAGMENT_WEIGHT`。
            if token not in typed:
                weight *= FRAGMENT_WEIGHT
            in_name = token in doc["name_tokens"]
            if in_name:
                weight *= NAME_WEIGHT
            score += weight
            detail.append((token, weight, in_name))
        # 按词数做**对数**归一：一个 CHANGELOG 有几万个词元，一点不归它命中任何
        # 稀有词都会压过一个小函数；但按 √ 归又太狠——一个 249 词元的函数里
        # 出现 `broken_lines` 正是要找的东西，不该因为「它比较长」就沉底。
        score *= ZONE_WEIGHT.get(doc["zone"], 0.7)
        score /= 1.0 + math.log(1.0 + len(doc["tokens"]) / 50.0)
        detail.sort(key=lambda item: (-item[1], item[0]))
        named = [t for t, _, flag in detail if flag]
        shown = ", ".join(t for t, _, _ in detail[:3])
        # 只列前三个就得说「还有」——「只有这些」和「只给你这些」是两回事。
        reason = f"命中 {len(detail)} 个词（{shown}{'…' if len(detail) > 3 else ''}）"
        if named:
            reason += f"；名字命中：{', '.join(named[:3])}"
        if doc["zone"] == "退休":
            reason += "；**在 attic/ 里，这是退休的代码**"
        scored.append({
            "path": doc["path"], "line": doc["line"], "kind": doc["kind"],
            "name": doc["name"], "qualname": doc["qualname"], "zone": doc["zone"],
            "retired": doc["retired"], "score": round(score, 4), "reason": reason,
            # `matched` / `named` 是给**调用方**看的，不是给人看的：`reason` 那句话
            # 是排版，谁想按「像不像」做决定，就不该去解析自己刚拼出来的字符串。
            "matched": len(detail), "named": list(named),
            "snippet": _snippet(doc["text"]), "note": doc["note"],
        })
    scored.sort(key=lambda hit: (-hit["score"], hit["path"], hit["line"]))
    capped = max(1, int(limit))
    base["hits"] = scored[:capped]
    base["counts"]["命中"] = len(scored)
    base["truncated"] = len(scored) > capped
    zones = Counter(hit["zone"] for hit in base["hits"])
    pieces = [f"搜了 {built['scanned']['文件']} 个文件、{total} 个符号"]
    if base.get("stopped"):
        stopped = base["stopped"]
        # 列前 6 个，但**说出总数**：只列 6 个而不说还有多少，读者会以为就这 6 个。
        # 这句话是「哪些词没参与排序」的**唯一**出处——界面原样贴它，不再自己拼一遍。
        pieces.append(
            f"太常见、没拿来排序的词：{'、'.join(stopped[:6])}"
            + (f"（共 {len(stopped)} 个）" if len(stopped) > 6 else "")
        )
    if zones:
        pieces.append("命中落在：" + "、".join(f"{z} {n}" for z, n in zones.most_common()))
    if base["truncated"]:
        pieces.append(f"还有 {len(scored) - capped} 条被上限切掉了")
    retired = sum(1 for hit in base["hits"] if hit["retired"])
    if retired:
        pieces.append(f"其中 {retired} 条在 attic/（退休代码，别照着改）")
    # 「`.` 开头的目录一律不搜」也要说出来。实测过：不说的话，`.agent-backups/`
    # 里躺着同一个函数的三代化石，而结果里一个字都不提它——「这个仓库里没有」
    # 和「我没看那儿」就又长成一样了。
    pieces.append("没搜的地方：.git、__pycache__、node_modules、state/、generated/，"
                  "以及任何 `.` 开头的目录")
    tail = "；".join(pieces) + "。"
    base["note"] = f"{pre_note}{tail}" if pre_note else tail
    return base


def _snippet(text: str) -> str:
    """第一行有内容的，掐到 ``SNIPPET_CHARS``。**掐了要说出来**（用省略号）。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > SNIPPET_CHARS:
                return stripped[:SNIPPET_CHARS] + "…"
            return stripped
    return ""


# --- 命令行 -----------------------------------------------------------------


def _format(result: Mapping[str, Any]) -> str:
    lines = [f"查询：{result['query']}", f"词元：{' '.join(result['tokens'])}", result["note"], ""]
    if not result["hits"]:
        lines.append("（一条都没命中）")
        return "\n".join(lines)
    for hit in result["hits"]:
        mark = "⏹" if hit["retired"] else " "
        lines.append(f"{mark} {hit['score']:>8.4f}  {hit['path']}:{hit['line']}  "
                     f"[{hit['zone']}·{hit['kind']}] {hit['qualname']}")
        lines.append(f"            {hit['reason']}")
        lines.append(f"            {hit['snippet']}")
    return "\n".join(lines)


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="按意思找代码（词面检索 + 标识符切分 + idf 加权）")
    parser.add_argument("query", help="要问的话，或者一个符号名")
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument("--subdir", action="append", default=None,
                        help="只搜这几棵子树，可以给多次")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = search_code(args.query, root=args.root, limit=args.limit, subdirs=args.subdir)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else _format(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
