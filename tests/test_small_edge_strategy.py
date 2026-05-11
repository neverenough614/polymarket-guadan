from types import SimpleNamespace

import pandas as pd


def level(price, size):
    return SimpleNamespace(price=str(price), size=str(size))


class ThinBook:
    market = "condition-small"
    bids = [level(0.49, 120), level(0.48, 80), level(0.47, 60)]
    asks = [level(0.51, 120), level(0.52, 80), level(0.53, 60)]


class ExtremeBook:
    market = "condition-extreme"
    bids = [level(0.98, 200), level(0.97, 200), level(0.96, 200)]
    asks = [level(0.99, 200), level(1.0, 200)]


class ExitGapBook:
    market = "condition-gap"
    bids = [level(0.78, 200), level(0.68, 20), level(0.67, 20), level(0.66, 20)]
    asks = [level(0.80, 200), level(0.81, 200), level(0.82, 200)]


class FakeClob:
    def __init__(self, book=None):
        self.book = book or ThinBook()

    def get_order_book(self, token_id):
        return self.book

    def get_market(self, condition_id):
        return {
            "rewards": {
                "rates": [{"asset_address": "x", "rewards_daily_rate": "20"}],
                "max_spread": "4.5",
            }
        }


class FakePolyClient:
    def __init__(self, book=None):
        self.client = FakeClob(book)
        self.created_orders = []

    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created_orders.append((token_id, side, price, size, neg_risk))
        return {"status": "live"}


def test_small_edge_uses_small_size_and_relaxed_depth(monkeypatch):
    import main

    monkeypatch.setattr(main.market_heat.tracker, "get_heat_state", lambda token_id: ("NORMAL", 0, ""))
    poly_client = FakePolyClient()

    result = main.place_order_for_token(
        poly_client,
        {
            "token_id": "small-token",
            "token_type": "YES",
            "question": "Small edge test market",
            "min_size": 50.0,
            "neg_risk": False,
            "max_spread": 0.045,
            "volatility_sum": 0.0,
            "source": "Small Edge",
        },
    )

    assert result["order_size"] <= main.SMALL_EDGE_MAX_ORDER_SIZE
    assert result["buy_status"] == "placed"
    assert poly_client.created_orders[0][3] <= main.SMALL_EDGE_MAX_ORDER_SIZE


def test_small_edge_skips_extreme_price_market(monkeypatch):
    import main

    monkeypatch.setattr(main.market_heat.tracker, "get_heat_state", lambda token_id: ("NORMAL", 0, ""))
    poly_client = FakePolyClient(ExtremeBook())

    result = main.place_order_for_token(
        poly_client,
        {
            "token_id": "small-extreme-token",
            "token_type": "NO",
            "question": "Small edge extreme test market",
            "min_size": 50.0,
            "neg_risk": False,
            "max_spread": 0.045,
            "volatility_sum": 0.0,
            "source": "Small Edge",
        },
    )

    assert result["buy_status"] == "small_edge_extreme_skip"
    assert result["sell_status"] == "small_edge_extreme_skip"
    assert poly_client.created_orders == []


def test_small_edge_skips_when_buy_price_has_no_exit_support(monkeypatch):
    import main

    monkeypatch.setattr(main.market_heat.tracker, "get_heat_state", lambda token_id: ("NORMAL", 0, ""))
    poly_client = FakePolyClient(ExitGapBook())

    result = main.place_order_for_token(
        poly_client,
        {
            "token_id": "small-gap-token",
            "token_type": "YES",
            "question": "Small edge gap test market",
            "min_size": 50.0,
            "neg_risk": False,
            "max_spread": 0.10,
            "volatility_sum": 0.0,
            "source": "Small Edge",
        },
    )

    assert result["buy_status"] == "small_edge_exit_risk"
    assert result["sell_status"] == "small_edge_exit_risk"
    assert poly_client.created_orders == []


def test_update_markets_builds_small_edge_strategy():
    import update_markets

    df = pd.DataFrame(
        [
            {
                "question": "Candidate",
                "rewards_daily_rate": 20.0,
                "min_size": 50.0,
                "max_spread": 4.5,
                "spread": 0.02,
                "best_bid": 0.49,
                "best_ask": 0.51,
                "bid_reward_per_100": 0.8,
                "ask_reward_per_100": 0.7,
                "gm_reward_per_100": 0.75,
                "mid_reward_per_100": 0.7,
                "volatility_sum": 2.0,
                "days_to_expiry": 30,
                "token1": "yes-token",
                "token2": "no-token",
                "condition_id": "condition-small",
            },
            {
                "question": "Too inefficient",
                "rewards_daily_rate": 20.0,
                "min_size": 50.0,
                "max_spread": 4.5,
                "spread": 0.02,
                "best_bid": 0.49,
                "best_ask": 0.51,
                "bid_reward_per_100": 0.1,
                "ask_reward_per_100": 0.1,
                "gm_reward_per_100": 0.1,
                "mid_reward_per_100": 0.1,
                "volatility_sum": 2.0,
                "days_to_expiry": 30,
                "token1": "bad-yes",
                "token2": "bad-no",
                "condition_id": "condition-bad",
            },
        ]
    )

    strategies = update_markets._apply_strategy_filters(df)

    assert "small_edge" in strategies
    assert list(strategies["small_edge"]["question"]) == ["Candidate"]
    assert strategies["small_edge"].iloc[0]["small_edge_order_size"] <= 300


def test_update_markets_excludes_extreme_small_edge_best_bid():
    import update_markets

    df = pd.DataFrame(
        [
            {
                "question": "Extreme small edge",
                "rewards_daily_rate": 20.0,
                "min_size": 50.0,
                "max_spread": 4.5,
                "spread": 0.02,
                "best_bid": 0.91,
                "best_ask": 0.93,
                "bid_reward_per_100": 0.8,
                "ask_reward_per_100": 0.7,
                "gm_reward_per_100": 0.75,
                "mid_reward_per_100": 0.7,
                "volatility_sum": 2.0,
                "days_to_expiry": 30,
                "token1": "yes-extreme",
                "token2": "no-extreme",
                "condition_id": "condition-extreme",
            },
        ]
    )

    strategies = update_markets._apply_strategy_filters(df)

    assert strategies["small_edge"].empty


def test_update_markets_keeps_all_small_edge_strategy_rows():
    import update_markets

    rows = []
    for i in range(8):
        rows.append(
            {
                "question": f"Candidate {i}",
                "rewards_daily_rate": 20.0,
                "min_size": 50.0,
                "max_spread": 4.5,
                "spread": 0.02,
                "best_bid": 0.49,
                "best_ask": 0.51,
                "bid_reward_per_100": 0.5 + i,
                "ask_reward_per_100": 0.5 + i,
                "gm_reward_per_100": 0.5 + i,
                "mid_reward_per_100": 0.5 + i,
                "volatility_sum": 1.0,
                "days_to_expiry": 30,
                "token1": f"yes-{i}",
                "token2": f"no-{i}",
                "condition_id": f"condition-{i}",
            }
        )

    strategies = update_markets._apply_strategy_filters(pd.DataFrame(rows))

    assert len(strategies["small_edge"]) == 8
    assert strategies["small_edge"].iloc[0]["question"] == "Candidate 7"
    assert strategies["small_edge"].iloc[-1]["question"] == "Candidate 0"
