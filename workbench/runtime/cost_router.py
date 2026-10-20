"""确定性成本路由：让简单任务走便宜模型，并且说得清是谁选的。

这个模块只做一件事：在**还没有发出任何请求**的时候，把「这次任务要多少能力」和
「候选目录里谁够用又最便宜」对起来，留下一份能读的决策记录。

三条纪律（与仓库其它地方同源）：

1. **不许让模型决定路由。** 分级只认输入本身（模式 / 长度 / 关键词），每一步都能
   指出是哪条规则抬的档。模型建议不能替换确定性决策。
2. **不许悄悄降级。** 没有候选够到需要的档位时，结论是「按原路由跑」，不是「拿一个
   更弱的顶上」——那会把「模型不够用」伪装成「任务很简单」。
3. **读不出来 ≠ 不存在。** 目录为空、计价单位混用、成本未知，各自说清是哪一种，
   不合并成一句「没找到更便宜的」。
4. **预算是一道闸，不是一次让步。** 设了单次上限就只在上限以内选；够档位的候选全都
   超支时，结论同样是「按原路由跑」——**不会为了省钱把档位降下来**（那是把「预算不够」
   伪装成「任务很简单」），也不会越支挑一个贵的。上限不设时行为与从前完全一致；
   设成 0 就是「只准用免费的」。
5. **省钱才动。** 候选和现在用的模型一个价时**不换**。路由是来省钱的，不是来换模型的：
   同价换过去，代价是把一场跑熟的运行换到一个没为这个项目验证过的模型上，收益是零。
6. **人的指名不被覆盖。** 档位、价格、上限，管的都只是「路由自己挑」的那条路。调用方
   钉住了出口或模型（``--model`` / ``LLM_MODEL`` / ``GATEWAY_MODEL`` / 请求里给了
   provider 或 model）时，路由只记录、不参与比较，而且**这一条排在预算之前**：一次
   显式选择不该因为「上限读不出来」而被拒。想用付费模型，指名就行，不需要动上限。

这里没有厂商名、没有网络、没有文件系统：候选目录由 adapters/ 一侧读进来再传进来
（R1/R2 要求 runtime/ 保持中立）。一次调用拿全：``plan()``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

MIN_TIER = 1
MAX_TIER = 3
TIER_LABELS = {1: "light", 2: "standard", 3: "heavy"}

# 模式只决定「到哪儿停」，不决定「怎么走」；但它确实说明这次要多少能力：
# ask 停在 S2、plan 停在 S3、code 走完整条链。
MODE_BASE_TIER = {"ask": 1, "plan": 2, "code": 3}

SHORT_CHARS = 40
LONG_CHARS = 400

# 确定性信号，故意写得很小：每一条都要能指着需求原文说「是这个词抬的档」。
# 词表本身就是这份实现的规格——加词要能说出它为什么代表更难/更简单。
HARD_SIGNALS = (
    "并发", "分布式", "一致性", "协议", "状态机", "解析器", "编译器",
    "重构", "优化", "加密", "调度", "缓存", "迁移", "回滚", "幂等",
    "多线程", "异步", "算法", "索引", "事务", "压测", "性能",
)
EASY_SIGNALS = (
    "打印", "输出", "求和", "计算", "格式化", "改名", "重命名", "注释",
    "文档", "说明", "示例", "读取", "转换", "排序",
)


@dataclass(frozen=True)
class ModelOffer:
    """一个候选：够不够强（tier）、贵不贵（cost + 单位）。

    ``tier`` 说的是「这个模型至少能撑到哪一档任务」，来源写在 ``tier_source`` 里
    （实测分 / 人工声明）——「我测过」和「我猜的」不该长得一样，``cost_source`` 同理。
    """

    provider: str
    model: str
    tier: int
    cost: float
    cost_unit: str
    tier_source: str = ""
    cost_source: str = ""
    note: str = ""


def _field(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(key, default)
    return getattr(raw, key, default)


def _as_offer(raw: Any) -> ModelOffer | None:
    """认得出就返回候选，认不出返回 None——不猜，也不把一次路由变成异常。

    一条目录写错了不该让整场运行起不来；但**没写清的候选也不能算数**：没有计价单位
    就没法说它便宜，宁可不选。
    """
    if isinstance(raw, ModelOffer):
        return raw
    try:
        provider = str(_field(raw, "provider") or "").strip()
        model = str(_field(raw, "model") or "").strip()
        tier = int(_field(raw, "tier"))
        cost = float(_field(raw, "cost"))
    except (TypeError, ValueError):
        return None
    if not provider or not model:
        return None
    if not MIN_TIER <= tier <= MAX_TIER:
        return None
    if cost < 0 or cost != cost:  # NaN 也不认
        return None
    unit = str(_field(raw, "cost_unit") or _field(raw, "unit") or "").strip()
    if not unit:
        return None
    return ModelOffer(
        provider=provider,
        model=model,
        tier=tier,
        cost=cost,
        cost_unit=unit,
        tier_source=str(_field(raw, "tier_source") or ""),
        cost_source=str(_field(raw, "cost_source") or ""),
        note=str(_field(raw, "note") or ""),
    )


def normalize_offers(raw: Any) -> list[ModelOffer]:
    """把任意形状的候选列表读成候选；同名同出口的重复只留第一条。"""
    if raw is None or isinstance(raw, (str, bytes, Mapping)):
        return []
    if not isinstance(raw, Sequence):
        return []
    seen: set[tuple[str, str]] = set()
    offers: list[ModelOffer] = []
    for item in raw:
        offer = _as_offer(item)
        if offer is None:
            continue
        key = (offer.provider, offer.model)
        if key in seen:
            continue
        seen.add(key)
        offers.append(offer)
    return offers


def task_tier(user_input: Any, *, mode: str = "code", domain: str | None = None) -> dict:
    """这次任务要多少能力：一条条规则加起来，每条都留下痕迹。

    读不出来（非字符串 / 空串）就只按模式给底档，并在 ``features.chars`` 里如实写 0——
    「没有需求原文」不是「需求很短」。
    """
    if mode not in MODE_BASE_TIER:
        raise ValueError(
            f"unknown mode {mode!r}; known: {', '.join(sorted(MODE_BASE_TIER))}"
        )
    text = user_input if isinstance(user_input, str) else ""
    chars = len(text.strip())
    tier = MODE_BASE_TIER[mode]
    signals = [f"mode:{mode}"]

    hard = sorted({word for word in HARD_SIGNALS if word in text})
    easy = sorted({word for word in EASY_SIGNALS if word in text})

    # 「短」只有在没有别的证据时才算简单：一句话说「把并发调度重构成状态机」，
    # 它短，但它不是简单任务。反过来，长文里出现简单词也照样是简单任务。
    if chars and chars < SHORT_CHARS and not hard:
        tier -= 1
        signals.append(f"short:{chars}<{SHORT_CHARS}")
    elif chars >= LONG_CHARS:
        tier += 1
        signals.append(f"long:{chars}>={LONG_CHARS}")

    # 命中了难词就不再拿简单词往下压：同一句话里既说「重构」又说「注释」，
    # 要按难的那条算。
    if hard:
        tier += 1
        signals.append("hard:" + ",".join(hard))
    elif easy:
        tier -= 1
        signals.append("easy:" + ",".join(easy))

    tier = max(MIN_TIER, min(MAX_TIER, tier))
    return {
        "tier": tier,
        "tier_name": TIER_LABELS[tier],
        "signals": signals,
        "features": {
            "chars": chars,
            "mode": mode,
            "domain": domain,
            "hard_terms": hard,
            "easy_terms": easy,
        },
    }


def _considered_entry(offer: ModelOffer, verdict: str, why: str) -> dict:
    return {
        "provider": offer.provider,
        "model": offer.model,
        "tier": offer.tier,
        "cost": offer.cost,
        "cost_unit": offer.cost_unit,
        "tier_source": offer.tier_source,
        "verdict": verdict,
        "why": why,
    }


def _budget(max_cost: Any) -> float | None:
    """把单次上限读成一个非负的数；读不出来就说读不出来，不悄悄当成「不设上限」。

    把 ``"abc"`` 读成 ``None`` 是危险的：那会把「我设了上限但写错了」变成「没有上限」，
    于是一次本该被拦住的付费选择照常发生。所以这里和 ``_as_offer`` 一样——宁可报错。
    """
    if max_cost is None:
        return None
    try:
        value = float(max_cost)
    except (TypeError, ValueError):
        raise ValueError("max_cost must be a number or None") from None
    if value != value or value < 0:  # NaN 也不认
        raise ValueError("max_cost must be a non-negative number")
    return value


def choose(offers: Any, need_tier: int, *, pinned: Mapping[str, Any] | None = None,
           baseline: Mapping[str, Any] | None = None,
           max_cost: float | None = None) -> dict:
    """在候选里选一个：够用、最便宜、**同价不换**。

    返回的 ``considered`` 是**每个候选各自的下场**，不是一句总结——「它为什么没被选上」
    和「选上了谁」一样重要。

    ``max_cost`` 是单次请求的钱上限（``None`` = 不设）。它只筛「谁有资格被选」，
    **不参与降档**：上限以内没人够到需要的能力时，结论是「按原路由跑」。它也只管
    「路由自己挑」的那条路——``pinned`` 一给，上限连读都不读。

    ``baseline`` 里那个「现在正在用的模型」还有第二个作用：它要是也在候选里、而且不比
    别人贵，就**不换**。路由是来省钱的，不是来换模型的——同价换过去只有风险没有收益。

    ``applied`` 只有一个含义：**真的换过出口**。挑中的就是 ``baseline`` 那一个时，
    ``applied`` 是 ``False``、``chosen_by`` 是 ``"confirmed"``——那一次它什么也没做，
    只是确认了「你现在这个已经是最便宜的」。这不是边角情形：默认配置（上限 0、出口
    免费）下它就是常态，因为免费候选里挑不出更便宜的。所以这道闸日常输出的是「确认
    不换」，只有在基线本身要花钱时才会真的换。
    """
    try:
        need = int(need_tier)
    except (TypeError, ValueError):
        raise ValueError("need_tier must be an integer") from None
    need = max(MIN_TIER, min(MAX_TIER, need))

    # 先看有没有人指名。这一步**必须在预算之前**：一次显式选择不该因为「上限读不出来」
    # 而被拒——上限管的是路由自己挑的那条路，不是人的决定。
    if pinned and (pinned.get("provider") or pinned.get("model")):
        return {
            "applied": False,
            "chosen_by": "explicit",
            "provider": str(pinned.get("provider") or "") or None,
            "model": str(pinned.get("model") or "") or None,
            "need_tier": need,
            "tier_name": TIER_LABELS[need],
            "reason": "调用方显式钉住了出口/模型：成本路由不覆盖人的选择",
            "considered": [],
            "savings": None,
        }

    offered = normalize_offers(offers)
    budget = _budget(max_cost)

    if not offered:
        return {
            "applied": False,
            "chosen_by": "unrouted",
            "provider": None,
            "model": None,
            "need_tier": need,
            "tier_name": TIER_LABELS[need],
            "reason": "候选目录是空的：没有可比较的贵与便宜，按原路由跑",
            "considered": [],
            "savings": None,
        }

    units = sorted({offer.cost_unit for offer in offered})
    if len(units) > 1:
        # 混着两种计价单位比大小，比出来的「更便宜」是假的。
        return {
            "applied": False,
            "chosen_by": "unrouted",
            "provider": None,
            "model": None,
            "need_tier": need,
            "tier_name": TIER_LABELS[need],
            "reason": (
                "候选目录混了 "
                + str(len(units))
                + " 种计价单位（"
                + "、".join(units)
                + "），不可比较——按原路由跑"
            ),
            "considered": [
                _considered_entry(offer, "not_comparable", f"单位 {offer.cost_unit}") 
                for offer in offered
            ],
            "savings": None,
        }

    considered: list[dict] = []
    for offer in offered:
        if offer.tier < need:
            considered.append(
                _considered_entry(offer, "too_weak", f"tier {offer.tier} < 需要 {need}")
            )
    eligible = [offer for offer in offered if offer.tier >= need]

    if not eligible:
        strongest = max(offered, key=lambda o: (o.tier, -o.cost, o.provider, o.model))
        return {
            "applied": False,
            "chosen_by": "unrouted",
            "provider": strongest.provider,
            "model": strongest.model,
            "need_tier": need,
            "tier_name": TIER_LABELS[need],
            "reason": (
                f"没有候选达到这次需要的档位（目录里最高的只有 tier {strongest.tier}）："
                "不降级跑，按原路由"
            ),
            "considered": considered,
            "savings": None,
        }

    # 预算闸：够档位的候选里，谁能被选。它只筛资格，**不降档位**——所以下面
    # 一旦筛空，走的是「按原路由」，不是「退而求其次挑个弱的」。
    affordable = eligible
    if budget is not None:
        affordable = [offer for offer in eligible if offer.cost <= budget]
        for offer in eligible:
            if offer.cost > budget:
                considered.append(
                    _considered_entry(
                        offer,
                        "over_budget",
                        f"够用（tier {offer.tier}）但 {offer.cost} > 上限 {budget} {offer.cost_unit}",
                    )
                )

    if not affordable:
        cheapest = min(eligible, key=lambda o: (o.cost, o.provider, o.model))
        return {
            "applied": False,
            "chosen_by": "unrouted",
            "provider": None,
            "model": None,
            "need_tier": need,
            "tier_name": TIER_LABELS[need],
            "max_cost": budget,
            "reason": (
                f"够档位（tier {need}）的候选都超过单次上限 {budget} {cheapest.cost_unit}"
                f"（最便宜的也要 {cheapest.cost}）：不降级也不超支，按原路由"
            ),
            "considered": considered,
            "savings": None,
        }

    # 省钱才动。候选和现在用的模型一个价，就没有「省」可言——这时候换过去，只是把一场
    # 跑熟的运行换到一个没为这个项目验证过的模型上：代价是真的，收益是零。所以同价不动。
    # （这条和上面的预算闸是两件事：闸管「准不准花钱」，这条管「值不值得换」。）
    current = None
    if isinstance(baseline, Mapping) and baseline.get("model"):
        base_provider = str(baseline.get("provider") or "")
        base_model = str(baseline.get("model") or "")
        current = next(
            (
                offer
                for offer in offered
                if (not base_provider or offer.provider == base_provider)
                and offer.model == base_model
            ),
            None,
        )

    chosen = min(affordable, key=lambda o: (o.cost, o.provider, o.model))
    held = (
        current is not None
        and any(offer is current for offer in affordable)
        and chosen.cost >= current.cost
    )
    if held:
        chosen = current
    for offer in sorted(affordable, key=lambda o: (o.cost, o.provider, o.model)):
        if offer is chosen:
            considered.append(
                _considered_entry(offer, "chosen", f"够用（tier {offer.tier}）且最便宜")
            )
        else:
            considered.append(
                _considered_entry(
                    offer, "pricier", f"也够用，但 {offer.cost} > {chosen.cost} {chosen.cost_unit}"
                )
            )

    # 省了多少钱只在**真的换了**的时候才算得出来：挑中的还是原来那一个，比例恒等于 0，
    # 报一个「省了 0%」只是噪音。
    savings = None
    if not held and current is not None and current.cost > 0:
        savings = {
            "baseline_provider": current.provider,
            "baseline_model": current.model,
            "baseline_cost": current.cost,
            "chosen_cost": chosen.cost,
            "cost_unit": chosen.cost_unit,
            "ratio": round(1 - chosen.cost / current.cost, 4),
            "baseline_source": "catalog" if current.cost_source else "unknown",
        }

    return {
        # `applied` 只有一个含义：真的换过出口。挑中的就是 baseline 那一个时它是 False，
        # `chosen_by` 说 "confirmed"——那一次它什么都没做，只是确认了「你已经是最便宜的」。
        "applied": not held,
        "chosen_by": "confirmed" if held else "cost_router",
        "provider": chosen.provider,
        "model": chosen.model,
        "need_tier": need,
        "tier_name": TIER_LABELS[need],
        "max_cost": budget,
        "kept_baseline": held,
        "reason": (
            f"需要 {TIER_LABELS[need]}（tier {need}）；够用的候选里 "
            f"{chosen.cost} {chosen.cost_unit} 最便宜"
            + (
                f"，而你现在用的就是这个（{chosen.provider}/{chosen.model}）：确认不换"
                if held
                else ""
            )
        ),
        "considered": considered,
        "savings": savings,
    }


def plan(
    user_input: Any,
    offers: Any,
    *,
    mode: str = "code",
    domain: str | None = None,
    pinned: Mapping[str, Any] | None = None,
    baseline: Mapping[str, Any] | None = None,
    max_cost: float | None = None,
) -> dict:
    """一次拿全：这次要多少能力 + 选谁 + 为什么。

    ``max_cost`` 一路透到 ``choose()``：它是单次请求的钱上限，不是难度旋钮。
    """
    read = task_tier(user_input, mode=mode, domain=domain)
    decision = choose(offers, read["tier"], pinned=pinned, baseline=baseline, max_cost=max_cost)
    decision["need_tier"] = read["tier"]
    decision["tier_name"] = read["tier_name"]
    decision["features"] = read["features"]
    decision["signals"] = read["signals"]
    if decision.get("chosen_by") in ("cost_router", "confirmed"):
        decision["reason"] = decision["reason"] + "（" + "；".join(read["signals"]) + "）"
    return decision


def format_decision(decision: Mapping[str, Any]) -> str:
    """一行给人看的结论。读不出来就说读不出来，不编。"""
    if not isinstance(decision, Mapping):
        return "路由决策：读不出来"
    who = decision.get("chosen_by") or "unknown"
    where = "/".join(
        part for part in (str(decision.get("provider") or ""), str(decision.get("model") or "")) if part
    )
    reason = decision.get("reason") or ""
    if not where:
        return f"路由决策：{who}——{reason}"
    return f"路由决策：{who} → {where}——{reason}"
