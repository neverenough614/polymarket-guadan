from types import SimpleNamespace


def level(price, size):
    return SimpleNamespace(price=str(price), size=str(size))


class FakeBook:
    def __init__(self, market="condition-1"):
        self.market = market
        self.bids = [
            level(0.49, 10000),
            level(0.48, 10000),
            level(0.47, 10000),
        ]
        self.asks = [
            level(0.51, 10000),
            level(0.52, 10000),
            level(0.53, 10000),
        ]


class FakeClob:
    def __init__(self, book=None, market_payload=None):
        self.book = book or FakeBook()
        self.market_payload = market_payload or {
            "rewards": {"rates": [{"asset_address": "x", "rewards_daily_rate": 100.0}], "max_spread": "0.05"}
        }

    def get_order_book(self, token_id):
        return self.book

    def get_market(self, condition_id):
        return self.market_payload


class FakePolyClient:
    def __init__(self, clob):
        self.client = clob
        self.created_orders = []
        self.cancelled_assets = []

    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created_orders.append((token_id, side, price, size, neg_risk))
        return {"status": "live"}

    def cancel_all_asset(self, token_id):
        self.cancelled_assets.append(token_id)


def token_info(**overrides):
    data = {
        "token_id": "token-1",
        "token_type": "YES",
        "question": "Test market",
        "min_size": 100.0,
        "neg_risk": False,
        "max_spread": 0.05,
        "volatility_sum": 0.0,
        "source": "Normal LP",
        "rewards_daily_rate": 1.0,
    }
    data.update(overrides)
    return data


def test_place_order_skips_when_reward_efficiency_below_threshold(monkeypatch):
    import main

    monkeypatch.setattr(main.market_heat.tracker, "get_heat_state", lambda token_id: ("NORMAL", 0, ""))
    poly_client = FakePolyClient(FakeClob())

    result = main.place_order_for_token(poly_client, token_info())

    assert poly_client.created_orders == []
    assert result["buy_status"].startswith("reward_efficiency_skip")
    assert result["sell_status"].startswith("reward_efficiency_skip")


def test_inactive_reward_program_is_rejected_and_existing_orders_cancelled(monkeypatch):
    import main

    inactive_market = {"rewards": {"rates": [], "max_spread": "0"}}
    poly_client = FakePolyClient(FakeClob(market_payload=inactive_market))
    book = FakeBook(market="condition-1")

    status = main.get_live_reward_program_status(poly_client, token_info(), book)
    cancelled = main.cancel_if_reward_program_inactive(
        poly_client,
        token_info(),
        book,
        my_bid_price=0.49,
        my_ask_price=None,
    )

    assert status["active"] is False
    assert status["reason"] == "inactive_reward_program"
    assert cancelled is True
    assert poly_client.cancelled_assets == ["token-1"]
