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
    return _RAW_SIDE_TO_CANON[str(raw_side).upper()]


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_status(raw: str) -> str:
    """OPEN/LIVE → LIVE；其余原样大写。"""
    s = str(raw or "").upper()
    return "LIVE" if s in ("OPEN", "LIVE", "ACTIVE") else s


def normalize_order(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id") or raw.get("orderId") or raw.get("hash") or ""),
        "token_id": str(raw.get("tokenId") or raw.get("token_id") or raw.get("asset_id") or ""),
        "market_id": raw.get("marketId", raw.get("market_id")),
        "side": _canon_side(raw.get("side", "BUY")),
        "price": _f(raw.get("price")),
        "size": _f(raw.get("quantity", raw.get("size"))),
        "size_matched": _f(raw.get("quantityMatched", raw.get("size_matched"))),
        "status": normalize_status(raw.get("status")),
        "raw": raw,
    }


def normalize_orderbook(raw: Dict[str, Any]) -> NormalizedBook:
    def levels(rows):
        out = []
        for row in rows or []:
            # 形如 [price, size]
            p, s = row[0], row[1]
            out.append(BookLevel(_f(p), _f(s)))
        return out
    return NormalizedBook(
        market_id=raw.get("marketId", raw.get("market_id")),
        bids=levels(raw.get("bids")),
        asks=levels(raw.get("asks")),
    )


def batch_ids(ids: List[str], size: int) -> List[List[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]
