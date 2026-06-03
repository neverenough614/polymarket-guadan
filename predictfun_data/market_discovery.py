"""predict.fun 市场发现/选市（SP3，纯逻辑，可测）。

选市标准：只挑当前有 active LP 奖励的市场。predict.fun 奖励字段直接映射现有体系：
  rewards.current.hourlyRate → 奖励强度（排序用）
  spreadThreshold            → max_spread（挂单须在 mid±此范围才拿奖励，已是小数）
  shareThreshold             → min_size（最低挂单量门槛）
产出的行字段对齐 strategy_io.sheet_loader 的列名，可被现有 _parse_sheet_tokens 解析。
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .market_registry import parse_market


def parse_iso(s: Any) -> Optional[datetime]:
    """ISO8601（含末尾 Z）→ aware datetime(UTC)；无法解析返回 None。"""
    if not s:
        return None
    try:
        txt = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def reward_current(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cur = (market.get("rewards") or {}).get("current")
    return cur if isinstance(cur, dict) else None


def reward_hourly_rate(market: Dict[str, Any]) -> float:
    cur = reward_current(market)
    if not cur:
        return 0.0
    try:
        return float(cur.get("hourlyRate") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_reward_active(market: Dict[str, Any], now: datetime, min_hourly_rate: float = 0.0) -> bool:
    """rewards.current 存在、hourlyRate>min、且 now ∈ [startsAt, endsAt)。

    要求 startsAt 存在且已开始（缺开始时间无法确认奖励生效，排除）；
    endsAt 可缺省（视为开放式持续奖励，不因缺 end 而排除）。
    """
    cur = reward_current(market)
    if not cur:
        return False
    if reward_hourly_rate(market) <= min_hourly_rate:
        return False
    start = parse_iso(cur.get("startsAt"))
    if start is None or now < start:        # 无开始时间 / 尚未开始
        return False
    end = parse_iso(cur.get("endsAt"))
    if end is not None and now >= end:      # 有结束时间且已过期
        return False
    return True


def _yes_no_ids(market: Dict[str, Any]):
    """复用 parse_market 的 native/complement 判定，取 (yes_id, no_id)。"""
    metas = parse_market(market)
    yes = next((m.token_id for m in metas if not m.is_complement), None)
    no = next((m.token_id for m in metas if m.is_complement), None)
    return yes, no


def market_to_row(market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """单个市场 → 一行选市记录（列名对齐 sheet_loader）。

    返回 None（被跳过）当：① 取不到二元 YES/NO token id；
    ② 缺 spreadThreshold —— 奖励市场必有奖励价带,缺失则无法约束挂单范围,
       不能写成"无 max_spread"导致裸挂单,直接跳过。
    """
    yes_id, no_id = _yes_no_ids(market)
    if not yes_id or not no_id:
        return None
    spread_threshold = market.get("spreadThreshold")
    if spread_threshold is None:
        return None
    # categorySlug 不作问题文本兜底（会把分类名误当市场名）；缺 question/title 留空由 loader 丢弃
    question = str(market.get("question") or market.get("title") or "").strip()
    share_threshold = market.get("shareThreshold")
    return {
        "question": question,
        "token1": yes_id,                       # YES onChainId
        "token2": no_id,                        # NO onChainId
        "neg_risk": bool(market.get("isNegRisk")),
        "min_size": float(share_threshold) if share_threshold is not None else 0.0,
        "max_spread": float(spread_threshold),  # spreadThreshold 已是小数
        "volatility_sum": 0.0,
        # 以下为 predict.fun 专属列（便于人工核对/排序，loader 不强依赖）
        "hourly_rate": reward_hourly_rate(market),
        "market_id": market.get("id"),
        "fee_rate_bps": int(market.get("feeRateBps") or 0),
        "yield_bearing": bool(market.get("isYieldBearing")),
        "source": "High Reward",
    }


def select_reward_markets(
    markets: List[Dict[str, Any]],
    now: datetime,
    min_hourly_rate: float = 0.0,
    max_markets: int = 0,
) -> List[Dict[str, Any]]:
    """筛出 active 奖励市场 → 行记录，按 hourly_rate 降序；max_markets>0 时截断。"""
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for m in markets:
        if not is_reward_active(m, now, min_hourly_rate):
            continue
        row = market_to_row(m)
        if row is None:
            skipped += 1          # active 但缺二元 id / 缺 spreadThreshold
        else:
            rows.append(row)
    if skipped:
        print(f"⚠️ [select_reward_markets] {skipped} 个 active 市场因缺二元 token id 或缺 spreadThreshold 被跳过")
    rows.sort(key=lambda r: r.get("hourly_rate", 0.0), reverse=True)
    if max_markets and max_markets > 0:
        rows = rows[:max_markets]
    return rows
