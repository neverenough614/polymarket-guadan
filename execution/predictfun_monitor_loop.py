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
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from config.bot_config import cfg
from orderbook.analyzer import get_orderbook_info
from predictfun_data.monitor import decide_action, reward_active, backfill_need, NONE
from predictfun_data.churn_guard import ChurnGuard
from predictfun_data.placer import place_bid
from predictfun_data.auto_close import run_auto_close
from predictfun_data.defense import DefenseState, evaluate_defense
from predictfun_data.heat import tracker as heat_tracker


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
    heat: Any = None,
) -> Dict[str, Any]:
    """单 token：先盯订单簿变化防御，再常规维护买单（均受 churn 放行）。my_bid=我的买价(None=无单)。

    heat=None 时不碰热度（便于测试）；传入热度追踪器时：防御触发→记账升温，
    市场冻结→跳过重挂（避免在反复被攻击的市场一次次重挂被吃）。
    """
    mcfg = mcfg or cfg.predictfun_monitor
    token_id = token_info["token_id"]
    max_spread = token_info.get("max_spread")

    book, best_bid, best_ask, mid = get_orderbook_info(backend, token_id)

    # ── 奖励失效：市场奖励窗口已过 → 撤单 + 停挂（不在无奖励市场白挂被吃）──
    if not reward_active(token_info.get("reward_ends_at"), datetime.now(timezone.utc)):
        if my_bid is None:
            return {"token_id": token_id, "action": "REWARD_INACTIVE",
                    "reason": "奖励窗口已过 → 不补挂", "cancelled": False}
        if not churn.allow(token_id, now, count_as_cancel=True):
            return {"token_id": token_id, "action": "REWARD_ALERT",
                    "reason": "奖励已过但撤单预算用尽，下轮再撤", "cancelled": False}
        try:
            backend.cancel_all_asset(token_id)
            churn.record(token_id, now, count_as_cancel=True)
            if defense_state is not None:
                defense_state.reset()
        except Exception as e:
            return {"token_id": token_id, "action": "ERROR", "reason": f"reward-inactive cancel failed: {e}"}
        return {"token_id": token_id, "action": "REWARD_INACTIVE",
                "reason": "奖励窗口已过 → 撤单停挂", "cancelled": True}

    # ── 防御：盯订单簿深度变化（前墙消失/同档被吃/高水位/趋势/偏斜）──
    if (mcfg.enable_defense and defense_state is not None
            and book is not None and my_bid is not None):
        bids, asks = _sorted_tuples(book)
        my_size = max(100.0, float(token_info.get("min_size", 0) or 0))
        triggered, reasons = evaluate_defense(defense_state, bids, asks, my_bid, my_size, best_bid)
        if triggered:
            if heat is not None:
                heat.record_defense_trigger(token_id, token_info.get("question", ""))  # 升温/必要时冻结
            # 保命撤单：只看高位小时预算安全网，不看 token 冷却 → 危险立即撤（不再干等冷却）。
            # 撤后 record 会武装 token 冷却，那只约束“重挂”，下次防御撤单仍可立即执行。
            if not churn.budget_ok(now):
                # 仅当 1h 撤单预算耗尽（异常情形）→ 降级告警；此时热度多半已冻结该市场并轮换
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

    # 冻结市场：不重挂/不重心（停止参与，等冷却过；防在反复被打的市场里反复挂）
    if heat is not None and heat.is_frozen(token_id):
        return {"token_id": token_id, "action": "SKIPPED", "wanted": decision.action,
                "reason": "market frozen (heat)"}

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


def apply_reload(
    fresh_tokens: List[Dict[str, Any]],
    by_id: Dict[str, Dict[str, Any]],
    defense_states: Dict[str, "DefenseState"],
    backend: Any,
) -> Dict[str, Any]:
    """运行内重载：用新表刷新在监控 token 的奖励字段；表里已消失(下架/不再合格)的 → 撤单并移出监控。

    predict.fun 语义不同于 Polymarket：新市场不直接加入监控（受 target_count 预算约束），
    而是由轮换补位按效率挑——故这里只 刷新现有 + 移除下架，新增交给候选池/backfill。
    返回 {refreshed, removed, removed_keys}。撤单失败不致命（下轮 auto_close/轮换兜底）。
    """
    fresh_by_id = {str(t["token_id"]): t for t in (fresh_tokens or [])}
    refreshed = 0
    removed_keys: List[str] = []
    for tid in list(by_id.keys()):
        if tid in fresh_by_id:
            by_id[tid] = fresh_by_id[tid]      # 刷新 reward_ends_at / rewards_daily_rate / max_spread 等
            refreshed += 1
        else:
            removed_keys.append(tid)
            by_id.pop(tid, None)
            defense_states.pop(tid, None)
            try:
                backend.cancel_all_asset(tid)
            except Exception:
                pass
    return {"refreshed": refreshed, "removed": len(removed_keys), "removed_keys": removed_keys}


