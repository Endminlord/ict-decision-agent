"""编程语言名的闭集归一化 —— 一个字段的边界校验。

**这个模块是一次真实失败留下的。** live run `65390e806f81`：提示词里没点名语言，
模型把「人类语言」写进了「编程语言」字段 —— ``spec.tech_stack.language`` 的值是
``"chinese"``。它一路漏到 ``tdd_coder._render_skeleton``，产出::

    // TODO: implement add in chinese

``code_stub`` 被标成 ``True``，``executor`` 拒跑（``green_code_is_stub``），整条链
停在 S5 ``paused_pending_human``。同一个提示词点名 ``Python 3.11`` 之后，链子跑到
S7 ``contract_pass`` 并且**真起了子进程**（``stdout "5\\n"``、``exit_code 0``）。

**断点不在模型能力上，在一个 ``or "python"`` 的兜底上**：它只挡 falsy（``None``、
``""``），不挡「有值但值不对」。所以同一个模型、同一条链，成不成只差提示词里有没有
写一个词。这里把「有值但值不对」也挡住。

两条设计约束：

1. **闭集是 executor 真能跑的语言，不是「认得的语言」。** ``verilog`` 必须在册 ——
   ``tdd_coder`` 为它走单独分支。
2. **认不出来不改写。** 归一化只处理「已知的等价写法」和「已知的泄漏」。遇到
   ``rust`` / ``c`` 这种链子确实产不出来的语言，原样放行，让它照旧走
   ``_render_skeleton`` 的兜底并标成 stub —— **「不猜」比「猜成 python」诚实**：
   猜错的语言会产出一份看着像成功的错代码，而 stub 会被 ``executor`` 拦住。
"""

#: executor 有后端能跑的语言。``verilog`` 在 ``tdd_coder`` 里走单独分支，必须在册。
SUPPORTED = ("python", "javascript", "typescript", "go", "verilog")

#: 已知的等价写法 + 已知的泄漏。
#:
#: ``chinese`` / ``中文`` 不是编程语言，是**真实漏进来过**的值 —— 把它们列在这里，
#: 这类泄漏就会走「归一化到链子的缺省语言」而不是走「认不出来」；同时因为
#: :func:`coerced_from` 会留下原值，改写不会是静默的。
ALIASES = {
    "chinese": "python",
    "中文": "python",
    "汉语": "python",
    "python3": "python",
    "python2": "python",
    "py": "python",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "ts": "typescript",
    "golang": "go",
}


def normalize_language(raw, fallback: str = "python") -> str:
    """把上游给的语言名收进闭集。

    顺序：非字符串/空 ⇒ ``fallback``；别名 ⇒ 别名指向的语言；闭集内 ⇒ 它自己
    （大小写归一）；其余 ⇒ **原样返回**（见模块 docstring 第 2 条）。
    """
    if not isinstance(raw, str):
        return fallback
    text = raw.strip()
    if not text:
        return fallback
    key = text.lower()
    if key in ALIASES:
        return ALIASES[key]
    if key in SUPPORTED:
        return key
    return text


def coerced_from(raw, resolved: str):
    """``raw`` 被归一化改写过就返回它的原样，否则 ``None``。

    调用方拿它当「这个字段被改过」的痕迹：一个静默改写的字段，下一个人会以为
    是模型自己选的 ``python``，于是同样的泄漏会再发生一次而没人知道。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or text.lower() == resolved:
        return None
    return text
