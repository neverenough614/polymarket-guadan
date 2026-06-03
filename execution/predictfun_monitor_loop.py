"""predict.fun 监控/防御循环（SP5）—— 稳挂少动，受 churn 双闸约束。

温和轮询（无 WS）：每轮拉一次我方挂单分组 + 各 token 当前簿，
用 predictfun_data.monitor.decide_action 给出克制建议，再经 ChurnGuard 放行后执行：
  REFILL  → 仅补缺失侧（不撤好单）
  RECENTER→ 撤该 token 双边后按当前 mid 重挂双边
其余一律不动。绝不抢档、不高频撤重挂（避免 predict.fun 反作弊清零）。

evaluate_and_execute 为同步、可注入 fake backend 的单 token 处理单元（便于测试）；
monitor_loop 仅做轮询调度。Polymarket 路径不受影响。
"""
import asyncio
from typing import Any, Callable, Dict, List, Optional

from config.bot_config import cfg
from orderbook.analyzer import (
    get_orderbook_info, analyze_best_place_price_from_book, calculate_dynamic_size,
)
from predictfun_data.monitor import decide_action, NONE
from predictfun_data.churn_guard import ChurnGuard


def _order_size(token_info: Dict[str, Any], book: Any, mid: Optional[float]) -> Optional[float]:
    """挂单量：满足 shareThreshold(min_size) 下限；优先用动态量，回退底量。"""
    base = max(100.0, float(token_info.get("min_size", 0) or 0))
    try:
        sz = calculate_dynamic_size(book, mid, base, volatility_sum=0.0,
                                    size_ratio=cfg.place.NORMAL_SIZE_RATIO,
                                    max_order_size=cfg.place.NORMAL_MAX_ORDER_SIZE)
        if sz:
            return sz
    except Exception:
        pass
    return base


def _place_side(backend: Any, token_info: Dict[str, Any], side: str,
                book: Any, mid: Optional[float]) -> Dict[str, Any]:
    """在 book 内为单侧挂一张奖励价带内的真实可成交单。"""
    token_id = token_info["token_id"]
    max_spread = token_info.get("max_spread")
    neg_risk = bool(token_info.get("neg_risk", False))
    size = _order_size(token_info, book, mid)
    if not size:
        return {"side": side, "status": "no_size"}
    res = analyze_best_place_price_from_book(book, side, max_spread, mid, size)
    if not res:
        return {"side": side, "status": "no_price"}
    price = res[0]
    resp = backend.create_order(token_id, side, price, size, neg_risk=neg_risk)
    ok = bool(resp) and resp.get("status") != "error"
    return {"side": side, "status": "placed" if ok else "failed",
            "price": price, "size": size, "resp": resp}


def evaluate_and_execute(
    backend: Any,
    token_info: Dict[str, Any],
    my_bid: Optional[float],
    my_ask: Optional[float],
    churn: ChurnGuard,
    now: float,
    mcfg=None,
) -> Dict[str, Any]:
    """单 token：评估并（受 churn 放行后）执行。返回结果字典（便于日志/测试）。"""
    mcfg = mcfg or cfg.predictfun_monitor
    token_id = token_info["token_id"]
    max_spread = token_info.get("max_spread")

    book, _bb, _ba, mid = get_orderbook_info(backend, token_id)
    decision = decide_action(
        my_bid, my_ask, mid, max_spread,
        deadband_ticks=mcfg.recenter_deadband_ticks,
        refill_on_fill=mcfg.refill_on_fill,
        min_two_sided=mcfg.min_two_sided,
    )
    if decision.action == NONE:
        return {"token_id": token_id, "action": NONE, "reason": decision.reason}

    if not churn.allow(token_id, now, count_as_cancel=decision.is_cancel):
        return {"token_id": token_id, "action": "SKIPPED", "wanted": decision.action,
                "reason": "churn cooldown/budget"}

    placed = []
    if decision.cancel_first:
        try:
            backend.cancel_all_asset(token_id)
        except Exception as e:
            return {"token_id": token_id, "action": "ERROR", "reason": f"cancel failed: {e}"}
        # 撤单一旦成功立即记账：即使后续补单抛错也绝不能漏记 churn，
        # 否则冷却未武装→下轮可能再撤→撤单循环→predict.fun 反作弊清零。
        churn.record(token_id, now, count_as_cancel=True)

    try:
        if decision.place_buy:
            placed.append(_place_side(backend, token_info, "BUY", book, mid))
        if decision.place_sell:
            placed.append(_place_side(backend, token_info, "SELL", book, mid))
    except Exception as e:
        # 补单失败不致命：撤单已记账、冷却已武装；下轮缺侧走 REFILL(不撤)，不会撤单循环。
        placed.append({"status": "error", "error": str(e)})

    if not decision.cancel_first:   # REFILL：纯补单，结束时记冷却(不计撤单预算)
        churn.record(token_id, now, count_as_cancel=False)
    return {"token_id": token_id, "action": decision.action, "reason": decision.reason,
            "placed": placed}


async def monitor_loop(
    backend: Any,
    strategy_tokens: List[Dict[str, Any]],
    churn: Optional[ChurnGuard] = None,
    now_fn: Optional[Callable[[], float]] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """温和轮询监控循环。strategy_tokens 由 predict.fun strategy_loader 提供。"""
    import time
    mcfg = cfg.predictfun_monitor
    churn = churn or ChurnGuard(mcfg.token_cooldown_sec, mcfg.max_cancels_per_hour)
    now_fn = now_fn or time.time
    by_id = {str(t["token_id"]): t for t in strategy_tokens}
    print(f"🛡️ [predict.fun 监控] 启动：{len(by_id)} 个 token，轮询 {mcfg.poll_interval_sec}s，"
          f"冷却 {mcfg.token_cooldown_sec}s，撤单预算 {mcfg.max_cancels_per_hour}/h（稳挂少动）")

    if not hasattr(backend, "get_all_my_orders_grouped"):
        # 契约缺失时绝不能把所有 token 当"无单"处理（会触发全量补单 churn）→ 直接拒绝启动。
        raise RuntimeError("backend 缺少 get_all_my_orders_grouped，无法安全监控")

    while not (stop_event and stop_event.is_set()):
        try:
            grouped = backend.get_all_my_orders_grouped()
            acted = 0
            for tid, token_info in by_id.items():
                info = grouped.get(tid, {})
                my_bid = info.get("best_bid")
                my_ask = info.get("best_ask")
                res = await asyncio.to_thread(
                    evaluate_and_execute, backend, token_info, my_bid, my_ask, churn, now_fn(), mcfg
                )
                if res.get("action") not in (NONE, "SKIPPED"):
                    acted += 1
                    print(f"   🛡️ [{res['action']}] {str(token_info.get('question'))[:36]} → {res.get('reason')}")
            if acted:
                print(f"🛡️ [predict.fun 监控] 本轮动作 {acted} 个（预算余 {churn.remaining_budget(now_fn())}/h）")
        except Exception as e:
            print(f"❌ [predict.fun 监控] 轮询出错：{e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(mcfg.poll_interval_sec)
