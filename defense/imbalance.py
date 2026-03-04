"""
买卖深度偏斜检测（Book Imbalance Detection）。
当一边深度严重不足时，说明市场有方向性压力，应撤掉危险方向的单。
"""
from typing import Any, Optional, Tuple

from config.bot_config import cfg


def check_book_imbalance(
    book: Any,
    my_bid_price: Optional[float],
    my_ask_price: Optional[float],
) -> Tuple[bool, bool, Optional[str]]:
    """
    检测买卖深度偏斜（前 N 档）。
    返回 (should_cancel_bid, should_cancel_ask, reason_str_or_None)
    """
    ic = cfg.imbalance
    if not book or not book.bids or not book.asks:
        return False, False, None

    bids_sorted = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
    asks_sorted = sorted(book.asks, key=lambda x: float(x.price), reverse=False)

    bid_depth = sum(float(b.price) * float(b.size) for b in bids_sorted[:ic.IMBALANCE_DEPTH_LEVELS])
    ask_depth = sum(float(a.price) * float(a.size) for a in asks_sorted[:ic.IMBALANCE_DEPTH_LEVELS])
    total = bid_depth + ask_depth

    if total < ic.IMBALANCE_MIN_TOTAL_DEPTH:
        return False, False, None

    bid_ratio = bid_depth / total
    ask_ratio = ask_depth / total

    cancel_bid = False
    cancel_ask = False
    reasons = []

    if bid_ratio < ic.IMBALANCE_THRESHOLD and my_bid_price is not None:
        cancel_bid = True
        reasons.append(
            f"🚨 [偏斜] 买方深度严重不足！买/卖={bid_ratio:.0%}/{ask_ratio:.0%} "
            f"(${bid_depth:.0f}/${ask_depth:.0f})，价格可能下跌 → 撤买单"
        )

    if ask_ratio < ic.IMBALANCE_THRESHOLD and my_ask_price is not None:
        cancel_ask = True
        reasons.append(
            f"🚨 [偏斜] 卖方深度严重不足！买/卖={bid_ratio:.0%}/{ask_ratio:.0%} "
            f"(${bid_depth:.0f}/${ask_depth:.0f})，价格可能上涨 → 撤卖单"
        )

    reason_str = "\n".join(reasons) if reasons else None
    return cancel_bid, cancel_ask, reason_str
