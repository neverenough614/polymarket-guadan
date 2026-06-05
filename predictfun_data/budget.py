"""predict.fun 资金守门 + 市场配对（纯函数）。

实测结论（探针 create_order_insufficient_collateral_balance）：predict.fun 每张买单按
`price×size` 预扣 available balance，所有未成交买单 notional 之和不得超过余额——与 Polymarket
"挂单不锁资金"相反。故必须在下单前做预算守门。

设计：
  - 完整套一对 = 买 YES + 买 NO（买 NO=卖 YES），both-or-skip：两腿都能报价才挂，否则跳过该市场
    （避免单边裸方向）。
  - 两腿同量（取 min_size）：均衡完整套、杜绝残余敞口；min_size 而非动态量，是为在小余额下
    最大化市场数（多个独立 PP 流优于单市场大仓）。
  - 预算贪心：按奖励从高到低塞市场，累计预扣 ≤ available×safety 为止。
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

Quote = Tuple[Dict[str, Any], Optional[float], Optional[float], str]   # (token, price, size, reason)
Leg = Tuple[Dict[str, Any], float, float]                             # (token, price, size)


def available_after_open_buys(total_balance: float, open_orders: List[Dict[str, Any]]) -> float:
    """可用抵押 = 链上总额 − 已挂买单 notional 之和（predict.fun 挂买单预扣抵押）。

    实测：get_usdt_balance() 返回链上总额，不随挂单变化；available=总额−Σ(price×size of open BUY)。
    重启/重复下单时必须据此扣减，否则预算守门会基于"总额"重复授信→超额下单被拒/超扣。
    """
    locked = sum(
        float(o.get("price", 0) or 0) * float(o.get("size", 0) or 0)
        for o in (open_orders or [])
        if str(o.get("side", "")).upper() == "BUY"
    )
    return max(0.0, float(total_balance) - locked)


def pair_market_legs(quotes: List[Quote], min_size: float, allow_single_sided=None) -> Tuple[List[Leg], str]:
    """一个市场的 outcome 报价 → 完整套双腿或单腿。两腿同量(=min_size) + 自成交护栏。

    quotes 需恰为该市场两个 outcome 的 (token, price, size, reason)。
    - 两腿都可报价 → 完整套(both)，需 p_yes+p_no<1（否则一套≥$1 必亏）。
    - 仅一腿可报价 → 单向(若 allow_single_sided)。能报价已隐含"非极端+出场深度足"
      （compute_quote 已用价带+exit 门控），故单向风险可控。
    - 都不能 → 跳过。
    返回 (legs, reason)：legs 为 []/长度1/长度2 的 [(token, price, min_size)]。
    """
    if allow_single_sided is None:
        from config.bot_config import cfg
        allow_single_sided = cfg.predictfun_place.allow_single_sided
    if len(quotes) != 2:
        return [], f"not_binary_{len(quotes)}"
    # 索引取值（非解包）：quote 可为 4 元 (t,price,size,reason) 或 5 元（末位 eff 效率）
    quoted = [(q[0], float(q[1])) for q in quotes if q[1] is not None]
    skip_reason = next((q[3] for q in quotes if q[1] is None), "")
    if len(quoted) == 2:
        p_sum = quoted[0][1] + quoted[1][1]
        if p_sum >= 1.0:                          # 自成交/亏损护栏
            return [], f"crossed_pair_{p_sum:.3f}"
        return [(t, p, float(min_size)) for t, p in quoted], "ok"
    if len(quoted) == 1:
        if not allow_single_sided:
            return [], f"one_sided:{skip_reason}"
        t, p = quoted[0]
        return [(t, p, float(min_size))], "single_sided"
    return [], f"both_unquotable:{skip_reason}"


def market_cost(legs: List[Leg]) -> float:
    """完整套预扣 USDT = Σ price×size（buyYES + buyNO，p+q≲1，故 ≈ size）。"""
    return sum(p * s for _t, p, s in legs)


def select_within_budget(
    market_legs: List[Tuple[Any, List[Leg]]],
    available: float,
    safety: float = 0.95,
    max_markets: Optional[int] = None,
) -> Tuple[List[Tuple[Any, List[Leg]]], float, int]:
    """market_legs 已按奖励降序 [(market_key, legs)]。贪心累计预扣 ≤ available×safety。

    max_markets：选满这么多市场就停（None=不限，仅受预算约束）。
    返回 (selected, total_cost, dropped_for_budget)。cost≈size/市场，故预算耗尽后基本全跳。
    """
    budget = max(0.0, available) * safety
    total = 0.0
    selected: List[Tuple[Any, List[Leg]]] = []
    dropped = 0
    for key, legs in market_legs:
        if not legs:
            continue
        if max_markets is not None and len(selected) >= max_markets:
            break                          # 已选满目标市场数
        cost = market_cost(legs)
        if total + cost <= budget:        # 严格上限；5% 安全边际已吸收浮点噪声
            selected.append((key, legs))
            total += cost
        else:
            dropped += 1
    return selected, total, dropped


def _legs_efficiency(quotes: List[Quote], legs: List[Leg]) -> float:
    """该市场实际入选腿的预期日奖励合计（quote 末位 eff；缺省按 0）。

    按 token 对象身份对齐 legs（pair_market_legs 原样返回 quote 里的 token 对象），
    故单向时只累计入选那一腿的效率。
    """
    leg_ids = {id(t) for t, _p, _s in legs}
    total = 0.0
    for q in quotes:
        if id(q[0]) in leg_ids:
            total += float(q[4]) if (len(q) > 4 and q[4] is not None) else 0.0
    return total


def build_plan(
    markets: List[Tuple[Any, List[Dict[str, Any]]]],
    quote_fn: Callable[[Dict[str, Any]], Quote],
    min_size_fn: Callable[[List[Dict[str, Any]]], float],
    available: float,
    safety: float = 0.95,
    max_markets: Optional[int] = None,
) -> Tuple[List[Tuple[Any, List[Leg], float]], Dict[str, int], float, int]:
    """串起来：报价全部市场→配对→**按单位资本效率(PP/USDT)降序**→预算贪心。

    markets: [(market_key, [outcome_token,...])]。
    quote_fn(token)->(token,price,size,reason[,eff])：末位 eff=该腿预期日奖励（PP/日），
      由上游用 placer.order_efficiency 算出（缺省 0 时退化为保持输入顺序的稳定排序）。
    min_size_fn(tokens)->该市场 min_size。max_markets：目标挂满的市场数（None=不限，仍受预算约束）。
    注：固定抵押下最大化总 PP 是 0/1 背包；按 PP/USDT 比降序贪心是其标准近似（各市场成本
      ≪预算时近最优）。比"按绝对 PP 排"更榨资本——便宜高比市场会优先于昂贵低比市场。
      max_markets 仍作市场数上限，防过度分散（订单多→盯防御/反作弊撤单暴露面大）。
    返回 (selected[(key,legs,eff_total)], skip_reasons, total_cost, dropped_for_budget)。
    """
    budget = max(0.0, available) * safety
    skip_reasons: Dict[str, int] = {}
    priced: List[Tuple[Any, List[Leg], float]] = []
    for key, tokens in markets:
        quotes = [quote_fn(t) for t in tokens]
        legs, reason = pair_market_legs(quotes, min_size_fn(tokens))
        if not legs:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        priced.append((key, legs, _legs_efficiency(quotes, legs)))

    def _capital_efficiency(item: Tuple[Any, List[Leg], float]) -> float:
        """每 USDT 的预期日 PP；成本为 0（eff 也必为 0）时记 0，排到最后。"""
        _k, legs, eff = item
        cost = market_cost(legs)
        return (eff / cost) if cost > 0 else 0.0

    priced.sort(key=_capital_efficiency, reverse=True)   # 单位资本效率降序（稳定：全 0 时保持输入序）

    selected: List[Tuple[Any, List[Leg], float]] = []
    total = 0.0
    dropped = 0
    for key, legs, eff in priced:
        if max_markets is not None and len(selected) >= max_markets:
            break                          # 已选满目标市场数（已是效率最高的若干）
        cost = market_cost(legs)
        if total + cost <= budget:
            selected.append((key, legs, eff))
            total += cost
        else:
            dropped += 1
            skip_reasons["budget_exhausted"] = skip_reasons.get("budget_exhausted", 0) + 1
    return selected, skip_reasons, total, dropped
