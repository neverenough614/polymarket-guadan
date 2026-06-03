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


def pair_market_legs(quotes: List[Quote], min_size: float) -> Tuple[List[Leg], str]:
    """一个市场的 outcome 报价 → 完整套双腿。both-or-skip + 两腿同量(=min_size) + 自成交护栏。

    quotes 需恰为该市场两个 outcome 的 (token, price, size, reason)。
    返回 (legs, reason)：legs 为 [] 或长度 2 的 [(token, price, min_size)]。
    """
    if len(quotes) != 2:
        return [], f"not_binary_{len(quotes)}"
    for _t, price, _s, reason in quotes:
        if price is None:
            return [], f"one_sided:{reason}"
    # 自成交/亏损护栏：买 YES + 买 NO 的价之和必须 < 1，否则一套成本 ≥ $1 = 必亏（违反完整套套利前提）
    p_sum = sum(float(price) for _t, price, _s, _r in quotes)
    if p_sum >= 1.0:
        return [], f"crossed_pair_{p_sum:.3f}"
    return [(t, float(price), float(min_size)) for t, price, _s, _r in quotes], "ok"


def market_cost(legs: List[Leg]) -> float:
    """完整套预扣 USDT = Σ price×size（buyYES + buyNO，p+q≲1，故 ≈ size）。"""
    return sum(p * s for _t, p, s in legs)


def select_within_budget(
    market_legs: List[Tuple[Any, List[Leg]]],
    available: float,
    safety: float = 0.95,
) -> Tuple[List[Tuple[Any, List[Leg]]], float, int]:
    """market_legs 已按奖励降序 [(market_key, legs)]。贪心累计预扣 ≤ available×safety。

    返回 (selected, total_cost, dropped_for_budget)。cost≈size/市场，故预算耗尽后基本全跳。
    """
    budget = max(0.0, available) * safety
    total = 0.0
    selected: List[Tuple[Any, List[Leg]]] = []
    dropped = 0
    for key, legs in market_legs:
        if not legs:
            continue
        cost = market_cost(legs)
        if total + cost <= budget:        # 严格上限；5% 安全边际已吸收浮点噪声
            selected.append((key, legs))
            total += cost
        else:
            dropped += 1
    return selected, total, dropped


def build_plan(
    markets: List[Tuple[Any, List[Dict[str, Any]]]],
    quote_fn: Callable[[Dict[str, Any]], Quote],
    min_size_fn: Callable[[List[Dict[str, Any]]], float],
    available: float,
    safety: float = 0.95,
) -> Tuple[List[Tuple[Any, List[Leg]]], Dict[str, int], float, int]:
    """串起来：对每个市场报价→配对→预算守门。

    markets: [(market_key, [outcome_token,...])] 已按奖励降序。
    quote_fn(token)->(token,price,size,reason)；min_size_fn(tokens)->该市场 min_size。
    返回 (selected_market_legs, skip_reasons, total_cost, dropped_for_budget)。
    """
    paired: List[Tuple[Any, List[Leg]]] = []
    skip_reasons: Dict[str, int] = {}
    for key, tokens in markets:
        quotes = [quote_fn(t) for t in tokens]
        legs, reason = pair_market_legs(quotes, min_size_fn(tokens))
        if legs:
            paired.append((key, legs))
        else:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    selected, total, dropped = select_within_budget(paired, available, safety)
    if dropped:
        skip_reasons["budget_exhausted"] = skip_reasons.get("budget_exhausted", 0) + dropped
    return selected, skip_reasons, total, dropped
