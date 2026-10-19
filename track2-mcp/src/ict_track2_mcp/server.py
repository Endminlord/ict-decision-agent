"""引用回查 MCP 服务（stdio，零第三方依赖）。

三个工具：
  lookup_chunk  —— 给定 chunk_id，返回原文片段与文档元信息
  verify_quote  —— 给定引用文本，验证它是否真的出现在语料里
  list_sources  —— 按主题列出语料覆盖的文档与来源统计

**为什么手写 JSON-RPC 而不用 mcp SDK**：
MCP 的 stdio 协议核心很小（initialize / tools/list / tools/call 三个方法 +
换行分隔的 JSON-RPC），而 SDK 在 1.x→2.x 之间改过 API（`FastMCP` 改名 `MCPServer`）。
手写让本包**零依赖**：AgentArts 沙箱里 `uvx` 装包更快，也不会因为 SDK 版本不匹配而启动失败。
协议正确性由 `tests/test_mcp_protocol.py` 用真实 stdio 往返验证。

设计取舍：返回「片段 + 精确定位」而不是全文——这正是「可回查」的定义
（能定位到原文的哪一块、哪一偏移），全文由知识库侧提供。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .lookup import MIN_QUOTE_CHARS, Corpus

SERVER_NAME = "ict-track2-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

_corpus: Corpus | None = None


def corpus(path: str | None = None) -> Corpus:
    """语料单例。首次调用时加载，`path` 只在第一次生效。

    数据路径优先级：显式参数 > 环境变量 `ICT_TRACK2_DATA` > 包内默认。
    与领域无关——换任何行业的语料都不用改代码。
    """
    global _corpus
    if _corpus is None:
        _corpus = Corpus(path) if path else Corpus()
    return _corpus


TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_chunk",
        "description": (
            "按 chunk_id 精确回查语料块，返回原文片段、文档标题、日期、来源与字符偏移。"
            "用于把检索结果里的 chunk_id 变成人可核对的原文依据。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chunk_id": {
                    "type": "string",
                    "description": "语料块 ID，形如 be1e65625142f499-0003",
                }
            },
            "required": ["chunk_id"],
        },
    },
    {
        "name": "verify_quote",
        "description": (
            "验证一段引用文本是否真的出现在语料中。返回所有命中位置（含 chunk_id、"
            "文档标题、字符偏移、匹配方式）。`verified=true` 表示可以作为原文引用；"
            "`verified=false` 且 `partial=true` 表示内容确实在语料里、但省略号吞掉了"
            "否定/转折词或省略跨度过大（>40 字），**不能当原文引用**（风险见 "
            "`risk_flags`：omitted_negation / long_omission）；命中为空表示该引用无法"
            "在语料中核实——此时不应把它当作依据写进答案。`specificity` 是命中块数占"
            "语料比例，过高（`generic=true`）说明引用太泛、没有区分度。`fragment=true` "
            "表示引用短于 12 字，只能证明这些字出现过、证明不了整句语境；此时 hit 里的 "
            "`context_sentence`（命中所在原句）与 `sentence_negation` 供你核对——"
            "若原句含否定而引用不含，即使 verified=true 也不要直接引用。"
            "`omitted` 是省略号实际吞掉的原文，可用来判断省略是否改变了句意。"
            "`hit_count` 是全量命中数，`returned_hits` 是本次实际返回的条数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "quote": {
                    "type": "string",
                    "description": ("待验证的引用文本（规范化后至少 4 个字符；"
                                    "更短无法核验，会返回 short=true 而不是「不存在」）"),
                },
                "max_hits": {
                    "type": "integer",
                    "description": ("最多返回多少条命中，默认 10。只影响返回列表长度——"
                                    "风险扫描（omitted_negation / long_omission）"
                                    "始终对全量命中进行，与这个值无关"),
                    "default": 10,
                },
            },
            "required": ["quote"],
        },
    },
    {
        "name": "list_sources",
        "description": (
            "列出语料覆盖的文档与来源统计（按来源、按年份、按文档）。"
            "可传 topic 限定只在标题或正文中含该词的块上统计；不传则统计全量。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "可选主题词：2-6 字的专名或术语，不要传整句问句",
                    "default": "",
                },
                "top": {
                    "type": "integer",
                    "description": "每类最多返回多少条，默认 30",
                    "default": 30,
                },
            },
        },
    },
]


_TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


def _call_tool(name: str, arguments: dict[str, Any]) -> tuple[dict, bool]:
    """执行工具，返回 `(payload, is_error)`。

    失败与否由**这一层**显式决定，不靠 payload 里有没有 `error` 键去猜：
    一个正常返回带 `error_count` 统计字段的工具，会让 `"error" in payload`
    把它误标成失败 —— 隐式契约，加个字段就翻车（M2）。
    """
    try:
        if name == "lookup_chunk":
            cid = arguments.get("chunk_id")
            # 统一成字符串再查、再回传：schema 声明 chunk_id 是 string，
            # 传数组/null 时不能把原值原样塞回返回体（调用方按 schema 解析会炸）。
            cid_s = cid if isinstance(cid, str) else ("" if cid is None else str(cid))
            c = corpus().lookup(cid_s)
            if c is None:
                return {
                    "found": False,
                    "chunk_id": cid_s,
                    "message": "语料中不存在该 chunk_id，该引用无法核实。",
                }, False
            return {"found": True, **c.to_dict(corpus().snippet_limit),
                    "text": c.text}, False

        if name == "verify_quote":
            quote = str(arguments.get("quote", ""))
            # B1：风险扫描在 verify_report 里对**全量命中**做，max_hits 只管
            # 返回几条。这里不再从 hits 反推风险——那样 max_hits=10 会把排在第
            # 11 条的风险静默丢掉，同一条引用因展示参数改判。
            rep = corpus().verify_report(
                quote, max_hits=int(arguments.get("max_hits", 10) or 10))
            hits = rep.hits
            # 区分「逐字命中」与「省略号分段命中」：后者内容确实在语料里，
            # 但不是逐字原文，调用方（或人）据此决定是否要求模型给原文。
            verbatim = rep.verbatim
            # 命中不等于可引用：省略号可以吞掉否定词（语义反转），也可以把相距
            # 上千字的两段拼起来（拼接）。这两种情况下「内容在语料里」仍然成立，
            # 但**不能当原文引用** —— 用 partial 表达「在，但不可用」。
            risk_flags: list[str] = []
            if rep.omitted_negation:
                risk_flags.append("omitted_negation")
            if rep.long_omission:
                risk_flags.append("long_omission")
            # 省略号后的短段没参与核验：命中成立，但引用的这部分内容完全没被
            # 证明过 —— 不能当原文引用（M6）。
            if rep.dropped_segments:
                risk_flags.append("unverified_segment")
            # R1/A2（第二轮对抗）：整句含否定/转折框架而引用不含 —— 引用是
            # 禁止性条款的一截，读出来是陈述句（原文「不得把规模条件作为资格
            # 要求」被引成「把规模条件作为资格要求」，语义 180° 反转）。
            # 这条以前只是 fragment 时的一句 message 提示，非 fragment 时连提示
            # 都没有（A2 的引用 54 字，`sentence_negation=True` 是死字段）。
            # 只在**非 fragment** 时升级为风险：短专名（「市场准入负面清单」）
            # 的命中块里必然混有带否定的邻句，一刀切会把合法引用全部误伤。
            # 判定用比例而不是存在性：条款片段的命中块几乎全带否定（≈1.0），
            # 高频短语只是邻句噪声（实测「优化营商环境条例」4.75%、
            # 「一网通办」10.85%）。用 any 语义会把合法引用全部误伤。
            if rep.sentence_neg_ratio >= 0.5 and not rep.fragment:
                risk_flags.append("context_negation")
            # R1/A5（第二轮对抗）：引用停在「不得少于」这类量词比较词上，原文
            # 紧接着是数字。数量是这句话的全部信息量 —— 截掉数字后引用只剩
            # 「要求至少/至多」的骨架，模型可以任意补数而仍显示「逐字可核实」
            # （A5：17 字，连 fragment 标记都不给）。
            if rep.dangling_comparison:
                risk_flags.append("dangling_comparison")
            # R4（第三轮对抗 A3/A4/A8/H1/B3/O1/O2/B1/B2）：引用是原文的逐字
            # 子串，但把贴在子串边界外侧的限定语丢掉了 —— 「除银团贷款外，不得…」
            # 引成「不得…」，「原则上不再依据…」引成「不再依据…」。字面全对，
            # 范围/条件/例外/强度全丢，返回体与合法引用一模一样。
            if rep.dropped_qualifiers:
                risk_flags.append("dropped_qualifier")
            partial = bool(risk_flags)
            verified = bool(hits) and not partial
            if rep.short:
                # 「太短不查」不等于「语料里没有」。混用同一句话会把真实的
                # 4-5 字专名说成编造 —— 把真的说成假的比漏报更糟。
                msg = (f"引用规范化后仅 {rep.qlen} 字，短于可核验下限"
                       f"（{MIN_QUOTE_CHARS} 字），无法核验——这不等于语料中"
                       "不存在，请补充成完整句子再查。")
            elif not hits:
                msg = "语料中未找到该引用，不能作为依据使用。"
            elif "omitted_negation" in risk_flags:
                words = rep.negation_words
                msg = ("引用内容在语料中存在，但省略或边界处含否定/转折词（原文侧："
                       + "、".join(words[:5])
                       + "），引用把否定框架去掉了，语义可能反转——不能当原文引用。")
            elif "long_omission" in risk_flags:
                msg = (f"引用内容在语料中存在，但被省略的原文跨度达 {rep.gap_max} 字"
                       "（阈值 40），两段原文相距过远，属拼接而非省略——"
                       "不能当原文引用。")
            elif "unverified_segment" in risk_flags:
                msg = (f"引用内容在语料中存在，但省略号后有 {len(rep.dropped_segments)}"
                       f" 段过短（{'、'.join(rep.dropped_segments[:5])}），"
                       "短于分段下限、未参与核验——不能当原文引用，"
                       "请把这些段补成完整句子再查。")
            elif "dangling_comparison" in risk_flags:
                msg = ("引用在量词比较词上截断（「不得少于/不超过…」），原文紧接着"
                       "就是具体数字——数量是这句话的全部信息量，不能当原文引用，"
                       "请把数字一并引出。")
            elif "dropped_qualifier" in risk_flags:
                _kinds = {"exception": "例外", "condition": "条件",
                          "strength": "限定强度", "scope": "主体范围"}
                _shown = "、".join(
                    f"「{d['text']}」（{_kinds.get(d['kind'], d['kind'])}）"
                    for d in rep.dropped_qualifiers[:4])
                msg = ("引用内容在语料中存在，但原文在被引用片段旁边还有限定语、"
                       f"引用没有带上：{_shown}——丢掉限定语会把局部规则读成普遍"
                       "规则（范围/条件/例外/强度被削掉），不能当原文引用，"
                       "请连同限定语一起引出。")
            elif "context_negation" in risk_flags:
                msg = ("引用内容在语料中存在，但所在原句含否定/转折框架而引用不含"
                       f"（原句：「{rep.sentence_example[:60]}…」）——引用等于把"
                       "禁止性表述读成事实陈述，不能当原文引用，请回到原句核对。")
            elif verbatim:
                msg = "引用逐字可在语料中核实。"
            else:
                msg = ("引用内容可在语料中核实，但为省略写法（含「……」），非逐字——"
                       "如需精确回查请让模型给出原文。")
            generic = rep.generic
            if generic and hits:
                msg += (f" 注意：该引用命中 {rep.block_hits} 块"
                        f"（占语料 {rep.specificity:.1%}），区分度低，"
                        "不宜单独作为依据。")
            # 短片段：字面在语料里，但证明不了「这句话是这么说的」。若原句还带
            # 否定框架（「不得出现……破坏营商环境的行为」），引用等于把禁止性
            # 列举读成事实陈述 —— 这种情况必须说出来，但不改 verified：
            # 6 字短语（「优化营商环境」）同样短，一刀切会把合法引用误伤。
            #
            # 长度用规范化后的 rep.qlen：原始 len() 会被零宽字符撑大，
            # 末尾加几个 U+200B 就能把 6 字撑到 12 字、把这条警告抹掉。
            fragment = rep.fragment
            if fragment and hits:
                if rep.sentence_negation:
                    msg += (f" 警告：该引用仅 {rep.qlen} 字，是原句的一截；"
                            f"所在原句含否定/转折（「{rep.sentence_example[:40]}…」），"
                            "单独引用可能把禁止性表述读成事实陈述，请回到原句核对。")
                else:
                    msg += (f" 提示：该引用仅 {rep.qlen} 字，只能证明这些字"
                            "在语料中出现过，不能证明整句语境。")
            return {
                "quote": quote,
                "verified": verified,
                "partial": partial,
                "verbatim": verbatim,
                "risk_flags": risk_flags,
                "match_kinds": rep.match_kinds,
                "hit_count": rep.matched,
                "returned_hits": len(hits),
                "dropped_segments": rep.dropped_segments,
                "dropped_qualifiers": rep.dropped_qualifiers,
                "specificity": rep.specificity,
                "generic": generic,
                "fragment": fragment,
                "hits": hits,
                "message": msg,
            }, False

        if name == "list_sources":
            raw_top = arguments.get("top")
            # `or 30` 会把显式的 top=0 也变成 30，让「只要统计、不要明细」
            # 这条路永远走不到（M5）。只有缺省才套默认值。
            top = 30 if raw_top is None else int(raw_top)
            return corpus().sources(
                topic=str(arguments.get("topic", "") or ""),
                top=top,
            ), False

        return {"error": f"未知工具：{name}"}, True
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, True


def _result_text(payload: dict, is_error: bool) -> dict:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "isError": is_error,
    }


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _params(msg: dict) -> dict | None:
    """取出 params，并保证它是对象。

    JSON-RPC 允许 params 缺省、为对象或为数组；本服务的三个方法都只接受对象。
    返回 None 表示结构非法 —— 调用方回 `-32602`，**不能**让它抛 AttributeError：
    一条合法 JSON、结构畸形的消息就能把整个 stdio 会话带走（B2）。
    """
    p = msg.get("params")
    if p is None:
        return {}
    return p if isinstance(p, dict) else None


def handle(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息。返回 None 表示这是通知，不该有响应。"""
    method = msg.get("method")
    mid = msg.get("id")
    # JSON-RPC 2.0 里通知的定义是**没有 id 成员**，不是「id 为 null」。
    # `"id": null` 是合法 id（规范只是不推荐用它），必须回响应 —— 否则
    # 客户端永远等不到答复（L9：修复前未知方法 + id:null 被静默丢弃）。
    is_notification = "id" not in msg

    if method == "initialize":
        if _params(msg) is None:
            return _error(mid, -32602, "Invalid params: params 必须是对象")
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        if _params(msg) is None:
            return _error(mid, -32602, "Invalid params: params 必须是对象")
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = _params(msg)
        if params is None:
            return _error(mid, -32602,
                          "Invalid params: tools/call 的 params 必须是对象"
                          "（含 name 与 arguments）")
        name = params.get("name", "")
        # 未知工具是**方法/参数**层面的错误，不是「工具执行失败」：MCP 规范要求
        # 协议级错误。回 result.isError 会让客户端的重试/降级策略走错分支
        # （以为工具跑挂了，于是重试一个根本不存在的工具）。
        if name not in _TOOL_NAMES:
            return _error(
                mid, -32602,
                f"Invalid params: 未知工具 {name!r}；可用："
                + ", ".join(sorted(_TOOL_NAMES)),
            )
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        payload, is_error = _call_tool(name, args)
        return {"jsonrpc": "2.0", "id": mid,
                "result": _result_text(payload, is_error)}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if is_notification:
        return None

    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description="引用回查 MCP 服务（stdio JSON-RPC）。与领域无关："
                    "语料换成任何行业的 jsonl.gz 都能直接跑。",
    )
    ap.add_argument("--data", default=None,
                    help="语料文件路径（.jsonl.gz 或 .jsonl）。缺省读环境变量 "
                         "ICT_TRACK2_DATA，再缺省用包内 data/chunks.jsonl.gz")
    ap.add_argument("--version", action="version",
                    version=f"{SERVER_NAME} {SERVER_VERSION}")
    a = ap.parse_args(argv)

    # 预热语料：加载失败要立刻报错到 stderr，而不是等到第一次调用
    try:
        st = corpus(a.data).stats()
        print(f"[{SERVER_NAME}] 已加载 {st['chunks']} 块 / {st['documents']} 篇 · "
              f"来源 {st['sources']} 个 · 年份 {st['year_range']} · {st['data_file']}",
              file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[{SERVER_NAME}] 语料加载失败：{type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        raise

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            # JSON-RPC 2.0 的 Parse error（-32700）。静默丢弃会让客户端永远等
            # 不到响应，排障时分不清「客户端发错」还是「服务端丢包」（M3）。
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e.msg}"}})
            continue
        if not isinstance(msg, dict):
            # 合法 JSON 但不是对象（数组 / 标量）：同属请求结构非法。同样不能
            # 静默丢弃 —— 否则客户端会一直挂在那里等一个不会来的响应。
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600,
                              "message": "Invalid Request: 必须是 JSON 对象"}})
            continue
        # B2 第二道闸：结构校验挡不住的意外（比如 params 里塞了奇怪的类型）
        # 也只能让这一条消息失败，不能让整个会话退出 —— 客户端不会重连。
        try:
            resp = handle(msg)
        except Exception as e:  # noqa: BLE001 — 服务端边界，兜住一切
            resp = _error(msg.get("id"), -32603,
                          f"Internal error: {type(e).__name__}: {e}")
        if resp is not None:
            _write(resp)


if __name__ == "__main__":
    main()
