"""predict.fun 选市分流（SP4）—— 对齐 Polymarket 的 Normal LP / High Reward Aggressive。

输入：已打分的市场行（discovery 行 + scoring 指标 + daily_rate）。
输出：两组行（normal / aggressive），与 Polymarket 现网两张策略表口径一致、互不互斥
（一个市场可同时进两组；下游 loader 去重）。

阈值见 config.PredictFunSelectionConfig。days_to_expiry / volatility 护栏因 predict.fun
无数据源不参与（见 SelectionConfig 说明）。
"""
from typing import Any, Dict, List

from config.bot_config import cfg


def _expiry_ok(row: Dict[str, Any], days_min: float) -> bool:
    """到期护栏：剩余天数 > 阈值，或 ==0（未知到期→纳入，对齐 Polymarket）。"""
    days = float(row.get("days_to_expiry", 0) or 0)
    return days > days_min or days == 0


def is_normal(row: Dict[str, Any], sc=None) -> bool:
    sc = sc or cfg.predictfun_selection
    spread = float(row.get("spread", 0) or 0)
    mid_reward = float(row.get("mid_reward_per_100", 0) or 0)
    return (sc.normal_spread_min <= spread <= sc.normal_spread_max
            and mid_reward >= sc.normal_mid_reward_min
            and _expiry_ok(row, sc.normal_days_min))


def is_aggressive(row: Dict[str, Any], sc=None) -> bool:
    sc = sc or cfg.predictfun_selection
    spread = float(row.get("spread", 0) or 0)
    gm_reward = float(row.get("gm_reward_per_100", 0) or 0)
    daily_rate = float(row.get("rewards_daily_rate", 0) or 0)
    return (daily_rate >= sc.agg_daily_rate_min
            and gm_reward >= sc.agg_reward_min
            and sc.agg_spread_min <= spread <= sc.agg_spread_max
            and _expiry_ok(row, sc.agg_days_min))


def split_strategies(scored_rows: List[Dict[str, Any]], sc=None):
    """→ (normal_rows[排序 by mid_reward], aggressive_rows[排序 by gm_reward])。"""
    sc = sc or cfg.predictfun_selection
    normal = [r for r in scored_rows if is_normal(r, sc)]
    aggressive = [r for r in scored_rows if is_aggressive(r, sc)]
    normal.sort(key=lambda r: float(r.get("mid_reward_per_100", 0) or 0), reverse=True)
    aggressive.sort(key=lambda r: float(r.get("gm_reward_per_100", 0) or 0), reverse=True)
    return normal, aggressive
