"""SP4: 奖励打分（复用 Polymarket 公式）+ 选市分流。"""
from predictfun_data.scoring import compute_reward_metrics
from predictfun_data.selection import is_normal, is_aggressive, split_strategies


def test_compute_metrics_empty_book_zero_spread_excluded_by_selection():
    # 空簿 → best_bid/ask=0、spread=0；由 spread 过滤（≥0.01/0.02）排除，
    # 与 Polymarket 一致（不靠 reward 归零，而靠 spread=0 不达标）。
    m = compute_reward_metrics([], [], 0.06, 1440.0)
    assert m["best_bid"] == 0.0 and m["best_ask"] == 0.0
    assert m["spread"] == 0.0
    assert is_normal({**m, "rewards_daily_rate": 99999}) is False
    assert is_aggressive({**m, "rewards_daily_rate": 99999}) is False


def test_compute_metrics_best_prices_and_spread():
    bids = [(0.48, 100), (0.47, 200)]
    asks = [(0.52, 100), (0.53, 150)]
    m = compute_reward_metrics(bids, asks, 0.06, 1440.0)
    assert m["best_bid"] == 0.48
    assert m["best_ask"] == 0.52
    assert round(m["spread"], 2) == 0.04
    assert round(m["midpoint"], 2) == 0.50


def test_compute_metrics_rewards_positive_and_gm_is_geomean():
    bids = [(0.49, 100)]
    asks = [(0.51, 100)]
    m = compute_reward_metrics(bids, asks, 0.06, 1440.0)
    assert m["bid_reward_per_100"] > 0
    assert m["ask_reward_per_100"] > 0
    # gm = sqrt(bid*ask)（容浮点）
    expect_gm = round((m["bid_reward_per_100"] * m["ask_reward_per_100"]) ** 0.5, 2)
    assert abs(m["gm_reward_per_100"] - expect_gm) < 0.02


def test_compute_metrics_higher_daily_reward_scales_up():
    bids = [(0.49, 100)]
    asks = [(0.51, 100)]
    low = compute_reward_metrics(bids, asks, 0.06, 240.0)
    high = compute_reward_metrics(bids, asks, 0.06, 2400.0)
    assert high["gm_reward_per_100"] > low["gm_reward_per_100"]


# ---- selection ----
def test_is_normal_spread_band_and_mid_reward():
    # days_to_expiry 缺省=0(未知→纳入)
    assert is_normal({"spread": 0.02, "mid_reward_per_100": 0.6}) is True
    assert is_normal({"spread": 0.05, "mid_reward_per_100": 5.0}) is False   # spread 超 0.04
    assert is_normal({"spread": 0.02, "mid_reward_per_100": 0.4}) is False   # mid_reward 不足


def test_expiry_guard_excludes_short_includes_long_and_unknown():
    base = {"spread": 0.02, "mid_reward_per_100": 5.0,
            "gm_reward_per_100": 5.0, "rewards_daily_rate": 9999}
    # 短期(0.5天 < 7/3)→ 两类都排除
    assert is_normal({**base, "days_to_expiry": 0.5}) is False
    assert is_aggressive({**base, "spread": 0.05, "days_to_expiry": 0.5}) is False
    # 长期(10天 > 7/3)→ 纳入
    assert is_normal({**base, "days_to_expiry": 10}) is True
    assert is_aggressive({**base, "spread": 0.05, "days_to_expiry": 10}) is True
    # 未知(0)→ 纳入(对齐 Polymarket)
    assert is_normal({**base, "days_to_expiry": 0}) is True
    # 介于 3 与 7 之间：Normal 排除(>7)、Aggressive 纳入(>3)
    assert is_normal({**base, "days_to_expiry": 5}) is False
    assert is_aggressive({**base, "spread": 0.05, "days_to_expiry": 5}) is True


def test_is_aggressive_requires_rate_gm_and_spread():
    ok = {"spread": 0.05, "gm_reward_per_100": 3.0, "rewards_daily_rate": 500}
    assert is_aggressive(ok) is True
    assert is_aggressive({**ok, "rewards_daily_rate": 50}) is False    # 日率不足
    assert is_aggressive({**ok, "gm_reward_per_100": 1.0}) is False    # gm 不足
    assert is_aggressive({**ok, "spread": 0.15}) is False              # 价差超 0.12


def test_split_strategies_sorts_and_allows_overlap():
    rows = [
        {"market_id": 1, "spread": 0.03, "mid_reward_per_100": 0.6, "gm_reward_per_100": 3.0, "rewards_daily_rate": 500},
        {"market_id": 2, "spread": 0.03, "mid_reward_per_100": 0.9, "gm_reward_per_100": 5.0, "rewards_daily_rate": 500},
        {"market_id": 3, "spread": 0.10, "mid_reward_per_100": 0.0, "gm_reward_per_100": 4.0, "rewards_daily_rate": 500},
    ]
    normal, agg = split_strategies(rows)
    # market 1,2 进 normal（spread 0.03 在带内，mid_reward≥0.5），按 mid_reward 降序
    assert [r["market_id"] for r in normal] == [2, 1]
    # 三个都进 aggressive（gm≥2、rate≥100、spread 在 [0.02,0.12]），按 gm 降序
    assert [r["market_id"] for r in agg] == [2, 3, 1]
    # 重叠允许：market 2 同时在两组
    assert 2 in [r["market_id"] for r in normal] and 2 in [r["market_id"] for r in agg]
