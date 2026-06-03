"""收尾: predict.fun 报价器 —— 只买不裸卖,贴 bid、夹进奖励带、薄簿照报。

双边靠"买 YES + 买 NO"(买 NO=卖 YES)实现,故每个 token 只算/挂一张买单。
"""
from predictfun_data.placer import compute_bid, place_bid


def test_bid_improves_one_tick_inside_book():
    # Starmer YES：bids 0.102, asks 0.11，tick=0.001 → 改善 1 tick：买 0.103
    assert compute_bid(0.102, 0.11, max_spread=0.06, tick_size=0.001, improve_ticks=1) == 0.103


def test_bid_joins_touch_when_too_tight_to_improve():
    # 1 tick 宽簿：改善会越过卖一 → 退为贴买一
    assert compute_bid(0.10, 0.11, max_spread=0.06, tick_size=0.01, improve_ticks=1) == 0.10


def test_bid_clamped_to_reward_band_lower_edge():
    # 宽簿 bb0.20/ba0.80,mid0.5,带0.06 → 买单不得低于 mid-band=0.44；贴买一0.21会被抬到0.44
    assert compute_bid(0.20, 0.80, max_spread=0.06, tick_size=0.01) == 0.44


def test_bid_none_when_one_sided_or_crossed():
    assert compute_bid(0.0, 0.11, 0.06, 0.01) is None     # 无买侧
    assert compute_bid(0.10, 0.0, 0.06, 0.01) is None     # 无卖侧
    assert compute_bid(0.55, 0.50, 0.06, 0.01) is None    # 交叉簿


class _Meta:
    tick_size = 0.001     # 3 位小数市场(decimalPrecision=3)


class FakeBackend:
    def __init__(self):
        self.created = []
    def meta_for(self, tid):
        return _Meta()
    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created.append({"side": side, "price": price, "size": size}); return {"status": "live"}


TOKEN = {"token_id": "T", "max_spread": 0.06, "min_size": 150}


def test_place_bid_places_single_buy_at_bid():
    be = FakeBackend()
    res = place_bid(be, TOKEN, 0.102, 0.11)
    assert res["status"] == "placed" and res["side"] == "BUY"
    assert be.created == [{"side": "BUY", "price": 0.103, "size": 150.0}]   # tick=0.001, size=shareThreshold


def test_place_bid_never_sells():
    be = FakeBackend()
    place_bid(be, TOKEN, 0.102, 0.11)
    assert all(c["side"] == "BUY" for c in be.created)      # 永不发 SELL（不裸卖）


def test_place_bid_no_quote_when_one_sided():
    be = FakeBackend()
    res = place_bid(be, TOKEN, 0.0, 0.11)
    assert res["status"] == "no_quote"
    assert be.created == []
