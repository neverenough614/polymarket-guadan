"""SP3: 市场发现/选市 —— 只挑 active 奖励市场 + 行映射（依据主网实测奖励字段）。"""
from datetime import datetime, timezone

from predictfun_data.market_discovery import (
    parse_iso, is_reward_active, reward_hourly_rate, market_to_row, select_reward_markets,
    days_to_expiry,
)

NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)


def _market(reward_current, **over):
    m = {
        "id": 52261,
        "question": "Will Ethereum hit $1,000 or $3,000 first",
        "isNegRisk": True,
        "isYieldBearing": True,
        "feeRateBps": 200,
        "decimalPrecision": 2,
        "spreadThreshold": 0.06,
        "shareThreshold": 100,
        "rewards": {"current": reward_current, "schedule": []},
        "outcomes": [
            {"name": "Yes", "indexSet": 1, "onChainId": "YES"},
            {"name": "No", "indexSet": 2, "onChainId": "NO"},
        ],
    }
    m.update(over)
    return m


ACTIVE = {"hourlyRate": 60, "startsAt": "2026-05-14T15:00:00.000Z", "endsAt": "2027-01-01T05:00:00.000Z"}
EXPIRED = {"hourlyRate": 60, "startsAt": "2026-04-28T19:00:00.000Z", "endsAt": "2026-05-07T00:15:00.000Z"}
FUTURE = {"hourlyRate": 60, "startsAt": "2026-07-01T00:00:00.000Z", "endsAt": "2027-01-01T00:00:00.000Z"}
ZERO = {"hourlyRate": 0, "startsAt": "2026-05-14T15:00:00.000Z", "endsAt": "2027-01-01T05:00:00.000Z"}


def test_parse_iso_handles_trailing_z():
    dt = parse_iso("2027-01-01T05:00:00.000Z")
    assert dt.year == 2027 and dt.tzinfo is not None


def test_is_reward_active_true_within_window():
    assert is_reward_active(_market(ACTIVE), NOW) is True


def test_is_reward_active_false_when_expired():
    assert is_reward_active(_market(EXPIRED), NOW) is False


def test_is_reward_active_false_when_not_started():
    assert is_reward_active(_market(FUTURE), NOW) is False


def test_is_reward_active_false_when_zero_rate():
    assert is_reward_active(_market(ZERO), NOW) is False


def test_is_reward_active_false_when_no_reward():
    assert is_reward_active(_market(None), NOW) is False


def test_is_reward_active_respects_min_hourly_rate():
    assert is_reward_active(_market(ACTIVE), NOW, min_hourly_rate=100) is False  # 60 < 100


def test_is_reward_active_false_when_missing_start():
    no_start = {"hourlyRate": 60, "endsAt": "2027-01-01T05:00:00.000Z"}
    assert is_reward_active(_market(no_start), NOW) is False


def test_is_reward_active_true_when_open_ended_no_end():
    # 开放式持续奖励（有 start、无 end）应视为 active
    perpetual = {"hourlyRate": 60, "startsAt": "2026-05-14T15:00:00.000Z"}
    assert is_reward_active(_market(perpetual), NOW) is True


def test_market_to_row_none_when_missing_spread_threshold():
    m = _market(ACTIVE)
    m.pop("spreadThreshold")
    assert market_to_row(m) is None      # 缺奖励价带 → 跳过,不写裸挂单


def test_days_to_expiry_future_positive_unknown_zero():
    far = days_to_expiry("2026-07-03T13:00:00.000Z", NOW)   # 30 天后
    assert 29 < far < 31
    soon = days_to_expiry("2026-06-03T14:00:00.000Z", NOW)  # ~0.5h 后(NOW=06-03 00:00)
    assert 0 < soon < 1
    assert days_to_expiry(None, NOW) == 0.0                  # 未知 → 0


def test_market_to_row_carries_reward_ends_at():
    row = market_to_row(_market(ACTIVE))
    assert row["reward_ends_at"] == ACTIVE["endsAt"]


def test_market_to_row_maps_reward_fields():
    row = market_to_row(_market(ACTIVE))
    assert row["token1"] == "YES" and row["token2"] == "NO"
    assert row["min_size"] == 100.0            # shareThreshold
    assert row["max_spread"] == 0.06           # spreadThreshold（保持小数，不 /100）
    assert row["neg_risk"] is True
    assert row["hourly_rate"] == 60.0
    assert row["market_id"] == 52261
    assert row["fee_rate_bps"] == 200
    assert row["source"] == "High Reward"


def test_select_reward_markets_filters_and_sorts_desc():
    m_hi = _market(dict(ACTIVE, hourlyRate=120), id=1, question="hi")
    m_lo = _market(dict(ACTIVE, hourlyRate=30), id=2, question="lo")
    m_dead = _market(EXPIRED, id=3, question="dead")
    rows = select_reward_markets([m_lo, m_dead, m_hi], NOW)
    assert [r["market_id"] for r in rows] == [1, 2]   # 仅 active，按 hourly_rate 降序
    assert rows[0]["hourly_rate"] == 120.0


def test_select_reward_markets_max_markets_truncates():
    ms = [_market(dict(ACTIVE, hourlyRate=r), id=r) for r in (10, 20, 30)]
    rows = select_reward_markets(ms, NOW, max_markets=2)
    assert len(rows) == 2
    assert [r["hourly_rate"] for r in rows] == [30.0, 20.0]
