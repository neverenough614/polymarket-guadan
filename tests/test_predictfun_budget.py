"""predict.fun 资金守门 + 市场配对（both-or-skip / 两腿同量 / 预算贪心）。

实测前提：predict.fun 挂买单按 price×size 预扣 available balance，未成交买单 notional 之和
不得超过余额（与 Polymarket 相反）。故下单前必须预算守门。
"""
from predictfun_data.budget import (
    pair_market_legs, market_cost, select_within_budget, build_plan,
    available_after_open_buys,
)


def _tok(tt):
    return {"token_id": tt, "token_type": tt, "question": "q"}


# ---------- pair_market_legs：both-or-skip + 两腿同量 ----------
def test_pair_both_legs_quotable_uses_min_size():
    quotes = [(_tok("YES"), 0.10, 500.0, "ok"), (_tok("NO"), 0.89, 300.0, "ok")]
    legs, reason = pair_market_legs(quotes, min_size=100.0)
    assert reason == "ok" and len(legs) == 2
    assert all(s == 100.0 for _t, _p, s in legs)        # 两腿同量=min_size，非动态量
    assert [p for _t, p, _s in legs] == [0.10, 0.89]


def test_pair_single_sided_when_allowed():
    quotes = [(_tok("YES"), 0.40, 100.0, "ok"), (_tok("NO"), None, None, "thin_top5_50")]
    legs, reason = pair_market_legs(quotes, min_size=100.0, allow_single_sided=True)
    assert len(legs) == 1 and legs[0][0]["token_type"] == "YES" and reason == "single_sided"


def test_pair_skips_single_sided_when_disallowed():
    quotes = [(_tok("YES"), 0.40, 100.0, "ok"), (_tok("NO"), None, None, "thin_top5_50")]
    legs, reason = pair_market_legs(quotes, min_size=100.0, allow_single_sided=False)
    assert legs == [] and reason.startswith("one_sided:")


def test_pair_skips_when_not_binary():
    legs, reason = pair_market_legs([(_tok("YES"), 0.1, 100.0, "ok")], min_size=100.0)
    assert legs == [] and reason.startswith("not_binary")


def test_pair_rejects_self_cross_when_price_sum_ge_one():
    # 买 YES 0.55 + 买 NO 0.50 = 1.05 ≥ 1 → 一套成本 ≥$1 必亏 → 拒绝
    quotes = [(_tok("YES"), 0.55, 100.0, "ok"), (_tok("NO"), 0.50, 100.0, "ok")]
    legs, reason = pair_market_legs(quotes, min_size=100.0)
    assert legs == [] and reason.startswith("crossed_pair")


def test_pair_allows_when_price_sum_below_one():
    quotes = [(_tok("YES"), 0.55, 100.0, "ok"), (_tok("NO"), 0.44, 100.0, "ok")]   # 0.99<1
    legs, reason = pair_market_legs(quotes, min_size=100.0)
    assert reason == "ok" and len(legs) == 2


# ---------- available_after_open_buys：挂买单预扣抵押 ----------
def test_available_subtracts_open_buy_notional():
    orders = [
        {"side": "BUY", "price": 0.10, "size": 100},   # 锁 10
        {"side": "BUY", "price": 0.89, "size": 100},   # 锁 89
        {"side": "SELL", "price": 0.95, "size": 100},  # 卖单不锁买方抵押
    ]
    assert abs(available_after_open_buys(362.5, orders) - (362.5 - 99.0)) < 1e-9


def test_available_never_negative():
    orders = [{"side": "BUY", "price": 0.9, "size": 1000}]   # 锁 900 > 余额
    assert available_after_open_buys(362.5, orders) == 0.0


def test_available_no_orders_returns_total():
    assert available_after_open_buys(362.5, []) == 362.5


# ---------- market_cost ----------
def test_market_cost_is_sum_of_leg_notional():
    legs = [(_tok("YES"), 0.10, 100.0), (_tok("NO"), 0.89, 100.0)]
    assert abs(market_cost(legs) - 99.0) < 1e-9          # ≈ size×(p+q)，p+q≲1


# ---------- select_within_budget：贪心累计 ≤ 余额×safety ----------
def _legs(p_yes, p_no, size):
    return [(_tok("YES"), p_yes, size), (_tok("NO"), p_no, size)]


def test_budget_stops_when_exhausted():
    # 每市场≈100 USDT，余额 362×0.95≈344 → 容纳 3 个
    markets = [("m1", _legs(0.10, 0.89, 100)), ("m2", _legs(0.20, 0.79, 100)),
               ("m3", _legs(0.30, 0.69, 100)), ("m4", _legs(0.40, 0.59, 100))]
    selected, total, dropped = select_within_budget(markets, available=362.0, safety=0.95)
    assert len(selected) == 3 and dropped == 1
    assert total <= 362.0 * 0.95 + 1e-9


def test_budget_caps_at_max_markets():
    # 预算够 4 个，但 max_markets=2 → 只选 2（按降序前 2）
    markets = [("m1", _legs(0.10, 0.89, 100)), ("m2", _legs(0.20, 0.79, 100)),
               ("m3", _legs(0.30, 0.69, 100)), ("m4", _legs(0.40, 0.59, 100))]
    selected, total, _ = select_within_budget(markets, available=1000.0, safety=0.95, max_markets=2)
    assert len(selected) == 2 and [k for k, _ in selected] == ["m1", "m2"]


def test_budget_skips_empty_legs():
    markets = [("m1", []), ("m2", _legs(0.10, 0.89, 100))]
    selected, total, dropped = select_within_budget(markets, available=362.0)
    assert len(selected) == 1 and dropped == 0


def test_budget_zero_balance_selects_nothing():
    selected, total, dropped = select_within_budget([("m1", _legs(0.1, 0.9, 100))], available=0.0)
    assert selected == [] and total == 0.0


# ---------- build_plan：端到端 ----------
def test_build_plan_pairs_then_budgets():
    markets = [
        ("m1", [_tok("YES"), _tok("NO")]),   # 双腿 ok
        ("m2", [_tok("YES"), _tok("NO")]),   # 一腿不可报价 → 跳
        ("m3", [_tok("YES"), _tok("NO")]),   # 双腿 ok 但预算不够
    ]
    quotes = {
        id(markets[0][1][0]): (markets[0][1][0], 0.10, 100.0, "ok"),
        id(markets[0][1][1]): (markets[0][1][1], 0.89, 100.0, "ok"),
        id(markets[1][1][0]): (markets[1][1][0], 0.10, 100.0, "ok"),
        id(markets[1][1][1]): (markets[1][1][1], None, None, "thin_top5_9"),
        id(markets[2][1][0]): (markets[2][1][0], 0.10, 100.0, "ok"),
        id(markets[2][1][1]): (markets[2][1][1], 0.89, 100.0, "ok"),
    }
    quote_fn = lambda t: quotes[id(t)]
    # 单向默认开：m1 双腿(99)+m2 单向(10)=109≤120×0.95=114 → 都入选；m3(99) 预算不够
    selected, skip_reasons, total, dropped = build_plan(
        markets, quote_fn, lambda toks: 100.0, available=120.0, safety=0.95,
    )
    assert len(selected) == 2                       # m1 完整套 + m2 单向
    assert len(selected[0][1]) == 2 and len(selected[1][1]) == 1
    assert skip_reasons.get("budget_exhausted") == 1   # m3
