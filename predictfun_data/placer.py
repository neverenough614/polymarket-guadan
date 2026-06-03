"""predict.fun 报价器（收尾 runner）—— 为积分farming定制的双边贴价。

与 Polymarket 的 analyze_best_place_price_from_book 不同：predict.fun 奖励"越贴市价越多、
最紧价差拿最多",且薄簿正是你独占流动性=积分最大的地方。故不做深度门控,只在奖励价带
(mid±spreadThreshold)内、贴近 mid 报双边,改善 1 tick 抢内侧但绝不交叉。

compute_quotes 为纯函数,便于测试。安全前置：买<卖、双边都在 (0,1)、需要两侧簿(可靠 mid)。
"""
from typing import Any, Dict, List, Optional, Tuple

from . import units


def _ok(resp: Any) -> bool:
    return bool(resp) and resp.get("status") != "error"


def place_for_token(
    backend: Any,
    token_info: Dict[str, Any],
    best_bid: float,
    best_ask: float,
    sides,
    tick_size: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """按 compute_quotes 的双边价,为 token 挂指定 sides({"BUY","SELL"} 子集)。

    tick 缺省取该 token 注册表 tick_size(默认 0.01)。size 取 shareThreshold(min_size,≥100)。
    neg_risk/yield/fee 由 backend.create_order 从注册表注入,无需在此传。
    """
    tid = token_info["token_id"]
    if tick_size is None:
        meta = backend.meta_for(tid) if hasattr(backend, "meta_for") else None
        tick_size = getattr(meta, "tick_size", 0.01) if meta is not None else 0.01

    q = compute_quotes(best_bid, best_ask, token_info.get("max_spread"), tick_size)
    if not q:
        return [{"status": "no_quote", "reason": "单侧簿/交叉/超带"}]
    buy, sell = q
    size = max(100.0, float(token_info.get("min_size", 0) or 0))

    out: List[Dict[str, Any]] = []
    if "BUY" in sides:
        r = backend.create_order(tid, "BUY", buy, size)
        out.append({"side": "BUY", "price": buy, "size": size,
                    "status": "placed" if _ok(r) else "failed", "resp": r})
    if "SELL" in sides:
        r = backend.create_order(tid, "SELL", sell, size)
        out.append({"side": "SELL", "price": sell, "size": size,
                    "status": "placed" if _ok(r) else "failed", "resp": r})
    return out


def compute_quotes(
    best_bid: float,
    best_ask: float,
    max_spread: Optional[float],
    tick_size: float = 0.01,
    improve_ticks: int = 1,
) -> Optional[Tuple[float, float]]:
    """→ (buy_price, sell_price)，无法安全报价返回 None。

    要求两侧簿存在且未交叉（单侧簿无可靠 mid → 跳过,避免逆向成交）。
    在 [mid-band, mid+band] 内贴 mid 报价；improve_ticks 抢内侧一档,过紧则退为贴盘口。
    """
    if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0:
        return None
    if best_bid >= best_ask:        # 交叉/锁定簿,不报
        return None

    mid = (best_bid + best_ask) / 2.0
    band = float(max_spread) if max_spread else 0.0
    step = max(0, int(improve_ticks)) * tick_size

    buy = best_bid + step
    sell = best_ask - step
    if buy >= sell:                 # 改善后会交叉 → 退为贴盘口（join touch）
        buy, sell = best_bid, best_ask

    if band > 0:                    # 夹进奖励价带（否则不计奖励）
        buy = max(buy, mid - band)
        sell = min(sell, mid + band)

    buy = units.price_to_tick(buy, tick_size)
    sell = units.price_to_tick(sell, tick_size)

    if not (0.0 < buy < sell < 1.0):
        return None
    return buy, sell
