"""把 predict.fun REST/SDK 原始数据归一化为下游已在用的形状（纯函数）。

待 OpenAPI 确认的字段名集中在本文件，是平台差异的唯一吸收点。
"""
from dataclasses import dataclass
from typing import Any, Dict, List

from .units import from_wei  # 预留：若金额以 wei 返回则用 from_wei


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class NormalizedBook:
    market_id: Any
    bids: List[BookLevel]
    asks: List[BookLevel]


_SIDE_TO_SDK = {"BUY": 0, "SELL": 1}
# predict.fun 簿/单中 "Bid"=买=BUY，"Ask"=卖=SELL
_RAW_SIDE_TO_CANON = {"BID": "BUY", "BUY": "BUY", "ASK": "SELL", "SELL": "SELL"}


def side_to_sdk(side: str) -> int:
    return _SIDE_TO_SDK[str(side).upper()]


def side_from_sdk(v: int) -> str:
    return "BUY" if int(v) == 0 else "SELL"


def _canon_side(raw_side: str) -> str:
    key = str(raw_side).upper()
    canon = _RAW_SIDE_TO_CANON.get(key)
    if canon is None:
        raise ValueError(f"未知 side 值: {raw_side!r}")
    return canon


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_status(raw: str) -> str:
    """OPEN/LIVE → LIVE；其余原样大写。"""
    s = str(raw or "").upper()
    return "LIVE" if s in ("OPEN", "LIVE", "ACTIVE") else s


def _canon_side_any(raw_side: Any) -> str:
    """side 可能是 EIP-712 整数(0/1)、数字串("0"/"1")或 "Bid"/"Ask"/"BUY"/"SELL"。"""
    if isinstance(raw_side, bool):  # bool 是 int 子类，先排除
        raise ValueError(f"未知 side 值: {raw_side!r}")
    if isinstance(raw_side, (int, float)):
        return side_from_sdk(int(raw_side))
    s = str(raw_side).strip()
    if s in ("0", "1"):
        return side_from_sdk(int(s))
    return _canon_side(s)


def _to_int_wei(x: Any) -> int:
    """wei 金额解析为 int，避免 float 中转丢精度（18 位整数远超 float 安全整数范围）。"""
    if x is None:
        return 0
    try:
        return int(str(x).strip())
    except (TypeError, ValueError):
        return int(_f(x))  # 兜底（极少触发）


def _price_size_from_amounts(side: str, maker_amount_wei: Any, taker_amount_wei: Any):
    """由 EIP-712 maker/taker 金额(wei)反推 (price, size_shares)。

    BUY(side=0)：付 USDT(maker)、收 shares(taker)；SELL(side=1)：给 shares(maker)、收 USDT(taker)。
    price = USDT_wei / shares_wei（同为 18 位，比值即每股价）；size = from_wei(shares_wei)。
    整数运算，避免大额 wei 经 float 丢精度。
    """
    m, t = _to_int_wei(maker_amount_wei), _to_int_wei(taker_amount_wei)
    if side == "BUY":
        usdt_wei, shares_wei = m, t
    else:
        usdt_wei, shares_wei = t, m
    if not shares_wei:
        return 0.0, 0.0
    price = round(usdt_wei / shares_wei, 6)
    return price, from_wei(shares_wei)


def normalize_order(raw: Dict[str, Any]) -> Dict[str, Any]:
    """归一化挂单。兼容两种形状：
    ① 扁平（簿/测试）：顶层 tokenId/side/price/quantity。
    ② predict.fun /v1/orders 实测：顶层 id/marketId/status，EIP-712 字段嵌套在 raw["order"]
       （tokenId、side 为 0/1、makerAmount/takerAmount，无 price/quantity，需反推）。
    """
    core = raw.get("order") if isinstance(raw.get("order"), dict) else {}

    oid = str(raw.get("id") or raw.get("orderId") or core.get("hash") or raw.get("hash") or "")
    token_id = str(core.get("tokenId") or raw.get("tokenId") or raw.get("token_id")
                   or raw.get("asset_id") or "")
    market_id = raw.get("marketId", raw.get("market_id"))

    side = _canon_side_any(core.get("side", raw.get("side", "BUY")))

    explicit_price = raw.get("price", core.get("price"))
    explicit_size = raw.get("quantity", raw.get("size"))
    maker_amt = core.get("makerAmount", raw.get("makerAmount"))
    taker_amt = core.get("takerAmount", raw.get("takerAmount"))
    has_amounts = maker_amt is not None and taker_amt is not None

    if explicit_price is not None:
        price = _f(explicit_price)
    elif has_amounts:
        price, _ = _price_size_from_amounts(side, maker_amt, taker_amt)
    else:
        price = 0.0

    if explicit_size is not None:
        size = _f(explicit_size)
    elif has_amounts:
        _, size = _price_size_from_amounts(side, maker_amt, taker_amt)
    else:
        size = 0.0

    matched_raw = raw.get("quantityMatched", raw.get("size_matched"))
    if matched_raw is not None:
        size_matched = _f(matched_raw)
    elif raw.get("amountFilled") is not None:
        size_matched = from_wei(_to_int_wei(raw.get("amountFilled")))
    else:
        size_matched = 0.0

    return {
        "id": oid,
        "token_id": token_id,
        "market_id": market_id,
        "side": side,
        "price": price,
        "size": size,
        "size_matched": size_matched,
        "status": normalize_status(raw.get("status") or core.get("status")),
        "raw": raw,
    }


def normalize_orderbook(raw: Dict[str, Any]) -> NormalizedBook:
    def levels(rows):
        out = []
        for row in rows or []:
            # 形如 [price, size]（数组）或 {"price":..,"size":..}（对象）；VERIFY 实际格式
            if isinstance(row, (list, tuple)):
                p, s = row[0], row[1]
            else:
                p, s = row.get("price"), row.get("size")
            out.append(BookLevel(_f(p), _f(s)))
        return out
    return NormalizedBook(
        market_id=raw.get("marketId", raw.get("market_id")),
        bids=levels(raw.get("bids")),
        asks=levels(raw.get("asks")),
    )


def complement_book(book: NormalizedBook) -> NormalizedBook:
    """把"Yes 口径"的簿换算成"No 口径"（swap + complement，主网实测确认）。

    买 No @q ≡ 卖 Yes @(1-q)，卖 No @q ≡ 买 Yes @(1-q)，因此：
      No.bids = complement(Yes.asks)（价降序）
      No.asks = complement(Yes.bids)（价升序）
    价格取 1-p（6 位四舍五入消除浮点噪声），size 原样保留。
    """
    def comp(levels):
        # 跳过 (0,1) 之外的异常价位：p=0/1 的补价会得到 1/0 这种"白送"价位，必须丢弃
        return [BookLevel(round(1.0 - lv.price, 6), lv.size)
                for lv in levels if 0.0 < lv.price < 1.0]

    new_bids = sorted(comp(book.asks), key=lambda x: x.price, reverse=True)
    new_asks = sorted(comp(book.bids), key=lambda x: x.price)
    return NormalizedBook(market_id=book.market_id, bids=new_bids, asks=new_asks)


def batch_ids(ids: List[str], size: int) -> List[List[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]
