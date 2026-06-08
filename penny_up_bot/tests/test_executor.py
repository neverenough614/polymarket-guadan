"""executor 决策测试 —— penny-up 撤+挂逻辑（用假 client 捕获调用，无网络）。"""
import asyncio

from penny_up_bot.book_state import BookState
from penny_up_bot.config import Settings
from penny_up_bot.executor import DRY_ORDER_ID, Executor
from penny_up_bot.position_state import PositionState


class FakeClient:
    def __init__(self):
        self.created = []
        self.cancelled = []
        self._n = 0

    def create_order(self, token_id, side, price, size, neg_risk=False):
        self._n += 1
        oid = f"oid{self._n}"
        self.created.append({"token_id": token_id, "side": side, "price": price, "size": size, "oid": oid})
        return {"orderID": oid}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True


def make_state(total=1000.0, cap=0.65):
    return PositionState("tok", total, cap, tick=0.01, neg_risk=False, label="M")


def book_with(bids):
    bs = BookState()
    bs.apply_snapshot(bids=[{"price": str(p), "size": str(s)} for p, s in bids], asks=[])
    return bs


def live_settings():
    return Settings(requote_min_interval_ms=0, dry_run=False)


def run(coro):
    return asyncio.run(coro)


class TestPlace:
    def test_places_penny_up_over_competitor(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))
        assert len(c.created) == 1
        assert c.created[0]["price"] == 0.61
        assert c.created[0]["size"] == 1000.0
        assert state.order_id == "oid1"

    def test_noop_when_already_correct(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        b = book_with([(0.60, 100)])
        run(ex.reconcile_token(state, b))           # 挂 0.61
        run(ex.reconcile_token(state, b))           # 同盘口再来一次
        assert len(c.created) == 1                  # 不重复挂
        assert len(c.cancelled) == 0


class TestRepeg:
    def test_repeg_up_when_outbid(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))      # 0.61
        run(ex.reconcile_token(state, book_with([(0.62, 50), (0.60, 100)])))  # 对手 0.62
        assert c.cancelled == ["oid1"]
        assert c.created[-1]["price"] == 0.63

    def test_repeg_down_when_competitor_drops(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))      # 0.61
        run(ex.reconcile_token(state, book_with([(0.50, 100)])))      # 对手跌到 0.50
        assert c.cancelled == ["oid1"]
        assert c.created[-1]["price"] == 0.51


class TestWithdraw:
    def test_competitor_at_cap_no_order(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.65, 100)])))      # 顶到上限
        assert len(c.created) == 0

    def test_cancels_existing_when_market_tops_out(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))      # 挂 0.61
        run(ex.reconcile_token(state, book_with([(0.65, 100)])))      # 对手顶到 0.65 → 撤
        assert c.cancelled == ["oid1"]
        assert state.order_id is None

    def test_no_competitor_no_order(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([])))                 # 买盘空
        assert len(c.created) == 0


class TestDone:
    def test_done_state_cancels_and_skips(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))      # 挂 0.61
        state.done = True
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))
        assert c.cancelled == ["oid1"]
        assert state.order_id is None

    def test_target_reached_marks_done(self):
        c = FakeClient()
        ex = Executor(c, live_settings())
        state = make_state(total=100.0)
        state.record_order_match("x", 100.0)        # 已满
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))
        assert state.done
        assert len(c.created) == 0


class TestDryRun:
    def test_dry_run_places_synthetic_no_network(self):
        c = FakeClient()
        ex = Executor(c, Settings(requote_min_interval_ms=0, dry_run=True))
        state = make_state()
        run(ex.reconcile_token(state, book_with([(0.60, 100)])))
        assert len(c.created) == 0                  # 不碰网络
        assert state.order_id == DRY_ORDER_ID
        assert state.order_price == 0.61
