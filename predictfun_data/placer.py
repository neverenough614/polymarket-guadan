"""predict.fun 报价器（仿 Polymarket place_order_for_token，按真实薄簿校准）。

只买不裸卖：双边做市靠"买两个 outcome"——买 YES（共享簿 bid 侧）+ 买 NO（=卖 YES，
共享簿 ask 侧），两个都是 BUY、只需 USDT 抵押、不自成交（buyYES+buyNO<1）。故每个 outcome
token 只挂"一张买单"。本模块为该买单选价+定量。

报价逻辑（对齐 Polymarket，阈值据 config.PredictFunPlaceConfig 校准成 predict.fun 薄盘版）：
  1. 需双侧簿（可靠 mid）、前5档累计 ≥ min_top5_depth（滤掉死簿），否则不报价。
  2. 贴价：在 mid±max_spread 奖励带内，**贴已有深度的最优档（最接近 mid，不抢内侧/不+1tick）**；
     跳过灰尘档（<min_level_depth）。带内无现成档→不报价（下轮重试，绝不硬抢）。
  3. 定量：**固定 min_size（=shareThreshold 下限，≥100）**。predict.fun 挂买单预扣抵押，全靠预算
     守门控市场数；监控重挂也走本函数，故尺寸必须与预算配对一致，绝不用动态量（否则逃逸预算）。
  4. 出场护栏：买入成交后须能把持仓卖回更低 bid 平仓（predict.fun 不能裸卖，但持仓后可卖）——
     最近更低 bid 距我买价 ≤ exit_max_gap，且更低档累计深度 ≥ 我 notional × multiplier，否则不报价。

纯函数（compute_quote / select_join_price / exit_liquidity_ok）便于单测；
place_bid 串起来下单。NO 侧用其自身（已 complement）的簿，逻辑对称。
"""
from typing import Any, Dict, List, Optional, Tuple

from config.bot_config import cfg
from . import units


def _ok(resp: Any) -> bool:
    return bool(resp) and resp.get("status") != "error"


def _sorted_levels(book: Any) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """book → (bids[(price,size)降序], asks[(price,size)升序])。空簿返回空列表。"""
    bids = sorted(((float(b.price), float(b.size)) for b in (book.bids or [])),
                  key=lambda x: x[0], reverse=True) if book else []
    asks = sorted(((float(a.price), float(a.size)) for a in (book.asks or [])),
                  key=lambda x: x[0]) if book else []
    return bids, asks


def select_join_price(
    bids: List[Tuple[float, float]],
    mid: float,
    band: float,
    min_level_depth: float,
) -> Optional[Tuple[float, float]]:
    """带内贴最优档（不改善）：返回 (price, depth)。

    在 [mid-band, mid] 内、深度≥min_level_depth 的买档中，取**最接近 mid（价最高）**的一档
    （奖励/Q-score 最高）。无合格档返回 None。bids 须按价降序。
    """
    lo = mid - band
    for price, size in bids:                     # 已降序：第一个落带内且非灰尘的即最优
        if price > mid:                          # 高于 mid 的买档不参与（越界/异常）
            continue
        if price < lo:                           # 低于带下沿：再往下都更低，停止
            break
        if price * size >= min_level_depth:
            return price, price * size
    return None


def exit_liquidity_ok(
    bids: List[Tuple[float, float]],
    buy_price: float,
    size: float,
    max_gap: float,
    multiplier: float,
) -> Tuple[bool, str]:
    """买入成交后能否卖回更低 bid 平仓：最近更低档距≤max_gap 且累计深度≥notional×mult。"""
    nearest_lower: Optional[float] = None
    support = 0.0
    for price, sz in bids:                       # 降序
        if price >= buy_price:
            continue
        if nearest_lower is None:
            nearest_lower = price
        if buy_price - price <= max_gap + 1e-9:
            support += price * sz
    if nearest_lower is None:
        return False, "no_lower_bid"
    if buy_price - nearest_lower > max_gap + 1e-9:
        return False, f"gap_{buy_price - nearest_lower:.3f}"
    need = buy_price * size * multiplier
    if support < need:
        return False, f"exit_depth_{support:.0f}<{need:.0f}"
    return True, "ok"


