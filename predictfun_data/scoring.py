"""predict.fun 奖励打分（SP4）—— 复用 Polymarket 的官方 LP 奖励公式。

predict.fun 与 Polymarket 共用同一套实时 LP 奖励机制，故 reward_per_100 打分
直接复用 data_updater.find_markets 的三个纯函数（get_bid_ask_range / generate_numbers /
add_formula_params），保证打分数学与 Polymarket 现网完全一致：
  - 复用 = 同一公式，零漂移；
  - 仅做 predict.fun 侧的输入适配（簿来自 backend，max_spread 已是小数）。

注意单位：Polymarket 的 get_bid_ask_range 用 ret['max_spread']/100（max_spread 以"厘/美分"
整数计，如 6→0.06）；predict.fun spreadThreshold 已是小数（0.06）。为复用同一函数，
本模块把传入的 max_spread_decimal 乘 100 塞进 ret['max_spread']，使其 /100 还原为小数。
"""
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from data_updater.find_markets import (
    get_bid_ask_range, generate_numbers, add_formula_params,
)

N_LEVELS = 3  # mid_reward 取 midpoint ±3 档（与 Polymarket process_single_row 一致）


def _levels_to_df(levels: List[Any]) -> pd.DataFrame:
    """[(price,size)...] 或 [BookLevel...] → DataFrame(price,size)（float）。"""
    rows = []
    for lv in levels or []:
        if hasattr(lv, "price"):
            rows.append((float(lv.price), float(lv.size)))
        elif isinstance(lv, (list, tuple)):
            rows.append((float(lv[0]), float(lv[1])))
        elif isinstance(lv, dict):
            rows.append((float(lv.get("price")), float(lv.get("size"))))
    return pd.DataFrame(rows, columns=["price", "size"]) if rows else pd.DataFrame(columns=["price", "size"])


def _side_rewards(grid_df: pd.DataFrame, book_df: pd.DataFrame,
                  midpoint: float, v: float, daily_reward: float,
                  tick_size: float, is_bid: bool) -> Tuple[float, float]:
    """返回 (best_reward_per_100, mid_reward_per_100)。复用 add_formula_params。"""
    if not book_df.empty:
        grid_df = grid_df.merge(book_df, on="price", how="left").fillna(0)
    else:
        grid_df = grid_df.copy()
        grid_df["size"] = 0.0
    if grid_df.empty:
        return 0.0, 0.0
    try:
        scored = add_formula_params(grid_df, midpoint, v, daily_reward)
    except Exception:
        return 0.0, 0.0
    best = round(float(scored["reward_per_100"].max()), 2) if "reward_per_100" in scored else 0.0
    # mid：bid 取 [mid-3tick, mid]，ask 取 [mid, mid+3tick] 的均值
    mid = 0.0
    try:
        if is_bid:
            mask = scored["price"].between(midpoint - N_LEVELS * tick_size, midpoint)
        else:
            mask = scored["price"].between(midpoint, midpoint + N_LEVELS * tick_size)
        rows = scored[mask]
        if not rows.empty:
            mid = round(float(rows["reward_per_100"].mean()), 4)
    except Exception:
        mid = 0.0
    return best, mid


def compute_reward_metrics(
    bids: List[Any],
    asks: List[Any],
    max_spread_decimal: Optional[float],
    daily_reward: float,
    tick_size: float = 0.01,
) -> Dict[str, Any]:
    """给定簿（Yes 口径）+ 奖励参数，算 Polymarket 同款 reward_per_100 系列指标。

    返回 best_bid/best_ask/midpoint/spread + bid/ask/gm/mid/sm_reward_per_100。
    max_spread_decimal 为 None 或 0 时按 0.01 兜底（与 Polymarket v 兜底一致）。
    """
    bids_df = _levels_to_df(bids)
    asks_df = _levels_to_df(asks)

    best_bid = float(bids_df["price"].max()) if not bids_df.empty else 0.0
    best_ask = float(asks_df["price"].min()) if not asks_df.empty else 0.0
    midpoint = (best_bid + best_ask) / 2.0
    spread = abs(best_ask - best_bid) if (best_bid and best_ask) else 0.0

    v = round(max_spread_decimal, 4) if max_spread_decimal else 0.01
    # 复用 get_bid_ask_range：它读 ret['max_spread']/100，故塞入 *100 的厘值
    ret = {"best_bid": best_bid, "best_ask": best_ask, "midpoint": midpoint,
           "max_spread": (max_spread_decimal * 100) if max_spread_decimal else 1}
    bid_from, bid_to, ask_from, ask_to = get_bid_ask_range(ret, tick_size)

    bid_grid = pd.DataFrame({"price": generate_numbers(bid_from, bid_to, tick_size)})
    ask_grid = pd.DataFrame({"price": generate_numbers(ask_from, ask_to, tick_size)})

    bid_best, bid_mid = _side_rewards(bid_grid, bids_df, midpoint, v, daily_reward, tick_size, is_bid=True)
    ask_best, ask_mid = _side_rewards(ask_grid, asks_df, midpoint, v, daily_reward, tick_size, is_bid=False)

    gm = round((bid_best * ask_best) ** 0.5, 2) if (bid_best > 0 and ask_best > 0) else 0.0
    mid = round((bid_mid * ask_mid) ** 0.5, 2) if (bid_mid > 0 and ask_mid > 0) else 0.0
    sm = round((bid_best + ask_best) / 2.0, 2)

    return {
        "best_bid": round(best_bid, 4),
        "best_ask": round(best_ask, 4),
        "midpoint": round(midpoint, 4),
        "spread": round(spread, 4),
        "bid_reward_per_100": bid_best,
        "ask_reward_per_100": ask_best,
        "gm_reward_per_100": gm,
        "mid_reward_per_100": mid,
        "sm_reward_per_100": sm,
    }


def score_discovery_row(row: Dict[str, Any], bids: List[Any], asks: List[Any],
                        tick_size: float = 0.01) -> Dict[str, Any]:
    """把 discovery 行 + 该市场 Yes 簿 → 含打分指标的行。

    daily_reward = hourly_rate × 24（与 Polymarket rewards_daily_rate 同口径）；
    max_spread（=spreadThreshold）为奖励价带 v。
    返回 原行 ∪ 打分指标 ∪ {rewards_daily_rate}。
    """
    hourly = float(row.get("hourly_rate", 0) or 0)
    daily = hourly * 24.0
    metrics = compute_reward_metrics(bids, asks, row.get("max_spread"), daily, tick_size)
    return {**row, **metrics, "rewards_daily_rate": daily}
