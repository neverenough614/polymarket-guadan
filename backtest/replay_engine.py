"""
Event replay engine for LP strategy backtests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from backtest.schema import ensure_schema


@dataclass
class ReplayParams:
    normal_size_ratio: float = 0.30
    normal_max_order_size: float = 800.0
    aggressive_size_ratio: float = 0.08
    aggressive_max_order_size: float = 300.0
    chain_rewards_size_ratio: float = 0.10
    chain_rewards_max_order_size: float = 500.0
    depth_threshold_tier1: float = 1500.0
    depth_threshold_tier2: float = 200.0
    close_price_offset: float = 0.01
    min_position_to_close: float = 5.0
    taker_fee_bps: float = 0.0
    maker_fee_bps: float = 0.0
    fill_sensitivity: float = 1.2


def _volatility_sum_proxy(row: pd.Series) -> float:
    # Keep shape close to main.py where volatility_sum is used.
    # This scales annualized volatility to a 0-60ish range.
    v24 = float(row.get("volatility_24h", 0.0) or 0.0)
    return max(0.0, min(60.0, v24 * 10.0))


def _dynamic_order_size(row: pd.Series, params: ReplayParams) -> float:
    source = str(row.get("source", "Normal LP"))
    mid = float(row.get("mid_price", 0.0) or 0.0)
    min_size = float(row.get("min_size", 0.0) or 0.0)
    bid_depth = float(row.get("top3_bid_depth", 0.0) or 0.0)
    ask_depth = float(row.get("top3_ask_depth", 0.0) or 0.0)
    if mid <= 0.0 or (bid_depth <= 0 and ask_depth <= 0):
        return 0.0

    if source == "High Reward":
        ratio, max_size = params.aggressive_size_ratio, params.aggressive_max_order_size
    elif source == "Chain Rewards":
        ratio, max_size = params.chain_rewards_size_ratio, params.chain_rewards_max_order_size
    else:
        ratio, max_size = params.normal_size_ratio, params.normal_max_order_size

    bid_target = (bid_depth * ratio / mid) if bid_depth > 0 else 0.0
    ask_target = (ask_depth * ratio / mid) if ask_depth > 0 else 0.0
    target = min(bid_target, ask_target) if (bid_target > 0 and ask_target > 0) else max(bid_target, ask_target)

    vol_sum = _volatility_sum_proxy(row)
    if vol_sum <= 10:
        vol_factor = 1.0
    else:
        vol_factor = max(0.2, 1.0 - (vol_sum - 10) / 60.0)
    target *= vol_factor

    if target < min_size:
        return 0.0
    return float(round(min(target, max_size)))


def _pick_quotes(row: pd.Series, params: ReplayParams) -> Tuple[float, float]:
    bid_depth = float(row.get("top3_bid_depth", 0.0) or 0.0)
    ask_depth = float(row.get("top3_ask_depth", 0.0) or 0.0)
    best_bid = float(row.get("best_bid", 0.0) or 0.0)
    best_ask = float(row.get("best_ask", 0.0) or 0.0)
    if best_bid <= 0 or best_ask <= 0:
        return 0.0, 0.0
    if bid_depth < params.depth_threshold_tier2 or ask_depth < params.depth_threshold_tier2:
        return 0.0, 0.0
    return best_bid, best_ask


def _fill_probability(quote_price: float, next_last: float, spread: float, sensitivity: float, is_bid: bool) -> float:
    spread = max(spread, 0.01)
    if is_bid:
        if next_last > quote_price:
            return 0.0
        move = quote_price - next_last
    else:
        if next_last < quote_price:
            return 0.0
        move = next_last - quote_price
    p = min(1.0, max(0.0, sensitivity * move / spread))
    return p


def replay_market(df_market: pd.DataFrame, params: ReplayParams) -> Dict[str, float]:
    if df_market.empty:
        return {
            "trades": 0,
            "fills": 0,
            "close_events": 0,
            "reward": 0.0,
            "pnl_trading": 0.0,
            "fees": 0.0,
            "net_pnl": 0.0,
            "max_drawdown": 0.0,
            "max_single_close_loss": 0.0,
        }

    df_market = df_market.sort_values("timestamp").reset_index(drop=True)
    position = 0.0
    avg_cost = 0.0
    reward = 0.0
    pnl_trading = 0.0
    fees = 0.0
    fills = 0
    close_events = 0
    trades = 0
    equity_curve: List[float] = [0.0]
    max_single_close_loss = 0.0

    for i in range(len(df_market) - 1):
        row = df_market.iloc[i]
        nxt = df_market.iloc[i + 1]

        spread = float(row.get("spread", 0.02) or 0.02)
        bid_quote, ask_quote = _pick_quotes(row, params)
        order_size = _dynamic_order_size(row, params)
        min_size = float(row.get("min_size", 0.0) or 0.0)

        if bid_quote > 0 and ask_quote > 0 and order_size > 0:
            # Reward proxy: if both sides are quoted and size passes min_size.
            if order_size >= min_size and min_size > 0:
                reward += float(row.get("reward_daily_rate", 0.0) or 0.0) / (24.0 * 60.0)

            next_last = float(nxt.get("last_price", 0.0) or 0.0)

            buy_fill_prob = _fill_probability(bid_quote, next_last, spread, params.fill_sensitivity, is_bid=True)
            sell_fill_prob = _fill_probability(ask_quote, next_last, spread, params.fill_sensitivity, is_bid=False)

            buy_filled = order_size * buy_fill_prob
            sell_filled = order_size * sell_fill_prob

            if buy_filled > 0:
                fills += 1
                trades += 1
                new_pos = position + buy_filled
                avg_cost = ((avg_cost * position) + (bid_quote * buy_filled)) / max(new_pos, 1e-9) if new_pos > 0 else 0.0
                position = new_pos
                fees += bid_quote * buy_filled * (params.maker_fee_bps / 10000.0)

            if sell_filled > 0 and position > 0:
                fills += 1
                trades += 1
                realized = (ask_quote - avg_cost) * min(position, sell_filled)
                pnl_trading += realized
                fees += ask_quote * min(position, sell_filled) * (params.maker_fee_bps / 10000.0)
                position = max(0.0, position - sell_filled)
                if position == 0:
                    avg_cost = 0.0

        # Defensive close proxy: if inventory exceeds threshold, close at best_bid-offset.
        if position >= params.min_position_to_close:
            close_bid = max(0.01, float(row.get("best_bid", 0.01) or 0.01) - params.close_price_offset)
            realized = (close_bid - avg_cost) * position
            pnl_trading += realized
            fees += close_bid * position * (params.taker_fee_bps / 10000.0)
            close_events += 1
            if realized < max_single_close_loss:
                max_single_close_loss = realized
            position = 0.0
            avg_cost = 0.0

        mtm = 0.0
        if position > 0:
            mtm = (float(row.get("mid_price", 0.0) or 0.0) - avg_cost) * position
        equity_curve.append(pnl_trading + reward - fees + mtm)

    curve = np.array(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(curve)
    drawdown = curve - running_max
    max_drawdown = float(drawdown.min()) if len(drawdown) else 0.0

    net = pnl_trading + reward - fees
    return {
        "trades": float(trades),
        "fills": float(fills),
        "close_events": float(close_events),
        "reward": float(reward),
        "pnl_trading": float(pnl_trading),
        "fees": float(fees),
        "net_pnl": float(net),
        "max_drawdown": float(max_drawdown),
        "max_single_close_loss": float(max_single_close_loss),
    }


def replay_dataset(df: pd.DataFrame, params: ReplayParams) -> pd.DataFrame:
    data = ensure_schema(df)
    if data.empty:
        return pd.DataFrame()
    metrics: List[Dict] = []
    for token_id, grp in data.groupby("token_id", dropna=False):
        m = replay_market(grp, params)
        m["token_id"] = str(token_id)
        metrics.append(m)
    return pd.DataFrame(metrics)
