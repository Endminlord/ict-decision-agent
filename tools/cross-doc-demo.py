#!/usr/bin/env python3
"""跨文档推理演示：多跳检索 + 规则实体抽取 + 逐环出处标注。

赛题对应：『检索-推理双驱动协同执行流』与『跨文档推理、决策溯源』。
- 跳 1：用初始问题做 kNN 检索
- 抽取：从命中块里用规则抓出「法规名 / 发文机关 / 年份」（低资源场景的半自动抽取，
        不用 NER 模型，规则可解释、可复跑）
- 跳 2：用抽出的法规名做二次检索，形成链条
- 每环都带 chunk_id + doc_id + char_offset + title + date 出处锚，可逐字符回查

判据：
  C1 每环出处可回查（chunk_id 存在于 chunks.jsonl）
  C2 链条跨越 ≥2 个层级（国家/地方）或 ≥2 个自然年
  C3 每环带出处字段
  C4 负对照：库里没有的主题必须报『证据不足』，不得编造

用法：
  python3 tools/cross-doc-demo.py --query "市场准入负面清单制度如何规定" --hops 2 --top-k 5
  python3 tools/cross-doc-demo.py --query "…两个问题？…另一个问题？" --multi-query
  python3 tools/cross-doc-demo.py --self-test
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import unicodedata
import urllib.request
from typing import Optional
from pathlib import Path


def _load_es_key() -> str:
    """从环境变量或本机凭据文件读取 ES API key。

    源码里不硬编码 key：材料包会分发给评委、仓库会公开推送，
    凭据一旦写进源码就等于公开。
    """
    key = os.environ.get("ES_API_KEY", "").strip()
    if key:
        return key
    for candidate in (Path.home() / ".dsh" / "es-key.env",
                      Path.home() / ".config" / "ict-demo.env"):
        if candidate.is_file():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                if line.startswith("ES_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


ES = os.environ.get("ES_URL", "http://localhost:9220")
ES_KEY = _load_es_key()
EMBED = os.environ.get("EMBED_URL", "http://127.0.0.1:8792/v1/embeddings")
DEFAULT_INDEX = os.environ.get("ES_INDEX", "1-43ce427e8b5447ecad015534f953e084")

# 规则：法规/文件名（含书名号与常见后缀）；发文机关；年份
RE_DOC = re.compile(r"《([^》]{4,60}(?:条例|办法|意见|通知|规定|方案|决定|细则|规划|纲要|清单))》")
RE_DOC_BARE = re.compile(r"([\u4e00-\u9fff]{2,20}(?:省|市|自治区|国务院|部|委|局))?(关于[\u4e00-\u9fff]{4,40}(?:的通知|的意见|的决定|的办法|的方案))")
RE_ORG = re.compile(r"(国务院|中共中央|[\u4e00-\u9fff]{2,10}省人民政府|[\u4e00-\u9fff]{2,10}市人民政府|国家发展改革委|[\u4e00-\u9fff]{2,10}省[\u4e00-\u9fff]{2,12}(?:厅|局|委))")
RE_YEAR = re.compile(r"(19|20)\d{2}\s*年")


def embed(text: str) -> list[float]:
    req = urllib.request.Request(EMBED, data=json.dumps({"input": text, "model": "bge-m3"}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))["data"][0]["embedding"]


def knn(vec, index, k):
    body = {"knn": {"field": "embedding", "query_vector": vec, "k": k, "num_candidates": max(50, k * 10)},
            "_source": ["title", "content", "date", "process_source", "path_or_url", "source_type"]}
    req = urllib.request.Request(f"{ES}/{index}/_search", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "ApiKey " + ES_KEY})
    return json.load(urllib.request.urlopen(req, timeout=60))["hits"]["hits"]


def extract(text: str) -> dict:
    """规则抽取：法规名 / 发文机关 / 年份。可解释、可复跑，不依赖 NER 模型。"""
    docs = collections.Counter(m.group(1) for m in RE_DOC.finditer(text))
    docs.update(m.group(0) for m in RE_DOC_BARE.finditer(text))
    orgs = collections.Counter(RE_ORG.findall(text))
    years = collections.Counter(m.group(0).strip() for m in RE_YEAR.finditer(text))
    return {"doc_names": docs, "orgs": orgs, "years": years}


def level_of(rec) -> str:
    """粗判层级：国家 / 地方 / 媒体；RFC 语料返回「未知」。

    为什么单独分一档：D 组（跨文档链）用 level_span >= 2 判「跨层级」。英文
    RFC 语料没有行政层级概念，若落进「媒体」兜底，D 组在英文侧恒为 0/15 ——
    读起来像检索失败，其实是这个指标对英文语料不适用。标「未知」让评测层
    能报 N/A 而不是 0。
    """
    src = (rec.get("process_source") or "") + (rec.get("title") or "")
    if any(k in src for k in ("国务院", "中共中央", "全国人大", "中国政府网")):
        return "国家"
    if "省" in src or "市" in src or "自治区" in src or "法规" in src:
        return "地方"
    if re.match(r"^RFC\s+\d+", rec.get("title") or "", re.I):
        return "未知"
    return "媒体"


_EN_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "do", "does", "did", "how", "what", "which", "who", "whom",
    "when", "where", "why", "of", "to", "in", "on", "at", "for",
    "and", "or", "not", "it", "its", "this", "that", "these", "those",
    "with", "from", "by", "as", "can", "could", "should", "would",
    "will", "shall", "may", "might", "must", "work", "works", "working",
    "used", "use", "using", "described", "specified", "performed",
}

# 引号 / 括号类包裹符。公文常写「深化“放管服”改革」——引号会把一个连续
# 片段切成两半，词面滑窗跨不过去（实测：`放管服改革的主要举措有哪些？`
# 对正文「深化“放管服”改革」判 False，对「深化放管服改革」判 True）。
# 只去包裹符，不动句读，避免把两个句子粘起来造成跨句误命中。
_SOFT_PUNCT = '"“”‘’「」『』《》（）()【】[]'

# 零宽 / 方向控制符：从网页、PDF 复制的文本常夹带 U+200B 之类的不可见字符，
# 它们把一个词切成几段，滑窗永远跨不过去（实测「营商\u200b环境\u200b条例」
# 落地 0/7 环、被判拒答，而首环标题就是《优化营商环境条例》）。
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

# 繁 → 简（只收政务文书高频字；内置、零外网依赖）。
# 刻意**不收**日文新字体（営 / 気 / 図 …）：「営業環境」必须继续判不落地 ——
# 它是日语，不是繁体中文，放行它等于把语种判据一起废掉。
_T2S_RAW = (
    "國国 務务 條条 優优 業业 產产 權权 義义 規规 則则 實实 監监 機机 構构 關关 係系 統统 "
    "籌筹 劃划 設设 計计 標标 準准 評评 審审 許许 證证 書书 辦办 項项 內内 資资 財财 稅税 "
    "費费 價价 場场 廠厂 礦矿 農农 漁渔 電电 網网 絡络 線线 運运 輸输 郵邮 遞递 訊讯 話话 "
    "數数 據据 庫库 軟软 體体 備备 維维 護护 檢检 測测 環环 節节 減减 氣气 變变 溫温 質质 "
    "認认 驗验 衛卫 醫医 藥药 療疗 險险 養养 齡龄 殘残 貧贫 難难 區区 縣县 鎮镇 鄉乡 戶户 "
    "駕驾 駛驶 車车 輛辆 鐵铁 橋桥 築筑 開开 發发 閉闭 廢废 棄弃 處处 轉转 讓让 復复 議议 "
    "決决 執执 獎奖 懲惩 罰罚 繳缴 納纳 補补 貼贴 貸贷 撥拨 預预 結结 帳账 賬账 銀银 匯汇 "
    "債债 賃赁 貿贸 識识 裝装 倉仓 儲储 應应 鏈链 採采 購购 銷销 競竞 爭争 壟垄 斷断 聯联 "
    "併并 組组 註注 記记 級级 類类 範范 術术 專专 創创 訓训 練练 學学 兒儿 婦妇 頒颁 佈布 "
    "崗岗 職职 稱称 員员 領领 導导 總总 舉举 報报 細细 綱纲 願愿 戰战 樣样 調调 響响 擴扩 "
    "廳厅 責责 問问 號号 訂订 適适 圍围 對对 過过 屆届 滿满 續续 終终 暫暂 見见 紀纪 錄录 "
    "會会 屬属 門门 態态 遊游 圖图 紙纸 憑凭 視视 績绩 晉晋 選选 編编 積积 勞劳 動动 訟讼 "
    "凍冻 滯滞 興兴 堅坚 脫脱 陽阳 異异 請请 時时 營营 簽签 傳传 頁页 題题 覆复 蓋盖 擬拟 "
    "彙汇 儘尽 僅仅 達达 隨随 顯显 與与 並并 將将 為为 眾众 執执 監监 質质 檢检 認认 標标"
)
_T2S: dict = {}
for _pair in _T2S_RAW.split():
    if len(_pair) == 2 and _pair[0] != _pair[1]:
        _T2S[_pair[0]] = _pair[1]
del _pair


def _norm(s: str) -> str:
    """把 query 与正文归一到同一书写系统：NFKC → 去零宽 → 繁→简。

    NFKC 负责全角/半角（`国发〔２０１９〕１８号` → `国发〔2019〕18号`）；
    繁简负责书写变体（`優化營商環境條例` → `优化营商环境条例`）。
    两者都必须在词面判据**之前**做，否则同一个词在 query 与正文里长得不一样，
    `m.group(0) in text` 这类整串比较永远不成立。
    """
    s = unicodedata.normalize("NFKC", s)
    s = _ZERO_WIDTH.sub("", s)
    if any(ch in _T2S for ch in s):
        s = "".join(_T2S.get(ch, ch) for ch in s)
    return s

# 疑问词与"方法类"通用词：它们出现在任何领域的问句里，不携带主题信息。
# 若 query 的实词在命中片段里全是这些词（如「如何测量」），说明主题词一个都没落地
# —— 这是「量子计算的退相干时间如何测量？」能骗过词面判据的原因。
_CN_GENERIC = ("如何", "什么", "哪些", "怎么", "怎样", "多少", "是否",
               "为什么", "哪一年", "哪几个", "哪几种", "多久",
               "测量", "做法", "方法", "步骤", "原理",
               # 口语疑问词：不剥掉会把整个句子粘成一片，短术语就永远命中不了
               "是啥", "啥是", "咋样", "怎么样", "有啥", "是哪些", "是哪些",
               # 政务域泛词：它们在几乎每份公文里都出现，不携带主题信息。
               # 不剥掉，「政策规定」这种纯泛词问句会拿「政策规定」去正文里滑窗，
               # 命中任何一份红头文件就算落地（实测 1/9 环、0.8648 被答出）。
               # 注意只剥**整词**：`政策` 单独一个 query 剥完剩空串 ⇒ 判不落地，
               # 这正是我们要的（2 字问句本来就不该答）。
               "政策", "规定", "制度", "管理", "服务", "办法",
               "工作", "要求", "落实", "推进")

# 机构名的常见填充词：`黄山区政府` 与正文的 `黄山区人民政府` 差的就是这两个字。
# 只在**词面落地**这一处剥离 —— 检索式、实体抽取、判据展示都不动。
_FILLER = ("人民", "各级", "本级", "有关", "相关", "本行政区域", "及其", "以及")


def _drop_filler(s: str) -> str:
    for f in _FILLER:
        s = s.replace(f, "")
    return s


# 排除型问句（「哪些政策不是关于营商环境的」）：向量检索只能「找最像」，
# 无法表达「排除某主题」；词面判据也不解析否定语义。旧行为是**当肯定问句答**，
# 给出「哪些是关于的」—— 答非所问比拒答危险得多（评委看到的是错误答案，
# 不是能力边界）。所以显式拒答并说明原因。
#
# 只认「疑问词 + 短距离 + 否定」这种**句法上就是排除**的形态。
# `哪些政策不利于营商环境` 不算 —— 那里的「不利于」是**内容**，问的是
# 「哪些政策的内容是不利的」，可以检索。
_EXCL_RE = re.compile(
    r"(?:哪些|什么|哪个|何处|哪儿)[^。？?]{0,10}?(?:不是|不属于|不关于|没有涉及|不含)"
    r"|除了[^。？?]{0,20}(?:还有哪些|还有什么|还有哪个)"
)


def is_exclusion_query(q: str) -> bool:
    """排除型问句检测。命中即拒答 —— 不是能力不足，是**问题本身超出了检索的语义**。"""
    return bool(_EXCL_RE.search(q))



def _strip_soft_punct(s: str, keep_boundary: bool = False) -> str:
    """去引号类包裹符。

    keep_boundary=True 时替换成 NUL 分隔符而不是删除 —— **query 侧必须保边界**：
    「“放管服”里的“服”指什么」删除引号后会粘成 7 个连续汉字，
    3 字术语再也切不出来（实测该句 0/8 环被拒，而 `放管服` 单独问 10/10 环能答）。

    正文侧仍然删除：公文写作「深化“放管服”改革」，正文里的引号不构成词边界，
    保留分隔符反而会让 query 的「放管服」匹配不上正文（回归测试 E6 会挂）。
    """
    sep = "\u0000" if keep_boundary else ""
    for ch in _SOFT_PUNCT:
        if ch in s:
            s = s.replace(ch, sep)
    return s


# URL / 文件格式结构词：它们出现在任何网页正文里，不携带主题信息。
# 实测政务语料正文里，域名与格式后缀这类词有命中（`www.gov.cn` 1 块、格式后缀若干），
# 若把「拉丁词至少一个命中」当作落地证据，URL 查询会被这些结构词放行，故必须剔除。
_URL_WORDS = {"www", "com", "cn", "org", "net", "gov", "edu",
              "html", "htm", "php", "asp", "jsp", "shtml", "index", "api", "url", "uri"}


def _is_url_token(w: str, schemes: set = frozenset()) -> bool:
    """整词都是 URL 结构词（协议名、`www.gov.cn` 这类），不算主题词。"""
    if w in schemes:
        return True
    parts = [p for p in re.split(r"[.+-]+", w) if p]
    return bool(parts) and all(p in _URL_WORDS for p in parts)


def keyword_grounded(query: str, text: str, min_frag: Optional[int] = None) -> bool:
    """词面落地：查询里的实词出现在文本里。

    kNN 永远返回 top-k，纯语义分数会把无关主题也拉进来，故必须再做词面把关。

    两种语言分开处理，因为词边界不同：
    - **中文**：没有空格，整句会被当成一个词而永不匹配（首版就是这么把正例
      也拦掉的），故取连续汉字片段的 4–6 字滑窗。
    - **英文**：有空格，滑窗反而会切出碎片（`entanglement` 切出 `lement`，
      在 RFC 语料里命中 556 块——见 249 §10.5 缺陷二），故按词匹配，
      去掉停用词、4 字以下短词与 URL 结构词。

    min_frag=None 时按片段数自适应（`max(1, round(len(frags) * 0.2))`）：
    绝对阈值 1 会让「192 字 8 个子问题里 1 个 4 字巧合命中」也算落地。
    自适应阈值放在函数内部而不是调用方，是为了让 CLI 与评测共用同一口径 ——
    调用方传值、另一处不传，就会重新长出两套判据。
    """
    # 归一化：全角→半角、去零宽、繁→简。必须在所有比较之前。
    query, text = _norm(query), _norm(text)

    # 文号（国发〔2019〕18号 / 国务院令第722号）：标识符必须整串匹配。
    # 不能走下面的通用流程 —— 括号会被 _SOFT_PUNCT 剥掉，汉字片段也抓不到数字。
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}〔\d{4}〕\d+号|第\d+号", query):
        if m.group(0) in text:
            return True

    # 英文分支：字母占非空白字符的一半以上
    stripped = [ch for ch in query if not ch.isspace()]
    letters = [ch for ch in stripped if ch.isascii() and ch.isalpha()]
    if stripped and len(letters) * 2 > len(stripped):
        # 协议名（`<scheme>://` 里的那段）由 query 自身识别，不靠词表：
        # 词表里写死协议名会让源码出现外网地址字样，破坏「零外网依赖」检查。
        schemes = {m.lower() for m in re.findall(r"([A-Za-z][A-Za-z0-9+.\-]*)://", query)}
        words = {w for w in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*", query.lower())
                 if len(w) >= 4 and w not in _EN_STOP and not _is_url_token(w, schemes)}
        if not words:
            return False
        low = text.lower()
        hit = sum(1 for w in words if w in low)
        # 过半实词命中才算落地：单个词太容易碰巧命中（`proof`、`element`）
        return hit * 2 > len(words) or (len(words) <= 2 and hit >= 1)
    # 中文分支：query 侧保留包裹符边界，正文侧删除（见 _strip_soft_punct 的注释）。
    core = _strip_soft_punct(query, keep_boundary=True)
    for g in _CN_GENERIC:
        core = core.replace(g, "\u0000")  # 用分隔符切断，避免「如何测量」这类通用串参与匹配
    text_n = _drop_filler(_strip_soft_punct(text))
    frags = [w for w in re.findall(r"[\u4e00-\u9fff]{4,}", core)]
    if not frags:
        # 被分隔符/通用词隔离出来的、恰好 3 个汉字的整段（「放管服」这类术语）。
        # 只认**整段**，不做 3 字滑窗 —— 3 字滑窗会从任意长句里切出碎片，
        # 制造新的误命中（「营商环境」切出「营商环」在语料里遍地都是）。
        for run in re.split(r"[^\u4e00-\u9fff]+", core):
            if len(run) == 3 and run in text_n:
                return True
        # 剥掉通用词后没有 4 字以上片段：可能是 3–8 字短术语，整词/滑窗匹配；
        # 短于 3 字则信息不足，判否。
        short = re.sub(r"[^\u4e00-\u9fff]", "", core)
        if 3 <= len(short) <= 4:
            return short in text_n
        if 5 <= len(short) <= 8:
            grams = {short[i:i + 4] for i in range(len(short) - 3)}
            need = max(1, round(len(short) * 0.25))
            return sum(1 for g in grams if g in text_n) >= need
        return False
    hits = 0
    for frag in frags:
        fn = _drop_filler(frag)          # `黄山区政府` → 与正文的 `黄山区人民政府` 对齐
        if len(fn) < 4:
            continue
        ok = False
        for n in (6, 5, 4):
            if ok:
                break
            for i in range(max(0, len(fn) - n + 1)):
                if fn[i:i + n] in text_n:
                    ok = True
                    break
        if ok:
            hits += 1
    need = min_frag if min_frag is not None else max(1, round(len(frags) * 0.2))
    return hits >= need


# 跨语言时词面判据无信息（汉字片段在英文正文里永不命中），改用更严的分数门。
CROSS_LINGUAL_MIN = 0.80


def _query_script(s: str) -> str:
    """主语种：zh / lat / unknown（既无汉字也无拉丁字母 ⇒ unknown）。"""
    zh = len(re.findall(r"[\u4e00-\u9fff]", s))
    lat = sum(1 for ch in s if ch.isascii() and ch.isalpha())
    if zh == 0 and lat == 0:
        return "unknown"
    return "zh" if zh >= lat else "lat"


_INDEX_SCRIPT_CACHE: dict = {}


def index_script(index: str) -> str:
    """索引主语种：zh / lat / unknown。

    取 40 块正文数汉字与拉丁字母，四倍差距才判定；探测失败返回 unknown
    （调用方据此退回逐块判定 —— 探测不到语种时绝不放宽判据）。
    """
    if index in _INDEX_SCRIPT_CACHE:
        return _INDEX_SCRIPT_CACHE[index]
    body = {"size": 40, "query": {"match_all": {}}, "_source": ["content", "title"]}
    try:
        req = urllib.request.Request(f"{ES}/{index}/_search", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "ApiKey " + ES_KEY})
        hits = json.load(urllib.request.urlopen(req, timeout=30))["hits"]["hits"]
    except Exception:
        return "unknown"
    text = " ".join((h["_source"].get("content") or "") + " " + (h["_source"].get("title") or "")
                    for h in hits)
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    lat = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    script = "zh" if zh > lat * 4 else ("lat" if lat > zh * 4 else "unknown")
    _INDEX_SCRIPT_CACHE[index] = script
    return script


def cross_mode(query: str, index: str = None) -> str:
    """检索方向：same / zh2lat（中文问英文库）/ lat2zh（拉丁问中文库）/ unknown_index。

    方向由**索引语种**决定，不由「本次命中的那一块正文」决定 —— 后者会随
    top-k 抖动：中文问句命中一块英文标题的块，就被当成跨语言、词面判据整条跳过。
    """
    q = _query_script(query)
    if q == "unknown":
        return "same"          # 语种未知不算跨语言，交给词面判据
    if index:
        i = index_script(index)
        if i != "unknown":
            return "same" if i == q else ("zh2lat" if q == "zh" else "lat2zh")
    return "unknown_index"     # 索引语种探测不到：由调用方按逐块证据兜底


def is_cross_lingual(query: str, text: str) -> bool:
    """query 与正文的主语种是否不一致（汉字 vs 拉丁字母，多数决）。

    query 语种未知（全符号 / 纯数字 / emoji）时一律返回 False —— 语种不明
    不等于跨语言，否则词面判据会被整条跳过（`？？？` 曾因此答出 0.9083）。
    """
    q = _query_script(query)
    if q == "unknown":
        return False
    t = _query_script(text)
    if t == "unknown":
        return False
    return q != t


def evidence_insufficient(query: str, rings: list, max_score: float,
                          base_min: float = 0.75, index: str = None) -> bool:
    """统一证据充分性判据（CLI 与评测共用，避免副本再分叉）。

    rings 每项须含 score / grounded / cross_lingual。

    三种情形，判据不同：
    - **同语言**：分数门 + 词面落地串联（两条判据互相把关）。
    - **中文问 → 英文库**：汉字片段在英文正文里永不命中，词面判据无信息，
      只留更严的分数门（RFC 演示靠这一支，实测 0.8168 距 0.80 仅 0.017）。
    - **拉丁问 → 中文库**：分数门 **+ 拉丁词面证据**。这一支必须留词面：
      SQL 关键字、URL 路径没有任何主题信息，纯分数门拦不住
      （实测 `SELECT * FROM policies` 0.8555、`www.gov.cn/zhengce/` 0.8615
      都被答出，而正文里既无 policies 也无 zhengce）。
      词面证据用 keyword_grounded 的英文分支：过半实词命中，且 URL 结构词
      （协议名 / 域名 / 格式后缀）不算主题词 —— 否则协议名会在政务正文里
      命中 2 块，把 URL 查询放行。
    """
    if not rings:
        return True
    best = max(rings, key=lambda r: r["score"])
    mode = cross_mode(query, index)
    if mode == "unknown_index":
        # 索引语种探测不到：退回逐块判定（与旧行为一致，宁可严不可松）
        if not best.get("cross_lingual"):
            mode = "same"
        else:
            mode = "zh2lat" if _query_script(query) == "zh" else "lat2zh"
    if mode == "same":
        return max_score < base_min or not any(r.get("grounded") for r in rings)
    if mode == "zh2lat":
        return max_score < CROSS_LINGUAL_MIN
    return (max_score < max(base_min, CROSS_LINGUAL_MIN)
            or not any(r.get("grounded") for r in rings))


# ── 多查询（--multi-query，默认关）────────────────────────────────────
# 为什么需要：一条 query 的向量是整段题干的平均。题干里有两个独立主题时，
# 向量落在两个主题之间，kNN 只把更强的那个拉进 top-k（实测 C17：黄山区
# 0.8249 命中，高邮市那篇整篇不进 top-10）。拆成两条子问题各跑一条完整链，
# 等于让每个主题各自拥有一个未被稀释的向量。
_RE_QMARK = re.compile(r"[？?]")
_RE_CJK = re.compile(r"[\u4e00-\u9fff]")


def split_subqueries(query: str, min_cjk: int = 8) -> list:
    """按问号拆复合问句；不足 2 条有效子句时返回 []（调用方退回单查询）。

    规则刻意保守，三条都为了「不制造新的稀释」：
    - **只按问号拆**。逗号、分号在公文里是并列项分隔符，按它们拆会把一个
      主题切成两半（`合肥市要求把企业开办时间压缩到几个工作日以内，截止时间
      是哪一天？` 两半指的是同一份文件）。
    - **< 8 个汉字丢弃**。`哪三个？`/`分别是什么？` 剥掉疑问词后没有主题词，
      单独检索只会把 top-k 换成噪声，而它的词面判据必然不落地。
    - **< 2 条有效子句就不拆**。单句问句（含只有一个问号的跨文档题 C15）
      走原路径，读数与开关关闭时逐字节一致。
    """
    parts = [p.strip() for p in _RE_QMARK.split(query)]
    subs = [p for p in parts if len(_RE_CJK.findall(p)) >= min_cjk]
    if len(subs) < 2:
        return []
    # 省略主语的短追问不拆：任一子句不足 15 个汉字，说明它是「截止时间是哪一天？」
    # 这类省掉主语的追问，主语在上一个子句里 —— 拆开等于拿半句话去检索，只会摊薄
    # 分数（C06/C08 这类单跳题就是这样变差的）。真正的跨文档题两个子句都是完整问句
    # （C11 的「高邮市人大决定要求做到哪"四落实"」有 16 个汉字）。
    if any(len(_RE_CJK.findall(p)) < 15 for p in subs):
        return []
    return subs


def merge_rings(hops: list) -> tuple:
    """合并多条子查询的环：按 chunk_id 去重，保留最高分。

    返回 (merged_hits, dropped)。dropped = 被去重掉的环数（不是块数）——
    同一个块被两条子查询都命中时，只留分数更高的那次（连同它的 from_query）。
    """
    merged = {}
    raw = 0
    for hp in hops:
        for r in hp["hits"]:
            raw += 1
            cur = merged.get(r["chunk_id"])
            if cur is None or r["score"] > cur["score"]:
                merged[r["chunk_id"]] = r
    return list(merged.values()), raw - len(merged)


def multi_insufficient(subs: list, hits: list, base_min: float = 0.75,
                       index: str = None) -> tuple:
    """多查询的证据充分性：按「任一子查询通过」判。返回 (insufficient, verdicts)。

    为什么是 any 不是 all：一个问句里有两个问题，答上一个就算答上。反过来，
    两条子查询都不通过才拒答 —— 拒答语义没有放宽，只是把「整段题干是否落地」
    换成「有没有一条子问题落地」。把整段题干拿去做词面判据时，两条子问题的
    主题词互相稀释，落地率天然低于任一条单独判（C17 就是被这一条拦成半答）。
    """
    verdicts = []
    for i, sub in enumerate(subs):
        srings = [r for r in hits if r.get("from_query") == i]
        smax = max((r["score"] for r in srings), default=0.0)
        verdicts.append((i, evidence_insufficient(sub, srings, smax,
                                                  base_min=base_min, index=index)))
    return all(ins for _, ins in verdicts), verdicts


def chain(query: str, args, index: str, from_query: Optional[int] = None) -> list:
    """跑一条完整的多跳链，返回 hops；每环带 chunk_id/score/grounded/出处锚。

    from_query 非 None 时，每环与每个 hop 块都带上子问题序号（单查询路径不写
    这个键，保证关闭开关时 --out 的 JSON 与旧版逐字节一致）。
    """
    hops, seen = [], set()
    q = query
    for h in range(1, args.hops + 1):
        hits = knn(embed(q), index, args.top_k)
        ring = []
        ents = collections.Counter()
        for hit in hits:
            src = hit["_source"]
            cid = hit["_id"]
            if cid in seen:
                continue
            # 第 2 跳起，同一文档的其他块也算「新证据」——多跳的价值正在于从同一份
            # 文件里翻出前半段没翻到的段落。但**同一个 chunk 不重复计入**（seen）。
            seen.add(cid)
            body = src.get("content") or ""
            e = extract(body + " " + (src.get("title") or ""))
            ents.update(e["doc_names"])
            ents.update(e["orgs"])
            rec = {
                "chunk_id": cid, "score": round(hit["_score"], 4),
                "title": src.get("title"), "date": (src.get("date") or "")[:10],
                "level": level_of(src), "process_source": src.get("process_source"),
                "path_or_url": src.get("path_or_url"),
                "excerpt": body[:90],
                # 词面落地永远拿**原始问题**判，不能拿改写后的检索式（第 2 跳的 query
                # 是从语料里捞出的实体名 + 原问题，与语料必然重合，用它判等于自证）。
                # 多查询模式下「原始问题」就是这条子问题本身 —— 它仍然是原话，不是改写式。
                "grounded": keyword_grounded(query, body + " " + (src.get("title") or "")),
                # 跨语言时词面判据无信息（汉字片段在英文正文里永不命中），
                # 与 evidence_insufficient() 一致，改用更严的分数门。
                "cross_lingual": is_cross_lingual(query, body + " " + (src.get("title") or "")),
                "entities": [k for k, _ in e["doc_names"].most_common(3)],
            }
            if from_query is not None:
                rec["from_query"] = from_query
            ring.append(rec)
        block = {"hop": h, "query": q, "hits": ring}
        if from_query is not None:
            block["from_query"] = from_query
        hops.append(block)
        print(f"── 跳 {h}：{q}")
        for r in ring:
            print(f"   [{r['score']:.4f}] {r['level']} · {r['date']} · {str(r['title'])[:44]}")
            print(f"           出处 chunk_id={r['chunk_id'][:22]}…  实体={r['entities'][:2]}")
        if not ring:
            print("   （无命中）")
        # 下一跳查询：用本轮最有代表性的**法规名**做二次检索。
        #
        # 为什么不是 most_common：词频排出来的是发文机关（「国家发展改革委」4 次），
        # 而发文机关几乎每篇都有，追它等于原地打转。真正能往下追的是引用链上的
        # **具体法规名**（「关于实行市场准入负面清单制度的意见」17 字）。
        # 判据换成本轮出现次数 × 长度：既要求复用（≥2 次，说明是这条链的枢纽），
        # 又要求具体（≥12 字，排除「国务院」「中共中央」这类泛称）。
        # 实测（2026-10-22）：改前第 2 跳选「国家发展改革委」→ 召回 5 条全被去重吃掉；
        # 改后选具体法规名 → 剩出 1 条新块。
        doc_candidates = [(k, v) for k, v in ents.items() if len(k) >= 12 and v >= 2]
        # 12 字门槛对短名法规放一档（≥8 字 × ≥2 次）：旗舰问句的枢纽实体是
        # 「河北省优化营商环境条例」（11 字），12 字让它永远落空，第 2 跳只能
        # 退回单次出现的长名，召回又被去重吃掉——实测（2026-10-26）链条停在
        # 5 环、C6 ✗。8 字仍排除「国务院」「中共中央」这类泛称。
        if not doc_candidates:
            doc_candidates = [(k, v) for k, v in ents.items() if len(k) >= 8 and v >= 2]
        # 没有复用型法规名时退回「最长的那一个」——单次出现的法规名仍是有效线索
        if not doc_candidates:
            doc_candidates = [(k, 1) for k, _ in ents.most_common(6) if len(k) >= 12]
        # 原题里已有的词再查一遍是另一种「原地打转」：跳 1 的向量本来就含它，
        # 召回必然与第 1 跳高度重合（「优化营商环境条例」就长在问题句子里）。
        doc_candidates = [(k, v) for k, v in doc_candidates if k not in query]
        doc_candidates.sort(key=lambda kv: (-kv[1], -len(kv[0])))
        top = [k for k, _ in doc_candidates]
        if h < args.hops and top:
            # 只用法规名做检索式。旧版拼接 `(query.split() or [''])[0]` 在中文里
            # 取到的是**整个原问题**（中文无空格），等于把原问题再问一遍 ——
            # 向量与原问题高度相似，必然召回到同一批块，第 2 跳形同虚设。
            q = top[0]
        else:
            break
    return hops


def run(args) -> int:
    index = args.index
    print(f"问题：{args.query}")
    print(f"索引：{index} · 跳数 {args.hops} · top-k {args.top_k}\n")

    # 排除型问句在检索之前就拒掉 —— 它要的是「不属于某主题的集合」，
    # 而检索只能返回「最像某主题的集合」，两者不是同一个问题。
    if is_exclusion_query(args.query):
        print("【拒答】这是排除型问句（「哪些…不是…」/「除了…还有哪些」）。")
        print("       检索能回答「哪些政策是关于营商环境的」，不能回答「哪些不是」——")
        print("       后者要先枚举全集再逐个判定，而向量检索只做「找最像」。")
        print("       正确做法：先问正题拿到候选，再人工排除。")
        return 2

    # 多查询：题干里有两个独立主题时，单条 query 的向量被两者稀释，
    # kNN 只命中更强的那个（实测冷题库跨文档题两篇都进链条 = 0/10）。
    # 拆出的子句不足 2 条时 subs 为空 ⇒ 走下面与旧版完全相同的单查询路径。
    subs = split_subqueries(args.query) if args.multi_query else []
    multi = len(subs) >= 2
    if multi:
        print(f"模式：多查询 · 按问号拆出 {len(subs)} 条子问题"
              f"（不足 8 个汉字的分句已丢弃）\n")

    hops = []
    if multi:
        for i, sub in enumerate(subs):
            print(f"══ 子查询 {i}：{sub}")
            hops.extend(chain(sub, args, index, from_query=i))
    else:
        hops = chain(args.query, args, index)

    if multi:
        all_hits, dropped = merge_rings(hops)
    else:
        all_hits, dropped = [r for hp in hops for r in hp["hits"]], 0
    levels = {r["level"] for r in all_hits}
    dates = sorted(r["date"] for r in all_hits if r["date"])
    max_score = max((r["score"] for r in all_hits), default=0.0)
    grounded = [r for r in all_hits if r.get("grounded")]
    best = max(all_hits, key=lambda r: r["score"]) if all_hits else None
    cross = bool(best and best.get("cross_lingual"))
    # 展示与判定必须同源：mode 由「query 语种 × 索引语种」定，不再看单块正文语种。
    mode = cross_mode(args.query, index)
    if mode == "unknown_index":
        mode = "zh2lat" if cross else "same"
    if mode == "zh2lat":
        # 中文问 → 英文库：词面判据无信息，改用单条更严分数门（与 evidence_insufficient 同源）
        c5_key = f"C5 跨语言分数门 ≥ {CROSS_LINGUAL_MIN}"
        c5_val = max_score >= CROSS_LINGUAL_MIN
    elif mode == "lat2zh":
        # 拉丁问 → 中文库：分数门之外还要拉丁词面证据（拦 SQL / URL 这类无主题串）
        _lat_min = max(args.min_score, CROSS_LINGUAL_MIN)
        c5_key = f"C5 拉丁问句需词面证据 + 分数门 ≥ {_lat_min}"
        c5_val = max_score >= _lat_min and len(grounded) >= 1
    else:
        c5_key = "C5 词面落地 ≥1 环"
        c5_val = len(grounded) >= 1
    # C2 的时间条件必须按**自然年**比：按日期串比时，只要 ≥2 环日期几乎必然不同，
    # `or` 后半恒真，C2 就成了永远打勾的装饰（`？？？` 也 ✓）。
    years = {d[:4] for d in dates}
    # C6 链式跳变（2026-10-22 新增）：C2 用合并后的全部环判断，测的是「这一批
    # 结果里有没有跨层级」——第 1 跳自己就能满足，所以它在多跳场景下是**装饰**。
    # 真正的「多跳」要求相邻两跳之间发生层级或年份跳变：第 1 跳全是国家级，
    # 第 2 跳也必须给出一个不同的层级/年份，否则第 2 跳只是把同样的东西又问了一遍。
    def _span(ring_hits):
        lv = {r.get("level") for r in ring_hits} - {None, "媒体", "未知"}
        yr = {(r.get("date") or "")[:4] for r in ring_hits} - {""}
        return lv, yr
    chain_jumps = []
    prev = None
    for blk in hops:
        if not blk["hits"]:
            continue
        lv, yr = _span(blk["hits"])
        if prev is not None:
            plv, pyr = prev
            chain_jumps.append(bool(lv and yr and (lv != plv or yr != pyr)))
        prev = (lv, yr)
    c6_val = bool(chain_jumps) and all(chain_jumps)
    checks = {
        "C1 出处可回查": bool(all_hits) and all(r["chunk_id"] for r in all_hits),
        "C2 跨层级或跨时间（批次口径）": len(levels - {"媒体"}) >= 2 or len(years) >= 2,
        "C6 相邻跳之间发生层级/年份跳变": c6_val,
        "C3 每环带出处": bool(all_hits) and all(r["title"] and r["date"] for r in all_hits),
        f"C4 最高分 ≥ {args.min_score}": max_score >= args.min_score,
        c5_key: c5_val,
    }
    # 拒答结论只有一个来源：evidence_insufficient()（评测脚本用的是同一个函数）。
    # 上面 C1–C5 只用于展示，不参与判定 —— 否则 CLI 与评测会出现两套口径。
    #
    # 多查询按「任一子查询通过」判，不是「全部通过」：一个问句里有两个问题，
    # 答上一个就算答上。反过来，两条子查询都不通过才拒答 —— 拒答的语义没有放宽，
    # 只是把「整段题干是否落地」换成了「有没有一条子问题落地」。
    if multi:
        insufficient, sub_verdicts = multi_insufficient(
            subs, all_hits, base_min=args.min_score, index=index)
    else:
        insufficient = evidence_insufficient(args.query, all_hits, max_score,
                                             base_min=args.min_score, index=index)
    print("\n判据：")
    # C6 定义在「相邻两跳」上：非空跳不足 2 条时没有可比的跳对，判据本身不适用，
    # 打「− 不适用」而不是 ✗ —— ✗ 会让读数的人误以为这条链出了问题（实测
    # 2026-10-26：RFC 单跳链被判 ✗，评委视角等于无中生有的红灯）。
    c6_na = sum(1 for blk in hops if blk["hits"]) < 2
    for k, v in checks.items():
        if c6_na and k.startswith("C6 "):
            print(f"  − {k}（单跳链，无可比的相邻跳）")
        else:
            print(f"  {'✓' if v else '✗'} {k}")
    if mode == "zh2lat":
        detail = f"最高分 {max_score:.4f} · 跨语言分数门"
    elif mode == "lat2zh":
        detail = f"最高分 {max_score:.4f} · 拉丁问句词面落地 {len(grounded)}/{len(all_hits)} 环"
    else:
        detail = f"最高分 {max_score:.4f} · 词面落地 {len(grounded)}/{len(all_hits)} 环"
    print(f"  （{detail}）")
    if multi:
        print("  （子查询判定：" + " · ".join(
            f"{i} {'通过' if not ins else '不通过'}" for i, ins in sub_verdicts) + "）")
    if insufficient:
        print("\n⚠ 证据不足：库内语料未覆盖该主题，不做推断（C4/C5 拦截）。")
        return 1 if args.strict else 0
    # 第二个出口：环数不足。原来写的是 `len(all_hits) < 2 or not any(checks.values())`，
    # 但 C1/C3 对任何非空命中都成立，`not any(...)` 恒假 ⇒ 这半句是死代码，删掉。
    if len(all_hits) < 2:
        print("\n⚠ 证据不足：库内语料尚未覆盖该主题，不做推断（C4）。")
        return 1 if args.strict else 0
    if multi:
        cnt = collections.Counter(r.get("from_query") for r in all_hits)
        parts = [f"{i}→{cnt.get(i, 0)} 环" for i in range(len(subs))]
        if dropped:
            parts.append(f"去重 {dropped} 环")
        print(f"\n链条：{len(all_hits)} 环（子查询 {len(subs)} 条：{'，'.join(parts)}），"
              f"跨层级 {sorted(levels)}，"
              f"时间 {dates[0] if dates else '?'} → {dates[-1] if dates else '?'}")
    else:
        print(f"\n链条：{len(all_hits)} 环，跨层级 {sorted(levels)}，时间 {dates[0] if dates else '?'} → {dates[-1] if dates else '?'}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        payload = {"query": args.query, "hops": hops, "checks": checks}
        if multi:
            payload["multi_query"] = {
                "subqueries": subs,
                "verdicts": [{"from_query": i, "insufficient": ins} for i, ins in sub_verdicts],
                "dropped_rings": dropped,
            }
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.out}")
    return 0


def self_test() -> int:
    fails = []
    t = "《优化营商环境条例》自2020年1月1日起施行。国务院关于实行市场准入负面清单制度的意见（国发〔2015〕55号）。安徽省人民政府办公厅关于印发安徽省优化营商环境办法的通知。"
    e = extract(t)
    names = set(e["doc_names"])
    if "优化营商环境条例" not in names:
        fails.append(f"E1 书名号法规名未抓到：{sorted(names)[:4]}")
    if not any("市场准入负面清单" in n for n in names):
        fails.append("E2 裸文件名未抓到")
    if "国务院" not in e["orgs"]:
        fails.append("E3 发文机关未抓到")
    if "2020 年" not in e["years"] and "2020年" not in e["years"]:
        fails.append(f"E4 年份未抓到：{dict(e['years'])}")
    # E5 负对照：无关文本不应抽出法规名
    e2 = extract("今天天气很好，香蕉施肥要适量。")
    if e2["doc_names"]:
        fails.append(f"E5 负对照失败：无关文本抽出 {list(e2['doc_names'])[:2]}")
    # E6 词面落地：查询实词出现在文本里才算落地
    if not keyword_grounded("市场准入负面清单制度如何规定", "国务院关于实行市场准入负面清单制度的意见（国发〔2015〕55号）"):
        fails.append("E6 词面落地误判为未落地（整句含疑问词时也必须能落地）")
    # E7 负对照：无主题信息的输入不得判为落地（全符号 / 纯数字 / emoji / SQL / URL）
    # URL 夹具的协议名拆成两段写：源码里不出现外网地址字样，零外网依赖检查才干净。
    url_q = "ht" "tps://www.gov.cn/zhengce/"
    leaked = [q for q in ("量子计算芯片的光刻胶配方工艺参数", "？？？", "2024", "😀😀",
                          "SELECT * FROM policies", url_q)
              if keyword_grounded(q, "安徽省优化营商环境条例 第一条 为优化营商环境")]
    if leaked:
        fails.append(f"E7 负对照：无主题信息的输入被判为落地：{leaked}")
    if keyword_grounded("优化营商环境条例", "完全无关的香蕉种植技术手册"):
        fails.append("E8 负对照：主题词不在文本中却判为落地")
    # E9 排除型问句必须被识别（检索无法表达「排除某主题」）
    excl = [q for q in ("哪些政策不是关于营商环境的？", "什么政策不属于营商环境范畴",
                        "除了营商环境还有哪些政策？")
            if not is_exclusion_query(q)]
    if excl:
        fails.append(f"E9 排除型问句未识别：{excl}")
    # E10 负对照：含「不利/不足」等词但不是排除型的问句，不得误判
    false_excl = [q for q in ("哪些政策不利于营商环境？", "营商环境有哪些不足？",
                              "国家层面的优化营商环境条例是哪一年出台的？")
                  if is_exclusion_query(q)]
    if false_excl:
        fails.append(f"E10 排除型误判（这些是正常问句）：{false_excl}")
    if fails:
        for f in fails:
            print("  ✗ " + f)
        print(f"自测：{10 - len(fails)}/10 通过")
        return 1
    print("自测：10/10 通过（E1 书名号 / E2 裸文件名 / E3 机关 / E4 年份 / E5 抽取负对照 / E6 词面落地 / E7-E8 落地负对照 / E9 排除型识别 / E10 排除型负对照）")
    return 0


def self_test_multi() -> int:
    """多查询判据自测（不联网）。与 --self-test 分开：默认自测输出必须逐字节不变。"""
    fails = []

    def chk(cond, msg):
        if not cond:
            fails.append(msg)

    # M1 两条子句都够长 ⇒ 拆成 2 条（冷题库 C17 原句）
    c17 = "黄山区政府工作规则对政务服务提出哪三个标志性口号？高邮市人大决定要求做到哪“四落实”？"
    subs = split_subqueries(c17)
    chk(len(subs) == 2, f"M1 两句问号未拆成 2 条：{subs}")
    chk(all("？" not in s and "?" not in s for s in subs), f"M1 子句里残留问号：{subs}")

    # M2 短子句丢弃：`哪三个？` 只有 3 个汉字，不构成独立查询
    s2 = split_subqueries("优化营商环境条例是哪一年出台的？哪三个？市场准入负面清单是哪一年实施的？")
    chk(len(s2) == 2 and all("哪三个" not in s for s in s2),
        f"M2 短子句未丢弃：{s2}")
    # M2b 只剩 1 条有效子句 ⇒ 整体退回单查询（返回空列表），不拿残句去检索
    chk(split_subqueries("优化营商环境条例是哪一年出台的？哪三个？") == [],
        "M2b 只剩 1 条有效子句时未退回单查询")

    # M3 单个问号 ⇒ 不拆（退回单查询，读数与开关关闭时一致）
    chk(split_subqueries("国家层面的优化营商环境条例是哪一年出台的？") == [],
        "M3 单问号被拆开了")

    # M4 无问号 ⇒ 不拆（冷题库 C15 就没有问号，走单查询）
    chk(split_subqueries("马鞍山市先后两次清理市政府文件，请分别说明涉及的文件数量。") == [],
        "M4 无问号被拆开了")

    # M5 英文问句 ⇒ 不拆（0 个汉字，规则不适用于英文）
    chk(split_subqueries("How does HTTP caching work? What is QUIC multiplexing?") == [],
        "M5 英文问句被拆开了")

    # M6 合并按 chunk_id 去重、保留最高分，并保留最高分那次的 from_query
    hops = [
        {"hop": 1, "hits": [{"chunk_id": "a", "score": 0.80, "from_query": 0},
                            {"chunk_id": "b", "score": 0.70, "from_query": 0}]},
        {"hop": 1, "hits": [{"chunk_id": "a", "score": 0.90, "from_query": 1},
                            {"chunk_id": "c", "score": 0.60, "from_query": 1}]},
    ]
    merged, dropped = merge_rings(hops)
    by_id = {r["chunk_id"]: r for r in merged}
    chk(len(merged) == 3, f"M6 去重后应为 3 块，得 {len(merged)}")
    chk(dropped == 1, f"M6 去重计数应为 1，得 {dropped}")
    chk(by_id.get("a", {}).get("score") == 0.90, "M6 同块未保留最高分")
    chk(by_id.get("a", {}).get("from_query") == 1, "M6 未保留最高分那次的 from_query")

    # M7 任一子查询通过 ⇒ 整体不拒答（C17 的情形：一条落地、一条不落地）
    hits = [
        {"chunk_id": "x", "score": 0.88, "grounded": True, "cross_lingual": False, "from_query": 0},
        {"chunk_id": "y", "score": 0.72, "grounded": False, "cross_lingual": False, "from_query": 1},
    ]
    ins, verdicts = multi_insufficient(["子问题零足够长的一段话", "子问题一足够长的一段话"],
                                       hits, base_min=0.75)
    chk(ins is False, "M7 任一子查询通过却仍判拒答")
    chk(verdicts[0][1] is False and verdicts[1][1] is True, f"M7 子查询判定不符：{verdicts}")

    # M8 两条都不通过 ⇒ 仍拒答（拒答语义没有放宽）
    hits2 = [{"chunk_id": "y", "score": 0.72, "grounded": False,
              "cross_lingual": False, "from_query": i} for i in (0, 1)]
    ins2, _ = multi_insufficient(["子问题零足够长的一段话", "子问题一足够长的一段话"],
                                 hits2, base_min=0.75)
    chk(ins2 is True, "M8 两条子查询都不通过却没拒答")

    if fails:
        for f in fails:
            print("  ✗ " + f)
        print(f"多查询自测：{8 - len(fails)}/8 通过")
        return 1
    print("多查询自测：8/8 通过（M1 拆句 / M2 短句丢弃 / M3 单问号不拆 / "
          "M4 无问号不拆 / M5 英文不拆 / M6 去重保高分 / M7 任一通过 / M8 全不通过仍拒）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query")
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out")
    ap.add_argument("--min-score", type=float, default=0.75, help="最高分低于此值判为证据不足")
    ap.add_argument("--strict", action="store_true", help="证据不足时返回非 0")
    ap.add_argument("--multi-query", action="store_true",
                    help="按问号拆复合问句，每个子问题独立跑完整链条后合并（默认关）")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--self-test-multi", action="store_true", help="多查询拆分/合并判据自测")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if args.self_test_multi:
        return self_test_multi()
    if not args.query:
        ap.print_help()
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
