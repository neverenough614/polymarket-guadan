import pytest
from predictfun_data.rest_api import PredictRest, PredictApiError, HttpResp


class FakeTransport:
    def __init__(self, responses):
        # responses: list of (status_code, body_dict)
        self._responses = list(responses)
        self.calls = []  # (method, url, headers, json_body)

    def __call__(self, method, url, headers, json_body):
        self.calls.append((method, url, dict(headers or {}), json_body))
        status, body = self._responses.pop(0)
        return HttpResp(status, body)


def make_rest(transport, **kw):
    return PredictRest(
        base_url="https://api-testnet.predict.fun",
        api_key=None,
        jwt_provider=lambda: "JWT123",
        transport=transport,
        throttle=lambda: None,
        **kw,
    )


def test_get_injects_bearer_and_builds_url():
    t = FakeTransport([(200, {"success": True, "data": {"marketId": 7}})])
    rest = make_rest(t)
    out = rest.get_orderbook(7)
    assert out["data"]["marketId"] == 7
    method, url, headers, _ = t.calls[0]
    assert method == "GET"
    assert url == "https://api-testnet.predict.fun/v1/markets/7/orderbook"
    assert headers["Authorization"] == "Bearer JWT123"


def test_non_2xx_raises_predict_api_error():
    t = FakeTransport([(500, {"success": False, "error": "boom"})])
    rest = make_rest(t)
    with pytest.raises(PredictApiError) as ei:
        rest.get_market(7)
    assert ei.value.status == 500


def test_401_triggers_reauth_then_retries_once():
    t = FakeTransport([(401, {"error": "expired"}), (200, {"success": True, "data": []})])
    reauth_calls = []
    rest = make_rest(t, on_unauthorized=lambda: reauth_calls.append(1))
    out = rest.get_my_orders()
    assert out["success"] is True
    assert len(reauth_calls) == 1
    assert len(t.calls) == 2  # 第一次 401，重鉴后重试


def test_401_twice_raises():
    t = FakeTransport([(401, {}), (401, {})])
    rest = make_rest(t, on_unauthorized=lambda: None)
    with pytest.raises(PredictApiError):
        rest.get_my_orders()


def test_remove_orders_posts_ids_payload():
    t = FakeTransport([(200, {"success": True, "removed": ["1"], "noop": []})])
    rest = make_rest(t)
    out = rest.remove_orders(["1", "2"])
    method, url, _, body = t.calls[0]
    assert method == "POST"
    assert url == "https://api-testnet.predict.fun/v1/orders/remove"
    assert body == {"data": {"ids": ["1", "2"]}}
    assert out["removed"] == ["1"]


def test_mainnet_adds_api_key_header():
    t = FakeTransport([(200, {"success": True, "data": []})])
    rest = PredictRest(
        base_url="https://api.predict.fun", api_key="KEY",
        jwt_provider=lambda: "JWT", transport=t, throttle=lambda: None,
    )
    rest.get_markets()
    _, _, headers, _ = t.calls[0]
    assert headers["X-API-KEY"] == "KEY"
