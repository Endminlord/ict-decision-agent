"""跨会话长期记忆：把每次运行学到的选择留下来，下次同类任务直接能用。

它记的是**决定**，不是聊天记录——这次任务用了什么语言、什么测试框架、领域是什么、
结果如何。检索靠词面重合（中文按字符二元组、英文按词），不靠 embedding：这个仓库不引
外部依赖，而「同一领域的同一类任务」这件事，词面检索够用。

三条纪律，和 `audit.py` / `storage.py` 一致：

1. **追加写**，永不重写历史。
2. **永不抛异常**。记忆坏了不该让运行失败（旁路只降级）。坏行跳过并计数，
   调用方想知道坏了几行就问 `broken_lines`。
3. **每条 ref 都要说清为什么被取出来**。不说理由的检索结果和随机数没有区别——
   这是这个仓库反复踩过的坑：`input_incomplete` 那种话等于没说。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_LIMIT = 3
# 中文没有空格，按字符二元组切；英文按词切。两套都留着，混合输入也能匹配。
_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_SNIPPET = 80


def tokens(text: Any) -> set[str]:
    """把一段文本切成可比对的词元：英文词 + 中文二元组。

    公开的，因为**切词只该有一个实现**。`runtime/code_index.py` 找代码用的就是
    这一套，它只多做一件事（切开标识符），而不是另写一份。
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    lowered = text.lower()
    out = set(_ASCII_WORD.findall(lowered))
    han = _CJK.findall(lowered)
    # 二元组比单字稳：单字「器」会匹配到「加法器」和「乘法器」，二元组不会。
    out.update(han[i] + han[i + 1] for i in range(len(han) - 1))
    out.update(han)  # 单字也留一份，短查询（一个字）才有得匹配
    return out


#: 老名字，内部还有两处在用。切词永远只有一份实现，这个别名只是让旧调用点不用改。
_tokens = tokens


