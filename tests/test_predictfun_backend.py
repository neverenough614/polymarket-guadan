"""SP2: PredictFunBackend —— IExecutionBackend 实现（注入 fee/yield/neg + 簿适配 + 撤单按 id）。"""
from execution.predictfun_backend import PredictFunBackend
from predictfun_data.market_registry import parse_market
from predictfun_data.normalize import NormalizedBook, BookLevel


RAW_MARKET = {
    "id": 420295, "conditionId": "0xabc", "feeRateBps": 200,
    "isNegRisk": True, "isYieldBearing": True, "decimalPrecision": 2,
    "outcomes": [
        {"name": "Yes", "indexSet": 1, "onChainId": "YES",
         "bestBid": {"price": 0.05, "size": 500}, "bestAsk": {"price": 0.79, "size": 10}},
        {"name": "No", "indexSet": 2, "onChainId": "NO",
         "bestBid": {"price": 0.21, "size": 10}, "bestAsk": {"price": 0.95, "size": 500}},
    ],
}


class FakeClient:
    def __init__(self):
        self.created = []
        self.removed = []
        self._open_orders = []
        self._book = NormalizedBook(
            market_id=420295,
            bids=[BookLevel(0.05, 500)],
            asks=[BookLevel(0.79, 10), BookLevel(0.80, 100)],
        )

    def create_order(self, token_id, side, price, size, neg_risk=False,
                     is_yield_bearing=None, order_type="LIMIT", fee_rate_bps=0):
        self.created.append(dict(token_id=token_id, side=side, price=price, size=size,
                                 neg_risk=neg_risk, is_yield_bearing=is_yield_bearing,
                                 fee_rate_bps=fee_rate_bps))
        return {"status": "live", "order_id": "o1"}

    def get_orderbook(self, market_id):
        assert market_id == 420295
        return self._book

    def get_open_orders(self, market_id=None):
        return list(self._open_orders)

    def remove_orders(self, ids):
        self.removed.append(list(ids))
        return {"success": True, "removed": list(ids), "noop": []}

    def get_positions(self):
        return [{"tokenId": "YES", "size": 10}]


def make_backend():
    c = FakeClient()
    be = PredictFunBackend(c)
    be.register_markets([RAW_MARKET])
    return be, c


def test_create_order_injects_fee_yield_neg_from_registry():
    be, c = make_backend()
    be.create_order("YES", "BUY", 0.5, 100)
    sent = c.created[0]
    assert sent["fee_rate_bps"] == 200          # 来自市场，而非默认 0
    assert sent["is_yield_bearing"] is True
    assert sent["neg_risk"] is True
    assert sent["token_id"] == "YES"


def test_get_order_book_yes_returns_native():
    be, c = make_backend()
    book = be.get_order_book("YES")
    assert [(l.price, l.size) for l in book.bids] == [(0.05, 500)]
    assert [(round(l.price, 2), l.size) for l in book.asks] == [(0.79, 10), (0.80, 100)]


def test_get_order_book_no_returns_complemented():
    be, c = make_backend()
    book = be.get_order_book("NO")
    # No.bids = complement(Yes.asks) 降序
    assert [(round(l.price, 2), l.size) for l in book.bids] == [(0.21, 10), (0.20, 100)]
    # No.asks = complement(Yes.bids) 升序
    assert [(round(l.price, 2), l.size) for l in book.asks] == [(0.95, 500)]


def test_get_order_book_unregistered_returns_none():
    be, c = make_backend()
    assert be.get_order_book("UNKNOWN") is None


def test_cancel_all_asset_removes_only_that_token_ids():
    be, c = make_backend()
    c._open_orders = [
        {"id": "1", "token_id": "YES", "side": "BUY", "status": "LIVE", "price": 0.4},
        {"id": "2", "token_id": "YES", "side": "SELL", "status": "LIVE", "price": 0.6},
        {"id": "3", "token_id": "NO", "side": "BUY", "status": "LIVE", "price": 0.3},
    ]
    be.cancel_all_asset("YES")
    assert c.removed[0] == ["1", "2"]           # 只撤 YES 的两张，不动 NO


def test_cancel_one_side_filters_side():
    be, c = make_backend()
    c._open_orders = [
        {"id": "1", "token_id": "YES", "side": "BUY", "status": "LIVE"},
        {"id": "2", "token_id": "YES", "side": "SELL", "status": "LIVE"},
    ]
    ok = be.cancel_one_side("YES", "SELL")
    assert ok is True
    assert c.removed[0] == ["2"]


def test_cancel_one_side_uses_cached_ids_without_fetch():
    be, c = make_backend()
    ok = be.cancel_one_side("YES", "BUY", cached_order_ids=["a", "b"])
    assert ok is True
    assert c.removed[0] == ["a", "b"]


def test_cancel_one_side_no_match_returns_false():
    be, c = make_backend()
    c._open_orders = [{"id": "1", "token_id": "YES", "side": "BUY", "status": "LIVE"}]
    assert be.cancel_one_side("YES", "SELL") is False


def test_cancel_all_removes_all_open_ids():
    be, c = make_backend()
    c._open_orders = [
        {"id": "1", "token_id": "YES", "side": "BUY", "status": "LIVE"},
        {"id": "2", "token_id": "NO", "side": "SELL", "status": "LIVE"},
    ]
    be.cancel_all()
    assert sorted(c.removed[0]) == ["1", "2"]


def test_get_all_orders_passthrough():
    be, c = make_backend()
    c._open_orders = [{"id": "1", "token_id": "YES", "side": "BUY", "status": "LIVE"}]
    out = be.get_all_orders()
    assert out[0]["id"] == "1"


def test_get_all_my_orders_grouped_best_prices_and_ids():
    be, c = make_backend()
    c._open_orders = [
        {"id": "1", "token_id": "YES", "side": "BUY", "status": "LIVE", "price": 0.4},
        {"id": "2", "token_id": "YES", "side": "BUY", "status": "LIVE", "price": 0.45},
        {"id": "3", "token_id": "YES", "side": "SELL", "status": "LIVE", "price": 0.6},
    ]
    g = be.get_all_my_orders_grouped()
    assert g["YES"]["best_bid"] == 0.45
    assert g["YES"]["best_ask"] == 0.6
    assert sorted(g["YES"]["bid_ids"]) == ["1", "2"]
    assert g["YES"]["ask_ids"] == ["3"]


def test_create_order_unregistered_token_raises_fail_fast():
    import pytest
    be, c = make_backend()
    with pytest.raises(RuntimeError):
        be.create_order("UNKNOWN", "BUY", 0.5, 100, neg_risk=True)
    assert c.created == []                       # 未注册 → 拒绝下单，绝不盲发


def test_cancel_all_batches_over_100():
    be, c = make_backend()
    c._open_orders = [
        {"id": str(i), "token_id": "YES", "side": "BUY", "status": "LIVE"}
        for i in range(150)
    ]
    be.cancel_all()
    assert [len(b) for b in c.removed] == [100, 50]   # 分批 ≤100
