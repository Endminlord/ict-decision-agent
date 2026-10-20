"""Search across what the workbench kept: work orders, generated artifacts, snapshots.

The point is 「按内容找」. 记得让模型写过一个 ``is_even``，但忘了是哪个单号——搜索
要能从**生成物里的那一行代码**把它找回来，而不是只能按标题找。

三条纪律，每一条都对应一种「搜索在撒谎」的方式：

1. **搜了哪儿要说出来。** 结果里带 ``searched``：因为「没找到」和「没搜那儿」
   在界面上长得一模一样，而它们是完全不同的两件事。
2. **截断了要说出来。** 命中被上限切掉时 ``truncated`` 为真——「只有三条」和
   「只给你三条」是两回事。
3. **没搜的地方也要说出来。** 记忆、模型探测记录、日志都不在范围里。明写出来，
   免得把「搜索里没有」当成「它没记过」。

范围只包含这三样，而且每一样都是**真的有人问过的问题**：工作单（那次到底说了
什么、是谁定的领域、是不是重放来的）、生成物（那段代码长什么样）、快照（这个
文件在被改之前是什么）。

搜的是**内容**，不是字段名：搜 ``stopped_by_user`` 找得到（它是 ``status`` 的值），
搜 ``replayed_from`` 找不到（那是键，不是值）——想知道「哪几单是重放来的」，就搜
那个单号，命中的 ``field`` 会告诉你它在 ``lineage.replayed_from`` 里。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Iterator, Mapping

__all__ = ["SOURCES", "MAX_FILE_BYTES", "search", "main"]

SOURCES = ("工作单", "生成物", "快照")

#: 超过这个大小的文件不读——搜索不该因为一个巨大的产物把整页拖住。
#: 跳过会被计入 ``skipped_large``，不是悄悄忽略。
MAX_FILE_BYTES = 1024 * 1024

SNIPPET_CHARS = 200

#: 「工作单」这一源不看 ``artifacts``——那棵树归「生成物」管。见 ``_search_orders``。
_ORDER_SKIPS = ("artifacts",)


def _text_leaves(node: Any, prefix: str = "") -> Iterator[tuple[str, str]]:
    """把 ``artifacts`` 那棵树里的文本叶子摊平：``路径 -> 文本``。

    生成物不是「一个字符串」，它是一棵树（``artifacts.tdd_coder.green_code.code``）。
    摊平以后每条命中都能说清自己是树的哪一根枝——只报「在生成物里」等于没说。
    """
    if isinstance(node, str):
        yield prefix, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _text_leaves(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _text_leaves(value, f"{prefix}[{index}]")


def _lines(text: str, needle: str) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line.lower():
            yield number, line.strip()[:SNIPPET_CHARS]


def _search_orders(orders: list[dict], needle: str, add) -> int:
    """工作单：整条记录**除 artifacts 之外**的文本叶子。返回扫了多少条。

    原来只搜 ``user_input``/``order_id``/``task_id``/``domain`` 四个字段。那是一条
    没写出来的边界：搜 ``replayed_from`` 会得到 0 条命中，而用户看不出这是「没有」
    还是「没搜那儿」——正是这个模块存在要防的那件事。所以改成摊平整条记录，
    字段路径如实写进 ``field``：``lineage.replayed_from``、``status``、
    ``model_route.model``……

    ``artifacts`` 不在这里看：那棵树归「生成物」管。两边都搜会让同一段代码报两条
    命中，而「它在哪个源里」正是这里最要紧的信息。
    """
    for order in orders:
        order_id = str(order.get("order_id") or "")
        rest = {key: value for key, value in order.items() if key not in _ORDER_SKIPS}
        for field, text in _text_leaves(rest):
            if not text.strip():
                continue
            for line_no, snippet in _lines(text, needle):
                add({
                    "source": "工作单", "order_id": order_id,
                    "field": field, "line": line_no, "snippet": snippet,
                })
    return len(orders)


def _search_artifacts(orders: list[dict], needle: str, add) -> int:
    """生成物：工作单里模型写出来的代码与文本。返回扫了多少片文本。"""
    scanned = 0
    for order in orders:
        order_id = str(order.get("order_id") or "")
        for field, text in _text_leaves(order.get("artifacts") or {}):
            if not text.strip():
                continue
            scanned += 1
            for line_no, snippet in _lines(text, needle):
                add({
                    "source": "生成物", "order_id": order_id,
                    "field": field, "line": line_no, "snippet": snippet,
                })
    return scanned


def _search_snapshots(snapshot_dir: str, needle: str, add, skipped: list) -> int:
    """快照：落笔**之前**的原文。返回读了几份。

    读的是解析出来的 ``content`` 而不是清单文件的原始文本：原始文本里换行是
    ``\\n`` 两个字符，命中会落在引号和转义符中间，给出来的片段没法看。
    """
    scanned = 0
    if not os.path.isdir(snapshot_dir):
        return 0
    for root, _dirs, files in os.walk(snapshot_dir):
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    skipped.append(full)
                    continue
                with open(full, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, Mapping):
                continue
            scanned += 1
            content = manifest.get("content")
            if not isinstance(content, str):
                # 快照记的是「这个文件本来不存在」——没有原文可搜，但这一份**读过**了。
                continue
            for line_no, snippet in _lines(content, needle):
                add({
                    "source": "快照",
                    "order_id": str(manifest.get("snapshot_id") or os.path.basename(root)),
                    "field": str(manifest.get("path") or full),
                    "line": line_no, "snippet": snippet,
                })
    return scanned


def search(state_dir: Any, query: Any, *, limit: int = 50) -> dict:
    """在 ``state_dir`` 里找 ``query``，返回命中的位置和搜过的地方。

    ``limit`` 是**总**命中上限，不是每个来源一份：切掉的时候要说出来。
    """
    needle = str(query or "").strip().lower()
    result: dict[str, Any] = {
        "query": str(query or "").strip(),
        "hits": [],
        "counts": {name: 0 for name in SOURCES},
        "searched": list(SOURCES),
        "scanned": {name: 0 for name in SOURCES},
        "skipped_large": [],
        "truncated": False,
        "note": "",
    }
    if not needle:
        result["note"] = "空查询：没有要找的东西。"
        return result

    state_dir = str(state_dir or "state")
    total = 0

    def add(hit: dict) -> None:
        nonlocal total
        total += 1
        if total <= limit:
            result["hits"].append(hit)
            result["counts"][hit["source"]] += 1
        else:
            result["truncated"] = True

    # 工作单与生成物来自同一份记录：读一次，搜两遍。
    orders: list[dict] = []
    try:
        from runtime.storage import WorkOrderStore
        orders = WorkOrderStore(
            os.path.join(state_dir, "work_orders.jsonl")
        ).records()
    except (OSError, ValueError):
        orders = []
    result["scanned"]["工作单"] = _search_orders(orders, needle, add)
    result["scanned"]["生成物"] = _search_artifacts(orders, needle, add)
    result["scanned"]["快照"] = _search_snapshots(
        os.path.join(state_dir, "snapshots"), needle, add, result["skipped_large"]
    )

    if result["truncated"]:
        result["note"] = f"命中超过 {limit} 条，这里只给了前 {limit} 条。"
    elif not result["hits"]:
        result["note"] = "搜过的地方里没有这一条。"
    return result


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="在工作单、生成物和快照里按内容搜索"
    )
    parser.add_argument("query", help="要找的文本（大小写不敏感）")
    parser.add_argument("--state-dir", default="state", help="状态目录（默认 state）")
    parser.add_argument("--limit", type=int, default=50, help="最多给多少条命中")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    found = search(args.state_dir, args.query, limit=args.limit)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    print(f"搜「{found['query']}」：{len(found['hits'])} 条命中"
          f"（工作单 {found['counts']['工作单']}、生成物 {found['counts']['生成物']}、"
          f"快照 {found['counts']['快照']}）")
    print("搜过：" + "、".join(found["searched"])
          + f"；扫了 {found['scanned']['工作单']} 张工作单、"
          f"{found['scanned']['生成物']} 片生成物文本、"
          f"{found['scanned']['快照']} 份快照")
    for hit in found["hits"]:
        where = hit["order_id"] or hit["field"]
        print(f"  [{hit['source']}] {where} · {hit['field']}:{hit['line']}  {hit['snippet']}")
    if found["note"]:
        print(found["note"])
    if found["skipped_large"]:
        print(f"有 {len(found['skipped_large'])} 个文件太大没读：{found['skipped_large'][0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
