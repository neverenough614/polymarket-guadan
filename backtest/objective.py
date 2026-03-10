"""
Hybrid objective with risk-first gating.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import pandas as pd


@dataclass
class RiskLimits:
    max_drawdown_abs: float = -300.0
    max_single_close_loss_abs: float = -80.0
    min_close_events: int = 1


@dataclass
class ScoreWeights:
    net_pnl: float = 1.0
    reward: float = 0.3
    drawdown_penalty: float = 0.5
    close_loss_penalty: float = 0.7


def aggregate_metrics(metrics_df: pd.DataFrame) -> Dict[str, float]:
    if metrics_df.empty:
        return {
            "tokens": 0.0,
            "net_pnl": 0.0,
            "reward": 0.0,
            "pnl_trading": 0.0,
            "fees": 0.0,
            "max_drawdown": 0.0,
            "max_single_close_loss": 0.0,
            "close_events": 0.0,
            "fills": 0.0,
            "trades": 0.0,
        }

    return {
        "tokens": float(metrics_df["token_id"].nunique()),
        "net_pnl": float(metrics_df["net_pnl"].sum()),
        "reward": float(metrics_df["reward"].sum()),
        "pnl_trading": float(metrics_df["pnl_trading"].sum()),
        "fees": float(metrics_df["fees"].sum()),
        "max_drawdown": float(metrics_df["max_drawdown"].sum()),
        "max_single_close_loss": float(metrics_df["max_single_close_loss"].min()),
        "close_events": float(metrics_df["close_events"].sum()),
        "fills": float(metrics_df["fills"].sum()),
        "trades": float(metrics_df["trades"].sum()),
    }


def evaluate_hybrid_objective(
    summary: Dict[str, float],
    risk_limits: RiskLimits | None = None,
    weights: ScoreWeights | None = None,
) -> Dict[str, float | bool]:
    limits = risk_limits or RiskLimits()
    w = weights or ScoreWeights()

    max_drawdown = float(summary.get("max_drawdown", 0.0))
    max_single_close_loss = float(summary.get("max_single_close_loss", 0.0))
    close_events = float(summary.get("close_events", 0.0))

    risk_ok = (
        max_drawdown >= limits.max_drawdown_abs
        and max_single_close_loss >= limits.max_single_close_loss_abs
        and close_events >= limits.min_close_events
    )

    if not risk_ok:
        # Risk-first policy: infeasible combinations are heavily penalized.
        risk_penalty = 10_000.0
        score = -risk_penalty + float(summary.get("net_pnl", 0.0))
    else:
        score = (
            w.net_pnl * float(summary.get("net_pnl", 0.0))
            + w.reward * float(summary.get("reward", 0.0))
            - w.drawdown_penalty * abs(min(0.0, max_drawdown))
            - w.close_loss_penalty * abs(min(0.0, max_single_close_loss))
        )

    return {
        "risk_ok": risk_ok,
        "score": float(score),
        "max_drawdown": max_drawdown,
        "max_single_close_loss": max_single_close_loss,
    }
