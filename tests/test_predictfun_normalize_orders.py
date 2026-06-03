"""SP2: normalize_order 兼容 predict.fun /v1/orders 实测嵌套形状（主网抓取）。"""
from predictfun_data.normalize import normalize_order


# 主网实测：BUY No @0.02 x 50（order 字段嵌套，side=0，无 price/quantity）
RAW_LIVE_BUY = {
    "amount": "50000000000000000000",
    "amountFilled": "0",
    "currency": "USDT",
    "id": "300177070",
    "isNegRisk": True,
    "isYieldBearing": True,
    "marketId": 420294,
    "order": {
        "expiration": 4102444800,
        "feeRateBps": "200",
        "hash": "0x158d0f",
        "maker": "0x5322",
        "makerAmount": "1000000000000000000",   # 1.0 USDT
        "nonce": "0",
        "salt": "1255665763",
        "side": 0,                               # BUY
        "signatureType": 0,
        "signer": "0x5322",
        "taker": "0x0000000000000000000000000000000000000000",
        "takerAmount": "50000000000000000000",   # 50 shares
        "tokenId": "64064053505788208683485407135557779016802465915147364814314720603469680982232",
    },
    "status": "OPEN",
    "strategy": "LIMIT",
}


def test_normalize_live_buy_extracts_nested_fields():
    o = normalize_order(RAW_LIVE_BUY)
    assert o["id"] == "300177070"
    assert o["market_id"] == 420294
    assert o["token_id"].endswith("80982232")     # order.tokenId（No outcome）
    assert o["side"] == "BUY"                      # side=0 → BUY
    assert o["status"] == "LIVE"                   # OPEN → LIVE


def test_normalize_live_buy_derives_price_and_size_from_amounts():
    o = normalize_order(RAW_LIVE_BUY)
    assert o["price"] == 0.02                      # 1.0 USDT / 50 shares
    assert o["size"] == 50.0                       # from_wei(takerAmount)
    assert o["size_matched"] == 0.0               # amountFilled=0


def test_normalize_live_sell_derives_price_from_taker_over_maker():
    raw = dict(RAW_LIVE_BUY)
    raw["order"] = dict(RAW_LIVE_BUY["order"])
    raw["order"]["side"] = 1                        # SELL：给 shares(maker)、收 USDT(taker)
    raw["order"]["makerAmount"] = "50000000000000000000"  # 50 shares
    raw["order"]["takerAmount"] = "30000000000000000000"  # 30 USDT
    o = normalize_order(raw)
    assert o["side"] == "SELL"
    assert o["price"] == 0.6                        # 30 / 50
    assert o["size"] == 50.0


def test_normalize_flat_shape_still_works():
    # 扁平形状（簿/旧测试）保持不变
    o = normalize_order({"id": "1", "tokenId": "tok", "side": "Bid",
                         "price": "0.5", "quantity": "10", "status": "OPEN"})
    assert o["side"] == "BUY"
    assert o["price"] == 0.5
    assert o["size"] == 10.0
    assert o["status"] == "LIVE"
    assert o["token_id"] == "tok"
