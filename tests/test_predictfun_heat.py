"""predict.fun 市场热度：防御触发升温、分级冻结冷却、2h 窗口衰减、持久化。"""
from predictfun_data.heat import (
    MarketHeatTracker, COOLDOWN_6H, COOLDOWN_24H, TRIGGER_WINDOW_SECS,
)


class Clock:
    def __init__(self, t=0.0):
        self.t = t
    def __call__(self):
        return self.t


def _tracker(tmp_path, clock):
    return MarketHeatTracker(state_file=str(tmp_path / "heat.json"), now_fn=clock)


# ---------- 升温 ----------
def test_escalates_warm_hot_frozen(tmp_path):
    h = _tracker(tmp_path, Clock(0.0))
    h.record_defense_trigger("T", "q")
    assert h.get_state("T")[0] == "WARM"
    h.record_defense_trigger("T")
    assert h.get_state("T")[0] == "HOT"
    h.record_defense_trigger("T")
    assert h.get_state("T")[0] == "FROZEN"
    assert h.is_frozen("T")


def test_unknown_token_not_frozen(tmp_path):
    h = _tracker(tmp_path, Clock(0.0))
    assert h.is_frozen("nope") is False
    assert h.get_state("nope") == ("SAFE", 0, "")


# ---------- 冷却 ----------
def test_frozen_holds_during_cooldown_then_thaws(tmp_path):
    clk = Clock(0.0)
    h = _tracker(tmp_path, clk)
    for _ in range(3):
        h.record_defense_trigger("T")        # 3 次 → FROZEN，冷却 6h
    assert h.is_frozen("T")
    clk.t = COOLDOWN_6H - 1                    # 冷却期内 → 仍冻结
    assert h.is_frozen("T")
    clk.t = COOLDOWN_6H + TRIGGER_WINDOW_SECS + 1   # 冷却到期且触发已出 2h 窗口
    assert h.is_frozen("T") is False           # 解冻回 SAFE
    assert h.get_state("T")[0] == "SAFE"


def test_cooldown_grades_by_trigger_count(tmp_path):
    clk = Clock(0.0)
    h = _tracker(tmp_path, clk)
    for _ in range(5):                          # 5 次 → 冷却 24h
        h.record_defense_trigger("T")
    clk.t = COOLDOWN_6H + 1                      # 已过 6h，但 5 次档应冷却 24h
    assert h.is_frozen("T")
    clk.t = COOLDOWN_24H - 1
    assert h.is_frozen("T")


# ---------- 2h 窗口衰减 ----------
def test_triggers_decay_out_of_window(tmp_path):
    clk = Clock(0.0)
    h = _tracker(tmp_path, clk)
    h.record_defense_trigger("T")               # WARM
    h.record_defense_trigger("T")               # HOT
    assert h.get_state("T")[0] == "HOT"
    clk.t = TRIGGER_WINDOW_SECS + 1             # 两次都滑出 2h 窗口
    assert h.get_state("T")[0] == "SAFE"        # 分数归零


# ---------- 持久化 ----------
def test_persistence_roundtrip(tmp_path):
    clk = Clock(0.0)
    path = str(tmp_path / "heat.json")
    h1 = MarketHeatTracker(state_file=path, now_fn=clk)
    for _ in range(3):
        h1.record_defense_trigger("T", "q")     # FROZEN 已落盘
    h2 = MarketHeatTracker(state_file=path, now_fn=clk)   # 新实例读盘
    assert h2.is_frozen("T")
