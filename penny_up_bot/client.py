"""对 py_clob_client_v2 的轻封装（自包含，不依赖主 bot）。

只暴露 penny_up_bot 需要的几件事：初始化、下单、单单撤单、查 tick、查市场、
查活跃单、查持仓。撤单一律按 order_id 单撤——绝不全局撤单，保证不碰其它程序的单。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs,
    OrderPayload,
    PartialCreateOrderOptions,
)
from py_clob_client_v2.constants import POLYGON

from .config import HOST


class PennyUpClient:
    """penny_up_bot 专用 CLOB 客户端。"""

    def __init__(self) -> None:
        pk = os.getenv("PK")
        funder = os.getenv("BROWSER_ADDRESS")
        if not pk or not funder:
            raise RuntimeError("缺少 PK 或 BROWSER_ADDRESS（请在 penny_up_bot/.env 填另一个号）")

        self.browser_wallet = funder
        self.client = ClobClient(
            host=HOST,
            key=pk,
            chain_id=POLYGON,
            funder=funder,
            signature_type=2,
        )
        self.creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds=self.creds)

    # ---- 下单 / 撤单 ----

    def create_order(
        self, token_id: str, side: str, price: float, size: float, neg_risk: bool = False
    ) -> Dict[str, Any]:
        """挂单，返回 API 响应（含 order id）。失败返回 {}。"""
        args = OrderArgs(token_id=str(token_id), price=price, size=size, side=side)
        if neg_risk:
            signed = self.client.create_order(args, options=PartialCreateOrderOptions(neg_risk=True))
        else:
            signed = self.client.create_order(args)
        try:
            return self.client.post_order(signed)
        except Exception as ex:  # noqa: BLE001 — 下单失败不应中断主循环
            print(f"[client] create_order 失败: {ex}")
            return {}

    def cancel_order(self, order_id: str) -> bool:
        """按 order_id 单撤。永不全局撤单。"""
        try:
            self.client.cancel_order(OrderPayload(orderID=order_id))
            return True
        except Exception as ex:  # noqa: BLE001
            print(f"[client] cancel_order 失败 {order_id}: {ex}")
            return False

    # ---- 查询 ----

    def get_tick_size(self, token_id: str) -> Optional[float]:
        try:
            return float(self.client.get_tick_size(str(token_id)))
        except Exception:  # noqa: BLE001
            return None

    def get_market(self, condition_id: str):
        """返回 MarketDetails（含 tokens / min_tick_size / neg_risk）。"""
        return self.client.get_market(str(condition_id))

    def get_open_orders(self) -> List[Dict[str, Any]]:
        try:
            orders = self.client.get_open_orders()
            return list(orders) if orders else []
        except Exception as ex:  # noqa: BLE001
            print(f"[client] get_open_orders 失败: {ex}")
            return []

    def get_positions_by_token(self) -> Dict[str, float]:
        """从 data-api 拉持仓，返回 {token_id: size}。失败返回 {}。"""
        try:
            res = requests.get(
                f"https://data-api.polymarket.com/positions?user={self.browser_wallet}",
                timeout=10,
            )
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict):
                if "error" in data or "message" in data:
                    return {}
                data = [data]
            out: Dict[str, float] = {}
            for p in data:
                tid = p.get("asset") or p.get("asset_id") or p.get("token_id")
                if tid is not None:
                    out[str(tid)] = float(p.get("size", 0) or 0)
            return out
        except Exception as ex:  # noqa: BLE001
            print(f"[client] get_positions 失败: {ex}")
            return {}
