"""价格/份额 ↔ wei(18 位) 换算与 tick 取整（纯函数，无副作用）。"""

from decimal import Decimal, ROUND_HALF_UP

WEI = 10 ** 18


def price_to_tick(price: float, tick: float) -> float:
    """把价格量化到最近的 tick，并夹到 [0, 1]。"""
    if tick <= 0:
        raise ValueError("tick 必须 > 0")
    clamped = min(1.0, max(0.0, float(price)))
    # Use Decimal for accurate rounding (round half up)
    d_clamped = Decimal(str(clamped))
    d_tick = Decimal(str(tick))
    steps = (d_clamped / d_tick).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    result = (steps * d_tick).quantize(Decimal('0.0000000001'), rounding=ROUND_HALF_UP)
    return float(result)


def to_wei(amount: float) -> int:
    """float 金额/份额 → wei，四舍五入（避免浮点系统性少 1）。"""
    return int(round(float(amount) * WEI))


def price_per_share_wei(price: float, tick: float) -> int:
    """价格(0~1) 先取 tick 再 → 每股 wei。"""
    return to_wei(price_to_tick(price, tick))


def shares_to_wei(size: float) -> int:
    """份额 → quantity_wei。"""
    return to_wei(size)


def from_wei(x) -> float:
    """wei(int 或 str) → float。"""
    return int(x) / WEI
