"""predict.fun 监控防御决策（SP5，纯逻辑）—— 稳挂少动。

只在两种情况建议动作，其余一律不动（避免触发反作弊"撤单滥用"）：
  REFILL  —— 某一侧订单消失（被成交/被作废）→ 仅补回缺失侧（不撤仍在带内的好单，churn 最小）
  RECENTER—— 双边都在但有一侧脱离奖励价带(mid±spreadThreshold)超过滞回 → 撤双边、按当前 mid 重挂

reward 价带 = mid ± max_spread(=spreadThreshold)。滞回 deadband 防止贴边抖动反复重心。
决策不含频率判断；频率由 churn_guard 在循环层把关。
"""
from dataclasses import dataclass
from typing import Optional

# 动作类型
NONE = "NONE"
REFILL = "REFILL"
RECENTER = "RECENTER"


@dataclass(frozen=True)
class Decision:
    action: str          # NONE | REFILL | RECENTER
    place_buy: bool      # 是否需要(重新)挂买单
    place_sell: bool     # 是否需要(重新)挂卖单
    cancel_first: bool   # 执行前是否先撤该 token 现有单（RECENTER=True）
    reason: str

    @property
    def is_cancel(self) -> bool:
        return self.cancel_first


_NOOP = Decision(NONE, False, False, False, "in-band, two-sided")


def decide_action(
    my_bid: Optional[float],
    my_ask: Optional[float],
    mid: Optional[float],
    max_spread: Optional[float],
    *,
    deadband_ticks: float = 1.0,
    tick_size: float = 0.01,
    refill_on_fill: bool = True,
    min_two_sided: bool = True,
) -> Decision:
    """给定我方双边挂价 + 当前 mid + 奖励价带，返回克制的动作建议。"""
    # 无 mid / 无奖励带 → 无法安全判断，不动
    if mid is None or not max_spread or max_spread <= 0:
        return _NOOP

    missing_buy = my_bid is None
    missing_sell = my_ask is None

    # ① 缺侧 → 仅补缺侧（最小 churn，不撤仍在带内的好单）
    if (missing_buy or missing_sell) and refill_on_fill and min_two_sided:
        return Decision(REFILL, place_buy=missing_buy, place_sell=missing_sell,
                        cancel_first=False, reason="missing side → refill")

    # ② 双边都在：检查是否脱离奖励价带（带滞回）
    band = float(max_spread)
    margin = float(deadband_ticks) * float(tick_size)
    lower = mid - band - margin
    upper = mid + band + margin
    bid_out = my_bid is not None and not (lower <= my_bid <= upper)
    ask_out = my_ask is not None and not (lower <= my_ask <= upper)
    if bid_out or ask_out:
        return Decision(RECENTER, place_buy=True, place_sell=True,
                        cancel_first=True, reason="out of reward band → recenter")

    return _NOOP
