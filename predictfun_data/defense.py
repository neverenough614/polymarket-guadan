"""predict.fun 订单簿变化防御（仿 Polymarket monitor_defense_loop，只买侧、薄簿校准）。

Polymarket 的防御逐档盯**深度变化**，本模块照搬其威胁判定，但：
  - 只看买侧（predict.fun 双边=买YES+买NO，每 token 只有一张买单，在自己簿里是 bid）。
    "前墙"=价高于我的买档（排在我前面，被撤光→我暴露在最前→价跌即被吃）；
    "同档"=与我同价的买量（排除自己，被吃光→轮到我）。
  - 阈值按 predict.fun 真实薄簿校准（见 config.PredictFunDefenseConfig）：绝对深度门槛
    大幅下调（Polymarket 前墙100/同档200 → predict.fun 50/80），百分比门槛（单轮/高水位/
    趋势跌幅）尺度无关，沿用。
  - 触发只产生"建议撤单"信号，真正撤单由循环层经 ChurnGuard 限流（防反作弊清零）。

DefenseState 逐 token 持有深度历史/高水位；纯函数 layered_bid_depth / check_bid_threats /
check_imbalance / check_trend 便于单测。
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from config.bot_config import cfg

_EPS = 0.0005   # 价格相等容差（predict.fun tick 0.001~0.01）


@dataclass
class DefenseState:
    """单 token 的买侧深度记忆（跨轮）。"""
    last_front: float = 0.0
    last_same: float = 0.0
    front_hw: float = 0.0          # 前墙历史高水位
    same_hw: float = 0.0           # 同档历史高水位
    first_run: bool = True
    front_hist: deque = field(default_factory=lambda: deque(maxlen=cfg.predictfun_defense.trend_window))
    same_hist: deque = field(default_factory=lambda: deque(maxlen=cfg.predictfun_defense.trend_window))

    def reset(self) -> None:
        """撤单/重挂后重置（避免拿旧墙数据误判新单）。"""
        self.front_hw = self.same_hw = 0.0
        self.first_run = True
        self.front_hist.clear()
        self.same_hist.clear()


def layered_bid_depth(
    bids: List[Tuple[float, float]],
    my_bid: float,
    my_size: float,
) -> Tuple[float, float]:
    """买侧分层深度（USDT）：front=价>我的买档累计；same=与我同价累计(排除自己)。bids 降序。"""
    front = same = 0.0
    for price, size in bids:
        depth = price * size
        if price > my_bid + _EPS:
            front += depth
        elif abs(price - my_bid) < _EPS:
            same += depth
    same = max(0.0, same - my_bid * my_size)
    return front, same


def check_trend(history: deque, label: str, cum_drop_threshold: float, min_consecutive: int) -> Tuple[bool, str]:
    """慢刀子：连续 min_consecutive 轮下降且累计跌幅 ≥ 阈值。"""
    if len(history) < min_consecutive + 1:
        return False, ""
    consecutive = 0
    for i in range(len(history) - 1, 0, -1):
        if history[i] < history[i - 1]:
            consecutive += 1
        else:
            break
    if consecutive < min_consecutive:
        return False, ""
    start = history[len(history) - 1 - consecutive]
    cur = history[-1]
    if start <= 0:
        return False, ""
    cum = 1 - cur / start
    if cum >= cum_drop_threshold:
        return True, f"[慢刀子]{label}连续{consecutive}轮降 ${start:.0f}→${cur:.0f}(-{cum:.0%})"
    return False, ""


def check_bid_threats(
    state: DefenseState,
    my_bid: Optional[float],
    front: float,
    same: float,
    is_extreme: bool = False,
    dc=None,
) -> Tuple[bool, List[str]]:
    """买侧威胁判定（仿 Polymarket check_side_threats 的 bid 分支）。返回 (triggered, reasons)。"""
    dc = dc or cfg.predictfun_defense
    if my_bid is None:
        return False, []
    reasons: List[str] = []
    triggered = False

    front_drop = dc.ext_front_drop if is_extreme else dc.front_drop
    same_drop = dc.ext_same_drop if is_extreme else dc.same_drop
    front_hw_drop = dc.ext_front_hw_drop if is_extreme else dc.front_hw_drop
    same_hw_drop = dc.ext_same_hw_drop if is_extreme else dc.same_hw_drop
    same_safe = dc.ext_same_safe if is_extreme else dc.same_safe
    front_abs = dc.ext_front_absolute if is_extreme else dc.front_absolute

    was_behind_wall = state.last_front > dc.front_present
    now_exposed = front <= dc.front_present
    if was_behind_wall and now_exposed:
        drop = (1 - front / state.last_front) * 100 if state.last_front > 0 else 100
        reasons.append(f"[前墙消失]买单前墙 ${state.last_front:.0f}→${front:.0f}(-{drop:.0f}%)")
        triggered = True
    if front < front_abs and state.front_hw > dc.front_absolute_ref:
        reasons.append(f"[绝对兜底]买单前墙极危 ${front:.0f}(高水位${state.front_hw:.0f})")
        triggered = True
    if state.front_hw > dc.front_present and front < state.front_hw * (1 - front_hw_drop):
        reasons.append(f"[高水位]买单前墙累计大跌 ${state.front_hw:.0f}→${front:.0f}")
        triggered = True
    if front > dc.front_present:
        if state.last_front > dc.front_present and front < state.last_front * (1 - front_drop):
            reasons.append(f"[单轮]买单前墙塌陷 ${state.last_front:.0f}→${front:.0f}")
            triggered = True
    else:
        if same < same_safe:
            reasons.append(f"[第一档]买单同档太薄 ${same:.0f}")
            triggered = True
        elif state.last_same > same_safe and same < state.last_same * (1 - same_drop):
            reasons.append(f"[第一档]买单同档被大量吃 ${state.last_same:.0f}→${same:.0f}")
            triggered = True
        if state.same_hw > same_safe and same < state.same_hw * (1 - same_hw_drop):
            reasons.append(f"[高水位]同档累计被吃 ${state.same_hw:.0f}→${same:.0f}")
            triggered = True

    t1, r1 = check_trend(state.front_hist, "买单前墙", dc.ext_trend_cum_drop if is_extreme else dc.trend_cum_drop, dc.trend_min_consecutive)
    if t1:
        triggered = True
        reasons.append(r1)
    t2, r2 = check_trend(state.same_hist, "买单同档", dc.ext_trend_cum_drop if is_extreme else dc.trend_cum_drop, dc.trend_min_consecutive)
    if t2:
        triggered = True
        reasons.append(r2)
    return triggered, reasons


def check_imbalance(
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    is_extreme: bool = False,
    dc=None,
) -> Tuple[bool, str]:
    """买卖偏斜：买侧深度占比过低→价格可能下跌→买单危险。bids 降序 asks 升序。返回 (triggered, reason)。"""
    dc = dc or cfg.predictfun_defense
    bid_depth = sum(p * s for p, s in bids[: dc.imbalance_levels])
    ask_depth = sum(p * s for p, s in asks[: dc.imbalance_levels])
    total = bid_depth + ask_depth
    if total < dc.imbalance_min_total:
        return False, ""
    bid_ratio = bid_depth / total
    threshold = dc.ext_imbalance_threshold if is_extreme else dc.imbalance_threshold
    if bid_ratio < threshold:
        return True, f"[偏斜]买侧深度不足 买/卖={bid_ratio:.0%}/{1-bid_ratio:.0%}(${bid_depth:.0f}/${ask_depth:.0f})→价或跌"
    return False, ""


def evaluate_defense(
    state: DefenseState,
    bids: List[Tuple[float, float]],
    asks: List[Tuple[float, float]],
    my_bid: Optional[float],
    my_size: float,
    best_bid: Optional[float],
    dc=None,
) -> Tuple[bool, List[str]]:
    """整合一轮买侧防御：更新深度/高水位/趋势 + 判威胁。返回 (should_cancel, reasons)。

    无我方买单→不判（交给常规 REFILL）。首轮只建基线不触发。撤单与否由调用方经 churn 决定。
    """
    dc = dc or cfg.predictfun_defense
    if my_bid is None:
        return False, []
    is_extreme = best_bid is not None and (best_bid <= dc.extreme_threshold or best_bid >= 1 - dc.extreme_threshold)

    front, same = layered_bid_depth(bids, my_bid, my_size)

    # 偏斜独立判（不依赖历史，首轮也可触发）
    imb_trig, imb_reason = check_imbalance(bids, asks, is_extreme, dc)

    state.front_hw = max(state.front_hw, front)
    state.same_hw = max(state.same_hw, same)
    state.front_hist.append(front)
    state.same_hist.append(same)

    triggered = False
    reasons: List[str] = []
    if not state.first_run:
        triggered, reasons = check_bid_threats(state, my_bid, front, same, is_extreme, dc)
    if imb_trig:
        triggered = True
        reasons.append(imb_reason)

    state.last_front = front
    state.last_same = same
    state.first_run = False
    return triggered, reasons
