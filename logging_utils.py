"""
结构化日志工具，用于关键决策点的事件记录。
支持 JSON 格式输出和可检索字段，便于后续复盘与监控。
"""
import logging
import json
from datetime import datetime
from typing import Any, Dict, Optional


# 关键事件类型
EVENT_ORDER_PLACED = "order_placed"
EVENT_ORDER_CANCELLED = "order_cancelled"
EVENT_DEFENSE_TRIGGERED = "defense_triggered"
EVENT_CLOSE_POSITION = "close_position"
EVENT_SPREAD_REBALANCED = "spread_rebalanced"
EVENT_DEPTH_INSUFFICIENT = "depth_insufficient"
EVENT_IMBALANCE_DETECTED = "imbalance_detected"
EVENT_EXTREME_LONE = "extreme_lone"
EVENT_SHEET_RELOADED = "sheet_reloaded"


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器，输出 JSON 或带时间戳的键值对"""

    def __init__(self, use_json: bool = False):
        super().__init__()
        self.use_json = use_json

    def format(self, record: logging.LogRecord) -> str:
        base = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event": getattr(record, "event", "unknown"),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        # 合并 extra 字段
        for k, v in record.__dict__.items():
            if k not in (
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "exc_info", "exc_text", "thread", "threadName",
                "message", "taskName", "event"
            ) and v is not None:
                base[k] = v
        if self.use_json:
            return json.dumps(base, ensure_ascii=False)
        parts = [f"[{base['timestamp']}]", base["event"], base["message"]]
        extras = {k: v for k, v in base.items() if k not in ("timestamp", "event", "message", "level", "module")}
        if extras:
            parts.append(json.dumps(extras, ensure_ascii=False))
        return " | ".join(parts)


def get_bot_logger(name: str = "poly_maker") -> logging.Logger:
    """获取 bot 使用的 logger"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        h = logging.StreamHandler()
        h.setFormatter(StructuredFormatter(use_json=False))
        logger.addHandler(h)
    return logger


_bot_logger: Optional[logging.Logger] = None


def log_event(
    event: str,
    message: str,
    token_id: Optional[str] = None,
    side: Optional[str] = None,
    price: Optional[float] = None,
    size: Optional[float] = None,
    reason: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """
    记录结构化事件。
    关键事件：order_placed, order_cancelled, defense_triggered, close_position,
    spread_rebalanced, depth_insufficient, imbalance_detected 等。
    """
    global _bot_logger
    if _bot_logger is None:
        _bot_logger = get_bot_logger()
    extra: Dict[str, Any] = {"event": event, **kwargs}
    if token_id is not None:
        extra["token_id"] = token_id[:16] + "..." if len(token_id) > 16 else token_id
    if side is not None:
        extra["side"] = side
    if price is not None:
        extra["price"] = price
    if size is not None:
        extra["size"] = size
    if reason is not None:
        extra["reason"] = reason
    _bot_logger.info(message, extra=extra)
