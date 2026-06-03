"""SP5: 监控防御 —— churn 双闸 + 稳挂少动决策 + 编排（受 churn 约束执行）。"""
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
    assert g.allow("c", now=3601, count_as_cancel=True) is True  # 滚动窗口外，预算恢复


def test_churn_refill_not_counted_in_budget():
    g = ChurnGuard(token_cooldown_sec=0, max_cancels_per_hour=1)
    g.record("a", now=0, count_as_cancel=False)   # 补单不计预算
    g.record("b", now=1, count_as_cancel=False)
    assert g.remaining_budget(now=2) == 1          # 预算未被补单消耗


# ---------- decide_action ----------
def test_decide_none_when_in_band_two_sided():
    d = decide_action(0.49, 0.51, mid=0.50, max_spread=0.02)
    assert d.action == NONE


def test_decide_refill_when_side_missing():
    d = decide_action(None, 0.51, mid=0.50, max_spread=0.02)
    assert d.action == REFILL
    assert d.place_buy is True and d.place_sell is False
    assert d.cancel_first is False          # 不撤仍在的卖单


def test_decide_recenter_when_out_of_band():
    # mid 0.50, band ±0.02(+1tick 滞回) → [0.47,0.53]；my_bid 0.40 出带
    d = decide_action(0.40, 0.52, mid=0.50, max_spread=0.02, deadband_ticks=1, tick_size=0.01)
    assert d.action == RECENTER
    assert d.cancel_first is True and d.place_buy and d.place_sell


def test_decide_deadband_suppresses_tiny_drift():
    # my_bid 0.475 在 [0.47,0.53] 内（带滞回）→ 不动
    d = decide_action(0.475, 0.515, mid=0.50, max_spread=0.02, deadband_ticks=1, tick_size=0.01)
    assert d.action == NONE


def test_decide_none_when_no_mid_or_band():
    assert decide_action(0.49, 0.51, mid=None, max_spread=0.02).action == NONE
    assert decide_action(0.49, 0.51, mid=0.50, max_spread=0).action == NONE


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
        self.created.append({"token_id": token_id, "side": side}); return {"status": "live"}


TOKEN = {"token_id": "T", "max_spread": 0.02, "min_size": 100, "neg_risk": False, "question": "q"}


def test_evaluate_none_does_nothing():
    be = FakeBackend(); g = ChurnGuard(0, 999)
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.49, my_ask=0.51, churn=g, now=1.0)
    assert res["action"] == NONE
    assert be.cancelled == [] and be.created == []


def test_evaluate_recenter_cancels_and_places_when_allowed(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=999)
    # 隔离定价：桩掉 place_for_token，只验证编排（撤+双边补 + churn 记账）
    calls = {}
    monkeypatch.setattr(mloop, "place_for_token",
                        lambda backend, ti, bb, ba, sides, tick_size=None: calls.update(sides=set(sides)) or [{"status": "placed"}])
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.40, my_ask=0.52, churn=g, now=1.0)
    assert res["action"] == RECENTER
    assert be.cancelled == ["T"]            # 先撤
    assert calls["sides"] == {"BUY", "SELL"}  # 再双边重挂
    assert g.allow("T", now=2.0) is False   # 已记冷却（300s 内不可再动）
    assert g.remaining_budget(now=2.0) == 998  # RECENTER 计入撤单预算


def test_evaluate_recenter_skipped_when_cooldown(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=999)
    monkeypatch.setattr(mloop, "place_for_token", lambda *a, **k: [{"status": "placed"}])
    g.record("T", now=1.0)                  # 刚动过
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.40, my_ask=0.52, churn=g, now=100.0)
    assert res["action"] == "SKIPPED"
    assert be.cancelled == []               # 冷却中不撤


def test_recenter_records_cancel_even_when_place_raises(monkeypatch):
    # 关键安全回归：撤单成功后补单抛错，churn 仍须记账(冷却武装/预算消耗)，
    # 否则下轮可能再撤→撤单循环→积分清零。
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=5)
    def boom(*a, **k):
        raise RuntimeError("create_order down")
    monkeypatch.setattr(mloop, "place_for_token", boom)
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.40, my_ask=0.52, churn=g, now=1.0)
    assert res["action"] == RECENTER
    assert be.cancelled == ["T"]                  # 撤单发生了
    assert g.allow("T", now=2.0) is False          # 冷却已武装(300s 内不再动)
    assert g.remaining_budget(now=2.0) == 4        # 撤单已计入预算(5-1)


def test_evaluate_refill_places_missing_side_without_cancel(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(0, max_cancels_per_hour=1)
    calls = {}
    monkeypatch.setattr(mloop, "place_for_token",
                        lambda backend, ti, bb, ba, sides, tick_size=None: calls.update(sides=set(sides)) or [{"status": "placed"}])
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=None, my_ask=0.51, churn=g, now=1.0)
    assert res["action"] == REFILL
    assert be.cancelled == []               # 不撤好单
    assert calls["sides"] == {"BUY"}        # 只补缺失的买侧
    assert g.remaining_budget(now=1.0) == 1 # 补单不耗撤单预算
