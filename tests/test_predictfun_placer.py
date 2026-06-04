"""收尾: predict.fun 报价器 —— 仿 Polymarket（带内贴最优档、动态量、出场护栏），按薄簿校准。

双边靠"买 YES + 买 NO"(买 NO=卖 YES)实现,故每个 token 只算/挂一张买单。
"""
from predictfun_data.placer import (
    select_join_price, exit_liquidity_ok, compute_quote, place_bid, order_efficiency,
)
from predictfun_data.normalize import NormalizedBook, BookLevel
from config.bot_config import PredictFunPlaceConfig


PC = PredictFunPlaceConfig()


# ---------- select_join_price：带内贴最优档（不改善）----------
def test_join_picks_highest_in_band_level_with_depth():
    # mid=0.50, band=0.06 → [0.44,0.50]。0.49 深度足 → 选最接近 mid 的 0.49（不+1tick 抢内侧）
    bids = [(0.49, 1000), (0.47, 2000), (0.30, 9999)]
    assert select_join_price(bids, mid=0.50, band=0.06, min_level_depth=15) == (0.49, 490.0)


def test_join_skips_dust_top_level():
    # 最优档是灰尘(0.49×20=9.8<15) → 跳到下一带内非灰尘档 0.47
    bids = [(0.49, 20), (0.47, 2000), (0.30, 9999)]
    price, _ = select_join_price(bids, mid=0.50, band=0.06, min_level_depth=15)
    assert price == 0.47


def test_join_none_when_no_in_band_level():
    # 最高买档 0.40 < mid-band(0.44) → 带内无档
    assert select_join_price([(0.40, 9999)], mid=0.50, band=0.06, min_level_depth=15) is None


# ---------- 固定 min_size：compute_quote 永远按 min_size 报，不随簿深放大 ----------
def test_quote_size_is_fixed_min_size_not_dynamic():
    # 极厚簿也只报 min_size（监控重挂走同函数，必须与预算两腿同量一致，不能逃逸预算）
    book = _book([(0.49, 9000), (0.47, 9000), (0.45, 9000)], [(0.51, 9000)])
    _price, size, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert reason == "ok" and size == 100.0


# ---------- exit_liquidity_ok：买入成交后能否卖回更低 bid ----------
def test_exit_ok_when_nearby_lower_bid_has_depth():
    bids = [(0.50, 1000), (0.48, 1000)]   # 我买 0.50，下方 0.48 gap=0.02≤0.05，深度 480≥notional
    ok, _ = exit_liquidity_ok(bids, buy_price=0.50, size=100, max_gap=0.05, multiplier=1.0)
    assert ok


def test_exit_fails_when_no_lower_bid():
    ok, why = exit_liquidity_ok([(0.50, 1000)], buy_price=0.50, size=100, max_gap=0.05, multiplier=1.0)
    assert not ok and why == "no_lower_bid"


def test_exit_fails_when_lower_bid_too_far():
    bids = [(0.50, 1000), (0.40, 9999)]   # gap 0.10 > 0.05
    ok, why = exit_liquidity_ok(bids, buy_price=0.50, size=100, max_gap=0.05, multiplier=1.0)
    assert not ok and why.startswith("gap_")


# ---------- compute_quote：端到端 ----------
def _book(bids, asks):
    return NormalizedBook(1, [BookLevel(p, s) for p, s in bids], [BookLevel(p, s) for p, s in asks])


def test_quote_joins_best_bid_when_healthy():
    book = _book([(0.49, 2000), (0.47, 2000), (0.45, 2000)], [(0.51, 2000)])
    price, size, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert reason == "ok" and price == 0.49 and size >= 100


def test_quote_none_when_one_sided():
    book = _book([(0.49, 2000)], [])
    price, _, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert price is None and reason == "one_sided"


def test_quote_none_when_top5_too_thin():
    # 前5档累计 < 150 → 死簿跳过
    book = _book([(0.49, 100), (0.47, 50)], [(0.51, 100)])  # top5≈72.5 USDT
    price, _, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert price is None and reason.startswith("thin_top5")


