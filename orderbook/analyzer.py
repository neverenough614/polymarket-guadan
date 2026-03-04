"""
订单簿分析：档位选择、动态挂单量计算、极端价格检测。
纯逻辑函数，除 get_orderbook_info 外均不依赖 API。
"""
from typing import Any, Dict, Optional, Tuple

from config.bot_config import cfg


def get_orderbook_info(backend: Any, token_id: str) -> Tuple[Any, Optional[float], Optional[float], Optional[float]]:
    """
    通过 backend 获取订单簿，返回 (book, best_bid, best_ask, mid_price)。
    backend 需实现 get_order_book(token_id)。
    """
    try:
        book = backend.get_order_book(token_id)
        if not book:
            return None, None, None, None
        bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        asks = sorted(book.asks, key=lambda x: float(x.price), reverse=False)
        best_bid = float(bids[0].price) if bids else None
        best_ask = float(asks[0].price) if asks else None
        mid = (best_bid + best_ask) / 2.0 if (best_bid is not None and best_ask is not None) else None
        return book, best_bid, best_ask, mid
    except Exception:
        return None, None, None, None


def analyze_best_place_price_from_book(
    book: Any,
    side: str,
    max_spread: Optional[float] = None,
    mid: Optional[float] = None,
    order_size: Optional[float] = None,
) -> Optional[Tuple[float, int, float]]:
    """
    从已有订单簿分析最优挂单价格（不请求 API）。
    返回 (price, tier, depth) 或 None。
    """
    pc = cfg.place
    try:
        if not book:
            return None

        if side == "BUY":
            levels = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        else:
            levels = sorted(book.asks, key=lambda x: float(x.price), reverse=False)

        if not levels:
            return None

        first_level = levels[0]
        first_depth = float(first_level.price) * float(first_level.size)
        if first_depth < pc.MIN_FIRST_TIER_DEPTH:
            return None

        top3_prices = [float(lv.price) for lv in levels[:3]]
        if len(top3_prices) >= 2:
            for j in range(len(top3_prices) - 1):
                gap = abs(top3_prices[j] - top3_prices[j + 1])
                if gap > pc.MAX_LEVEL_GAP + 1e-9:
                    return None

        for i, level in enumerate(levels[:3]):
            price = float(level.price)
            size = float(level.size)
            depth = price * size
            threshold = pc.DEPTH_THRESHOLD_TIER1 if i == 0 else pc.DEPTH_THRESHOLD_TIER2

            if depth < threshold:
                continue

            if i == 0:
                # 检查第1档/第2档深度比：防止"孤立厚墙"
                if len(levels) >= 2:
                    tier2_depth = float(levels[1].price) * float(levels[1].size)
                    if tier2_depth > 0 and depth / tier2_depth > pc.TIER1_MAX_DEPTH_RATIO:
                        continue  # 第1档深度异常集中于单一档位，跳过

                # 检查我的挂单占比
                if order_size is not None:
                    my_order_value = order_size * price
                    if my_order_value > depth * pc.TIER1_ORDER_RATIO:
                        continue

            if max_spread is not None and mid is not None:
                lower = mid - max_spread
                upper = mid + max_spread
                if not (lower <= price <= upper):
                    continue

            return price, i + 1, depth

        return None

    except Exception as e:
        print(f"   ⚠️ 分析订单簿失败: {e}")
        return None


def is_extreme_price_market(best_bid: Optional[float]) -> bool:
    if best_bid is None:
        return False
    pc = cfg.place
    return best_bid <= pc.EXTREME_PRICE_THRESHOLD or best_bid >= (1.0 - pc.EXTREME_PRICE_THRESHOLD)


def calculate_dynamic_size(
    book: Any,
    mid: Optional[float],
    min_size: float,
    volatility_sum: float = 0.0,
    size_ratio: Optional[float] = None,
    max_order_size: Optional[float] = None,
) -> Optional[float]:
    """
    根据市场前三档深度和波动率动态计算挂单量。
    返回 None 表示深度不足以支撑最小奖励挂单量。
    """
    pc = cfg.place
    if size_ratio is None:
        size_ratio = pc.DYNAMIC_SIZE_RATIO
    if max_order_size is None:
        max_order_size = pc.MAX_ORDER_SIZE

    if not book or mid is None or mid <= 0:
        return None

    try:
        bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        asks = sorted(book.asks, key=lambda x: float(x.price), reverse=False)
        top3_bid_depth = sum(float(b.price) * float(b.size) for b in bids[:3])
        top3_ask_depth = sum(float(a.price) * float(a.size) for a in asks[:3])

        if top3_bid_depth <= 0 and top3_ask_depth <= 0:
            return None

        bid_target = (top3_bid_depth * size_ratio / mid) if top3_bid_depth > 0 else 0
        ask_target = (top3_ask_depth * size_ratio / mid) if top3_ask_depth > 0 else 0

        target_size = (
            min(bid_target, ask_target)
            if (bid_target > 0 and ask_target > 0)
            else max(bid_target, ask_target)
        )

        if volatility_sum <= 10:
            vol_factor = 1.0
        else:
            vol_factor = max(0.2, 1.0 - (volatility_sum - 10) / 60)
        target_size = target_size * vol_factor

        if target_size < min_size:
            return None

        final_size = min(target_size, max_order_size)
        final_size = round(final_size)
        return float(final_size)

    except Exception:
        return None
