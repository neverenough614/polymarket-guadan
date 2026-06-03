"""执行后端工厂：按 PLATFORM 选择 Polymarket / predict.fun 实现。

懒导入：Polymarket 路径不触发 predict_sdk，反之亦然。
Polymarket 行为保持不变（默认 platform=polymarket）。
"""
import os
from typing import Any, Optional

from .interface import IExecutionBackend


def create_execution_backend(
    platform: Optional[str] = None,
    client: Any = None,
    **client_kwargs: Any,
) -> IExecutionBackend:
    """构建执行后端。

    platform: "polymarket" | "predictfun"；缺省读环境变量 PLATFORM，再缺省 polymarket。
    client:   可注入现成 client（测试/复用）；为 None 时按平台构建。
    """
    plat = (platform or os.getenv("PLATFORM", "polymarket")).strip().lower()

    if plat == "predictfun":
        from predictfun_data.predictfun_client import PredictFunClient
        from .predictfun_backend import PredictFunBackend
        c = client if client is not None else PredictFunClient(**client_kwargs)
        return PredictFunBackend(c)

    if plat == "polymarket":
        from poly_data.polymarket_client import PolymarketClient
        from .polymarket_backend import PolymarketBackend
        c = client if client is not None else PolymarketClient(**client_kwargs)
        return PolymarketBackend(c)

    raise ValueError(f"未知 PLATFORM={plat!r}（支持 polymarket | predictfun）")