def test_quote_none_when_extreme_price():
    # mid≈0.10 < price_min 0.15 → 极端价不报价（避开事件跳变被吃）
    book = _book([(0.099, 5000), (0.09, 5000)], [(0.11, 5000)])
    price, _, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert price is None and reason.startswith("extreme_price")


def test_quote_ok_for_mid_price_market():
    book = _book([(0.49, 5000), (0.47, 5000)], [(0.51, 5000)])   # mid 0.50 在带内
    price, _, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert reason == "ok" and price is not None


def test_quote_none_when_no_in_band_level():
    # 买侧全在带外（best_bid 0.40 < mid-band 0.45），但前5档够厚
    book = _book([(0.40, 5000), (0.39, 5000)], [(0.60, 5000)])  # mid=0.5, band 0.06 → [0.44,0.5]
    price, _, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert price is None and reason == "no_in_band_level"


def test_quote_none_when_imbalanced():
    # 买侧深度占比过低（前5档够厚以越过 thin 门槛，但买/卖严重失衡）→ 偏斜入场门槛拦下
    # bid5=0.49*700+0.47*600=625(≥500过thin)；ask5=0.51*5000+0.52*5000=5150；占比 625/5775≈11%<25%
    book = _book([(0.49, 700), (0.47, 600)], [(0.51, 5000), (0.52, 5000)])
    price, _, reason = compute_quote(book, max_spread=0.06, tick_size=0.01, min_size=100, pc=PC)
    assert price is None and reason == "imbalance"


# ---------- place_bid：只买不裸卖 ----------
class _Meta:
    tick_size = 0.001
    market_id = "m1"


class FakeBackend:
    def __init__(self):
        self.created = []
    def meta_for(self, tid):
        return _Meta()
    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created.append({"side": side, "price": price, "size": size}); return {"status": "live"}


TOKEN = {"token_id": "T", "max_spread": 0.06, "min_size": 150}


def test_place_bid_places_single_buy():
    be = FakeBackend()
    book = _book([(0.49, 2000), (0.47, 2000), (0.45, 2000)], [(0.51, 2000)])
    res = place_bid(be, TOKEN, book)
    assert res["status"] == "placed" and res["side"] == "BUY"
    assert be.created and all(c["side"] == "BUY" for c in be.created)   # 永不发 SELL


def test_place_bid_no_quote_when_one_sided():
    be = FakeBackend()
    res = place_bid(be, TOKEN, _book([(0.49, 2000)], []))
    assert res["status"] == "no_quote" and be.created == []


# ---------- order_efficiency：单张买单预期日奖励（复用 Polymarket 公式，零漂移）----------
def test_efficiency_positive_in_band():
    # 簿内 1000 share 竞争，我 100 share 同价 0.49；mid 0.50，max_spread 0.06，日奖励率 2400
    # q=((0.06-0.01)/0.06)²=0.6944；share=69.44/763.89=0.0909；日奖励=2400×0.5×0.0909≈109.09
    eff = order_efficiency([(0.49, 1000)], price=0.49, size=100, mid=0.50,
                           max_spread=0.06, daily_rate=2400.0)
    assert abs(eff["expected_daily_reward"] - 109.09) < 0.5
    assert eff["expected_reward_per_100"] > 0


def test_efficiency_zero_out_of_band():
    # 挂价 0.43 距 mid 0.07 > max_spread 0.06 → q=0 → 拿不到奖励
    eff = order_efficiency([(0.49, 1000)], price=0.43, size=100, mid=0.50,
                           max_spread=0.06, daily_rate=2400.0)
    assert eff["expected_daily_reward"] == 0.0


def test_efficiency_thin_competition_beats_crowded():
    # 同样日奖励率，竞争薄的市场份额大 → 效率高（"挂薄市场拿高奖励"）
    thin = order_efficiency([(0.49, 100)], 0.49, 100, 0.50, 0.06, 2400.0)
    crowded = order_efficiency([(0.49, 5000)], 0.49, 100, 0.50, 0.06, 2400.0)
    assert thin["expected_daily_reward"] > crowded["expected_daily_reward"]
