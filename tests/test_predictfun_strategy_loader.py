"""SP3: predict.fun 策略加载 —— 本地 JSON → token，max_spread 不按美分换算。"""
import json

from predictfun_data import strategy_loader


# onChainId 是很长的数字串（loader 要求 len>10）
YES1 = "98486341393966570826356063913768723506745391202735433434778619590985059167721"
NO1 = "64064053505788208683485407135557779016802465915147364814314720603469680982232"
YES2 = "11111111111111111111111111111111111111111111111111111111111111111111111111111"
NO2 = "22222222222222222222222222222222222222222222222222222222222222222222222222222"

SELECTED_ROWS = [
    {"question": "Will ETH hit 1k or 3k first", "token1": YES1, "token2": NO1,
     "neg_risk": True, "min_size": 100, "max_spread": 0.06, "volatility_sum": 0.0},
    {"question": "Will CZ tweet 0-5", "token1": YES2, "token2": NO2,
     "neg_risk": False, "min_size": 200, "max_spread": 0.03, "volatility_sum": 0.0},
]


def test_load_from_json_parses_rows_to_tokens(tmp_path):
    p = tmp_path / "pf.json"
    p.write_text(json.dumps(SELECTED_ROWS), encoding="utf-8")
    tokens = strategy_loader.load_from_json(str(p))
    # 每个市场 2 个 token（YES/NO）
    assert len(tokens) == 4
    by_id = {t["token_id"]: t for t in tokens}
    assert by_id[YES1]["token_type"] == "YES"
    assert by_id[NO1]["token_type"] == "NO"
    assert by_id[YES1]["source"] == "High Reward"


def test_load_from_json_keeps_max_spread_as_decimal(tmp_path):
    p = tmp_path / "pf.json"
    p.write_text(json.dumps(SELECTED_ROWS), encoding="utf-8")
    tokens = strategy_loader.load_from_json(str(p))
    by_id = {t["token_id"]: t for t in tokens}
    # predict.fun spreadThreshold 已是小数：0.06 应原样保留（不 /100）
    assert by_id[YES1]["max_spread"] == 0.06
    assert by_id[YES2]["max_spread"] == 0.03
    assert by_id[YES1]["min_size"] == 100.0
    assert by_id[YES2]["neg_risk"] is False


def test_load_from_json_missing_file_returns_empty(tmp_path):
    assert strategy_loader.load_from_json(str(tmp_path / "nope.json")) == []
