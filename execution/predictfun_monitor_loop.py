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
from orderbook.analyzer import get_orderbook_info
from predictfun_data.monitor import decide_action, NONE
from predictfun_data.churn_guard import ChurnGuard
from predictfun_data.placer import place_bid
from predictfun_data.auto_close import run_auto_close
from predictfun_data.defense import DefenseState, evaluate_defense


def _sorted_tuples(book):
    bids = sorted(((float(b.price), float(b.size)) for b in (book.bids or [])),
                  key=lambda x: x[0], reverse=True) if book else []
    asks = sorted(((float(a.price), float(a.size)) for a in (book.asks or [])),
                  key=lambda x: x[0]) if book else []
    return bids, asks


def evaluate_and_execute(
    backend: Any,
    token_info: Dict[str, Any],
    my_bid: Optional[float],
    churn: ChurnGuard,
    now: float,
    mcfg=None,
    defense_state: Optional[DefenseState] = None,
) -> Dict[str, Any]:
    """单 token：先盯订单簿变化防御，再常规维护买单（均受 churn 放行）。my_bid=我的买价(None=无单)。"""
    mcfg = mcfg or cfg.predictfun_monitor
    token_id = token_info["token_id"]
    max_spread = token_info.get("max_spread")

    book, best_bid, best_ask, mid = get_orderbook_info(backend, token_id)

    # ── 防御：盯订单簿深度变化（前墙消失/同档被吃/高水位/趋势/偏斜）──
    if (mcfg.enable_defense and defense_state is not None
            and book is not None and my_bid is not None):
        bids, asks = _sorted_tuples(book)
        my_size = max(100.0, float(token_info.get("min_size", 0) or 0))
        triggered, reasons = evaluate_defense(defense_state, bids, asks, my_bid, my_size, best_bid)
        if triggered:
            if not churn.allow(token_id, now, count_as_cancel=True):
                # 撤单预算/冷却用尽 → 降级为仅告警（不撤，防反作弊），下轮再判
                return {"token_id": token_id, "action": "DEFENSE_ALERT",
                        "reason": "; ".join(reasons), "defended": False}
            try:
                backend.cancel_all_asset(token_id)
                churn.record(token_id, now, count_as_cancel=True)
                defense_state.reset()
            except Exception as e:
                return {"token_id": token_id, "action": "ERROR", "reason": f"defense cancel failed: {e}"}
            # 撤后持仓由循环顶部 run_auto_close 处理；冷却到期后 REFILL 重挂
            return {"token_id": token_id, "action": "DEFEND", "reason": "; ".join(reasons),
                    "defended": True}

    decision = decide_action(
        my_bid, mid, max_spread,
        deadband_ticks=mcfg.recenter_deadband_ticks,
    )
    if decision.action == NONE:
        return {"token_id": token_id, "action": NONE, "reason": decision.reason}

    if not churn.allow(token_id, now, count_as_cancel=decision.is_cancel):
        return {"token_id": token_id, "action": "SKIPPED", "wanted": decision.action,
                "reason": "churn cooldown/budget"}

    if decision.cancel_first:
        try:
            backend.cancel_all_asset(token_id)
        except Exception as e:
            return {"token_id": token_id, "action": "ERROR", "reason": f"cancel failed: {e}"}
        # 撤单一旦成功立即记账：即使后续补单抛错也绝不能漏记 churn，
        # 否则冷却未武装→下轮可能再撤→撤单循环→predict.fun 反作弊清零。
        churn.record(token_id, now, count_as_cancel=True)

    try:
        placed = place_bid(backend, token_info, book)
    except Exception as e:
        # 补单失败不致命：撤单已记账、冷却已武装；下轮无单走 REFILL(不撤)，不会撤单循环。
        placed = {"status": "error", "error": str(e)}

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
    defense_states: Dict[str, DefenseState] = {tid: DefenseState() for tid in by_id}
    print(f"🛡️ [predict.fun 监控] 启动：{len(by_id)} 个 token，轮询 {mcfg.poll_interval_sec}s，"
          f"冷却 {mcfg.token_cooldown_sec}s，撤单预算 {mcfg.max_cancels_per_hour}/h，"
          f"防御={'开' if mcfg.enable_defense else '仅告警'}（盯订单簿变化）")

    if not hasattr(backend, "get_all_my_orders_grouped"):
        # 契约缺失时绝不能把所有 token 当"无单"处理（会触发全量补单 churn）→ 直接拒绝启动。
        raise RuntimeError("backend 缺少 get_all_my_orders_grouped，无法安全监控")

    while not (stop_event and stop_event.is_set()):
        try:
            # 安全网先行：清掉被吃出来的持仓（完整套 merge / 单边卖出），再维护挂单
            try:
                cr = await asyncio.to_thread(run_auto_close, backend)
                if cr.get("merged") or cr.get("sold"):
                    print(f"   🧯 [auto_close] merge {cr['merged']} / sell {cr['sold']}")
            except Exception as e:
                print(f"⚠️ [auto_close] 本轮跳过：{e}")

            grouped = await asyncio.to_thread(backend.get_all_my_orders_grouped)
            acted = defended = 0
            for tid, token_info in by_id.items():
                info = grouped.get(tid, {})
                my_bid = info.get("best_bid")   # 我在该 token 上的买价(只挂买单)
                if my_bid is None and tid in defense_states:
                    defense_states[tid].reset()  # 无单→重置防御基线，避免拿旧墙误判
                res = await asyncio.to_thread(
                    evaluate_and_execute, backend, token_info, my_bid, churn, now_fn(),
                    mcfg, defense_states.get(tid),
                )
                act = res.get("action")
                if act in ("DEFEND", "DEFENSE_ALERT"):
                    defended += 1
                    icon = "🛑" if act == "DEFEND" else "⚠️"
                    print(f"   {icon} [{act}] {str(token_info.get('question'))[:34]} → {res.get('reason')}")
                elif act not in (NONE, "SKIPPED"):
                    acted += 1
                    print(f"   🛡️ [{act}] {str(token_info.get('question'))[:36]} → {res.get('reason')}")
            if acted or defended:
                print(f"🛡️ [predict.fun 监控] 维护 {acted} / 防御 {defended}（撤单预算余 {churn.remaining_budget(now_fn())}/h）")
        except Exception as e:
            print(f"❌ [predict.fun 监控] 轮询出错：{e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(mcfg.poll_interval_sec)
