"""
Grid/random search for time-bucketed parameter optimization.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List

import numpy as np
import pandas as pd

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.objective import RiskLimits, ScoreWeights, aggregate_metrics, evaluate_hybrid_objective
from backtest.replay_engine import ReplayParams, replay_dataset
from backtest.schema import ensure_schema


def _parse_float_list(value: str) -> List[float]:
    return [float(x.strip()) for x in value.split(",") if x.strip()]


def _parse_int_list(value: str) -> List[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _parse_buckets(value: str) -> List[Dict]:
    # Format: name:start-end;name:start-end, example 0-7:0-8;8-15:8-16;16-23:16-24
    result: List[Dict] = []
    for item in value.split(";"):
        item = item.strip()
        if not item:
            continue
        name, rng = item.split(":")
        start_s, end_s = rng.split("-")
        result.append({"name": name.strip(), "start_hour": int(start_s), "end_hour": int(end_s)})
    return result


def _bucket_filter(df: pd.DataFrame, start_hour: int, end_hour: int) -> pd.DataFrame:
    if df.empty:
        return df
    hours = df["timestamp"].dt.hour
    if start_hour < end_hour:
        mask = (hours >= start_hour) & (hours < end_hour)
    else:
        mask = (hours >= start_hour) | (hours < end_hour)
    return df[mask]


def _candidate_to_runtime_params(p: ReplayParams) -> Dict[str, float]:
    return {
        "NORMAL_SIZE_RATIO": p.normal_size_ratio,
        "NORMAL_MAX_ORDER_SIZE": p.normal_max_order_size,
        "AGGRESSIVE_SIZE_RATIO": p.aggressive_size_ratio,
        "AGGRESSIVE_MAX_ORDER_SIZE": p.aggressive_max_order_size,
        "CHAIN_REWARDS_SIZE_RATIO": p.chain_rewards_size_ratio,
        "CHAIN_REWARDS_MAX_ORDER_SIZE": p.chain_rewards_max_order_size,
        "DEPTH_THRESHOLD_TIER1": p.depth_threshold_tier1,
        "DEPTH_THRESHOLD_TIER2": p.depth_threshold_tier2,
        "CLOSE_PRICE_OFFSET": p.close_price_offset,
        "MIN_POSITION_TO_CLOSE": p.min_position_to_close,
    }


def build_candidates(args: argparse.Namespace) -> List[ReplayParams]:
    normals = _parse_float_list(args.normal_size_ratios)
    normal_maxes = _parse_float_list(args.normal_max_order_sizes)
    aggs = _parse_float_list(args.aggressive_size_ratios)
    agg_maxes = _parse_float_list(args.aggressive_max_order_sizes)
    tier1 = _parse_float_list(args.depth_threshold_tier1)
    tier2 = _parse_float_list(args.depth_threshold_tier2)
    close_offsets = _parse_float_list(args.close_price_offsets)
    close_mins = _parse_float_list(args.min_positions_to_close)

    combos = itertools.product(normals, normal_maxes, aggs, agg_maxes, tier1, tier2, close_offsets, close_mins)
    candidates: List[ReplayParams] = []
    for c in combos:
        rp = ReplayParams(
            normal_size_ratio=c[0],
            normal_max_order_size=c[1],
            aggressive_size_ratio=c[2],
            aggressive_max_order_size=c[3],
            depth_threshold_tier1=c[4],
            depth_threshold_tier2=c[5],
            close_price_offset=c[6],
            min_position_to_close=c[7],
            chain_rewards_size_ratio=args.chain_rewards_size_ratio,
            chain_rewards_max_order_size=args.chain_rewards_max_order_size,
            taker_fee_bps=args.taker_fee_bps,
            maker_fee_bps=args.maker_fee_bps,
            fill_sensitivity=args.fill_sensitivity,
        )
        candidates.append(rp)

    if args.max_candidates > 0 and len(candidates) > args.max_candidates:
        rng = np.random.default_rng(args.random_seed)
        picked = rng.choice(len(candidates), size=args.max_candidates, replace=False)
        candidates = [candidates[i] for i in picked]
    return candidates


def optimize_one_bucket(
    df_bucket: pd.DataFrame,
    candidates: List[ReplayParams],
    risk_limits: RiskLimits,
    score_weights: ScoreWeights,
) -> Dict:
    best = None
    rows = []
    for idx, p in enumerate(candidates, start=1):
        metrics = replay_dataset(df_bucket, p)
        summary = aggregate_metrics(metrics)
        decision = evaluate_hybrid_objective(summary, risk_limits=risk_limits, weights=score_weights)
        row = {
            "candidate_idx": idx,
            **asdict(p),
            **summary,
            **decision,
        }
        rows.append(row)
        if best is None or row["score"] > best["score"]:
            best = row
    return {"best": best, "all": pd.DataFrame(rows)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize LP params with historical backtest data.")
    parser.add_argument("--dataset-csv", default="backtest/data/backtest_dataset.csv")
    parser.add_argument("--out-json", default="backtest/optimized_params.json")
    parser.add_argument("--out-candidates-csv", default="backtest/data/optimization_candidates.csv")
    parser.add_argument("--time-buckets", default="0-7:0-8;8-15:8-16;16-23:16-24")

    parser.add_argument("--normal-size-ratios", default="0.22,0.30,0.38")
    parser.add_argument("--normal-max-order-sizes", default="600,800")
    parser.add_argument("--aggressive-size-ratios", default="0.05,0.08,0.12")
    parser.add_argument("--aggressive-max-order-sizes", default="200,300")
    parser.add_argument("--depth-threshold-tier1", default="1000,1500")
    parser.add_argument("--depth-threshold-tier2", default="150,200")
    parser.add_argument("--close-price-offsets", default="0.005,0.01,0.02")
    parser.add_argument("--min-positions-to-close", default="3,5,10")

    parser.add_argument("--chain-rewards-size-ratio", type=float, default=0.10)
    parser.add_argument("--chain-rewards-max-order-size", type=float, default=500.0)
    parser.add_argument("--taker-fee-bps", type=float, default=0.0)
    parser.add_argument("--maker-fee-bps", type=float, default=0.0)
    parser.add_argument("--fill-sensitivity", type=float, default=1.2)

    parser.add_argument("--risk-max-drawdown", type=float, default=-300.0)
    parser.add_argument("--risk-max-single-close-loss", type=float, default=-80.0)
    parser.add_argument("--risk-min-close-events", type=int, default=1)

    parser.add_argument("--weight-net-pnl", type=float, default=1.0)
    parser.add_argument("--weight-reward", type=float, default=0.3)
    parser.add_argument("--weight-drawdown-penalty", type=float, default=0.5)
    parser.add_argument("--weight-close-loss-penalty", type=float, default=0.7)

    parser.add_argument("--max-candidates", type=int, default=0, help="0 means exhaustive grid.")
    parser.add_argument("--random-seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df = ensure_schema(pd.read_csv(args.dataset_csv))
    if df.empty:
        raise SystemExit("Dataset is empty.")

    candidates = build_candidates(args)
    if not candidates:
        raise SystemExit("No parameter candidates built.")

    buckets = _parse_buckets(args.time_buckets)
    risk_limits = RiskLimits(
        max_drawdown_abs=args.risk_max_drawdown,
        max_single_close_loss_abs=args.risk_max_single_close_loss,
        min_close_events=args.risk_min_close_events,
    )
    score_weights = ScoreWeights(
        net_pnl=args.weight_net_pnl,
        reward=args.weight_reward,
        drawdown_penalty=args.weight_drawdown_penalty,
        close_loss_penalty=args.weight_close_loss_penalty,
    )

    bucket_results = []
    all_candidate_rows = []

    for b in buckets:
        df_bucket = _bucket_filter(df, b["start_hour"], b["end_hour"])
        result = optimize_one_bucket(df_bucket, candidates, risk_limits, score_weights)
        best = result["best"] or {}
        best_params = _candidate_to_runtime_params(
            ReplayParams(
                normal_size_ratio=float(best.get("normal_size_ratio", 0.30)),
                normal_max_order_size=float(best.get("normal_max_order_size", 800.0)),
                aggressive_size_ratio=float(best.get("aggressive_size_ratio", 0.08)),
                aggressive_max_order_size=float(best.get("aggressive_max_order_size", 300.0)),
                chain_rewards_size_ratio=float(best.get("chain_rewards_size_ratio", args.chain_rewards_size_ratio)),
                chain_rewards_max_order_size=float(best.get("chain_rewards_max_order_size", args.chain_rewards_max_order_size)),
                depth_threshold_tier1=float(best.get("depth_threshold_tier1", 1500.0)),
                depth_threshold_tier2=float(best.get("depth_threshold_tier2", 200.0)),
                close_price_offset=float(best.get("close_price_offset", 0.01)),
                min_position_to_close=float(best.get("min_position_to_close", 5.0)),
            )
        )
        bucket_results.append(
            {
                "name": b["name"],
                "start_hour": b["start_hour"],
                "end_hour": b["end_hour"],
                "score": float(best.get("score", 0.0)),
                "risk_ok": bool(best.get("risk_ok", False)),
                "summary": {
                    "net_pnl": float(best.get("net_pnl", 0.0)),
                    "reward": float(best.get("reward", 0.0)),
                    "max_drawdown": float(best.get("max_drawdown", 0.0)),
                    "max_single_close_loss": float(best.get("max_single_close_loss", 0.0)),
                },
                "params": best_params,
            }
        )

        cand_df = result["all"]
        cand_df["bucket_name"] = b["name"]
        all_candidate_rows.append(cand_df)
        print(
            f"[bucket={b['name']}] best score={best.get('score', 0):.2f} "
            f"net={best.get('net_pnl', 0):.2f} dd={best.get('max_drawdown', 0):.2f}"
        )

    # Global default from whole dataset
    overall = optimize_one_bucket(df, candidates, risk_limits, score_weights)["best"] or {}
    default_params = _candidate_to_runtime_params(
        ReplayParams(
            normal_size_ratio=float(overall.get("normal_size_ratio", 0.30)),
            normal_max_order_size=float(overall.get("normal_max_order_size", 800.0)),
            aggressive_size_ratio=float(overall.get("aggressive_size_ratio", 0.08)),
            aggressive_max_order_size=float(overall.get("aggressive_max_order_size", 300.0)),
            chain_rewards_size_ratio=float(overall.get("chain_rewards_size_ratio", args.chain_rewards_size_ratio)),
            chain_rewards_max_order_size=float(overall.get("chain_rewards_max_order_size", args.chain_rewards_max_order_size)),
            depth_threshold_tier1=float(overall.get("depth_threshold_tier1", 1500.0)),
            depth_threshold_tier2=float(overall.get("depth_threshold_tier2", 200.0)),
            close_price_offset=float(overall.get("close_price_offset", 0.01)),
            min_position_to_close=float(overall.get("min_position_to_close", 5.0)),
        )
    )

    payload = {
        "objective": "hybrid_risk_first",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "default": default_params,
        "buckets": bucket_results,
    }

    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if all_candidate_rows:
        out_df = pd.concat(all_candidate_rows, ignore_index=True)
        out_df.to_csv(args.out_candidates_csv, index=False)
        print(f"Saved candidate table: {args.out_candidates_csv}")

    print(f"Saved optimized params: {args.out_json}")


if __name__ == "__main__":
    main()
