"""predict.fun 监控防御决策（SP5，纯逻辑）—— 稳挂少动 + 只买不裸卖。

predict.fun 不允许裸卖,每个 outcome token 只挂"一张买单(贴其 bid)";一个市场的
YES+NO 两个 token 各一张买单即构成共享簿上的双边。故监控是"每 token 管一张买单":
  REFILL  —— 我的买单消失(被成交/作废)→ 重挂买单(不撤,churn 最小)
  RECENTER—— 买单脱离奖励价带(mid±spreadThreshold)超滞回 → 撤后按当前 mid 重挂买单
其余一律不动(避免触发反作弊"撤单滥用")。频率由 churn_guard 在循环层把关。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .market_discovery import parse_iso

NONE = "NONE"
REFILL = "REFILL"
RECENTER = "RECENTER"


def reward_active(reward_ends_at: Any, now_dt: datetime) -> bool:
    """奖励窗口是否仍有效。无 endsAt / 解析失败 → True（无法判定→视为有效，不误撤）。

    奖励窗口结束（now ≥ endsAt）后应撤单并停挂：无奖励还挂只是白担被吃风险。
    reward_ends_at 来自发现期 rewards.current.endsAt（保守判定：到期即停；若被续期，
    重跑 discover 会刷新）。
    """
    if not reward_ends_at:
        return True
    end = parse_iso(reward_ends_at)
    if end is None:
        return True
    return now_dt < end


@dataclass(frozen=True)
class Decision:
    action: str          # NONE | REFILL | RECENTER
    cancel_first: bool   # 执行前是否先撤该 token 现有单（RECENTER=True）
    reason: str

    @property
    def is_cancel(self) -> bool:
        return self.cancel_first


_NOOP = Decision(NONE, False, "in-band")


def decide_action(
    my_bid: Optional[float],
    mid: Optional[float],
    max_spread: Optional[float],
    *,
    deadband_ticks: float = 1.0,
    tick_size: float = 0.01,
) -> Decision:
    """my_bid: 我在该 token 上的买价(None=无单)。返回克制的动作建议。"""
    if mid is None or not max_spread or max_spread <= 0:
        return _NOOP

    # ① 无我方买单 → 补挂(不撤)
    if my_bid is None:
        return Decision(REFILL, cancel_first=False, reason="missing bid → refill")

    # ② 买单脱离奖励价带(带滞回) → 撤后重挂
    band = float(max_spread)
    margin = float(deadband_ticks) * float(tick_size)
    if not (mid - band - margin <= my_bid <= mid + band + margin):
        return Decision(RECENTER, cancel_first=True, reason="out of reward band → recenter")

    return _NOOP
