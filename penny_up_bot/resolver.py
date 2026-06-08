"""把「市场 + YES/NO」解析成具体的 outcome token_id（启动时一次性）。

三种输入（见 config.TokenConfig）：
  - token_id 直填：跳过解析，仅查 tick。
  - condition_id：CLOB get_market 拿 tokens / min_tick_size / neg_risk。
  - slug：Gamma API 拿 conditionId / clobTokenIds / outcomes / negRisk。

解析不出、市场找不到、outcome 不唯一匹配 → 抛异常（fail-fast，不启动）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, List, Tuple

import requests

from .config import GAMMA_HOST, Settings, TokenConfig


@dataclass(frozen=True)
class ResolvedToken:
    token_id: str
    tick: float
    neg_risk: bool
    cap_price: float
    total_size: float
    outcome: str
    label: str


# ---- 纯逻辑（可单测，无网络） ----

def match_outcome_token(tokens: List[Tuple[str, str]], outcome: str) -> str:
    """从 [(token_id, outcome_label), ...] 中按 outcome 唯一匹配出 token_id。"""
    want = outcome.strip().upper()
    matches = [tid for tid, oc in tokens if str(oc).strip().upper() == want]
    if len(matches) == 0:
        raise ValueError(f"市场里找不到 outcome={outcome}，可选：{[oc for _, oc in tokens]}")
    if len(matches) > 1:
        raise ValueError(f"outcome={outcome} 匹配到多个 token，无法判定：{matches}")
    return matches[0]


def parse_gamma_market(market: dict) -> Tuple[str, List[Tuple[str, str]], bool]:
    """从 Gamma market 字典解析出 (condition_id, [(token_id, outcome)], neg_risk)。"""
    outcomes = market.get("outcomes")
    clob_ids = market.get("clobTokenIds")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(clob_ids, str):
        clob_ids = json.loads(clob_ids)
    if not outcomes or not clob_ids or len(outcomes) != len(clob_ids):
        raise ValueError(f"Gamma market 数据异常：outcomes={outcomes} clobTokenIds={clob_ids}")
    tokens = [(str(tid), str(oc)) for tid, oc in zip(clob_ids, outcomes)]
    return market.get("conditionId"), tokens, bool(market.get("negRisk", False))


def _market_tokens(md: Any) -> List[Tuple[str, str]]:
    """从 CLOB MarketDetails（dataclass 或 dict）取 [(token_id, outcome)]。"""
    tokens = md.get("tokens") if isinstance(md, dict) else getattr(md, "tokens", None)
    out: List[Tuple[str, str]] = []
    for t in tokens or []:
        if isinstance(t, dict):
            out.append((str(t.get("token_id")), str(t.get("outcome"))))
        else:
            out.append((str(getattr(t, "token_id")), str(getattr(t, "outcome"))))
    return out


def _md_attr(md: Any, name: str, default=None):
    return md.get(name, default) if isinstance(md, dict) else getattr(md, name, default)


# ---- 解析入口（带网络） ----

def resolve(cfg: TokenConfig, client, settings: Settings) -> ResolvedToken:
    cfg.validate()

    if cfg.token_id:
        token_id = str(cfg.token_id)
        tick = client.get_tick_size(token_id) or settings.default_tick
        neg_risk = bool(cfg.neg_risk) if cfg.neg_risk is not None else False
        return ResolvedToken(
            token_id=token_id, tick=tick, neg_risk=neg_risk,
            cap_price=cfg.cap_price, total_size=cfg.total_size,
            outcome=cfg.outcome.upper(), label=cfg.label or token_id,
        )

    if cfg.condition_id:
        md = client.get_market(cfg.condition_id)
        tokens = _market_tokens(md)
        token_id = match_outcome_token(tokens, cfg.outcome)
        raw_tick = _md_attr(md, "min_tick_size")
        tick = float(raw_tick) if raw_tick else (client.get_tick_size(token_id) or settings.default_tick)
        neg_risk = bool(cfg.neg_risk) if cfg.neg_risk is not None else bool(_md_attr(md, "neg_risk", False))
        return ResolvedToken(
            token_id=token_id, tick=tick, neg_risk=neg_risk,
            cap_price=cfg.cap_price, total_size=cfg.total_size,
            outcome=cfg.outcome.upper(), label=cfg.label or cfg.condition_id,
        )

    # slug
    res = requests.get(f"{GAMMA_HOST}/markets", params={"slug": cfg.slug}, timeout=10)
    res.raise_for_status()
    markets = res.json()
    if not markets:
        raise ValueError(f"Gamma 找不到 slug={cfg.slug} 的市场")
    _, tokens, gamma_neg_risk = parse_gamma_market(markets[0])
    token_id = match_outcome_token(tokens, cfg.outcome)
    tick = client.get_tick_size(token_id) or settings.default_tick
    neg_risk = bool(cfg.neg_risk) if cfg.neg_risk is not None else gamma_neg_risk
    return ResolvedToken(
        token_id=token_id, tick=tick, neg_risk=neg_risk,
        cap_price=cfg.cap_price, total_size=cfg.total_size,
        outcome=cfg.outcome.upper(), label=cfg.label or cfg.slug,
    )
