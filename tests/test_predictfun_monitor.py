"""SP5: 监控防御 —— churn 双闸 + 稳挂少动决策(每 token 一张买单) + 编排。"""
import execution.predictfun_monitor_loop as mloop
from predictfun_data.churn_guard import ChurnGuard
from predictfun_data.monitor import decide_action, NONE, REFILL, RECENTER
from predictfun_data.normalize import NormalizedBook, BookLevel


# ---------- ChurnGuard ----------
def test_churn_cooldown_blocks_then_allows():
    g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=999)
    assert g.allow("t", now=1000) is True
    g.record("t", now=1000)
    assert g.allow("t", now=1100) is False     # 仅过 100s < 300s
    assert g.allow("t", now=1301) is True       # 过 301s
    assert g.allow("other", now=1100) is True   # 不同 token 不受影响


def test_churn_global_cancel_budget():
    g = ChurnGuard(token_cooldown_sec=0, max_cancels_per_hour=2)
    g.record("a", now=0, count_as_cancel=True)
    g.record("b", now=1, count_as_cancel=True)
    assert g.allow("c", now=2, count_as_cancel=True) is False   # 预算用尽
    assert g.allow("c", now=2, count_as_cancel=False) is True   # 纯补单不受预算限
    assert g.allow("c", now=3601, count_as_cancel=True) is True  # 滚动窗口外,预算恢复


def test_churn_refill_not_counted_in_budget():
    g = ChurnGuard(token_cooldown_sec=0, max_cancels_per_hour=1)
    g.record("a", now=0, count_as_cancel=False)
    g.record("b", now=1, count_as_cancel=False)
    assert g.remaining_budget(now=2) == 1


# ---------- decide_action（每 token 一张买单）----------
def test_decide_none_when_bid_in_band():
    assert decide_action(0.49, mid=0.50, max_spread=0.02).action == NONE


def test_decide_refill_when_no_bid():
    d = decide_action(None, mid=0.50, max_spread=0.02)
    assert d.action == REFILL and d.cancel_first is False    # 补挂买单,不撤


def test_decide_recenter_when_bid_out_of_band():
    d = decide_action(0.40, mid=0.50, max_spread=0.02, deadband_ticks=1, tick_size=0.01)
    assert d.action == RECENTER and d.cancel_first is True


def test_decide_deadband_suppresses_tiny_drift():
    # 0.475 在 [0.47,0.53] 内(带滞回) → 不动
    assert decide_action(0.475, mid=0.50, max_spread=0.02, deadband_ticks=1, tick_size=0.01).action == NONE


def test_decide_none_when_no_mid_or_band():
    assert decide_action(0.49, mid=None, max_spread=0.02).action == NONE
    assert decide_action(0.49, mid=0.50, max_spread=0).action == NONE


# ---------- evaluate_and_execute 编排 ----------
class FakeBackend:
    def __init__(self):
        self._book = NormalizedBook(1, [BookLevel(0.49, 500)], [BookLevel(0.51, 500)])  # mid 0.5
        self.cancelled, self.created = [], []
    def get_order_book(self, tid):
        return self._book
    def cancel_all_asset(self, tid):
        self.cancelled.append(tid)
    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created.append({"side": side}); return {"status": "live"}


TOKEN = {"token_id": "T", "max_spread": 0.02, "min_size": 100, "neg_risk": False, "question": "q"}


def test_evaluate_none_does_nothing():
    be = FakeBackend(); g = ChurnGuard(0, 999)
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.49, churn=g, now=1.0)
    assert res["action"] == NONE
    assert be.cancelled == [] and be.created == []


def test_evaluate_recenter_cancels_and_places_when_allowed(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=999)
    calls = {}
    monkeypatch.setattr(mloop, "place_bid",
                        lambda backend, ti, book, tick_size=None: calls.update(placed=True) or {"status": "placed"})
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.40, churn=g, now=1.0)
    assert res["action"] == RECENTER
    assert be.cancelled == ["T"]                 # 先撤
    assert calls.get("placed") is True            # 再补挂买单
    assert g.allow("T", now=2.0) is False         # 已记冷却
    assert g.remaining_budget(now=2.0) == 998     # RECENTER 计入撤单预算


def test_recenter_records_cancel_even_when_place_raises(monkeypatch):
    # 关键安全回归：撤单成功后补单抛错,churn 仍须记账,否则撤单循环→积分清零。
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=5)
    def boom(*a, **k):
        raise RuntimeError("create_order down")
    monkeypatch.setattr(mloop, "place_bid", boom)
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.40, churn=g, now=1.0)
    assert res["action"] == RECENTER
    assert be.cancelled == ["T"]
    assert g.allow("T", now=2.0) is False
    assert g.remaining_budget(now=2.0) == 4


def test_evaluate_recenter_skipped_when_cooldown(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=999)
    monkeypatch.setattr(mloop, "place_bid", lambda *a, **k: {"status": "placed"})
    g.record("T", now=1.0)                  # 刚动过
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.40, churn=g, now=100.0)
    assert res["action"] == "SKIPPED"
    assert be.cancelled == []               # 冷却中不撤


def test_evaluate_refill_places_without_cancel(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(0, max_cancels_per_hour=1)
    calls = {}
    monkeypatch.setattr(mloop, "place_bid",
                        lambda backend, ti, book, tick_size=None: calls.update(placed=True) or {"status": "placed"})
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=None, churn=g, now=1.0)
    assert res["action"] == REFILL
    assert be.cancelled == []               # 不撤
    assert calls.get("placed") is True       # 补挂买单
    assert g.remaining_budget(now=1.0) == 1 # 补单不耗撤单预算
