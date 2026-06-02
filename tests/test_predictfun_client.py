from config.bot_config import PredictFunConfig


def test_predictfun_config_testnet_endpoint():
    c = PredictFunConfig(network="testnet")
    assert c.base_url == "https://api-testnet.predict.fun"
    assert c.chain_id == 97
    assert c.requires_api_key is False


def test_predictfun_config_mainnet_endpoint():
    c = PredictFunConfig(network="mainnet")
    assert c.base_url == "https://api.predict.fun"
    assert c.chain_id == 56
    assert c.requires_api_key is True


def test_predictfun_config_invalid_network_raises():
    import pytest
    with pytest.raises(ValueError):
        PredictFunConfig(network="devnet")
