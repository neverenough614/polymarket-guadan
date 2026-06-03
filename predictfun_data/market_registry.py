"""市场注册表：把 predict.fun 原始市场解析为按 token(onChainId) 索引的元数据。

平台差异吸收点：main.py 只用 token_id 调 backend，但 predict.fun 下单需要
fee_rate_bps / yield_bearing，取簿需要 market_id，No 侧还要 swap+complement。
这些都由 TokenMeta 提供。字段名以主网实测结构为准。
"""
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass(frozen=True)
class TokenMeta:
    token_id: str                       # outcome 的 onChainId（字符串，ERC1155 id 很大）
    market_id: Any                      # 整型市场 id
    name: str                           # "Yes"/"No"
    index_set: int                      # 1=Yes（簿原生方）
    is_complement: bool                 # True → 取簿需 swap+complement
    complement_token_id: Optional[str]  # 对手 outcome 的 onChainId（二元市场）
    neg_risk: bool
    yield_bearing: bool
    fee_rate_bps: int
    condition_id: Optional[str]
    tick_size: float


def _tick_from_precision(decimal_precision: Any, default: float) -> float:
    try:
        dp = int(decimal_precision)
        if dp > 0:
            return float(10 ** -dp)
    except (TypeError, ValueError):
        pass
    return default


def _has_native_signal(outcome: dict) -> bool:
    """该 outcome 是否带有"簿原生方"线索（indexSet==1 或 name=="yes"）。"""
    if outcome.get("indexSet") == 1:
        return True
    return str(outcome.get("name") or "").strip().lower() == "yes"


def parse_market(market: dict, default_tick: float = 0.01) -> List[TokenMeta]:
    """把一个原始市场拆成每个 outcome 一条 TokenMeta（二元市场=2 条）。"""
    outcomes = market.get("outcomes") or []
    market_id = market.get("id")
    neg = bool(market.get("isNegRisk"))
    yb = bool(market.get("isYieldBearing"))
    fee_raw = market.get("feeRateBps")
    fee = int(fee_raw) if fee_raw is not None else 0
    if fee < 200:
        # 主网实测要求 feeRateBps>=200 且签进订单，否则被拒；登记时即告警便于排查
        print(f"⚠️ [market_registry] 市场 {market.get('id')} feeRateBps={fee}（<200），"
              f"该市场下单很可能被服务端拒绝。")
    cond = market.get("conditionId")
    tick = _tick_from_precision(market.get("decimalPrecision"), default_tick)

    ids = [str(o.get("onChainId")) for o in outcomes]

    # 是否存在明确的原生线索（indexSet==1 或 name=="yes"）；没有则退化为位置0原生
    has_explicit_native = any(_has_native_signal(o) for o in outcomes)

    metas: List[TokenMeta] = []
    for i, o in enumerate(outcomes):
        oid = str(o.get("onChainId"))
        if has_explicit_native:
            native = _has_native_signal(o)
        else:
            native = (i == 0)
        comp_id = None
        if len(ids) == 2:
            comp_id = next((x for x in ids if x != oid), None)
        idx = o.get("indexSet")
        metas.append(TokenMeta(
            token_id=oid,
            market_id=market_id,
            name=str(o.get("name") or ""),
            index_set=int(idx) if idx is not None else 0,
            is_complement=not native,
            complement_token_id=comp_id,
            neg_risk=neg,
            yield_bearing=yb,
            fee_rate_bps=fee,
            condition_id=cond,
            tick_size=tick,
        ))
    return metas
