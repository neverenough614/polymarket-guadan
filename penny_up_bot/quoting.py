"""penny-up 目标价计算 —— 无依赖纯函数，是整个策略的心脏。

规约（单边 BUY 建仓）：
  best_comp = 剔除我自己挂单后的最高对手买价
  target    = best_comp + tick，但永不 > cap
  - 无对手（best_comp is None）          -> None（暂不挂单等待）
  - best_comp + tick > cap（顶到上限）    -> None（撤单等待）
  - 否则                                 -> round_to_tick(best_comp + tick)
"""
from __future__ import annotations

import math
from typing import Optional

# 浮点比较容差：价格以 tick(>=0.0001) 为最小粒度，1e-9 远小于任何 tick，足够安全。
_EPS = 1e-9


def round_to_tick(price: float, tick: float) -> float:
    """把价格规整到最近的 tick 网格点，并清除浮点误差。

    例：round_to_tick(0.64 + 0.01, 0.01) -> 0.65（而非 0.6500000001）。
    """
    if tick <= 0:
        raise ValueError(f"tick 必须 > 0：{tick}")
    decimals = max(0, round(-math.log10(tick)))
    return round(round(price / tick) * tick, decimals)


def compute_target(best_comp: Optional[float], tick: float, cap: float) -> Optional[float]:
    """计算这一刻应挂的 BUY 价；返回 None 表示「不该挂单，撤掉等待」。

    Args:
        best_comp: 剔除自己后的最高对手买价；None 表示买盘无对手。
        tick:      该市场最小价位。
        cap:       硬上限买价，结果永不 > cap。
    """
    if best_comp is None:
        return None

    raw = best_comp + tick
    # 容差比较：0.64+0.01 的浮点结果不能因误差被判为 > 0.65。
    if raw > cap + _EPS:
        return None

    target = round_to_tick(raw, tick)
    # 防御：规整后若仍越界（理论上不会），宁可不挂。
    if target > cap + _EPS:
        return None
    return target
