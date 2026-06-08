"""resolver 纯逻辑测试 —— outcome 匹配 + Gamma 解析（无网络）。"""
import json

import pytest

from penny_up_bot.resolver import match_outcome_token, parse_gamma_market


class TestMatchOutcomeToken:
    TOKENS = [("111", "Yes"), ("222", "No")]

    def test_match_yes_case_insensitive(self):
        assert match_outcome_token(self.TOKENS, "YES") == "111"

    def test_match_no(self):
        assert match_outcome_token(self.TOKENS, "no") == "222"

    def test_unknown_outcome_raises(self):
        with pytest.raises(ValueError):
            match_outcome_token(self.TOKENS, "MAYBE")

    def test_ambiguous_outcome_raises(self):
        with pytest.raises(ValueError):
            match_outcome_token([("1", "Yes"), ("2", "Yes")], "YES")


class TestParseGammaMarket:
    def test_parse_string_encoded_arrays(self):
        market = {
            "conditionId": "0xabc",
            "outcomes": json.dumps(["Yes", "No"]),
            "clobTokenIds": json.dumps(["111", "222"]),
            "negRisk": True,
        }
        cond, tokens, neg = parse_gamma_market(market)
        assert cond == "0xabc"
        assert tokens == [("111", "Yes"), ("222", "No")]
        assert neg is True

    def test_parse_list_arrays(self):
        market = {
            "conditionId": "0xdef",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["333", "444"],
        }
        cond, tokens, neg = parse_gamma_market(market)
        assert tokens == [("333", "Yes"), ("444", "No")]
        assert neg is False

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError):
            parse_gamma_market({"outcomes": ["Yes", "No"], "clobTokenIds": ["111"]})
