"""predict.fun 自动清仓安全网：完整套→merge、单边残余→卖出、持仓解析。"""
from predictfun_data.auto_close import (
    decide_close_actions, positions_to_map, CloseAction, MERGE, SELL,
)
from predictfun_data import units


class _Meta:
    def __init__(self, cond, comp, neg=False, yb=False):
        self.condition_id = cond
        self.complement_token_id = comp
        self.neg_risk = neg
        self.yield_bearing = yb


def _meta_of(table):
    return lambda tid: table.get(tid)


def _bid_of(table):
    return lambda tid: table.get(tid)


# ---------- decide_close_actions ----------
def test_complete_set_merges_min_amount():
    # YES 持 300、NO 持 200 → merge 200（取 min），各剩 100 残余
    metas = {"YES": _Meta("c1", "NO"), "NO": _Meta("c1", "YES")}
    actions = decide_close_actions(
        {"YES": 300, "NO": 200}, _meta_of(metas), _bid_of({"YES": 0.30, "NO": 0.69}),
        min_close=5, sell_offset=0.02, tick=0.01,
    )
    merges = [a for a in actions if a.kind == MERGE]
    assert len(merges) == 1 and merges[0].amount_shares == 200 and merges[0].condition_id == "c1"
    # 残余：YES 300-200=100 卖出；NO 200-200=0 不卖
    sells = [a for a in actions if a.kind == SELL]
    assert len(sells) == 1 and sells[0].token_id == "YES" and sells[0].size == 100


def test_merge_only_once_per_condition():
    metas = {"YES": _Meta("c1", "NO"), "NO": _Meta("c1", "YES")}
    actions = decide_close_actions(
        {"YES": 200, "NO": 200}, _meta_of(metas), _bid_of({"YES": 0.3, "NO": 0.7}),
        min_close=5, sell_offset=0.02,
    )
    assert len([a for a in actions if a.kind == MERGE]) == 1   # 不重复 merge 同一 condition
    assert [a for a in actions if a.kind == SELL] == []        # 等量→无残余


def test_single_sided_sells_at_bid_minus_offset():
    metas = {"YES": _Meta("c1", "NO")}     # 只持 YES，无 NO 持仓
    actions = decide_close_actions(
        {"YES": 150}, _meta_of(metas), _bid_of({"YES": 0.30}),
        min_close=5, sell_offset=0.02, tick=0.01,
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.kind == SELL and a.size == 150 and a.price == 0.28   # 0.30-0.02


def test_sell_price_floored_at_tick():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(
        {"YES": 100}, _meta_of(metas), _bid_of({"YES": 0.01}),
        min_close=5, sell_offset=0.02, tick=0.01,
    )
    assert actions[0].price == 0.01     # max(tick, 0.01-0.02)=tick


def test_below_min_close_ignored():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(
        {"YES": 3}, _meta_of(metas), _bid_of({"YES": 0.30}), min_close=5, sell_offset=0.02,
    )
    assert actions == []


def test_single_sided_no_bid_skips():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(
        {"YES": 150}, _meta_of(metas), _bid_of({}), min_close=5, sell_offset=0.02,
    )
    assert actions == []     # 无 best_bid 不冒进卖出


# ---------- positions_to_map ----------
def test_positions_parse_shares_and_wei():
    rows = [
        {"asset": "A", "size": "150"},                      # 份额
        {"tokenId": "B", "amount": str(200 * 10**18)},      # wei
        {"token_id": "", "size": "999"},                     # 空 id → 丢弃
    ]
    m = positions_to_map(rows, min_close=5)
    assert m["A"] == 150.0
    assert abs(m["B"] - 200.0) < 1e-9
    assert "" not in m
