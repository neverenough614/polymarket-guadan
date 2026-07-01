"""SP5: 监控防御 —— churn 双闸 + 稳挂少动决策(每 token 一张买单) + 编排。"""
import asyncio
from datetime import datetime, timezone

import execution.predictfun_monitor_loop as mloop
from predictfun_data.churn_guard import ChurnGuard
from predictfun_data.monitor import decide_action, reward_active, backfill_need, NONE, REFILL, RECENTER
from predictfun_data.normalize import NormalizedBook, BookLevel
from predictfun_data.defense import DefenseState


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


def test_churn_budget_ok_ignores_token_cooldown():
    # 保命撤单判定：只看小时预算，不看 token 冷却
    g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=2)
    g.record("t", now=1000)                 # t 刚动过（冷却中）
    assert g.allow("t", now=1100) is False  # 重挂会被冷却挡
    assert g.budget_ok(now=1100) is True    # 但撤单只看预算 → 放行
    g.record("x", now=1100)                 # 预算用掉第 2 个
    assert g.budget_ok(now=1101) is False   # 预算耗尽 → 安全网生效


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


def test_defense_cancels_and_records_when_front_wall_vanishes():
    # 防御：上轮厚前墙(last_front 高)，本轮 book 前墙塌光 → 撤单 + 计 churn 预算
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=180, max_cancels_per_hour=60)
    st = DefenseState(); st.first_run = False; st.last_front = 500.0; st.front_hw = 500.0
    # book: 我买 0.49，前墙(>0.49)为 0 → 前墙消失
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.49, churn=g, now=1.0, defense_state=st)
    assert res["action"] == "DEFEND" and res["defended"] is True
    assert be.cancelled == ["T"]                    # 防御撤单
    assert g.remaining_budget(now=2.0) == 59         # 计入撤单预算
    assert st.first_run is True                      # 撤后重置基线


def test_defense_alerts_only_when_budget_exhausted():
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=180, max_cancels_per_hour=0)  # 预算0
    st = DefenseState(); st.first_run = False; st.last_front = 500.0; st.front_hw = 500.0
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.49, churn=g, now=1.0, defense_state=st)
    assert res["action"] == "DEFENSE_ALERT" and res["defended"] is False
    assert be.cancelled == []                        # 预算用尽→仅告警不撤（防反作弊）


def test_defense_cancels_despite_token_cooldown():
    # 关键修复：token 刚重挂过(冷却中)，防御仍立即撤单(只受预算)——不再拿着危险单干等冷却
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=300, max_cancels_per_hour=60)
    g.record("T", now=1.0, count_as_cancel=False)    # 模拟刚 REFILL 重挂→token 冷却已武装
    st = DefenseState(); st.first_run = False; st.last_front = 500.0; st.front_hw = 500.0
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.49, churn=g, now=50.0, defense_state=st)
    assert res["action"] == "DEFEND"                 # 冷却中(50<1+300)仍立即撤
    assert be.cancelled == ["T"]


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


# ---------- 热度接入 ----------
class FakeHeat:
    def __init__(self, frozen=False):
        self.frozen = frozen
        self.recorded = []
    def record_defense_trigger(self, tid, question=""):
        self.recorded.append((tid, question))
    def is_frozen(self, tid):
        return self.frozen


def test_defense_trigger_records_heat():
    # 防御触发时给热度记账（升温）
    be = FakeBackend(); g = ChurnGuard(token_cooldown_sec=180, max_cancels_per_hour=60)
    st = DefenseState(); st.first_run = False; st.last_front = 500.0; st.front_hw = 500.0
    heat = FakeHeat()
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=0.49, churn=g, now=1.0,
                                     defense_state=st, heat=heat)
    assert res["action"] == "DEFEND"
    assert heat.recorded == [("T", "q")]            # 记了一次防御触发


def test_frozen_market_skips_refill(monkeypatch):
    # 冻结市场：即使缺单也不重挂（停止参与，等冷却）
    be = FakeBackend(); g = ChurnGuard(0, max_cancels_per_hour=5)
    placed = {}
    monkeypatch.setattr(mloop, "place_bid", lambda *a, **k: placed.update(p=True) or {"status": "placed"})
    res = mloop.evaluate_and_execute(be, TOKEN, my_bid=None, churn=g, now=1.0, heat=FakeHeat(frozen=True))
    assert res["action"] == "SKIPPED" and "frozen" in res["reason"]
    assert be.created == [] and placed == {}          # 不重挂


# ---------- reward_active 纯函数 ----------
def test_reward_active_rules():
    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    assert reward_active(None, now) is True            # 无 endsAt → 视为有效
    assert reward_active("garbage", now) is True        # 解析失败 → 视为有效（不误撤）
    assert reward_active("2099-01-01T00:00:00Z", now) is True   # 未到期
    assert reward_active("2020-01-01T00:00:00Z", now) is False  # 已过期


