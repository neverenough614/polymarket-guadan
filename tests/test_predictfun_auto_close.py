"""predict.fun 自动清仓安全网：完整套→merge、单边残余→走簿承接卖出、持仓解析。"""
from predictfun_data.auto_close import (
    decide_close_actions, positions_to_map, absorbing_sell_price,
    run_auto_close, CloseAction, MERGE, SELL,
)
from predictfun_data import units
from predictfun_data.normalize import NormalizedBook, BookLevel


class _Meta:
    def __init__(self, cond, comp, neg=False, yb=False):
        self.condition_id = cond
        self.complement_token_id = comp
        self.neg_risk = neg
        self.yield_bearing = yb


def _meta_of(table):
    return lambda tid: table.get(tid)


def _bids_of(table):
    """table: tid→[(price,size)] 降序买档列表。"""
    return lambda tid: table.get(tid, [])


# ---------- absorbing_sell_price：走簿求承接卖价 ----------
def test_absorbing_walks_book_until_size_absorbed():
    bids = [(0.50, 50), (0.48, 30), (0.45, 200)]   # 累计到 0.45 档 =280 ≥100
    price, ok = absorbing_sell_price(bids, size=100, base_offset=0.01, max_drop=0.10, tick=0.01)
    assert ok and abs(price - 0.44) < 1e-9         # 0.45 − base_offset 0.01

def test_absorbing_insufficient_within_max_drop():
    bids = [(0.50, 10), (0.49, 10)]                 # max_drop0.10→地板0.40，累计仅20<100
    price, ok = absorbing_sell_price(bids, size=100, base_offset=0.01, max_drop=0.10, tick=0.01)
    assert not ok and abs(price - 0.40) < 1e-9      # 尽力卖在地板价

def test_absorbing_empty_book():
    price, ok = absorbing_sell_price([], size=100, base_offset=0.01, max_drop=0.10, tick=0.01)
    assert price is None and not ok


# ---------- decide_close_actions ----------
def test_complete_set_merges_min_amount():
    # YES 持 300、NO 持 200 → merge 200（取 min），各剩 100 残余
    metas = {"YES": _Meta("c1", "NO"), "NO": _Meta("c1", "YES")}
    actions = decide_close_actions(
        {"YES": 300, "NO": 200}, _meta_of(metas),
        _bids_of({"YES": [(0.30, 500)], "NO": [(0.69, 500)]}),
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
        {"YES": 200, "NO": 200}, _meta_of(metas),
        _bids_of({"YES": [(0.3, 500)], "NO": [(0.7, 500)]}),
        min_close=5, sell_offset=0.02,
    )
    assert len([a for a in actions if a.kind == MERGE]) == 1   # 不重复 merge 同一 condition
    assert [a for a in actions if a.kind == SELL] == []        # 等量→无残余


def test_single_sided_sells_at_absorbing_price():
    metas = {"YES": _Meta("c1", "NO")}     # 只持 YES，无 NO 持仓
    actions = decide_close_actions(
        {"YES": 150}, _meta_of(metas), _bids_of({"YES": [(0.30, 500)]}),
        min_close=5, sell_offset=0.02, tick=0.01,
    )
    assert len(actions) == 1
    a = actions[0]
    assert a.kind == SELL and a.size == 150 and a.price == 0.28 and a.depth_sufficient   # 0.30-0.02


def test_sell_price_floored_at_tick():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(
        {"YES": 100}, _meta_of(metas), _bids_of({"YES": [(0.01, 500)]}),
        min_close=5, sell_offset=0.02, tick=0.01,
    )
    assert actions[0].price == 0.01     # max(tick, 0.01-0.02)=tick


def test_sell_flags_insufficient_depth():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(   # 簿仅 20 份，吃不下 150
        {"YES": 150}, _meta_of(metas), _bids_of({"YES": [(0.30, 10), (0.29, 10)]}),
        min_close=5, sell_offset=0.02, max_drop=0.10, tick=0.01,
    )
    assert actions[0].kind == SELL and actions[0].depth_sufficient is False


def test_sell_offset_escalates_with_attempts():
    # 同一仓连续未成交 → 让价逐轮加大（保成交止损）
    metas = {"YES": _Meta("c1", "NO")}
    bids = {"YES": [(0.30, 500)]}
    base = decide_close_actions({"YES": 150}, _meta_of(metas), _bids_of(bids),
                                min_close=5, sell_offset=0.01, max_drop=0.10, tick=0.01)
    esc = decide_close_actions({"YES": 150}, _meta_of(metas), _bids_of(bids),
                               min_close=5, sell_offset=0.01, max_drop=0.10, tick=0.01,
                               attempts_of=lambda t: 3, escalate_step=0.01)
    assert base[0].price == 0.29        # 0.30 - 0.01
    assert esc[0].price == 0.26         # 0.30 - (0.01 + 3×0.01)


