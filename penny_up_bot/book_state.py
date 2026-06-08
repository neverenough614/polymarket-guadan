"""每个 token 的订单簿状态，由市场 websocket 维护。

核心职责：在剔除「我自己挂的那张单」之后，算出对手的最高买价（best_comp），
供 quoting 计算 penny-up 目标价。
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from sortedcontainers import SortedDict

# 小于此值的剩余视为尘埃/我方残留，不算对手。
_DUST = 1e-6


def best_competing_bid(
    bids: Mapping[float, float],
    my_price: Optional[float],
    my_size: float,
    tick: float = 0.01,
) -> Optional[float]:
    """返回剔除自己后的最高对手买价；无对手返回 None。

    Args:
        bids:     价位 -> 总挂单量（含我自己那部分）。
        my_price: 我当前挂单的价位；None 表示我没挂单。
        my_size:  我在 my_price 上的挂单量，需从该档总量中扣除。
        tick:     用于把我的价位匹配到盘口档位（容差 tick/2）。
    """
    if not bids:
        return None
    for price in sorted(bids.keys(), reverse=True):
        level_size = bids[price]
        if my_price is not None and abs(price - my_price) < tick / 2.0:
            level_size = level_size - my_size
        if level_size > _DUST:
            return price
    return None


class BookState:
    """单个 token 的盘口，bids/asks 用 SortedDict(price->size) 存储。"""

    def __init__(self) -> None:
        self.bids: SortedDict = SortedDict()
        self.asks: SortedDict = SortedDict()

    def apply_snapshot(
        self,
        bids: Iterable[Mapping[str, str]],
        asks: Iterable[Mapping[str, str]],
    ) -> None:
        """处理 websocket 'book' 全量快照（覆盖式）。"""
        self.bids = SortedDict({float(e["price"]): float(e["size"]) for e in bids})
        self.asks = SortedDict({float(e["price"]): float(e["size"]) for e in asks})

    def apply_price_change(self, side: str, price: float, new_size: float) -> None:
        """处理 websocket 'price_change' 增量。side: 'BUY'/'bids' 或 'SELL'/'asks'。"""
        book = self.bids if side in ("BUY", "bids") else self.asks
        if new_size == 0:
            book.pop(price, None)
        else:
            book[price] = new_size

    def best_competing_bid(
        self, my_price: Optional[float], my_size: float, tick: float = 0.01
    ) -> Optional[float]:
        return best_competing_bid(self.bids, my_price, my_size, tick)
