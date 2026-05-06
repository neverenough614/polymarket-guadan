from types import SimpleNamespace


def level(price, size):
    return SimpleNamespace(price=str(price), size=str(size))


class FakeBook:
    market = "condition-small"
    bids = [level(0.49, 120), level(0.48, 80), level(0.47, 60)]
    asks = [level(0.51, 120), level(0.52, 80), level(0.53, 60)]


class FakeClob:
    def get_order_book(self, token_id):
        return FakeBook()

    def get_market(self, condition_id):
        return {
            "rewards": {
                "rates": [{"asset_address": "x", "rewards_daily_rate": "20"}],
                "max_spread": "4.5",
            }
        }


class FakePolyClient:
    def __init__(self):
        self.client = FakeClob()
        self.created_orders = []

    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created_orders.append((token_id, side, price, size, neg_risk))
        return {"status": "live"}


def test_existing_live_order_detects_same_token_side_price():
    import main

    orders = [
        {
            "id": "order-1",
            "status": "LIVE",
            "asset_id": "token-1",
            "side": "BUY",
            "price": "0.25",
            "original_size": "100",
        },
        {
            "id": "order-2",
            "status": "LIVE",
            "asset_id": "token-1",
            "side": "SELL",
            "price": "0.75",
            "original_size": "100",
        },
    ]

    assert main.has_matching_live_order(orders, "token-1", "BUY", 0.25)
    assert not main.has_matching_live_order(orders, "token-1", "BUY", 0.24)
    assert not main.has_matching_live_order(orders, "token-2", "BUY", 0.25)


def test_existing_order_lookup_ignores_non_live_orders():
    import main

    orders = [
        {
            "id": "order-1",
            "status": "CANCELED",
            "asset_id": "token-1",
            "side": "BUY",
            "price": "0.25",
            "original_size": "100",
        },
    ]

    assert not main.has_matching_live_order(orders, "token-1", "BUY", 0.25)


def test_place_order_skips_existing_same_side_same_price(monkeypatch):
    import main

    monkeypatch.setattr(main.market_heat.tracker, "get_heat_state", lambda token_id: ("NORMAL", 0, ""))
    monkeypatch.setattr(
        main,
        "get_live_orders_safe",
        lambda poly_client: [
            {
                "id": "existing-buy",
                "status": "LIVE",
                "asset_id": "small-token",
                "side": "BUY",
                "price": "0.49",
                "original_size": "100",
            }
        ],
    )
    poly_client = FakePolyClient()

    result = main.place_order_for_token(
        poly_client,
        {
            "token_id": "small-token",
            "token_type": "YES",
            "question": "Small edge duplicate test market",
            "min_size": 50.0,
            "neg_risk": False,
            "max_spread": 0.045,
            "volatility_sum": 0.0,
            "source": "Small Edge",
        },
    )

    assert result["buy_status"] == "duplicate_skip"
    assert [order[1] for order in poly_client.created_orders] == ["SELL"]