def compute_quote(
    book: Any,
    max_spread: Optional[float],
    tick_size: float,
    min_size: float,
    pc=None,
) -> Tuple[Optional[float], Optional[float], str]:
    """该 outcome 买单的 (price, size, reason)。无法安全报价时 price=size=None、reason 说明原因。"""
    pc = pc or cfg.predictfun_place
    if not book:
        return None, None, "no_book"
    bids, asks = _sorted_levels(book)
    if not bids or not asks:
        return None, None, "one_sided"
    bb, ba = bids[0][0], asks[0][0]
    if bb <= 0 or ba <= 0 or bb >= ba:           # 交叉/锁定/无效
        return None, None, "crossed_or_invalid"
    if not max_spread or max_spread <= 0:
        return None, None, "no_band"
    mid = (bb + ba) / 2.0
    if mid < pc.price_min or mid > pc.price_max:   # 极端价(接近 0/1)→事件跳变易被逆向吃→不报价
        return None, None, f"extreme_price_{mid:.2f}"
    top5 = sum(p * s for p, s in bids[:5])
    if top5 < pc.min_top5_depth:
        return None, None, f"thin_top5_{top5:.0f}"

    band = float(max_spread)
    pick = select_join_price(bids, mid, band, pc.min_level_depth)
    if pick is None:
        return None, None, "no_in_band_level"
    price = units.price_to_tick(pick[0], tick_size)
    if not (0.0 < price < ba):                    # 取整后不得越过卖一
        return None, None, "bad_price_after_tick"

    # 固定 min_size（=shareThreshold 下限）：predict.fun 挂买单预扣抵押，且监控重挂也走本函数，
    # 必须与预算配对的两腿同量(min_size)一致——绝不能用动态量，否则重挂会逃逸预算+破坏两腿同量+超额锁仓。
    size = float(min_size)

    if pc.require_exit_liquidity:
        ok, why = exit_liquidity_ok(bids, price, size, pc.exit_max_gap, pc.exit_depth_multiplier)
        if not ok:
            return None, None, f"exit_{why}"
    return price, size, "ok"


def _tick_for(backend: Any, token_id: str, tick_size: Optional[float]) -> float:
    if tick_size is not None:
        return tick_size
    meta = backend.meta_for(token_id) if hasattr(backend, "meta_for") else None
    return getattr(meta, "tick_size", 0.01) if meta is not None else 0.01


def place_bid(
    backend: Any,
    token_info: Dict[str, Any],
    book: Any,
    tick_size: Optional[float] = None,
) -> Dict[str, Any]:
    """为该 outcome token 挂一张买单（仿 Polymarket：带内贴最优档、动态量、出场护栏）。

    neg_risk/yield/fee 由 backend.create_order 从注册表注入。size 下限=token 的 min_size(≥100)。
    """
    tid = token_info["token_id"]
    tick = _tick_for(backend, tid, tick_size)
    min_size = max(100.0, float(token_info.get("min_size", 0) or 0))
    price, size, reason = compute_quote(book, token_info.get("max_spread"), tick, min_size)
    if price is None:
        return {"status": "no_quote", "reason": reason}
    return place_leg(backend, token_info, price, size)


def place_leg(
    backend: Any,
    token_info: Dict[str, Any],
    price: float,
    size: float,
) -> Dict[str, Any]:
    """按既定 (price, size) 挂一张买单（价/量由上游预算配对决定）。只买不裸卖。"""
    r = backend.create_order(token_info["token_id"], "BUY", float(price), float(size))
    return {"side": "BUY", "price": float(price), "size": float(size),
            "status": "placed" if _ok(r) else "failed", "resp": r}
