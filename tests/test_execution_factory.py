"""SP2: 执行后端工厂 —— PLATFORM 切换、懒导入、不破 Polymarket。"""
import pytest

from execution.factory import create_execution_backend
from execution.polymarket_backend import PolymarketBackend
from execution.predictfun_backend import PredictFunBackend


class _FakeClient:
    pass


def test_factory_predictfun_with_injected_client():
    be = create_execution_backend(platform="predictfun", client=_FakeClient())
    assert isinstance(be, PredictFunBackend)


def test_factory_polymarket_with_injected_client():
    be = create_execution_backend(platform="polymarket", client=_FakeClient())
    assert isinstance(be, PolymarketBackend)


def test_factory_reads_env_platform(monkeypatch):
    monkeypatch.setenv("PLATFORM", "predictfun")
    be = create_execution_backend(client=_FakeClient())
    assert isinstance(be, PredictFunBackend)


def test_factory_unknown_platform_raises():
    with pytest.raises(ValueError):
        create_execution_backend(platform="kalshi", client=_FakeClient())
