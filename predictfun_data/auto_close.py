"""predict.fun 自动清仓安全网（仿 Polymarket auto_close_positions_task，按 predict.fun 适配）。

买 YES + 买 NO 双边做市，成交后会产生持仓：
  - 双腿都成交 → 持有等量 YES+NO = 完整套 → **merge 回 USDT**（无滑点，最优；
    SDK 要求两 outcome 等量，故 amount=min(held_yes, held_no)，单位 wei）。
  - 仅单腿成交 → 持有单边 outcome → **以 best_bid−offset 限价卖出**平仓
    （注意：此时已持有份额，卖出非裸卖、合法；裸卖才被 predict.fun 拒）。

decide_close_actions 为纯函数（按 condition_id 归集双边、决定 merge/sell），便于单测；
run_auto_close 负责 IO（拉持仓/撤买单/执行 merge 与 sell）。

⚠️ 持仓字段名/单位以首次真实成交校验为准：本模块对常见字段与 wei/份额做防御解析。
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.bot_config import cfg
from . import units

MERGE = "MERGE"
SELL = "SELL"


@dataclass(frozen=True)
class CloseAction:
    kind: str                       # MERGE | SELL
    # MERGE
    condition_id: Optional[str] = None
    amount_shares: float = 0.0      # 每个 outcome 合并的份额（执行时转 wei）
    neg_risk: bool = False
    yield_bearing: bool = False
    # SELL
    token_id: Optional[str] = None
    price: float = 0.0
    size: float = 0.0
    depth_sufficient: bool = True   # 卖价处簿能否吃下全部 size（False=承接不足，尽力卖）
    # 通用
    reason: str = ""


def absorbing_sell_price(
    bids: List[Any],
    size: float,
    base_offset: float,
    max_drop: float,
    tick: float,
) -> Tuple[Optional[float], bool]:
    """走簿求"能被承接"的卖价：从买一往下累计深度，到累计≥size 的那一档即可吃下。

    bids: [(price,size)] 降序。返回 (sell_price, depth_sufficient)。
    在 best−max_drop 内吃不下→返回该地板价 + False（尽力卖、调用方告警），防卖穿到地板。
    """
    if not bids:
        return None, False
    best = bids[0][0]
    floor = max(tick, best - max_drop)
    cum = 0.0
    for price, sz in bids:
        if price < floor:
            break
        cum += sz
        if cum >= size:                       # 这一档（及以上）累计能吃下全部 size
            return max(floor, price - base_offset), True   # 让价不穿 max_drop 地板（升级时也受此约束）
    return floor, False                        # max_drop 内吃不下 → 地板价尽力卖


def decide_close_actions(
    positions: Dict[str, float],
    meta_of: Callable[[str], Any],
    bids_of: Callable[[str], List[Any]],
    *,
    min_close: float,
    sell_offset: float,
    max_drop: float = 0.10,
    tick: float = 0.01,
    attempts_of: Optional[Callable[[str], int]] = None,
    escalate_step: float = 0.0,
) -> List[CloseAction]:
    """positions: token_id→持仓份额。返回清仓动作列表（先 merge 完整套，再卖残余单边）。

    按 condition_id 归集二元市场两腿；每个市场只处理一次。merge 量=min(两腿)，
    残余单边走簿求承接卖价（absorbing_sell_price）。无 meta / 无买档的单边持仓跳过（不冒进）。
    attempts_of(token_id)：该仓连续未成交轮数 → 让价升级 sell_offset + n×escalate_step
    （封顶 max_drop），保证被吃后的止损单逐轮更激进、最终成交。
    """
    actions: List[CloseAction] = []
    consumed: Dict[str, float] = {}     # token_id → 已被 merge 消耗的份额
    done_conditions: set = set()

    # Pass 1：按 condition 归集双边，决定 merge（两腿各消耗 set_amount）
    for token_id, raw_size in positions.items():
        size = float(raw_size or 0)
        if size < min_close:
            continue
        meta = meta_of(token_id)
        cond = getattr(meta, "condition_id", None) if meta else None
        comp = getattr(meta, "complement_token_id", None) if meta else None
        if not cond or cond in done_conditions:
            continue
        comp_size = float(positions.get(comp, 0) or 0) if comp else 0.0
        set_amount = min(size, comp_size)
        if set_amount >= min_close:
            actions.append(CloseAction(
                kind=MERGE, condition_id=cond, amount_shares=set_amount,
                neg_risk=bool(getattr(meta, "neg_risk", False)),
                yield_bearing=bool(getattr(meta, "yield_bearing", False)),
                reason=f"complete_set×{set_amount:.0f}→merge",
            ))
            done_conditions.add(cond)
            consumed[token_id] = consumed.get(token_id, 0.0) + set_amount
            if comp:
                consumed[comp] = consumed.get(comp, 0.0) + set_amount

    # Pass 2：残余单边（持仓 − 已 merge 消耗）走簿求承接卖价
    for token_id, raw_size in positions.items():
        residual = float(raw_size or 0) - consumed.get(token_id, 0.0)
        if residual < min_close:
            continue
        meta = meta_of(token_id)
        bids = bids_of(token_id)
        if not bids:
            continue                     # 无买档不冒进卖出
        # attempts_of 可能是 dict.get（缺省返回 None）→ 对 None/缺失一律按 0，否则 None*float 崩
        # （曾致 live 每轮 auto_close 抛 TypeError 被吞、从不清仓；close 单跑不传 attempts_of 故未暴露）。
        n = (attempts_of(token_id) or 0) if attempts_of else 0
        eff_offset = min(sell_offset + n * escalate_step, max_drop)   # 连续未成交→逐轮更激进（封顶 max_drop）
        price, sufficient = absorbing_sell_price(bids, residual, eff_offset, max_drop, tick)
        if price is None:
            continue
        price = units.price_to_tick(price, tick)
        esc = f"·第{n+1}轮让{eff_offset:.2f}" if n else ""
        note = ("ok" if sufficient else f"承接不足(最多让{max_drop})尽力卖") + esc
        actions.append(CloseAction(
            kind=SELL, token_id=token_id, price=price, size=residual,
            neg_risk=bool(getattr(meta, "neg_risk", False)) if meta else False,
            depth_sufficient=sufficient,
            reason=f"residual×{residual:.0f}@{price:.3f}({note})",
        ))
    return actions


# ---------- 持仓解析（字段名/单位已用主网首次真实成交校验）----------
# 实测 /v1/positions 单条结构：{amount:wei字符串, outcome:{onChainId,name,...}, market:{conditionId,...},
#   id:base64游标(非token!), averageBuyPriceUsd, pnlUsd, valueUsd}。token id 在嵌套 outcome.onChainId。
def _pos_token_id(p: Dict[str, Any]) -> str:
    direct = (p.get("token_id") or p.get("asset") or p.get("asset_id")
              or p.get("tokenId") or p.get("outcomeId") or p.get("onChainId"))
    if direct:
        return str(direct)
    out = p.get("outcome")            # 主网实测：token id 嵌在 outcome.onChainId（顶层 id 是游标，勿用）
    if isinstance(out, dict):
        oid = out.get("onChainId") or out.get("tokenId") or out.get("outcomeId") or out.get("id")
        if oid:
            return str(oid)
    return ""


def _pos_size(p: Dict[str, Any]) -> float:
    raw = (p.get("size") if p.get("size") is not None else
           p.get("quantity") if p.get("quantity") is not None else
           p.get("amount") if p.get("amount") is not None else
           p.get("balance"))
    if raw is None:
        return 0.0
    try:
        # 单位防御：份额量级 1–1e7，wei 量级 ≥1e16；统一以 1e10 为界（两边都不会误判）
        s = str(raw).strip()
        if s.lstrip("-").isdigit():
            iv = int(s)
            return units.from_wei(iv) if iv >= 10 ** 10 else float(iv)
        val = float(s)
        return units.from_wei(int(val)) if val >= 1e10 else val
    except (ValueError, TypeError):
        return 0.0


def positions_to_map(rows: List[Dict[str, Any]], min_close: float = 0.0) -> Dict[str, float]:
    """原始持仓行 → {token_id: 份额}（过滤 < min_close 与空 id）。"""
    out: Dict[str, float] = {}
    for p in rows or []:
        tid = _pos_token_id(p)
        if not tid:
            continue
        size = _pos_size(p)
        if size >= min_close and size > 0:
            out[tid] = out.get(tid, 0.0) + size
    return out


def _order_ok(res: Any) -> bool:
    """create_order 成功判定：成功返回 {"status":"live"...}，失败返回 {"status":"error"...}。

    宽松接受常见成功标记，明确拒绝 error/空；非 dict（不应发生）按失败处理，绝不误记为成交。
    """
    if not isinstance(res, dict):
        return False
    status = str(res.get("status", "")).lower()
    if status in ("live", "placed", "matched", "ok", "success", "filled"):
        return True
    if status == "error" or "error" in res:
        return False
    return bool(res.get("order_id") or res.get("hash"))   # 无 status 但有单号也算成功


def run_auto_close(backend: Any, mc=None, attempts_of: Optional[Callable[[str], int]] = None) -> Dict[str, Any]:
    """拉持仓→决定动作→撤相关买单→执行 merge/sell。返回执行摘要。

    attempts_of(token_id)：该仓连续未成交轮数（由监控循环维护），用于卖价逐轮升级保成交。
    """
    mc = mc or cfg.predictfun_monitor
    client = backend.raw_client
    rows = backend.get_all_positions()
    # get_all_positions 可能返回 DataFrame 或 list[dict]
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    positions = positions_to_map(rows, min_close=0.0)
    if not positions:
        return {"merged": 0, "sold": 0, "actions": [], "closed_tokens": []}

    def bids_of(tid: str) -> List[Any]:
        try:
            book = backend.get_order_book(tid)
            return sorted(((float(b.price), float(b.size)) for b in (book.bids or [])),
                          key=lambda x: x[0], reverse=True) if book else []
        except Exception:
            return []

    actions = decide_close_actions(
        positions, backend.meta_for, bids_of,
        min_close=mc.close_min_position, sell_offset=mc.close_offset, max_drop=mc.close_max_drop,
        attempts_of=attempts_of, escalate_step=getattr(mc, "close_escalate_step", 0.0),
    )
    merged = sold = 0
    closed_tokens: set = set()      # 本轮在清仓的 token（含被撤买单的对手腿）→ 监控本轮跳过，防边清边买
    for a in actions:
        try:
            if a.kind == MERGE:
                client.merge_positions(
                    a.condition_id, units.shares_to_wei(a.amount_shares),
                    is_neg_risk=a.neg_risk, is_yield_bearing=a.yield_bearing,
                )
                merged += 1
                print(f"   🧬 [merge] cond={str(a.condition_id)[:12]} ×{a.amount_shares:.0f} → USDT")
            elif a.kind == SELL:
                backend.cancel_all_asset(a.token_id)   # 撤掉该 token 买单，防边清边买
                closed_tokens.add(str(a.token_id))
                # 同时撤对手腿买单：否则卖掉残余后对手买单成交→重新累积单边持仓
                meta = backend.meta_for(a.token_id)
                comp = getattr(meta, "complement_token_id", None) if meta else None
                if comp:
                    backend.cancel_all_asset(comp)
                    closed_tokens.add(str(comp))
                # create_order API 失败只返回 {"status":"error"} 不抛异常 → 必须检查返回，
                # 否则失败也会被记成 sold（曾导致"显示卖出实际没成交"）。
                res = backend.create_order(a.token_id, "SELL", a.price, a.size, neg_risk=a.neg_risk)
                if not _order_ok(res):
                    err = res.get("error") if isinstance(res, dict) else res
                    print(f"   ❌ [sell 失败] {str(a.token_id)[:12]} ×{a.size:.0f}@{a.price:.3f} → {str(err)[:140]}")
                    continue
                sold += 1
                warn = "" if a.depth_sufficient else " ⚠️承接不足"
                oid = res.get("order_id") or res.get("hash") or "" if isinstance(res, dict) else ""
                print(f"   💰 [sell] {str(a.token_id)[:12]} ×{a.size:.0f}@{a.price:.3f}{warn}"
                      f"{(' id=' + str(oid)[:14]) if oid else ''}")
        except Exception as e:
            print(f"   ⚠️ [auto_close] {a.kind} 失败: {str(e)[:80]}")
    return {"merged": merged, "sold": sold, "actions": actions, "closed_tokens": list(closed_tokens)}
