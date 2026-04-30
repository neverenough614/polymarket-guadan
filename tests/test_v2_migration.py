import poly_data.polymarket_client as polymarket_client


def test_polymarket_client_uses_v2_sdk_and_cancel_payload():
    assert polymarket_client.ClobClient.__module__.startswith("py_clob_client_v2")

    captured = []

    class FakeClob:
        def cancel_market_orders(self, payload):
            captured.append(payload)

    client = polymarket_client.PolymarketClient.__new__(polymarket_client.PolymarketClient)
    client.client = FakeClob()

    client.cancel_all_asset("token-123")

    assert captured
    assert captured[0].__class__.__name__ == "OrderMarketCancelParams"
    assert captured[0].asset_id == "token-123"


def test_update_market_client_helper_uses_v2_sdk():
    import data_updater.trading_utils as trading_utils

    assert trading_utils.ClobClient.__module__.startswith("py_clob_client_v2")


def test_cancel_one_side_orders_uses_v2_open_orders_and_order_payload():
    import main

    class FakeClob:
        def __init__(self):
            self.cancelled = []

        def get_open_orders(self):
            return [
                {"status": "LIVE", "asset_id": "token-123", "side": "BUY", "id": "buy-1"},
                {"status": "LIVE", "asset_id": "token-123", "side": "SELL", "id": "sell-1"},
                {"status": "LIVE", "asset_id": "other-token", "side": "BUY", "id": "buy-2"},
            ]

        def cancel_order(self, payload):
            self.cancelled.append(payload)

    class FakePolyClient:
        def __init__(self):
            self.client = FakeClob()
            self.fallback_used = False

        def cancel_all_asset(self, token_id):
            self.fallback_used = True

    poly_client = FakePolyClient()

    ok = main.cancel_one_side_orders(poly_client, "token-123", "BUY", "Test market")

    assert ok is True
    assert poly_client.fallback_used is False
    assert [payload.orderID for payload in poly_client.client.cancelled] == ["buy-1"]