# ---------- 奖励失效撤单/停挂 ----------
EXPIRED = {**TOKEN, "reward_ends_at": "2020-01-01T00:00:00Z"}


def test_reward_inactive_cancels_when_has_order():
    be = FakeBackend(); g = ChurnGuard(0, max_cancels_per_hour=60)
    res = mloop.evaluate_and_execute(be, EXPIRED, my_bid=0.49, churn=g, now=1.0)
    assert res["action"] == "REWARD_INACTIVE" and res["cancelled"] is True
    assert be.cancelled == ["T"] and be.created == []   # 撤单、不重挂


def test_reward_inactive_skips_refill_when_no_order(monkeypatch):
    be = FakeBackend(); g = ChurnGuard(0, max_cancels_per_hour=60)
    monkeypatch.setattr(mloop, "place_bid", lambda *a, **k: {"status": "placed"})
    res = mloop.evaluate_and_execute(be, EXPIRED, my_bid=None, churn=g, now=1.0)
    assert res["action"] == "REWARD_INACTIVE" and res["cancelled"] is False
    assert be.cancelled == [] and be.created == []      # 无奖励市场不补挂


def test_reward_active_future_runs_normally():
    be = FakeBackend(); g = ChurnGuard(0, max_cancels_per_hour=60)
    fut = {**TOKEN, "reward_ends_at": "2099-01-01T00:00:00Z"}
    res = mloop.evaluate_and_execute(be, fut, my_bid=0.49, churn=g, now=1.0)
    assert res["action"] == NONE                        # 奖励有效 + 在带内 → 正常不动


# ---------- backfill_need 自动轮换补位判定 ----------
def test_backfill_all_alive_needs_none():
    dead, n = backfill_need({"A": False, "B": False, "C": False}, target=3)
    assert dead == [] and n == 0


def test_backfill_one_dead_needs_one():
    dead, n = backfill_need({"A": False, "B": True, "C": False}, target=3)
    assert dead == ["B"] and n == 1                      # B 死(冻结/失效)→腾位→补 1


def test_backfill_counts_only_alive_slots():
    dead, n = backfill_need({"A": True, "B": True}, target=3)
    assert set(dead) == {"A", "B"} and n == 3            # 全死→占用0→补满3


def test_backfill_disabled_when_target_zero():
    dead, n = backfill_need({"A": False}, target=0)
    assert n == 0                                        # target=0(不限)→不补


# ---------- apply_reload（运行内重载：刷新现有 + 撤掉下架）----------
def test_apply_reload_refreshes_existing_token_fields():
    be = FakeBackend()
    old = {"token_id": "T", "max_spread": 0.02, "rewards_daily_rate": 100, "reward_ends_at": "old"}
    by_id = {"T": old}
    states = {"T": DefenseState()}
    fresh = [{"token_id": "T", "max_spread": 0.03, "rewards_daily_rate": 500, "reward_ends_at": "new"}]
    rr = mloop.apply_reload(fresh, by_id, states, be)
    assert rr["refreshed"] == 1 and rr["removed"] == 0
    assert by_id["T"]["rewards_daily_rate"] == 500       # 奖励字段被刷新
    assert by_id["T"]["reward_ends_at"] == "new"
    assert be.cancelled == []                            # 未下架 → 不撤单


def test_apply_reload_cancels_and_drops_delisted():
    be = FakeBackend()
    by_id = {"T": {"token_id": "T"}, "GONE": {"token_id": "GONE"}}
    states = {"T": DefenseState(), "GONE": DefenseState()}
    fresh = [{"token_id": "T", "rewards_daily_rate": 50}]   # GONE 不在新表
    rr = mloop.apply_reload(fresh, by_id, states, be)
    assert rr["removed"] == 1 and rr["removed_keys"] == ["GONE"]
    assert "GONE" not in by_id and "GONE" not in states    # 移出监控
    assert be.cancelled == ["GONE"]                          # 下架市场被撤单
    assert "T" in by_id                                      # 存活的保留


def test_apply_reload_empty_fresh_drops_all():
    be = FakeBackend()
    by_id = {"A": {"token_id": "A"}, "B": {"token_id": "B"}}
    states = {"A": DefenseState(), "B": DefenseState()}
    rr = mloop.apply_reload([], by_id, states, be)
    assert rr["removed"] == 2 and by_id == {}
    assert set(be.cancelled) == {"A", "B"}


# ---------- dedup_orders（去重保险：同 token 多余买单撤掉只留一张）----------
def test_dedup_orders_cancels_extras_keeps_first():
    class _BE:
        def __init__(self):
            self.removed = []
        def _remove_ids(self, ids):
            self.removed.append(list(ids))
    be = _BE()
    grouped = {
        "A": {"bid_ids": ["a1", "a2", "a3"]},   # 3 张 → 撤 2（留 a1）
        "B": {"bid_ids": ["b1"]},               # 1 张 → 不动
        "C": {"bid_ids": []},                   # 0 张 → 不动
    }
    n = mloop.dedup_orders(be, grouped)
    assert n == 2
    assert be.removed == [["a2", "a3"]]         # 保留第一张，撤其余


