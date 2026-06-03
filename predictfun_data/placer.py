"""predict.fun 报价器（收尾 runner）—— 只买不裸卖,为积分farming定制。

关键约束(主网实测): predict.fun 不允许裸卖(create_order_insufficient_shares_balance)——
挂 SELL 必须先持有该 outcome 份额。因此双边做市靠"买两个 outcome":
  买 YES @ YES.bid  → 在共享簿里是 bid 侧
  买 NO  @ NO.bid   → 在共享簿里是 ask 侧(NO.bid = 1 - YES.ask 的补价)
两个都是 BUY,只需 USDT 抵押、无需持仓,且不自成交(buyYES + buyNO < 1)。

故每个 outcome token 只挂"一张买单(贴其 bid 侧)"。一个市场的 YES+NO 两个 token 各一张买单,
即在那一本簿上形成 bid+ask 双边,满足"双边"最大奖励。compute_bid 为纯函数。
"""
from typing import Any, Dict, Optional

from . import units


def _ok(resp: Any) -> bool:
    return bool(resp) and resp.get("status") != "error"


def compute_bid(
    best_bid: float,
    best_ask: float,
    max_spread: Optional[float],
    tick_size: float = 0.01,
    improve_ticks: int = 1,
) -> Optional[float]:
    """该 outcome 的买价(贴 bid、改善 improve_ticks 抢内侧、不越过 ask、夹进奖励带下沿)。

    需两侧簿(可靠 mid);无法安全报价返回 None。
    """
    if not best_bid or not best_ask or best_bid <= 0 or best_ask <= 0:
        return None
    if best_bid >= best_ask:                 # 交叉/锁定簿
        return None
    mid = (best_bid + best_ask) / 2.0
    buy = best_bid + max(0, int(improve_ticks)) * tick_size
    if buy >= best_ask:                       # 改善会越过卖一 → 退为贴买一
        buy = best_bid
    if max_spread:                            # 奖励带下沿(买单须 ≥ mid - band 才计奖励)
        buy = max(buy, mid - float(max_spread))
    buy = units.price_to_tick(buy, tick_size)
    if not (0.0 < buy < best_ask):
        return None
    return buy


def _tick_for(backend: Any, token_id: str, tick_size: Optional[float]) -> float:
    if tick_size is not None:
        return tick_size
    meta = backend.meta_for(token_id) if hasattr(backend, "meta_for") else None
    return getattr(meta, "tick_size", 0.01) if meta is not None else 0.01


def place_bid(
    backend: Any,
    token_info: Dict[str, Any],
    best_bid: float,
    best_ask: float,
    tick_size: Optional[float] = None,
) -> Dict[str, Any]:
    """为该 outcome token 挂一张买单(贴 bid)。size 取 shareThreshold(min_size,≥100)。

    neg_risk/yield/fee 由 backend.create_order 从注册表注入。
    """
    tid = token_info["token_id"]
    tick = _tick_for(backend, tid, tick_size)
    buy = compute_bid(best_bid, best_ask, token_info.get("max_spread"), tick)
    if buy is None:
        return {"status": "no_quote", "reason": "单侧簿/交叉"}
    size = max(100.0, float(token_info.get("min_size", 0) or 0))
    r = backend.create_order(tid, "BUY", buy, size)
    return {"side": "BUY", "price": buy, "size": size,
            "status": "placed" if _ok(r) else "failed", "resp": r}
