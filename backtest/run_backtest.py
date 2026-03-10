"""
Run baseline/optimized backtest and export token-level metrics.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Dict, List

import pandas as pd

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.objective import aggregate_metrics, evaluate_hybrid_objective
from backtest.replay_engine import ReplayParams, replay_dataset
from backtest.schema import ensure_schema


def _params_from_runtime_map(m: Dict) -> ReplayParams:
    return ReplayParams(
        normal_size_ratio=float(m.get("NORMAL_SIZE_RATIO", 0.30)),
        normal_max_order_size=float(m.get("NORMAL_MAX_ORDER_SIZE", 800.0)),
        aggressive_size_ratio=float(m.get("AGGRESSIVE_SIZE_RATIO", 0.08)),
        aggressive_max_order_size=float(m.get("AGGRESSIVE_MAX_ORDER_SIZE", 300.0)),
        chain_rewards_size_ratio=float(m.get("CHAIN_REWARDS_SIZE_RATIO", 0.10)),
        chain_rewards_max_order_size=float(m.get("CHAIN_REWARDS_MAX_ORDER_SIZE", 500.0)),
        depth_threshold_tier1=float(m.get("DEPTH_THRESHOLD_TIER1", 1500.0)),
        depth_threshold_tier2=float(m.get("DEPTH_THRESHOLD_TIER2", 200.0)),
        close_price_offset=float(m.get("CLOSE_PRICE_OFFSET", 0.01)),
        min_position_to_close=float(m.get("MIN_POSITION_TO_CLOSE", 5.0)),
    )


def _bucket_filter(df: pd.DataFrame, start_hour: int, end_hour: int) -> pd.DataFrame:
    h = df["timestamp"].dt.hour
    if start_hour < end_hour:
        return df[(h >= start_hour) & (h < end_hour)]
    return df[(h >= start_hour) | (h < end_hour)]


def run_with_params_json(df: pd.DataFrame, params_json_path: str) -> pd.DataFrame:
    with open(params_json_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    rows: List[pd.DataFrame] = []
    covered_idx = set()
    for b in cfg.get("buckets", []) or []:
        dfb = _bucket_filter(df, int(b.get("start_hour", 0)), int(b.get("end_hour", 24)))
        if dfb.empty:
            continue
        covered_idx.update(dfb.index.tolist())
        rp = _params_from_runtime_map(b.get("params") or {})
        m = replay_dataset(dfb, rp)
        if not m.empty:
            m["bucket_name"] = b.get("name", "")
            rows.append(m)

    # Remaining timestamps use default params
    remaining = df[~df.index.isin(list(covered_idx))]
    if not remaining.empty:
        rp_default = _params_from_runtime_map(cfg.get("default") or {})
        m_def = replay_dataset(remaining, rp_default)
        if not m_def.empty:
            m_def["bucket_name"] = "default"
            rows.append(m_def)

    if not rows:
        return pd.DataFrame()
    combined = pd.concat(rows, ignore_index=True)
    agg_cols = [c for c in combined.columns if c not in ("token_id", "bucket_name")]
    final = combined.groupby("token_id", as_index=False)[agg_cols].sum(numeric_only=True)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LP backtest and export token metrics.")
    parser.add_argument("--dataset-csv", default="backtest/data/backtest_dataset.csv")
    parser.add_argument("--params-json", default=None, help="optimized_params.json path")
    parser.add_argument("--out-csv", required=True, help="output token metrics csv")
    args = parser.parse_args()

    df = ensure_schema(pd.read_csv(args.dataset_csv))
    if df.empty:
        raise SystemExit("Dataset is empty.")

    if args.params_json:
        token_metrics = run_with_params_json(df, args.params_json)
    else:
        token_metrics = replay_dataset(df, ReplayParams())

    token_metrics.to_csv(args.out_csv, index=False)
    summary = aggregate_metrics(token_metrics)
    decision = evaluate_hybrid_objective(summary)
    print(f"Saved backtest metrics: {args.out_csv}")
    print(f"net_pnl={summary['net_pnl']:.2f}, reward={summary['reward']:.2f}, risk_ok={decision['risk_ok']}, score={decision['score']:.2f}")


if __name__ == "__main__":
    main()
