"""best_competing_bid 测试 —— 从含我方挂单的盘口里剔除自己，算出对手最高买价。"""
from penny_up_bot.book_state import BookState, best_competing_bid


class TestBestCompetingBid:
    def test_empty_book_returns_none(self):
        assert best_competing_bid({}, my_price=None, my_size=0.0) is None

    def test_no_self_order_returns_top_bid(self):
        bids = {0.60: 100.0, 0.59: 200.0}
        assert best_competing_bid(bids, my_price=None, my_size=0.0) == 0.60

    def test_only_me_at_top_skips_to_next(self):
        # 0.61 这档只有我（100 全是我的），应跳到 0.55
        bids = {0.61: 100.0, 0.55: 300.0}
        assert best_competing_bid(bids, my_price=0.61, my_size=100.0) == 0.55

    def test_only_me_no_other_returns_none(self):
        bids = {0.61: 100.0}
        assert best_competing_bid(bids, my_price=0.61, my_size=100.0) is None

    def test_me_plus_others_same_level_counts_others(self):
        # 0.61 这档共 150，其中我 100，对手还有 50 → 对手最高买价仍是 0.61
        bids = {0.61: 150.0}
        assert best_competing_bid(bids, my_price=0.61, my_size=100.0) == 0.61

    def test_my_price_not_in_book_no_subtraction(self):
        bids = {0.60: 100.0}
        assert best_competing_bid(bids, my_price=0.70, my_size=100.0) == 0.60

    def test_float_tolerance_matches_self_level(self):
        # 盘口价由浮点构造，应能匹配到我的价位并扣减
        bids = {0.61: 100.0000001}
        assert best_competing_bid(bids, my_price=0.61, my_size=100.0) is None

    def test_partial_self_fill_leaves_competitor(self):
        bids = {0.61: 100.0, 0.60: 10.0}
        # 我在 0.61 挂 100，全是我的 → 跳到 0.60 的对手
        assert best_competing_bid(bids, my_price=0.61, my_size=100.0) == 0.60


class TestBookStateUpdates:
    def test_apply_snapshot(self):
        bs = BookState()
        bs.apply_snapshot(
            bids=[{"price": "0.60", "size": "100"}, {"price": "0.59", "size": "50"}],
            asks=[{"price": "0.62", "size": "80"}],
        )
        assert bs.best_competing_bid(my_price=None, my_size=0.0) == 0.60

    def test_price_change_removes_level_on_zero_size(self):
        bs = BookState()
        bs.apply_snapshot(bids=[{"price": "0.60", "size": "100"}], asks=[])
        bs.apply_price_change("BUY", 0.60, 0.0)  # size 0 -> 该档消失
        assert bs.best_competing_bid(my_price=None, my_size=0.0) is None

    def test_price_change_updates_level(self):
        bs = BookState()
        bs.apply_snapshot(bids=[{"price": "0.60", "size": "100"}], asks=[])
        bs.apply_price_change("BUY", 0.61, 30.0)  # 新增更高一档
        assert bs.best_competing_bid(my_price=None, my_size=0.0) == 0.61