class MemoryStore:
    """一个追加式的 JSONL 记忆文件。``path`` 为 None 时它是个哑对象，什么都不做。"""

    def __init__(self, path: Any = None) -> None:
        self.path = Path(path) if path else None
        self.broken_lines = 0
        self.written = 0
        # 读不出来和「还没有记忆」不是一件事。不分开的话，一个权限不对的记忆文件
        # 在界面上会显示成「它什么都不记得」——那是替文件撒谎。
        self.read_error: str | None = None

    # --- 写 ---------------------------------------------------------------

    def record(self, entry: Mapping[str, Any]) -> bool:
        """追加一条记忆。返回是否写成功——**不抛异常**。"""
        if self.path is None or not isinstance(entry, Mapping):
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(dict(entry), ensure_ascii=False, sort_keys=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            self.written += 1
            return True
        except Exception:
            # 磁盘满、权限、编码——都不该让一次已经跑完的运行失败。
            return False

    # --- 读 ---------------------------------------------------------------

    def entries(self) -> list[dict]:
        """所有能解析的记忆，按写入顺序。坏行跳过。

        每次读都重新数坏行、重新判读不读得出来。之前 `broken_lines` 是累加的，
        于是「这个文件坏了几行」变成了「你一共读过几次」——读两次同一个坏文件
        会得到 2，那这个数就不说明任何事了。
        """
        self.broken_lines = 0
        self.read_error = None
        if self.path is None:
            return []
        out: list[dict] = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 文件不在 = 还没有记忆。这是**读出来了**：读出来「这里什么都没有」。
            return []
        except Exception as exc:
            # 权限、编码、磁盘——文件在，但读不出来。这两件事长得一样，意思完全不同。
            self.read_error = f"{type(exc).__name__}: {exc}"
            return []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                self.broken_lines += 1
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
            else:
                self.broken_lines += 1
        return out

    def search(
        self,
        query: Any,
        *,
        domain_id: Any = None,
        limit: int = DEFAULT_LIMIT,
        exclude_task_id: Any = None,
    ) -> list[dict]:
        """按词面重合 + 同领域 + 时间新旧找相关记忆，返回**带理由**的 ref 列表。

        ``exclude_task_id`` 用来排除当前这次任务自己：不排除的话，运行写到一半再查，
        最相关的永远是它自己，记忆就成了回音壁。
        """
        if limit <= 0:
            return []
        wanted = _tokens(query)
        scored: list[tuple[float, int, dict, list[str]]] = []
        for index, entry in enumerate(self.entries()):
            if exclude_task_id is not None and entry.get("task_id") == exclude_task_id:
                continue
            if not wanted:
                continue
            same_domain = bool(domain_id) and entry.get("domain_id") == domain_id
            # 记忆里的文本面：需求原文 + 领域 + 语言 + 框架
            haystack = " ".join(
                str(entry.get(key) or "")
                for key in ("user_input", "domain_id", "language", "test_framework")
            )
            overlap = wanted & _tokens(haystack)
            if not overlap and not same_domain:
                continue
            score = len(overlap) / max(len(wanted), 1)
            if same_domain:
                score += 0.5
            scored.append((score, index, entry, sorted(overlap)))
        scored.sort(key=lambda item: (-item[0], -item[1]))  # 分数高的在前，同分新的在前

        refs = []
        for score, _, entry, overlap in scored[:limit]:
            why = []
            if domain_id and entry.get("domain_id") == domain_id:
                why.append(f"同领域({domain_id})")
            if overlap:
                # 展示时长词优先：单字（「一」「两」）在匹配时有用（短查询要靠它），
                # 但写进理由里只是噪音——理由是给人看的，不是给分词器看的。
                shown = sorted(overlap, key=lambda token: (-len(token), token))
                why.append(f"词面重合 {len(overlap)} 项: " + ", ".join(shown[:4]))
            refs.append({
                "id": entry.get("task_id") or f"mem-{entry.get('timestamp') or 'unknown'}",
                "task_id": entry.get("task_id"),
                "timestamp": entry.get("timestamp"),
                "domain_id": entry.get("domain_id"),
                "score": round(score, 3),
                "reason": " + ".join(why) or "仅同领域",
                "snippet": str(entry.get("user_input") or "")[:_SNIPPET],
                # `user_input` / `status` / `verification_level` 和 `read()` 那份对齐：
                # 同一份记忆，两个入口出去的形状要能用一个渲染器画，不然界面就得
                # 分岔——而分岔出来的两份，迟早有一份忘了跟着改。
                "user_input": entry.get("user_input") or "",
                "status": entry.get("status"),
                "verification_level": entry.get("verification_level"),
                # 取出来是为了用：上次这类任务最终选了什么
                "settled": {
                    key: entry[key]
                    # `settled` 只装「上次这类任务最后**选**了什么」。`status` 曾经也塞在这里，
                    # 于是界面上出现了「上次最后选了：status=paused_pending_human」——
                    # 运行怎么结束的不是一次选择。结果如何改由顶层字段带，
                    # `summarize_for_prompt` 显式地去取，不再靠「字典里碰巧有什么」。
                    for key in ("language", "test_framework", "entrypoint")
                    if entry.get(key)
                },
            })
        return refs

    def read(self, limit: int = 20) -> list[dict]:
        """把记忆摊开给人看：**倒序**（最近写的在前），每条说清它记的是什么。

        这**不是**检索结果，所以没有 `reason` 字段——检索要理由，是因为
        「凭什么把这条捞出来」不写出来读者无从判断；而「把最近写的列出来」
        这件事本身就说清了自己为什么在这儿。两者混在一起，理由就不再是信号。

        每条只带能被复核的东西：什么时候、哪个领域、问了什么、最后选了什么、怎么结束的。
        """
        if limit <= 0:
            return []
        rows = self.entries()
        out: list[dict] = []
        for entry in reversed(rows[-limit:] if limit < len(rows) else rows):
            out.append({
                "id": entry.get("task_id") or f"mem-{entry.get('timestamp') or 'unknown'}",
                "task_id": entry.get("task_id"),
                "timestamp": entry.get("timestamp"),
                "domain_id": entry.get("domain_id"),
                "user_input": entry.get("user_input") or "",
                "status": entry.get("status"),
                "verification_level": entry.get("verification_level"),
                # 上次这类任务最后选了什么——记忆的用处就在这几个字段上。
                # 只有这三样：`status` / `verification_level` 上面已经有了。
                # `search()` 的 ref 里 `settled` 会多带这两个，因为 ref 上没有别的地方
                # 放「结果如何」——形状不一样是因为两份东西的用途不一样，不是因为漏了。
                "settled": {
                    key: entry[key]
                    for key in ("language", "test_framework", "entrypoint")
                    if entry.get(key)
                },
                # 磁盘上这个字段就叫 `reason`（「这次运行为什么这么结束」）。
                # 但出去的时候不能还叫 `reason`：`search()` 的每条 ref 上已经有一个
                # `reason` 了，那个说的是「**凭什么**把这条捞出来」。同一个词两个意思，
                # 而界面用同一个渲染器画这两种行——迟早有人把「运行失败了」读成
                # 「检索的理由是失败」。
                "ended_because": entry.get("reason"),
            })
        return out

    def status(self) -> dict:
        """这个记忆文件现在是什么状况。

        三种「空」必须分开说，因为它们该引出三种不同的动作：
        - ``not_configured``：压根没装记忆（没给状态目录）→ 什么都别指望
        - ``unreadable``：文件在，读不出来（权限 / 编码 / 磁盘）→ 去看那个文件
        - ``empty``：读出来了，里面确实什么都没有 → 正常，跑一次就有了
        - ``ok``：有东西

        以前这三种在调用方眼里都是「0 条」，于是「记忆坏了」会被当成「还没学到东西」，
        而后者听起来完全正常——一个故障就这么安静地活下去了。
        """
        entries = self.entries()
        if self.path is None:
            state = "not_configured"
        elif self.read_error is not None:
            state = "unreadable"
        elif not entries:
            state = "empty"
        else:
            state = "ok"
        size: int | None = None
        if self.path is not None:
            try:
                size = self.path.stat().st_size
            except OSError:
                size = None
        return {
            "state": state,
            "path": str(self.path) if self.path is not None else None,
            "count": len(entries),
            "broken_lines": self.broken_lines,
            "read_error": self.read_error,
            "size_bytes": size,
        }

    def count(self) -> int:
        return len(self.entries())


def open_memory(state_dir: Any) -> MemoryStore:
    """按状态目录开一个记忆文件；目录不可用就返回哑对象（旁路只降级）。"""
    if not state_dir:
        return MemoryStore(None)
    try:
        return MemoryStore(Path(state_dir) / "memory.jsonl")
    except Exception:
        return MemoryStore(None)


def summarize_for_prompt(refs: Iterable[Any], limit: int = 3) -> str:
    """把 refs 折成一段可以塞进提示词的话。没有记忆时返回空串。

    只带**能被复核**的东西（上次这类任务选了什么、结果如何），不带自由文本——
    记忆进提示词是会影响后续决定的，不该由一段无法追溯的散文来影响。
    """
    lines = []
    for ref in list(refs or [])[:limit]:
        if not isinstance(ref, Mapping):
            continue
        settled = ref.get("settled") or {}
        if not isinstance(settled, Mapping) or not settled:
            continue
        picked = ", ".join(f"{k}={v}" for k, v in sorted(settled.items()))
        # 「结果如何」和「选了什么」一样是能复核的事实，所以照样带上——但要**显式**
        # 去取：以前它是因为碰巧躺在 settled 里才被带上的，改一次 settled 的键表
        # 就会把它悄悄弄丢。
        ended = " / ".join(str(ref.get(key)) for key in ("status", "verification_level")
                           if ref.get(key))
        tail = f"{picked}（结果：{ended}）" if ended else picked
        lines.append(f"- {ref.get('snippet') or ''}（{ref.get('reason') or ''}）→ {tail}")
    if not lines:
        return ""
    return "同一领域的既往任务（按相关度）：\n" + "\n".join(lines)
