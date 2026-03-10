"""
Collect historical market features for backtesting.

Data source priority:
1) CLOB prices-history (public, minute-level history)
2) Strategy token config metadata (min_size/max_spread/source)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.schema import BACKTEST_SCHEMA_VERSION, STANDARD_COLUMNS, ensure_schema


CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_INTERVAL = "1m"


@dataclass
class CollectConfig:
    strategy_tokens_json: str
    output_csv: str
    start: Optional[datetime]
    end: Optional[datetime]
    interval: str = DEFAULT_INTERVAL
    fidelity: int = 10
    max_retries: int = 3
    sleep_seconds: float = 0.1


def _safe_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(dt):
        return None
    return dt.to_pydatetime()


def _load_strategy_tokens(path: str) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("strategy_tokens.json must be a list.")
    return data


def _rolling_volatility(price_series: pd.Series, window: int) -> pd.Series:
    ret = np.log(price_series / price_series.shift(1)).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    # Minute-level annualization
    annual_factor = np.sqrt(60 * 24 * 252)
    return ret.rolling(window=window, min_periods=max(2, window // 4)).std().fillna(0.0) * annual_factor


def _proxy_depth(price: float, min_size: float, vol_1h: float, vol_6h: float) -> float:
    # Higher volatility => thinner effective depth
    risk = max(0.05, (vol_1h + vol_6h) / 2.0)
    base_value = max(80.0, min_size * max(price, 0.1) * 3.0)
    return max(50.0, base_value / (1.0 + risk))


def _history_endpoint(token_id: str, interval: str, fidelity: int) -> str:
    return f"{CLOB_HOST}/prices-history?interval={interval}&market={token_id}&fidelity={fidelity}"


def _fetch_price_history(session: requests.Session, token_id: str, interval: str, fidelity: int, max_retries: int) -> List[Dict]:
    url = _history_endpoint(token_id, interval, fidelity)
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            history = data.get("history", [])
            if isinstance(history, list):
                return history
            return []
        except Exception:
            if attempt == max_retries - 1:
                return []
            time.sleep(0.5 * (attempt + 1))
    return []


def _filter_time_window(df: pd.DataFrame, start: Optional[datetime], end: Optional[datetime]) -> pd.DataFrame:
    out = df.copy()
    if start is not None:
        out = out[out["timestamp"] >= start]
    if end is not None:
        out = out[out["timestamp"] <= end]
    return out


def _get_existing_last_ts(output_csv: str) -> Dict[str, pd.Timestamp]:
    if not os.path.exists(output_csv):
        return {}
    try:
        existing = ensure_schema(pd.read_csv(output_csv))
    except Exception:
        return {}
    if existing.empty:
        return {}
    grouped = existing.groupby("token_id", dropna=False)["timestamp"].max()
    return grouped.to_dict()


def _build_rows_for_token(token: Dict, history: List[Dict]) -> List[Dict]:
    token_id = str(token.get("token_id", ""))
    source = str(token.get("source", "unknown"))
    min_size = float(token.get("min_size", 0) or 0)
    max_spread = token.get("max_spread", None)
    max_spread = float(max_spread) if max_spread not in (None, "", "nan") else 0.03
    reward_daily_rate = float(token.get("rewards_daily_rate", 0) or 0)
    volume_24h = float(token.get("volume_24h", 0) or 0)

    if not history:
        return []

    df = pd.DataFrame(history)
    if "t" not in df.columns or "p" not in df.columns:
        return []
    df["timestamp"] = pd.to_datetime(df["t"], unit="s", errors="coerce", utc=True)
    df["last_price"] = pd.to_numeric(df["p"], errors="coerce").fillna(0.0).clip(lower=0.01, upper=0.99)
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if df.empty:
        return []

    half_spread = max(0.005, min(0.06, float(max_spread) / 2.0))
    df["best_bid"] = (df["last_price"] - half_spread).clip(lower=0.01, upper=0.99)
    df["best_ask"] = (df["last_price"] + half_spread).clip(lower=0.01, upper=0.99)
    df["mid_price"] = (df["best_bid"] + df["best_ask"]) / 2.0
    df["spread"] = (df["best_ask"] - df["best_bid"]).clip(lower=0.0)

    df["volatility_1h"] = _rolling_volatility(df["last_price"], window=60)
    df["volatility_6h"] = _rolling_volatility(df["last_price"], window=360)
    df["volatility_24h"] = _rolling_volatility(df["last_price"], window=1440)

    depth_proxy = [
        _proxy_depth(
            price=row["last_price"],
            min_size=min_size,
            vol_1h=row["volatility_1h"],
            vol_6h=row["volatility_6h"],
        )
        for _, row in df.iterrows()
    ]
    df["top3_bid_depth"] = depth_proxy
    df["top3_ask_depth"] = depth_proxy

    rows: List[Dict] = []
    for _, row in df.iterrows():
        rows.append(
            {
                "timestamp": row["timestamp"],
                "token_id": token_id,
                "source": source,
                "last_price": float(row["last_price"]),
                "best_bid": float(row["best_bid"]),
                "best_ask": float(row["best_ask"]),
                "mid_price": float(row["mid_price"]),
                "spread": float(row["spread"]),
                "top3_bid_depth": float(row["top3_bid_depth"]),
                "top3_ask_depth": float(row["top3_ask_depth"]),
                "volume_24h": volume_24h,
                "reward_daily_rate": reward_daily_rate,
                "min_size": min_size,
                "max_spread": float(max_spread),
                "volatility_1h": float(row["volatility_1h"]),
                "volatility_6h": float(row["volatility_6h"]),
                "volatility_24h": float(row["volatility_24h"]),
            }
        )
    return rows


def collect_dataset(cfg: CollectConfig) -> pd.DataFrame:
    tokens = _load_strategy_tokens(cfg.strategy_tokens_json)
    existing_last_ts = _get_existing_last_ts(cfg.output_csv)

    session = requests.Session()
    all_rows: List[Dict] = []

    for idx, token in enumerate(tokens, start=1):
        token_id = str(token.get("token_id", ""))
        if not token_id:
            continue

        history = _fetch_price_history(
            session=session,
            token_id=token_id,
            interval=cfg.interval,
            fidelity=cfg.fidelity,
            max_retries=cfg.max_retries,
        )
        rows = _build_rows_for_token(token, history)
        if not rows:
            print(f"[{idx}/{len(tokens)}] {token_id[:10]}... no history")
            continue

        token_df = ensure_schema(pd.DataFrame(rows))
        token_df = _filter_time_window(token_df, cfg.start, cfg.end)

        last_ts = existing_last_ts.get(token_id)
        if last_ts is not None:
            token_df = token_df[token_df["timestamp"] > last_ts]

        if token_df.empty:
            print(f"[{idx}/{len(tokens)}] {token_id[:10]}... no incremental rows")
            continue

        all_rows.extend(token_df.to_dict(orient="records"))
        print(f"[{idx}/{len(tokens)}] {token_id[:10]}... +{len(token_df)} rows")
        time.sleep(cfg.sleep_seconds)

    if not all_rows:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    collected = ensure_schema(pd.DataFrame(all_rows))
    return collected


def save_dataset(df: pd.DataFrame, output_csv: str) -> None:
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)

    if os.path.exists(output_csv):
        existing = ensure_schema(pd.read_csv(output_csv))
        merged = pd.concat([existing, df], ignore_index=True)
        merged = ensure_schema(merged)
        merged = merged.drop_duplicates(subset=["token_id", "timestamp"], keep="last")
    else:
        merged = ensure_schema(df)

    merged.to_csv(output_csv, index=False)

    meta = {
        "schema_version": BACKTEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(merged)),
        "tokens": int(merged["token_id"].nunique()),
    }
    meta_path = output_csv.replace(".csv", ".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"Saved dataset: {output_csv}")
    print(f"Saved metadata: {meta_path}")
    print(f"Rows={meta['rows']} Tokens={meta['tokens']}")


def parse_args() -> CollectConfig:
    parser = argparse.ArgumentParser(description="Collect Polymarket historical features for backtesting.")
    parser.add_argument(
        "--strategy-json",
        default="poly_maker_rs/strategy_tokens.json",
        help="Path to strategy tokens json.",
    )
    parser.add_argument(
        "--output-csv",
        default="backtest/data/backtest_dataset.csv",
        help="Output csv path.",
    )
    parser.add_argument("--start", default=None, help="Inclusive UTC start time, e.g. 2026-02-01T00:00:00Z")
    parser.add_argument("--end", default=None, help="Inclusive UTC end time, e.g. 2026-03-01T00:00:00Z")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, help="History interval, default 1m.")
    parser.add_argument("--fidelity", type=int, default=10, help="History fidelity query param.")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    return CollectConfig(
        strategy_tokens_json=args.strategy_json,
        output_csv=args.output_csv,
        start=_safe_datetime(args.start),
        end=_safe_datetime(args.end),
        interval=args.interval,
        fidelity=args.fidelity,
        max_retries=args.max_retries,
    )


def main() -> None:
    cfg = parse_args()
    df = collect_dataset(cfg)
    if df.empty:
        print("No rows collected.")
        return
    save_dataset(df, cfg.output_csv)


if __name__ == "__main__":
    main()