def test_sell_escalation_capped_at_max_drop():
    metas = {"YES": _Meta("c1", "NO")}
    esc = decide_close_actions({"YES": 150}, _meta_of(metas), _bids_of({"YES": [(0.30, 500)]}),
                               min_close=5, sell_offset=0.01, max_drop=0.10, tick=0.01,
                               attempts_of=lambda t: 99, escalate_step=0.01)
    assert esc[0].price == 0.20         # 让价封顶 max_drop → 地板 0.30-0.10


def test_below_min_close_ignored():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(
        {"YES": 3}, _meta_of(metas), _bids_of({"YES": [(0.30, 500)]}), min_close=5, sell_offset=0.02,
    )
    assert actions == []


def test_single_sided_no_bid_skips():
    metas = {"YES": _Meta("c1", "NO")}
    actions = decide_close_actions(
        {"YES": 150}, _meta_of(metas), _bids_of({}), min_close=5, sell_offset=0.02,
    )
    assert actions == []     # 无买档不冒进卖出


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


def test_positions_parse_real_predictfun_shape():
    # 主网实测结构：token id 嵌在 outcome.onChainId，份额在 amount(wei)，顶层 id 是 base64 游标(非token)
    rows = [{
        "amount": "5000000000000000000",                    # 5 份(wei)
        "averageBuyPriceUsd": "0.524",
        "id": "eyJtYXJrZXRJZCI6MTUyNTF9",                   # base64 游标，不能当 token id
        "outcome": {"onChainId": "9243252904", "name": "No"},
        "market": {"conditionId": "0x5b7f"},
    }]
    m = positions_to_map(rows, min_close=5)
    assert m == {"9243252904": 5.0}                          # 取 outcome.onChainId、amount→5 份


# ---------- run_auto_close：清仓锁（返回本轮在清仓的 token，含被撤的对手腿）----------
class _RunMeta:
    def __init__(self, cond, comp):
        self.condition_id = cond
        self.complement_token_id = comp
        self.neg_risk = False
        self.yield_bearing = False


class _FakeClient:
    def merge_positions(self, *a, **k):
        return {"status": "ok"}


class _FakeBackend:
    def __init__(self):
        self.raw_client = _FakeClient()
        self.cancelled, self.created = [], []
    def get_all_positions(self):
        return [{"token_id": "YES", "size": "150"}]      # 仅单边持仓 → 走簿卖出
    def meta_for(self, tid):
        return _RunMeta("c1", "NO") if tid == "YES" else _RunMeta("c1", "YES")
    def get_order_book(self, tid):
        return NormalizedBook(1, [BookLevel(0.30, 500)], [BookLevel(0.32, 500)])
    def cancel_all_asset(self, tid):
        self.cancelled.append(str(tid))
    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created.append({"side": side, "token_id": token_id}); return {"status": "live"}


def test_run_auto_close_reports_closed_tokens_for_sell():
    be = _FakeBackend()
    res = run_auto_close(be)
    assert res["sold"] == 1
    # 卖残余前撤了该 token 买单 + 对手腿买单 → 两者都列入 closed_tokens（监控本轮跳过，防边清边买）
    assert set(res["closed_tokens"]) == {"YES", "NO"}
    assert set(be.cancelled) == {"YES", "NO"}


class _FailSellBackend(_FakeBackend):
    def create_order(self, token_id, side, price, size, neg_risk=False):
        self.created.append({"side": side, "token_id": token_id})
        return {"status": "error", "error": "create_order_rejected"}   # API 拒单不抛异常，只返回 error


def test_run_auto_close_does_not_count_failed_sell():
    """create_order 返回 error 时不得记为 sold（曾因不查返回值显示假成功）。"""
    be = _FailSellBackend()
    res = run_auto_close(be)
    assert res["sold"] == 0                       # 失败的卖单不计数
    assert be.created and be.created[0]["side"] == "SELL"   # 确实尝试过下单


def test_order_ok_judgement():
    from predictfun_data.auto_close import _order_ok
    assert _order_ok({"status": "live"}) is True
    assert _order_ok({"status": "placed"}) is True
    assert _order_ok({"order_id": "123"}) is True            # 无 status 但有单号
    assert _order_ok({"status": "error", "error": "x"}) is False
    assert _order_ok({"error": "x"}) is False
    assert _order_ok(None) is False
    assert _order_ok("oops") is False
