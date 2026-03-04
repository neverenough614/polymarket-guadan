"""
Polymarket Python 实现，封装 PolymarketClient 的调用。
"""
from collections import defaultdict
from typing import Any, Dict, List, Optional

from poly_data.polymarket_client import PolymarketClient

from .interface import IExecutionBackend


class PolymarketBackend(IExecutionBackend):
    """IExecutionBackend 的 Polymarket Python 实现。"""

    def __init__(self, client: PolymarketClient):
        self._client = client

    @property
    def raw_client(self) -> PolymarketClient:
        """访问原始 PolymarketClient（兼容部分旧逻辑）。"""
        return self._client

    def create_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        neg_risk: bool = False,
    ) -> Dict[str, Any]:
        return self._client.create_order(token_id, side, price, size, neg_risk=neg_risk)

    def cancel_all_asset(self, token_id: str) -> None:
        self._client.cancel_all_asset(token_id)

    def cancel_one_side(
        self,
        token_id: str,
        side: str,
        cached_order_ids: Optional[List[str]] = None,
    ) -> bool:
        side_cn = "买单" if side == "BUY" else "卖单"
        try:
            if cached_order_ids is not None:
                to_cancel = cached_order_ids
            else:
                orders = self._client.client.get_orders()
                to_cancel = []
                for o in orders:
                    if str(o.get("status", "")).upper() != "LIVE":
                        continue
                    tid = o.get("token_id") or o.get("asset_id")
                    if tid == token_id and o.get("side") == side:
                        oid = o.get("id")
                        if oid:
                            to_cancel.append(oid)
            if not to_cancel:
                return False
            for oid in to_cancel:
                self._client.client.cancel(oid)
            return True
        except Exception:
            return False

    def cancel_all(self) -> Any:
        return self._client.client.cancel_all()

    def get_order_book(self, token_id: str) -> Any:
        try:
            return self._client.client.get_order_book(token_id)
        except Exception:
            return None

    def get_all_orders(self) -> List[Dict[str, Any]]:
        try:
            orders = self._client.client.get_orders()
            return list(orders) if orders else []
        except Exception:
            return []

    def get_all_positions(self) -> Any:
        return self._client.get_all_positions()

    def get_all_my_orders_grouped(self) -> Dict[str, Dict[str, Any]]:
        """
        一次拉取所有订单，按 token_id 分组。
        返回 {token_id: {'best_bid': float|None, 'best_ask': float|None, 'bid_ids': [...], 'ask_ids': [...]}}
        """
        grouped = defaultdict(lambda: {"bids": [], "asks": [], "bid_ids": [], "ask_ids": []})
        for o in self.get_all_orders():
            status = str(o.get("status", "")).upper()
            if status and status != "LIVE":
                continue
            token_id = o.get("token_id") or o.get("asset_id")
            if not token_id:
                continue
            price = float(o.get("price", 0))
            side = o.get("side")
            order_id = o.get("id")
            if side == "BUY":
                grouped[token_id]["bids"].append(price)
                if order_id:
                    grouped[token_id]["bid_ids"].append(order_id)
            elif side == "SELL":
                grouped[token_id]["asks"].append(price)
                if order_id:
                    grouped[token_id]["ask_ids"].append(order_id)
        result = {}
        for token_id, sides in grouped.items():
            best_bid = max(sides["bids"]) if sides["bids"] else None
            best_ask = min(sides["asks"]) if sides["asks"] else None
            result[token_id] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_ids": sides["bid_ids"],
                "ask_ids": sides["ask_ids"],
            }
        return result
