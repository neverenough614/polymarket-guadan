# Execution module - unified trading interface
from .interface import IExecutionBackend
from .polymarket_backend import PolymarketBackend

__all__ = ["IExecutionBackend", "PolymarketBackend"]
