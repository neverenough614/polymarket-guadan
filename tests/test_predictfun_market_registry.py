"""SP2: 市场注册表解析 + 簿 swap+complement（依据主网实测结构）。"""
from predictfun_data.market_registry import parse_market, TokenMeta
from predictfun_data.normalize import NormalizedBook, BookLevel, complement_book


# 主网实测市场结构（id=420295，CZ tweets 6-10）
RAW_MARKET = {
    "id": 420295,
    "conditionId": "0xabc",
    "feeRateBps": 200,
    "isNegRisk": True,
    "isYieldBearing": True,
    "decimalPrecision": 2,
    "outcomes": [
        {"name": "Yes", "indexSet": 1,
         "onChainId": "98486341393966570826356063913768723506745391202735433434778619590985059167721",
         "bestBid": {"price": 0.05, "size": 500}, "bestAsk": {"price": 0.79, "size": 10}},
        {"name": "No", "indexSet": 2,
         "onChainId": "64064053505788208683485407135557779016802465915147364814314720603469680982232",
         "bestBid": {"price": 0.21, "size": 10}, "bestAsk": {"price": 0.95, "size": 500}},
    ],
}
YES_ID = RAW_MARKET["outcomes"][0]["onChainId"]
NO_ID = RAW_MARKET["outcomes"][1]["onChainId"]


def test_parse_market_returns_two_token_metas():
    metas = parse_market(RAW_MARKET)
    assert len(metas) == 2
    assert {m.token_id for m in metas} == {YES_ID, NO_ID}


def test_parse_market_yes_is_native_no_is_complement():
    metas = {m.token_id: m for m in parse_market(RAW_MARKET)}
    assert metas[YES_ID].is_complement is False   # indexSet==1 = 簿原生方
    assert metas[NO_ID].is_complement is True


def test_parse_market_carries_fee_neg_yield_market_id():
    m = parse_market(RAW_MARKET)[0]
    assert m.market_id == 420295
    assert m.fee_rate_bps == 200
    assert m.neg_risk is True
    assert m.yield_bearing is True
    assert m.condition_id == "0xabc"


def test_parse_market_tick_from_decimal_precision():
    m = parse_market(RAW_MARKET)[0]
    assert m.tick_size == 0.01            # decimalPrecision=2 → 0.01


def test_parse_market_complement_cross_links_token_ids():
    metas = {m.token_id: m for m in parse_market(RAW_MARKET)}
    assert metas[YES_ID].complement_token_id == NO_ID
    assert metas[NO_ID].complement_token_id == YES_ID


def test_parse_market_fallback_native_when_no_indexset():
    raw = {"id": 1, "feeRateBps": 0, "outcomes": [
        {"name": "Alpha", "onChainId": "A"},
        {"name": "Beta", "onChainId": "B"},
    ]}
    metas = parse_market(raw)
    # 无 indexSet/Yes → 位置0 视为原生
    by_id = {m.token_id: m for m in metas}
    assert by_id["A"].is_complement is False
    assert by_id["B"].is_complement is True


def test_complement_book_swaps_and_complements_both_directions():
    # Yes 簿（实测）：bids=[[0.05,500]], asks=[[0.79,10],[0.8,100],[0.94,17]]
    yes = NormalizedBook(
        market_id=420295,
        bids=[BookLevel(0.05, 500)],
        asks=[BookLevel(0.79, 10), BookLevel(0.80, 100), BookLevel(0.94, 17)],
    )
    no = complement_book(yes)
    # No.bids = complement(Yes.asks)，按价降序
    assert [(round(l.price, 2), l.size) for l in no.bids] == [(0.21, 10), (0.20, 100), (0.06, 17)]
    # No.asks = complement(Yes.bids)，按价升序
    assert [(round(l.price, 2), l.size) for l in no.asks] == [(0.95, 500)]
    assert no.market_id == 420295


def test_complement_book_empty_side():
    yes = NormalizedBook(market_id=1, bids=[], asks=[BookLevel(0.49, 20)])
    no = complement_book(yes)
    assert [(round(l.price, 2), l.size) for l in no.bids] == [(0.51, 20)]
    assert no.asks == []


def test_complement_book_skips_invalid_prices():
    # p=0/1 的补价会得到"白送"价位，必须被丢弃
    yes = NormalizedBook(market_id=1,
                         bids=[BookLevel(0.0, 10), BookLevel(0.05, 500)],
                         asks=[BookLevel(1.0, 10), BookLevel(0.79, 20)])
    no = complement_book(yes)
    assert [(round(l.price, 2), l.size) for l in no.bids] == [(0.21, 20)]   # 1.0 被跳过
    assert [(round(l.price, 2), l.size) for l in no.asks] == [(0.95, 500)]  # 0.0 被跳过


def test_parse_market_no_at_position_zero_not_double_native():
    # 防回归：No 在位置0、Yes 在位置1 且带 indexSet==1 时，No 不应被误判为原生
    raw = {"id": 9, "feeRateBps": 200, "outcomes": [
        {"name": "No", "indexSet": 2, "onChainId": "N"},
        {"name": "Yes", "indexSet": 1, "onChainId": "Y"},
    ]}
    by_id = {m.token_id: m for m in parse_market(raw)}
    assert by_id["Y"].is_complement is False
    assert by_id["N"].is_complement is True
