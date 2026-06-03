from config.bot_config import PredictFunConfig


def test_predictfun_config_testnet_endpoint():
    c = PredictFunConfig(network="testnet")
    assert c.base_url == "https://api-testnet.predict.fun"
    assert c.chain_id == 97
    assert c.requires_api_key is False


def test_predictfun_config_mainnet_endpoint():
    c = PredictFunConfig(network="mainnet")
    assert c.base_url == "https://api.predict.fun"
    assert c.chain_id == 56
    assert c.requires_api_key is True


def test_predictfun_config_invalid_network_raises():
    import pytest
    with pytest.raises(ValueError):
        PredictFunConfig(network="devnet")


from predictfun_data.predictfun_client import PredictFunClient


class FakeAmounts:
    maker_amount = 52_000_000
    taker_amount = 100_000_000


class FakeSignedOrder:
    signature = "0xSIG"
    def to_dict(self):
        return {"maker": "0xabc", "makerAmount": "52000000", "takerAmount": "100000000"}


class FakeBuilder:
    def __init__(self):
        self.cancelled = []
    def get_limit_order_amounts(self, _inp):
        return FakeAmounts()
    def build_order(self, _type, _inp):
        return {"order": "obj"}
    def build_typed_data(self, _order, is_neg_risk, is_yield_bearing):
        return {"typed": "data", "is_neg_risk": is_neg_risk, "is_yield_bearing": is_yield_bearing}
    def sign_typed_data_order(self, _typed):
        return FakeSignedOrder()
    def balance_of(self, _sym, address=None):
        return 123_500000000000000000  # 原始 wei(18位)，from_wei 后 = 123.5


class FakeRest:
    def __init__(self):
        self.created = []
        self.removed = []
        self._orders = []
    def create_order(self, body):
        self.created.append(body)
        return {"success": True, "data": {"id": "ord1", "hash": "0xh"}}
    def remove_orders(self, ids):
        self.removed.append(list(ids))
        return {"success": True, "removed": list(ids), "noop": []}
    def get_my_orders(self, **kw):
        return {"success": True, "data": self._orders}
    def get_orderbook(self, mid):
        return {"success": True, "data": {"marketId": mid, "bids": [[0.49, 100]], "asks": [[0.51, 80]]}}


def make_client(**kw):
    return PredictFunClient(
        network="testnet",
        builder=FakeBuilder(),
        rest=FakeRest(),
        signer=lambda msg: "0xSIG",
        address="0xMe",
        skip_auth=True,
        **kw,
    )


def test_create_order_builds_signed_body_and_normalizes():
    c = make_client()
    out = c.create_order("tok", "BUY", 0.523, 100, neg_risk=False, is_yield_bearing=False)
    assert out["status"] == "live"
    assert out["order_id"] == "ord1"
    body = c.rest.created[0]
    assert body["data"]["strategy"] == "LIMIT"
    assert body["data"]["order"]["signature"] == "0xSIG"
    assert "pricePerShare" in body["data"]


def test_create_order_error_returns_error_status():
    c = make_client()
    def boom(body):
        raise RuntimeError("rejected")
    c.rest.create_order = boom
    out = c.create_order("tok", "BUY", 0.5, 100)
    assert out["status"] == "error"
    assert "rejected" in out["error"]


def test_get_open_orders_normalizes():
    c = make_client()
    c.rest._orders = [{"id": "1", "tokenId": "tok", "side": "Bid",
                       "price": "0.5", "quantity": "10", "status": "OPEN"}]
    orders = c.get_open_orders()
    assert orders[0]["side"] == "BUY"
    assert orders[0]["status"] == "LIVE"
    assert orders[0]["price"] == 0.5


def test_remove_orders_batches_over_100():
    c = make_client()
    ids = [str(i) for i in range(150)]
    c.remove_orders(ids)
    assert [len(b) for b in c.rest.removed] == [100, 50]


def test_get_usdt_balance_delegates_to_builder():
    c = make_client()
    assert c.get_usdt_balance() == 123.5


class FakeAuthRest:
    """模拟生产 REST：auth 端点内部会调用 jwt_provider（即 _headers 的行为），用于复现递归路径。"""
    def __init__(self, token_resp):
        self.jwt_provider = None
        self._token_resp = token_resp
        self.auth_message_calls = 0
    def get_auth_message(self):
        if self.jwt_provider:
            self.jwt_provider()
        self.auth_message_calls += 1
        return {"data": {"message": "sign-me"}}
    def exchange_jwt(self, signer, message, signature):
        if self.jwt_provider:
            self.jwt_provider()
        return self._token_resp


def test_authenticate_no_infinite_recursion():
    rest = FakeAuthRest({"data": {"token": "JWT-OK", "expiresAt": 9999999999}})
    c = PredictFunClient(network="testnet", builder=FakeBuilder(), rest=rest,
                         signer=lambda m: "0xSIG", address="0xMe", skip_auth=True)
    rest.jwt_provider = c._ensure_jwt  # 复现生产 wiring
    c.authenticate()  # 必须不 RecursionError
    assert rest.auth_message_calls == 1
    assert c._ensure_jwt() == "JWT-OK"


def test_authenticate_raises_when_token_missing():
    import pytest
    rest = FakeAuthRest({"data": {}})  # 无 token/jwt 字段
    c = PredictFunClient(network="testnet", builder=FakeBuilder(), rest=rest,
                         signer=lambda m: "0xSIG", address="0xMe", skip_auth=True)
    rest.jwt_provider = c._ensure_jwt
    with pytest.raises(RuntimeError):
        c.authenticate()


def test_remove_orders_partial_failure_records_errors():
    c = make_client()
    calls = {"n": 0}
    def flaky(chunk):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("chunk2 failed")
        return {"removed": list(chunk), "noop": []}
    c.rest.remove_orders = flaky
    out = c.remove_orders([str(i) for i in range(150)])
    assert out["success"] is False
    assert len(out["removed"]) == 100      # chunk1 succeeded, recorded
    assert len(out["errors"]) == 1         # chunk2 failure recorded
