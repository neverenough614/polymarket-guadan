"""
Backtest data schema definitions and normalization helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable

import pandas as pd


BACKTEST_SCHEMA_VERSION = "1.0"

# Canonical feature columns for replay and optimization.
STANDARD_COLUMNS = [
    "timestamp",
    "token_id",
    "source",
    "last_price",
    "best_bid",
    "best_ask",
    "mid_price",
    "spread",
    "top3_bid_depth",
    "top3_ask_depth",
    "volume_24h",
    "reward_daily_rate",
    "min_size",
    "max_spread",
    "volatility_1h",
    "volatility_6h",
    "volatility_24h",
]

NUMERIC_COLUMNS = [
    "last_price",
    "best_bid",
    "best_ask",
    "mid_price",
    "spread",
    "top3_bid_depth",
    "top3_ask_depth",
    "volume_24h",
    "reward_daily_rate",
    "min_size",
    "max_spread",
    "volatility_1h",
    "volatility_6h",
    "volatility_24h",
]


@dataclass(frozen=True)
class BacktestRecord:
    timestamp: str
    token_id: str
    source: str
    last_price: float
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    top3_bid_depth: float
    top3_ask_depth: float
    volume_24h: float
    reward_daily_rate: float
    min_size: float
    max_spread: float
    volatility_1h: float
    volatility_6h: float
    volatility_24h: float


def default_row() -> Dict[str, float | str]:
    return {
        "timestamp": "",
        "token_id": "",
        "source": "unknown",
        "last_price": 0.0,
        "best_bid": 0.0,
        "best_ask": 0.0,
        "mid_price": 0.0,
        "spread": 0.0,
        "top3_bid_depth": 0.0,
        "top3_ask_depth": 0.0,
        "volume_24h": 0.0,
        "reward_daily_rate": 0.0,
        "min_size": 0.0,
        "max_spread": 0.0,
        "volatility_1h": 0.0,
        "volatility_6h": 0.0,
        "volatility_24h": 0.0,
    }


def ensure_schema(df: pd.DataFrame, required_columns: Iterable[str] | None = None) -> pd.DataFrame:
    normalized = df.copy()
    required = list(required_columns) if required_columns else STANDARD_COLUMNS

    defaults = default_row()
    for col in required:
        if col not in normalized.columns:
            normalized[col] = defaults.get(col, 0.0 if col in NUMERIC_COLUMNS else "")

    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce", utc=True)
    normalized["token_id"] = normalized["token_id"].astype(str)
    normalized["source"] = normalized["source"].astype(str)

    for col in NUMERIC_COLUMNS:
        if col in normalized.columns:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce").fillna(0.0)

    normalized = normalized.sort_values(["token_id", "timestamp"]).reset_index(drop=True)
    return normalized