def test_dedup_orders_noop_without_remover():
    class _BE:                                   # 无 _remove_ids（如 Polymarket backend/测试桩）
        pass
    assert mloop.dedup_orders(_BE(), {"A": {"bid_ids": ["x", "y"]}}) == 0


def test_dedup_orders_empty_grouped():
    class _BE:
        def _remove_ids(self, ids):
            raise AssertionError("空 grouped 不该撤单")
    assert mloop.dedup_orders(_BE(), {}) == 0


# ---------- monitor_loop 全循环集成：reload 分支真的接通 ----------
class ReloadFakeBackend:
    """够 monitor_loop 跑一轮的最小 backend：无我方挂单、撤单可记账。"""
    def __init__(self):
        self.cancelled = []
    def get_all_my_orders_grouped(self):
        return {}                                   # 无挂单 → 维护循环走 REFILL（已被 monkeypatch 掉）
    def cancel_all_asset(self, tid):
        self.cancelled.append(tid)
    def meta_for(self, tid):
        return None


def test_monitor_loop_seeds_churn_no_duplicate_first_pass(monkeypatch):
    """启动竞态防护：初挂单还没在挂单列表可见(grouped={})时，首轮不得重复挂单。

    监控启动会给初始 token 预置 churn 冷却 → 首轮 my_bid=None 的 REFILL 被冷却挡住，
    待订单可见再正常维护。否则每个 token 启动就被重挂一张（用户实测的重复挂单）。
    """
    mc = mloop.cfg.predictfun_monitor
    monkeypatch.setattr(mc, "poll_interval_sec", 0)
    monkeypatch.setattr(mloop, "run_auto_close",
                        lambda *a, **k: {"merged": 0, "sold": 0, "actions": [], "closed_tokens": []})
    stop = asyncio.Event()

    class _BE:
        def __init__(self):
            self.created = []
        def get_all_my_orders_grouped(self):
            stop.set()                          # 本轮跑完即停
            return {}                           # 刚挂的单还没可见（最终一致性）
        def get_order_book(self, tid):
            return NormalizedBook(1, [BookLevel(0.49, 500)], [BookLevel(0.51, 500)])  # mid 0.5
        def meta_for(self, tid):
            return None
        def create_order(self, *a, **k):
            self.created.append(a); return {"status": "live"}
        def cancel_all_asset(self, tid):
            pass

    be = _BE()
    tokens = [{"token_id": "T", "max_spread": 0.02, "min_size": 100, "question": "q"}]
    asyncio.run(mloop.monitor_loop(
        be, tokens, churn=ChurnGuard(mc.token_cooldown_sec, 999),
        now_fn=lambda: 1000.0, stop_event=stop,
    ))
    assert be.created == []                      # 预置冷却生效 → 首轮不重复挂


def test_monitor_loop_reload_branch_end_to_end(monkeypatch):
    """跑通整圈：reload 计时到点 → 灌候选池 → 刷新现有 + 撤下架。证明 B 的接线真的通。"""
    mc = mloop.cfg.predictfun_monitor
    monkeypatch.setattr(mc, "sheet_reload_interval_sec", 0)   # 立即到点
    monkeypatch.setattr(mc, "poll_interval_sec", 0)           # 不空等
    # auto_close / 单 token 维护 monkeypatch 成 no-op，聚焦验 reload
    monkeypatch.setattr(mloop, "run_auto_close",
                        lambda *a, **k: {"merged": 0, "sold": 0, "actions": [], "closed_tokens": []})
    monkeypatch.setattr(mloop, "evaluate_and_execute", lambda *a, **k: {"action": NONE})

    be = ReloadFakeBackend()
    stop = asyncio.Event()
    pool_seen = {}
    initial = [{"token_id": "T", "rewards_daily_rate": 100, "reward_ends_at": None},
               {"token_id": "GONE", "rewards_daily_rate": 100, "reward_ends_at": None}]
    fresh = [{"token_id": "T", "rewards_daily_rate": 999, "reward_ends_at": "x"}]  # GONE 已下架

    def reload_fn():
        return fresh
    def on_pool_reload(f):
        pool_seen["pool"] = f
        stop.set()                                  # 重载一次后即停，跑完本圈退出

    asyncio.run(mloop.monitor_loop(
        be, initial, churn=ChurnGuard(0, 999), now_fn=lambda: 100.0, stop_event=stop,
        reload_fn=reload_fn, on_pool_reload=on_pool_reload,
    ))

    assert pool_seen["pool"] is fresh               # 候选池被灌入新表
    assert be.cancelled == ["GONE"]                 # 下架市场撤单
    # 注：monitor_loop 内部维护的是局部 by_id，此处通过撤单行为间接验证 GONE 被移除
