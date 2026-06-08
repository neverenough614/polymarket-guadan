"""每个 token 的建仓 + 挂单状态。

记账真相：每个 order_id 的【累计】size_matched（幂等 set，非 add），
filled = 所有 order_id 累计成交之和。re-peg 换新 order_id 后旧单的已成交仍计入。
"""
from __future__ import annotations

from typing import Dict, Optional

_EPS = 1e-9


class PositionState:
    def __init__(
        self,
        token_id: str,
        total_size: float,
        cap_price: float,
        tick: float,
        neg_risk: bool,
        label: str = "",
        outcome: str = "",
    ) -> None:
        self.token_id = token_id
        self.total_size = total_size
        self.cap_price = cap_price
        self.tick = tick
        self.neg_risk = neg_risk
        self.label = label or token_id
        self.outcome = outcome

        # 成交记账：order_id -> 该单累计成交量
        self._matched_by_order: Dict[str, float] = {}
        self.filled: float = 0.0

        # 启动时该 token 的既有持仓基线；REST 兜底只把【启动后】新增的持仓计入 filled
        self.baseline_position: float = 0.0

        # 当前挂单
        self.order_id: Optional[str] = None
        self.order_price: Optional[float] = None
        self.order_size: float = 0.0

        self.done: bool = False

    # ---- 成交记账 ----

    def _recompute_filled(self) -> None:
        self.filled = sum(self._matched_by_order.values())

    def record_order_match(self, order_id: str, size_matched_cumulative: float) -> None:
        """记录某 order_id 的累计成交量（幂等：取已见过的最大值）。"""
        prev = self._matched_by_order.get(order_id, 0.0)
        if size_matched_cumulative > prev:
            self._matched_by_order[order_id] = size_matched_cumulative
            self._recompute_filled()

    def reconcile_filled(self, authoritative_filled: float) -> None:
        """用 REST 持仓兜底校正。只上调不下调，避免回退已知进度、防止超买。"""
        if authoritative_filled > self.filled + _EPS:
            self.filled = authoritative_filled

    def remaining(self) -> float:
        return max(0.0, self.total_size - self.filled)

    # ---- 挂单状态 ----

    def set_resting(self, order_id: str, price: float, size: float) -> None:
        self.order_id = order_id
        self.order_price = price
        self.order_size = size

    def clear_resting(self) -> None:
        self.order_id = None
        self.order_price = None
        self.order_size = 0.0

    # ---- 完成判定 ----

    def mark_done_if_complete(self, min_order_size: float) -> bool:
        """剩余量已达目标或小于最小下单量 → 标记完成。返回是否已完成。"""
        if self.remaining() <= min_order_size + _EPS:
            self.done = True
        return self.done