async def monitor_loop(
    backend: Any,
    strategy_tokens: List[Dict[str, Any]],
    churn: Optional[ChurnGuard] = None,
    now_fn: Optional[Callable[[], float]] = None,
    stop_event: Optional[asyncio.Event] = None,
    target_count: Optional[int] = None,
    backfill_fn: Optional[Callable[[set, int], List[Dict[str, Any]]]] = None,
    reload_fn: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    on_pool_reload: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
) -> None:
    """温和轮询监控循环。strategy_tokens 由 predict.fun strategy_loader 提供。

    target_count + backfill_fn 同时给定时启用**自动轮换补位**：市场因冻结/奖励失效掉出后，
    从候选里补效率最高的新市场，维持 target_count 个在挂（受预算 + churn + 补位冷却约束）。

    reload_fn 给定时启用**运行内重载**（对齐 Polymarket sheet_sync）：每隔
    mcfg.sheet_reload_interval_sec 重读策略表→刷新在监控 token 的奖励字段、撤掉下架市场；
    on_pool_reload(fresh) 用于把新表灌回候选池（供 backfill 看到新市场）。
    """
    import time
    mcfg = cfg.predictfun_monitor
    churn = churn or ChurnGuard(mcfg.token_cooldown_sec, mcfg.max_cancels_per_hour)
    now_fn = now_fn or time.time
    by_id = {str(t["token_id"]): t for t in strategy_tokens}
    defense_states: Dict[str, DefenseState] = {tid: DefenseState() for tid in by_id}
    close_attempts: Dict[str, int] = {}      # token_id → 连续未成交清仓轮数（卖价逐轮升级保成交）
    last_backfill = 0.0                       # 上次补位时间戳（受 backfill_cooldown_sec 约束）
    last_reload = now_fn()                    # 上次表重载时间戳（开局已是最新，隔 interval 才重载）
    # 防启动竞态重复挂单：_place_once 刚挂的单可能还没出现在 get_all_my_orders（predict.fun 最终一致性），
    # 首轮维护会误判"无单"→REFILL 再挂一张→重复。给初始 token 预置冷却，压住首轮重挂，
    # 待 token_cooldown_sec 后订单已可见再正常维护（补位新挂的 token 同理，在补位处补记）。
    _seed = now_fn()
    for _tid in by_id:
        churn.record(_tid, _seed, count_as_cancel=False)
    print(f"🛡️ [predict.fun 监控] 启动：{len(by_id)} 个 token，轮询 {mcfg.poll_interval_sec}s，"
          f"冷却 {mcfg.token_cooldown_sec}s，撤单预算 {mcfg.max_cancels_per_hour}/h，"
          f"防御={'开' if mcfg.enable_defense else '仅告警'}（盯订单簿变化）")

    if not hasattr(backend, "get_all_my_orders_grouped"):
        # 契约缺失时绝不能把所有 token 当"无单"处理（会触发全量补单 churn）→ 直接拒绝启动。
        raise RuntimeError("backend 缺少 get_all_my_orders_grouped，无法安全监控")

    while not (stop_event and stop_event.is_set()):
        try:
            # 安全网先行：清掉被吃出来的持仓（完整套 merge / 单边卖出），再维护挂单
            closing_tokens: set = set()
            try:
                # 用 lambda 带默认 0：close_attempts.get 对新 token 返回 None → decide 里 None*step 崩。
                cr = await asyncio.to_thread(
                    run_auto_close, backend, mcfg, lambda t: close_attempts.get(t, 0))
                if cr.get("merged") or cr.get("sold"):
                    print(f"   🧯 [auto_close] merge {cr['merged']} / sell {cr['sold']}")
                closing_tokens = set(cr.get("closed_tokens") or [])   # 本轮在清仓→跳过维护
                # 升级计数：本轮仍需卖出(未成交)的 token +1，已清掉的归零
                sold_now = {str(a.token_id) for a in cr.get("actions", []) if a.kind == "SELL"}
                for tid in sold_now:
                    close_attempts[tid] = close_attempts.get(tid, 0) + 1
                for tid in list(close_attempts):
                    if tid not in sold_now:
                        close_attempts.pop(tid, None)
            except Exception as e:
                print(f"⚠️ [auto_close] 本轮跳过：{e}")

            grouped = await asyncio.to_thread(backend.get_all_my_orders_grouped)
            acted = defended = 0
            for tid, token_info in by_id.items():
                if tid in closing_tokens:
                    continue                      # 清仓锁：正在平仓的 token 本轮不重挂/不重心
                info = grouped.get(tid, {})
                my_bid = info.get("best_bid")   # 我在该 token 上的买价(只挂买单)
                if my_bid is None and tid in defense_states:
                    defense_states[tid].reset()  # 无单→重置防御基线，避免拿旧墙误判
                res = await asyncio.to_thread(
                    evaluate_and_execute, backend, token_info, my_bid, churn, now_fn(),
                    mcfg, defense_states.get(tid), heat_tracker,
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

            # ── 运行内重载：每 interval 重读策略表，刷新奖励字段/候选池、撤掉下架市场 ──
            if reload_fn and (now_fn() - last_reload) >= mcfg.sheet_reload_interval_sec:
                try:
                    fresh = await asyncio.to_thread(reload_fn)
                    if fresh:
                        if on_pool_reload:           # 先灌候选池→下方轮换/backfill 看到新市场
                            on_pool_reload(fresh)
                        rr = await asyncio.to_thread(apply_reload, fresh, by_id, defense_states, backend)
                        print(f"   🔄 [重载] 表刷新：在监控 {rr['refreshed']} / 下架移除 {rr['removed']}"
                              f"（候选池 {len(fresh)} token）")
                    else:
                        print("   🔄 [重载] 表读到 0 token，保持原配置")
                except Exception as e:
                    print(f"⚠️ [重载] 本轮跳过：{e}")
                last_reload = now_fn()

            # ── 自动轮换补位：死市场(冻结/奖励失效)移出 → 用候选补满目标市场数 ──
            if target_count and backfill_fn:
                now_dt = datetime.now(timezone.utc)
                mkt_tokens: Dict[Any, List[str]] = defaultdict(list)
                for t_id in by_id:
                    meta = backend.meta_for(t_id)
                    mk = meta.market_id if meta is not None else ("u", t_id)
                    mkt_tokens[mk].append(t_id)
                market_dead = {}
                for mk, tids in mkt_tokens.items():
                    expired = any(not reward_active(by_id[x].get("reward_ends_at"), now_dt) for x in tids)
                    all_frozen = bool(tids) and all(heat_tracker.is_frozen(x) for x in tids)
                    market_dead[mk] = expired or all_frozen
                dead_keys, n_needed = backfill_need(market_dead, target_count)
                for mk in dead_keys:                      # 移出死市场（auto_close 全局清仓不受影响）
                    for x in mkt_tokens[mk]:
                        by_id.pop(x, None); defense_states.pop(x, None)
                        try:
                            await asyncio.to_thread(backend.cancel_all_asset, x)
                        except Exception:
                            pass
                    print(f"   ♻️ [轮换] 移出市场 {mk}（冻结/奖励失效），腾出名额")
                if n_needed > 0 and (now_fn() - last_backfill) >= mcfg.backfill_cooldown_sec:
                    exclude = {mk for mk in mkt_tokens if mk not in dead_keys}   # 现有活市场不重复补
                    try:
                        new_tokens = await asyncio.to_thread(backfill_fn, exclude, n_needed)
                    except Exception as e:
                        new_tokens = []
                        print(f"⚠️ [轮换] 补位失败：{e}")
                    for t in new_tokens or []:
                        ntid = str(t["token_id"])
                        by_id[ntid] = t
                        defense_states[ntid] = DefenseState()
                        churn.record(ntid, now_fn(), count_as_cancel=False)  # 同初挂：压住订单可见前的误重挂
                    if new_tokens:
                        print(f"   ♻️ [轮换] 补挂 {len(new_tokens)} 腿（目标 {target_count} 市场）")
                    last_backfill = now_fn()
        except Exception as e:
            print(f"❌ [predict.fun 监控] 轮询出错：{e}")
            import traceback
            traceback.print_exc()
        await asyncio.sleep(mcfg.poll_interval_sec)
