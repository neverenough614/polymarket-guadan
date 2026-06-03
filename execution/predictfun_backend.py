"""PredictFunBackend —— IExecutionBackend 的 predict.fun(BNB) 实现。

把 main.py「只用 token_id」的调用面，适配到 predict.fun 的现实：
- 下单需 fee_rate_bps / yield_bearing / neg_risk → 从市场注册表(TokenMeta)注入；
- 取簿是「每市场一本 Yes 口径簿」→ No 侧 swap+complement（主网实测确认）；
- 撤单仅支持按 order id → 拉我的挂单、按 token/side 过滤出 id，再批量 remove。

Polymarket 路径一行不动；本类仅在 PLATFORM=predictfun 时使用。
"""
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from predictfun_data.market_registry import TokenMeta, parse_market
from predictfun_data.normalize import batch_ids, complement_book

from .interface import IExecutionBackend


class PredictFunBackend(IExecutionBackend):
    """IExecutionBackend 的 predict.fun 实现，包装 PredictFunClient。"""

    def __init__(self, client: Any, registry: Optional[Dict[str, TokenMeta]] = None):
        self._client = client
        self._registry: Dict[str, TokenMeta] = dict(registry or {})

    @property
    def raw_client(self) -> Any:
        return self._client

    # ---- 市场注册（SP3 市场发现会调用；也可独立刷新）----
    def register_markets(self, markets: Iterable[dict]) -> int:
        """登记原始市场列表，按 token(onChainId) 建立元数据索引。返回登记的 token 数。"""
        count = 0
        for m in markets or []:
            for meta in parse_market(m):
                self._registry[meta.token_id] = meta
                count += 1
        return count

    def refresh_markets(self, **filters: Any) -> int:
        """从 API 拉取市场并登记（默认 OPEN）。返回登记的 token 数。"""
        params = {"status": "OPEN", **filters}
        markets = self._client.get_markets(**params)
        return self.register_markets(markets)

    def meta_for(self, token_id: str) -> Optional[TokenMeta]:
        return self._registry.get(str(token_id))

    # ---- 下单 ----
    def create_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        neg_risk: bool = False,
    ) -> Dict[str, Any]:
        meta = self.meta_for(token_id)
        if meta is not None:
            # 价格已是该 token 自身口径（No 价由 get_order_book 复合后给到 main.py）；
            # 每个 outcome 都是独立 ERC1155，直接按其 onChainId 下单，无需写侧变换。
            return self._client.create_order(
                token_id, side, price, size,
                neg_risk=meta.neg_risk,
                is_yield_bearing=meta.yield_bearing,
                fee_rate_bps=meta.fee_rate_bps,
            )
        # 未注册：fee_rate_bps 未知会被服务端拒；快速失败而非盲发（fail fast）。
        # IExecutionBackend 调用方（place_order_for_token）已包 try/except，异常会被记为 error 状态。
        raise RuntimeError(
            f"token {token_id} 未在市场注册表中，拒绝下单（缺 fee_rate_bps 会被拒）。"
            f"请先调用 refresh_markets()/register_markets()。")

    # ---- 取簿（Yes 原生 / No 复合）----
    def get_order_book(self, token_id: str) -> Any:
        meta = self.meta_for(token_id)
        if meta is None:
            print(f"⚠️ [PredictFunBackend] token {token_id} 未注册，无法定位 market_id，取簿返回 None。")
            return None
        try:
            book = self._client.get_orderbook(meta.market_id)  # NormalizedBook（Yes 口径）
        except Exception as e:
            print(f"⚠️ [PredictFunBackend] 取簿失败 token={token_id} market={meta.market_id}: {e}")
            return None
        return complement_book(book) if meta.is_complement else book

    # ---- 撤单（按 id）----
    def _live_orders(self, market_id: Any = None) -> List[Dict[str, Any]]:
        try:
            orders = self._client.get_open_orders(market_id=market_id) if market_id is not None \
                else self._client.get_open_orders()
        except Exception as e:
            print(f"⚠️ [PredictFunBackend] 拉取挂单失败: {e}")
            return []
        live = []
        for o in orders or []:
            status = str(o.get("status", "")).upper()
            if status and status != "LIVE":
                continue
            live.append(o)
        return live

    @staticmethod
    def _order_tid(o: Dict[str, Any]) -> str:
        # 入参通常是 client.get_open_orders() 归一化后的单（token_id 在顶层）；
        # 兜底兼容原始单（tokenId 嵌套在 order 内）。
        core = o.get("order") if isinstance(o.get("order"), dict) else {}
        return str(o.get("token_id") or o.get("asset_id")
                   or core.get("tokenId") or o.get("tokenId") or "")

    def _remove_ids(self, ids: List[str]) -> Any:
        """按 id 撤单；分批 ≤100（remove 端点上限）。返回最后一批响应。"""
        ids = [str(i) for i in ids if str(i).strip()]
        if not ids:
            return None
        last = None
        for chunk in batch_ids(ids, 100):
            last = self._client.remove_orders(chunk)
        return last

    def cancel_all_asset(self, token_id: str) -> None:
        tid = str(token_id)
        ids = [str(o.get("id")) for o in self._live_orders() if self._order_tid(o) == tid and o.get("id")]
        self._remove_ids(ids)

    def cancel_one_side(
        self,
        token_id: str,
        side: str,
        cached_order_ids: Optional[List[str]] = None,
    ) -> bool:
        try:
            if cached_order_ids is not None:
                to_cancel = [str(i) for i in cached_order_ids if str(i).strip()]
            else:
                tid = str(token_id)
                to_cancel = [
                    str(o.get("id")) for o in self._live_orders()
                    if self._order_tid(o) == tid and o.get("side") == side and o.get("id")
                ]
            if not to_cancel:
                return False
            self._remove_ids(to_cancel)
            return True
        except Exception as e:
            print(f"⚠️ [PredictFunBackend] cancel_one_side 失败: {e}")
            return False

    def cancel_all(self) -> Any:
        ids = [str(o.get("id")) for o in self._live_orders() if o.get("id")]
        return self._remove_ids(ids)

    # ---- 查询 ----
    def get_all_orders(self) -> List[Dict[str, Any]]:
        try:
            return list(self._client.get_open_orders() or [])
        except Exception as e:
            print(f"⚠️ [PredictFunBackend] get_all_orders 失败: {e}")
            return []

    def get_all_positions(self) -> Any:
        try:
            return self._client.get_positions()
        except Exception as e:
            print(f"⚠️ [PredictFunBackend] get_all_positions 失败: {e}")
            return []

    def get_all_my_orders_grouped(self) -> Dict[str, Dict[str, Any]]:
        """一次拉取所有挂单，按 token_id 分组为 best_bid/best_ask/bid_ids/ask_ids。"""
        grouped: Dict[str, Dict[str, List]] = defaultdict(
            lambda: {"bids": [], "asks": [], "bid_ids": [], "ask_ids": []}
        )
        for o in self.get_all_orders():
            status = str(o.get("status", "")).upper()
            if status and status != "LIVE":
                continue
            tid = self._order_tid(o)
            if not tid:
                continue
            price = float(o.get("price", 0) or 0)
            side = o.get("side")
            oid = o.get("id")
            if side == "BUY":
                grouped[tid]["bids"].append(price)
                if oid:
                    grouped[tid]["bid_ids"].append(str(oid))
            elif side == "SELL":
                grouped[tid]["asks"].append(price)
                if oid:
                    grouped[tid]["ask_ids"].append(str(oid))
        result: Dict[str, Dict[str, Any]] = {}
        for tid, sides in grouped.items():
            result[tid] = {
                "best_bid": max(sides["bids"]) if sides["bids"] else None,
                "best_ask": min(sides["asks"]) if sides["asks"] else None,
                "bid_ids": sides["bid_ids"],
                "ask_ids": sides["ask_ids"],
            }
        return result
