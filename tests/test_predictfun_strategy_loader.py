"""SP3/SP4: predict.fun 策略加载 —— 读两张策略表/本地JSON，max_spread 不按美分换算，跨表去重。"""
import json

from predictfun_data import strategy_loader
from config.bot_config import cfg

# onChainId 是很长的数字串（loader 要求 len>10）
YES1 = "98486341393966570826356063913768723506745391202735433434778619590985059167721"
NO1 = "64064053505788208683485407135557779016802465915147364814314720603469680982232"
YES2 = "11111111111111111111111111111111111111111111111111111111111111111111111111111"
NO2 = "22222222222222222222222222222222222222222222222222222222222222222222222222222"

NORMAL_ROWS = [
    {"question": "ETH 1k or 3k first", "token1": YES1, "token2": NO1,
     "neg_risk": True, "min_size": 100, "max_spread": 0.04, "volatility_sum": 0.0},
]
AGG_ROWS = [
    {"question": "CZ tweet 0-5", "token1": YES2, "token2": NO2,
     "neg_risk": False, "min_size": 200, "max_spread": 0.06, "volatility_sum": 0.0},
    # 与 NORMAL 重叠的同一市场（应被跨表去重）
    {"question": "ETH 1k or 3k first", "token1": YES1, "token2": NO1,
     "neg_risk": True, "min_size": 100, "max_spread": 0.04, "volatility_sum": 0.0},
]


def _setup(tmp_path, monkeypatch):
    npath = tmp_path / "normal.json"
    apath = tmp_path / "agg.json"
    npath.write_text(json.dumps(NORMAL_ROWS), encoding="utf-8")
    apath.write_text(json.dumps(AGG_ROWS), encoding="utf-8")
    monkeypatch.setattr(cfg.predictfun_selection, "normal_json_path", str(npath))
    monkeypatch.setattr(cfg.predictfun_selection, "aggressive_json_path", str(apath))


def test_load_from_json_merges_both_tabs_with_dedup(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    tokens = strategy_loader.load_from_json()
    by_id = {t["token_id"]: t for t in tokens}
    # 4 个唯一 token（市场1 YES/NO + 市场2 YES/NO），重叠市场只算一次
    assert len(tokens) == 4
    assert by_id[YES1]["token_type"] == "YES"
    assert by_id[NO2]["token_type"] == "NO"
    # 市场1 先由 Normal 表登记 → source=Normal LP（去重保留首登记）
    assert by_id[YES1]["source"] == "Normal LP"
    assert by_id[YES2]["source"] == "High Reward"


def test_load_from_json_keeps_max_spread_as_decimal(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    by_id = {t["token_id"]: t for t in strategy_loader.load_from_json()}
    assert by_id[YES1]["max_spread"] == 0.04     # 不 /100
    assert by_id[YES2]["max_spread"] == 0.06
    assert by_id[YES2]["neg_risk"] is False


def test_load_from_json_missing_files_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg.predictfun_selection, "normal_json_path", str(tmp_path / "no.json"))
    monkeypatch.setattr(cfg.predictfun_selection, "aggressive_json_path", str(tmp_path / "no2.json"))
    assert strategy_loader.load_from_json() == []
