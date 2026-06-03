"""SP收尾: predict.fun 报价器 —— 贴 mid 双边、夹进奖励带、不交叉、薄簿照报。"""
from predictfun_data.placer import compute_quotes, place_for_token


def test_quotes_improve_one_tick_inside_book():
    # 实测 Starmer：bids 0.102, asks 0.11，该市场 tick=0.001 → 改善 1 tick：买 0.103 / 卖 0.109
    q = compute_quotes(0.102, 0.11, max_spread=0.06, tick_size=0.001, improve_ticks=1)
    assert q == (0.103, 0.109)


def test_quotes_join_touch_when_too_tight_to_improve():
    # 1 tick 宽簿：改善会交叉 → 退为贴盘口
    q = compute_quotes(0.10, 0.11, max_spread=0.06, tick_size=0.01, improve_ticks=1)
    assert q == (0.10, 0.11)


def test_quotes_clamped_into_reward_band():
    # 宽簿 bb0.20/ba0.80,mid0.5,带0.06 → 夹进 [0.44,0.56]
    q = compute_quotes(0.20, 0.80, max_spread=0.06, tick_size=0.01)
    assert q == (0.44, 0.56)


def test_quotes_none_when_one_sided_or_crossed():
    assert compute_quotes(0.0, 0.11, 0.06, 0.01) is None    # 无买侧
    assert compute_quotes(0.10, 0.0, 0.06, 0.01) is None    # 无卖侧
    assert compute_quotes(0.55, 0.50, 0.06, 0.01) is None   # 交叉簿


class _Meta:
    tick_size = 0.001     # 该市场 3 位小数(decimalPrecision=3)


class FakeBackend:
    def __init__(self):
        self.created = []
    def meta_for(self, tid):
        return _Meta()
    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created.append({"side": side, "price": price, "size": size}); return {"status": "live"}


TOKEN = {"token_id": "T", "max_spread": 0.06, "min_size": 150}


def test_place_for_token_both_sides_at_quotes():
    be = FakeBackend()
    out = place_for_token(be, TOKEN, 0.102, 0.11, {"BUY", "SELL"})
    assert {o["side"] for o in out} == {"BUY", "SELL"}
    assert all(o["status"] == "placed" for o in out)
    sent = {c["side"]: c for c in be.created}
    assert sent["BUY"]["price"] == 0.103 and sent["SELL"]["price"] == 0.109
    assert sent["BUY"]["size"] == 150.0          # shareThreshold(min_size)


def test_place_for_token_only_requested_side():
    be = FakeBackend()
    place_for_token(be, TOKEN, 0.102, 0.11, {"BUY"})
    assert [c["side"] for c in be.created] == ["BUY"]


def test_place_for_token_no_quote_when_one_sided():
    be = FakeBackend()
    out = place_for_token(be, TOKEN, 0.0, 0.11, {"BUY", "SELL"})
    assert out[0]["status"] == "no_quote"
    assert be.created == []
