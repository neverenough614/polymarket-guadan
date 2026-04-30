from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


Level = Tuple[float, float]


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "nan", "None"):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def normalize_levels(raw_levels: Optional[Iterable[Any]]) -> list[Level]:
    levels: list[Level] = []
    for level in raw_levels or []:
        if isinstance(level, Mapping):
            price = as_float(level.get("price"))
            size = as_float(level.get("size"))
        elif isinstance(level, Sequence) and len(level) >= 2 and not isinstance(level, (str, bytes)):
            price = as_float(level[0])
            size = as_float(level[1])
        else:
            price = as_float(getattr(level, "price", 0))
            size = as_float(getattr(level, "size", 0))
        if 0 < price < 1 and size > 0:
            levels.append((price, size))
    return levels


def q_score_ratio(price: float, mid: float, max_spread: float) -> float:
    if max_spread <= 0:
        return 0.0
    distance = abs(price - mid)
    if distance >= max_spread:
        return 0.0
    return ((max_spread - distance) / max_spread) ** 2


def estimate_order_reward_per_100(
    levels: Iterable[Any],
    my_price: float,
    my_size: float,
    midpoint: float,
    max_spread: float,
    rewards_daily_rate: float,
    side_reward_fraction: float = 0.5,
) -> Dict[str, Any]:
    my_price = as_float(my_price)
    my_size = as_float(my_size)
    midpoint = as_float(midpoint)
    max_spread = as_float(max_spread)
    rewards_daily_rate = as_float(rewards_daily_rate)

    empty = {
        "eligible_for_q": False,
        "q_score_ratio": 0.0,
        "my_q": 0.0,
        "visible_total_q": 0.0,
        "my_q_share": 0.0,
        "expected_daily_reward": 0.0,
        "expected_reward_per_100": 0.0,
        "rewards_daily_rate": round(rewards_daily_rate, 6),
        "my_order_notional": 0.0,
    }
    if my_price <= 0 or my_size <= 0 or midpoint <= 0 or max_spread <= 0 or rewards_daily_rate <= 0:
        return {**empty, "reason": "invalid_input"}

    my_ratio = q_score_ratio(my_price, midpoint, max_spread)
    if my_ratio <= 0:
        return {**empty, "reason": "zero_q_score"}

    visible_total_q = sum(
        q_score_ratio(price, midpoint, max_spread) * size
        for price, size in normalize_levels(levels)
    )
    my_q = my_ratio * my_size
    total_q = visible_total_q + my_q
    my_q_share = my_q / total_q if total_q > 0 else 0.0
    expected_daily_reward = rewards_daily_rate * side_reward_fraction * my_q_share
    my_order_notional = my_price * my_size
    expected_reward_per_100 = (
        expected_daily_reward * 100.0 / my_order_notional
        if my_order_notional > 0
        else 0.0
    )

    return {
        "eligible_for_q": True,
        "reason": "ok",
        "q_score_ratio": round(my_ratio, 6),
        "my_q": round(my_q, 6),
        "visible_total_q": round(visible_total_q, 6),
        "my_q_share": round(my_q_share, 6),
        "expected_daily_reward": round(expected_daily_reward, 6),
        "expected_reward_per_100": round(expected_reward_per_100, 6),
        "rewards_daily_rate": round(rewards_daily_rate, 6),
        "my_order_notional": round(my_order_notional, 6),
    }
