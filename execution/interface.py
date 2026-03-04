"""
统一的执行接口抽象（IExecutionBackend）。
所有下单、撤单、查询操作均通过此接口，便于未来切换 Rust 实现。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class IExecutionBackend(ABC):
    """执行后端抽象基类。未来 Rust 版本可实现此接口（如通过 FFI/gRPC）无缝切换。"""

    @abstractmethod
    def create_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        neg_risk: bool = False,
    ) -> Dict[str, Any]:
        """挂单。side: BUY 或 SELL。返回 API 响应或空 dict。"""
        pass

    @abstractmethod
    def cancel_all_asset(self, token_id: str) -> None:
        """撤销某 token 的所有挂单。"""
        pass

    @abstractmethod
    def cancel_one_side(
        self,
        token_id: str,
        side: str,
        cached_order_ids: Optional[List[str]] = None,
    ) -> bool:
        """只撤指定方向（BUY/SELL）的挂单。cached_order_ids 可省一次 get_orders。返回是否成功撤单。"""
        pass

    @abstractmethod
    def cancel_all(self) -> Any:
        """全局撤单。返回 API 响应。"""
        pass

    @abstractmethod
    def get_order_book(self, token_id: str) -> Any:
        """获取订单簿，返回具有 .bids 和 .asks 的对象，或 None。"""
        pass

    @abstractmethod
    def get_all_orders(self) -> List[Dict[str, Any]]:
        """获取所有活跃挂单。"""
        pass

    @abstractmethod
    def get_all_positions(self) -> Any:
        """获取所有持仓，返回 DataFrame 或等效结构。"""
        pass
