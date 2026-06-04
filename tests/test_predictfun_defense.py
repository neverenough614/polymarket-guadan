"""predict.fun 订单簿变化防御：分层深度、买侧威胁、偏斜、趋势、整合。"""
from collections import deque

from predictfun_data.defense import (
    DefenseState, layered_bid_depth, check_bid_threats, check_imbalance,
    check_trend, evaluate_defense,
)
from config.bot_config import PredictFunDefenseConfig

DC = PredictFunDefenseConfig()


# ---------- layered_bid_depth ----------
def test_layered_depth_front_and_same_excludes_self():
    # 我买 0.50（持 100 份）；0.52/0.51 是前墙，0.50 同档(含我)
    bids = [(0.52, 1000), (0.51, 500), (0.50, 300), (0.48, 1000)]
    front, same = layered_bid_depth(bids, my_bid=0.50, my_size=100)
    assert abs(front - (0.52 * 1000 + 0.51 * 500)) < 1e-6
    # same = 0.50×300 − 自己 0.50×100 = 150 − 50 = 100
    assert abs(same - (0.50 * 300 - 0.50 * 100)) < 1e-6


# ---------- check_trend ----------
def test_trend_fires_on_consecutive_drop():
    hist = deque([1000, 900, 700, 500], maxlen=5)   # 连降3轮，累计 -50%
    fired, _ = check_trend(hist, "x", cum_drop_threshold=0.30, min_consecutive=3)
    assert fired


def test_trend_quiet_when_not_enough_drops():
    hist = deque([1000, 900, 1000, 950], maxlen=5)
    fired, _ = check_trend(hist, "x", cum_drop_threshold=0.30, min_consecutive=3)
    assert not fired


# ---------- check_bid_threats ----------
def test_front_wall_vanished_triggers():
    st = DefenseState()
    st.first_run = False
    st.last_front = 500.0            # 上轮有厚前墙
    fired, reasons = check_bid_threats(st, my_bid=0.50, front=10.0, same=300.0, dc=DC)
    assert fired and any("前墙消失" in r for r in reasons)


def test_same_tier_eaten_triggers_when_no_front():
    st = DefenseState()
    st.first_run = False
    st.last_front = 0.0
    st.last_same = 500.0             # 上轮同档厚
    # 无前墙(front<present)，同档从 500 暴跌到 100 (-80% > same_drop 0.30)
    fired, reasons = check_bid_threats(st, my_bid=0.50, front=0.0, same=100.0, dc=DC)
    assert fired and any("同档" in r for r in reasons)


def test_no_trigger_when_book_stable():
    st = DefenseState()
    st.first_run = False
    st.last_front = 500.0
    st.front_hw = 500.0
    fired, _ = check_bid_threats(st, my_bid=0.50, front=480.0, same=300.0, dc=DC)
    assert not fired           # 前墙仅微降，安全


def test_no_trigger_when_my_bid_none():
    st = DefenseState(); st.first_run = False
    fired, _ = check_bid_threats(st, my_bid=None, front=0, same=0, dc=DC)
    assert not fired


# ---------- check_imbalance ----------
def test_imbalance_fires_when_bid_side_thin():
    bids = [(0.49, 100)]                       # 买侧薄 ~49
    asks = [(0.51, 2000), (0.52, 2000)]        # 卖侧厚 ~2060
    fired, reason = check_imbalance(bids, asks, dc=DC)
    assert fired and "偏斜" in reason


def test_imbalance_quiet_when_balanced():
    bids = [(0.49, 2000)]
    asks = [(0.51, 2000)]
    fired, _ = check_imbalance(bids, asks, dc=DC)
    assert not fired


def test_imbalance_quiet_when_total_too_small():
    bids = [(0.49, 10)]
    asks = [(0.51, 100)]
    fired, _ = check_imbalance(bids, asks, dc=DC)   # 总深度 < imbalance_min_total
    assert not fired


# ---------- evaluate_defense 整合 ----------
def test_evaluate_first_run_sets_baseline_no_trigger():
    st = DefenseState()
    bids = [(0.52, 1000), (0.50, 300)]
    asks = [(0.53, 1000)]
    fired, _ = evaluate_defense(st, bids, asks, my_bid=0.50, my_size=100, best_bid=0.52)
    assert not fired and st.first_run is False     # 首轮只建基线


def test_evaluate_triggers_on_front_wall_collapse_second_round():
    st = DefenseState()
    # 第1轮：厚前墙建基线
    evaluate_defense(st, [(0.52, 1000), (0.50, 300)], [(0.53, 1000)],
                     my_bid=0.50, my_size=100, best_bid=0.52)
    # 第2轮：前墙塌光
    fired, reasons = evaluate_defense(st, [(0.50, 300)], [(0.53, 1000)],
                                      my_bid=0.50, my_size=100, best_bid=0.50)
    assert fired and reasons


def test_stable_healthy_book_no_trigger():
    # 健康非极端簿：前墙厚、同档够、买卖均衡，两轮不变 → 静态门槛+变化检测都不该报
    st = DefenseState()
    bids = [(0.52, 1000), (0.51, 1000), (0.50, 1000), (0.48, 1000)]
    asks = [(0.53, 1000), (0.54, 1000)]
    evaluate_defense(st, bids, asks, my_bid=0.50, my_size=100, best_bid=0.52)
    fired, reasons = evaluate_defense(st, bids, asks, my_bid=0.50, my_size=100, best_bid=0.52)
    assert not fired, f"健康稳定簿不该触发：{reasons}"


def test_absolute_floors_toggle():
    # 静态绝对兜底：默认开（用户要"防一单被吃"）；显式关则静态薄不报（仍可走变化检测）
    from config.bot_config import PredictFunDefenseConfig
    st = DefenseState(); st.first_run = False
    on = PredictFunDefenseConfig()                       # 默认已开 use_absolute_floors
    fired_on, _ = check_bid_threats(st, my_bid=0.10, front=0.0, same=20.0, dc=on)
    assert fired_on               # 同档 $20 < same_safe 80 → 静态兜底报
    off = PredictFunDefenseConfig(); off.use_absolute_floors = False
    st2 = DefenseState(); st2.first_run = False
    fired_off, _ = check_bid_threats(st2, my_bid=0.10, front=0.0, same=20.0, dc=off)
    assert not fired_off          # 关掉 → 静态薄不报（簿无变化）


def test_evaluate_skips_when_no_my_bid():
    st = DefenseState()
    fired, _ = evaluate_defense(st, [(0.5, 100)], [(0.51, 100)],
                                my_bid=None, my_size=100, best_bid=0.5)
    assert not fired
