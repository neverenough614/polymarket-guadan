from predictfun_data.units import (
    price_to_tick, to_wei, price_per_share_wei, shares_to_wei, from_wei, WEI,
)


def test_wei_constant():
    assert WEI == 10 ** 18


def test_price_to_tick_rounds_to_nearest():
    assert price_to_tick(0.523, 0.01) == 0.52
    assert price_to_tick(0.525, 0.01) == 0.53
    assert price_to_tick(0.4999, 0.01) == 0.50


def test_price_to_tick_clamps_to_unit_interval():
    assert price_to_tick(1.5, 0.01) == 1.0
    assert price_to_tick(-0.2, 0.01) == 0.0


def test_to_wei_rounds_not_floors():
    assert to_wei(1.0) == 10 ** 18
    assert to_wei(0.5) == 5 * 10 ** 17
    # 浮点误差不应系统性少 1
    assert to_wei(0.1) == 10 ** 17


def test_price_per_share_wei_applies_tick():
    assert price_per_share_wei(0.523, 0.01) == 52 * 10 ** 16  # 0.52 * 1e18


def test_shares_to_wei():
    assert shares_to_wei(10) == 10 * 10 ** 18


def test_from_wei_accepts_int_and_str():
    assert from_wei(5 * 10 ** 17) == 0.5
    assert from_wei(str(10 ** 18)) == 1.0
