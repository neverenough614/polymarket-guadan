"""
监控防御主循环：威胁检测、撤单、防御后重挂。
"""
import asyncio
import concurrent.futures
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.bot_config import cfg
from orderbook.analyzer import is_extreme_price_market
from defense.imbalance import check_book_imbalance
from execution.place_orders import place_order_for_token
from logging_utils import log_event, EVENT_DEFENSE_TRIGGERED, EVENT_ORDER_CANCELLED, EVENT_IMBALANCE_DETECTED


class MarketState:
    def __init__(self, question: str, token_type: str):
        self.question = question
        self.token_type = token_type
        self.my_bid_price: Optional[float] = None
        self.my_ask_price: Optional[float] = None
        self.my_order_size: float = 0.0
        self.last_bid_front_depth: float = 0
        self.last_bid_same_depth: float = 0
        self.last_ask_front_depth: float = 0
        self.last_ask_same_depth: float = 0
        self.bid_front_high_water: float = 0
        self.bid_same_high_water: float = 0
        self.ask_front_high_water: float = 0
        self.ask_same_high_water: float = 0
        self.first_run: bool = True

    def reset_high_water(self) -> None:
        self.bid_front_high_water = 0
        self.bid_same_high_water = 0
        self.ask_front_high_water = 0
        self.ask_same_high_water = 0


def _get_order_book_safe(backend: Any, token_id: str) -> Tuple[str, Any, Optional[str]]:
    try:
        book = backend.get_order_book(token_id)
        return token_id, book, None
    except Exception as e:
        return token_id, None, str(e)


def get_all_order_books_concurrent(backend: Any, token_ids: List[str]) -> Dict[str, Any]:
    dc = cfg.defense
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=dc.MAX_CONCURRENT_WORKERS) as executor:
        future_to_token = {
            executor.submit(_get_order_book_safe, backend, tid): tid
            for tid in token_ids
        }
        try:
            for future in concurrent.futures.as_completed(future_to_token, timeout=dc.ORDERBOOK_TIMEOUT + 2):
                tid = future_to_token[future]
                try:
                    returned_id, book, _ = future.result()
                    if book:
                        results[returned_id] = book
                except Exception:
                    pass
        except concurrent.futures.TimeoutError:
            unfinished = sum(1 for f in future_to_token if not f.done())
            if unfinished > 0:
                print(f"\n   ⚠️ {unfinished} 个盘口查询超时，已跳过（网络延迟）")
    return results


def calculate_layered_depth(
    book: Any,
    my_bid_price: Optional[float],
    my_ask_price: Optional[float],
) -> Tuple[float, float, float, float]:
    """返回 (bid_front, bid_same, ask_front, ask_same) 深度（USDC）"""
    bid_front = bid_same = ask_front = ask_same = 0.0
    if not book or not book.bids or not book.asks:
        return 0.0, 0.0, 0.0, 0.0
    if my_bid_price is not None:
        for bid in book.bids:
            price = float(bid.price)
            size = float(bid.size)
            depth = price * size
            if price > my_bid_price + 0.001:
                bid_front += depth
            elif abs(price - my_bid_price) < 0.001:
                bid_same += depth
    if my_ask_price is not None:
        for ask in book.asks:
            price = float(ask.price)
            size = float(ask.size)
            depth = price * size
            if price < my_ask_price - 0.001:
                ask_front += depth
            elif abs(price - my_ask_price) < 0.001:
                ask_same += depth
    return bid_front, bid_same, ask_front, ask_same


def _cancel_specific_token(backend: Any, token_id: str, question: str, token_type: str) -> bool:
    print(f"\n🧨 正在对 [{question[:30]}] 执行精准撤单...")
    try:
        backend.cancel_all_asset(token_id)
        print(f"✅ 已成功撤销 {token_type} ({token_id[:10]}...) 的所有挂单。")
        log_event(EVENT_ORDER_CANCELLED, "防御撤单", token_id=token_id, reason="危险信号")
        return True
    except Exception as e:
        print(f"⚠️ 撤单失败: {e}")
        return False


def _check_bid_threats(
    state: MarketState,
    my_bid_price: Optional[float],
    bid_front: float,
    bid_same: float,
) -> Tuple[bool, List[str]]:
    dc = cfg.defense
    reasons = []
    triggered = False
    if my_bid_price is None:
        return False, []
    was_behind_wall = state.last_bid_front_depth > dc.MIN_FRONT_DEPTH_THRESHOLD
    now_exposed = bid_front <= dc.MIN_FRONT_DEPTH_THRESHOLD
    if was_behind_wall and now_exposed:
        drop_pct = (1 - bid_front / state.last_bid_front_depth) * 100 if state.last_bid_front_depth > 0 else 100
        reasons.append(f"🚨 [跨分支] 买单前墙消失！前墙: ${state.last_bid_front_depth:.0f}→${bid_front:.0f} (-{drop_pct:.0f}%)")
        triggered = True
    if bid_front < dc.MIN_FRONT_DEPTH_ABSOLUTE and state.bid_front_high_water > dc.MIN_FRONT_DEPTH_ABSOLUTE_REF:
        reasons.append(f"🚨 [绝对兜底] 买单前墙极度危险！当前: ${bid_front:.0f} (历史最高: ${state.bid_front_high_water:.0f})")
        triggered = True
    if state.bid_front_high_water > dc.MIN_FRONT_DEPTH_THRESHOLD and bid_front < state.bid_front_high_water * (1 - dc.THRESHOLD_FRONT_HIGH_WATER_DROP):
        reasons.append(f"🚨 [高水位] 买单前墙累计大幅下跌！高水位: ${state.bid_front_high_water:.0f}→当前: ${bid_front:.0f}")
        triggered = True
    if bid_front > dc.MIN_FRONT_DEPTH_THRESHOLD:
        if state.last_bid_front_depth > dc.MIN_FRONT_DEPTH_THRESHOLD and bid_front < state.last_bid_front_depth * (1 - dc.THRESHOLD_FRONT_DEPTH_DROP):
            reasons.append(f"🚨 [单轮] 买单前墙塌陷！${state.last_bid_front_depth:.0f}→${bid_front:.0f}")
            triggered = True
    else:
        if bid_same < dc.MIN_SAME_DEPTH_SAFE:
            reasons.append(f"🚨 [第一档] 买单深度太薄！同档: ${bid_same:.0f}")
            triggered = True
        elif state.last_bid_same_depth > dc.MIN_SAME_DEPTH_SAFE and bid_same < state.last_bid_same_depth * (1 - dc.THRESHOLD_SAME_DEPTH_DROP):
            reasons.append(f"🚨 [第一档] 买单被大量吃掉！${state.last_bid_same_depth:.0f}→${bid_same:.0f}")
            triggered = True
        if state.bid_same_high_water > dc.MIN_SAME_DEPTH_SAFE and bid_same < state.bid_same_high_water * (1 - dc.THRESHOLD_SAME_HIGH_WATER_DROP):
            reasons.append(f"🚨 [高水位] 第一档买单累计被吃！高水位: ${state.bid_same_high_water:.0f}→当前: ${bid_same:.0f}")
            triggered = True
    return triggered, reasons


def _check_ask_threats(
    state: MarketState,
    my_ask_price: Optional[float],
    ask_front: float,
    ask_same: float,
) -> Tuple[bool, List[str]]:
    dc = cfg.defense
    reasons = []
    triggered = False
    if my_ask_price is None:
        return False, []
    was_behind_wall = state.last_ask_front_depth > dc.MIN_FRONT_DEPTH_THRESHOLD
    now_exposed = ask_front <= dc.MIN_FRONT_DEPTH_THRESHOLD
    if was_behind_wall and now_exposed:
        drop_pct = (1 - ask_front / state.last_ask_front_depth) * 100 if state.last_ask_front_depth > 0 else 100
        reasons.append(f"🚨 [跨分支] 卖单前墙消失！前墙: ${state.last_ask_front_depth:.0f}→${ask_front:.0f} (-{drop_pct:.0f}%)")
        triggered = True
    if ask_front < dc.MIN_FRONT_DEPTH_ABSOLUTE and state.ask_front_high_water > dc.MIN_FRONT_DEPTH_ABSOLUTE_REF:
        reasons.append(f"🚨 [绝对兜底] 卖单前墙极度危险！当前: ${ask_front:.0f} (历史最高: ${state.ask_front_high_water:.0f})")
        triggered = True
    if state.ask_front_high_water > dc.MIN_FRONT_DEPTH_THRESHOLD and ask_front < state.ask_front_high_water * (1 - dc.THRESHOLD_FRONT_HIGH_WATER_DROP):
        reasons.append(f"🚨 [高水位] 卖单前墙累计大幅下跌！高水位: ${state.ask_front_high_water:.0f}→当前: ${ask_front:.0f}")
        triggered = True
    if ask_front > dc.MIN_FRONT_DEPTH_THRESHOLD:
        if state.last_ask_front_depth > dc.MIN_FRONT_DEPTH_THRESHOLD and ask_front < state.last_ask_front_depth * (1 - dc.THRESHOLD_FRONT_DEPTH_DROP):
            reasons.append(f"🚨 [单轮] 卖单前墙塌陷！${state.last_ask_front_depth:.0f}→${ask_front:.0f}")
            triggered = True
    else:
        if ask_same < dc.MIN_SAME_DEPTH_SAFE:
            reasons.append(f"🚨 [第一档] 卖单深度太薄！同档: ${ask_same:.0f}")
            triggered = True
        elif state.last_ask_same_depth > dc.MIN_SAME_DEPTH_SAFE and ask_same < state.last_ask_same_depth * (1 - dc.THRESHOLD_SAME_DEPTH_DROP):
            reasons.append(f"🚨 [第一档] 卖单被大量吃掉！${state.last_ask_same_depth:.0f}→${ask_same:.0f}")
            triggered = True
        if state.ask_same_high_water > dc.MIN_SAME_DEPTH_SAFE and ask_same < state.ask_same_high_water * (1 - dc.THRESHOLD_SAME_HIGH_WATER_DROP):
            reasons.append(f"🚨 [高水位] 第一档卖单累计被吃！高水位: ${state.ask_same_high_water:.0f}→当前: ${ask_same:.0f}")
            triggered = True
    return triggered, reasons


async def monitor_defense_loop(backend: Any, strategy_tokens: List[Dict]) -> None:
    dc = cfg.defense
    ic = cfg.imbalance
    print(f"\n{'='*60}")
    print(f"🛡️  [监控防御] 启动中...")
    print(f"    ⚙️  自动防御: {dc.ENABLE_AUTO_DEFENSE}")
    print(f"    ⚖️  偏斜检测: {ic.ENABLE_IMBALANCE_DETECTION} (阈值: {ic.IMBALANCE_THRESHOLD:.0%})")
    print(f"    ⏱️  扫描间隔: {dc.MONITOR_CHECK_INTERVAL}秒")
    print(f"{'='*60}\n")

    market_states: Dict[str, MarketState] = {}
    scan_count = 0

    while True:
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            scan_count += 1
            loop_start = time.time()
            current_tokens = list(strategy_tokens)

            for t in current_tokens:
                if t["token_id"] not in market_states:
                    market_states[t["token_id"]] = MarketState(t["question"], t["token_type"])

            all_orders = backend.get_all_my_orders_grouped() if hasattr(backend, "get_all_my_orders_grouped") else {}

            known_token_ids = {t["token_id"] for t in current_tokens}
            for token_id in all_orders:
                if token_id not in known_token_ids:
                    manual_token = {
                        "token_id": token_id,
                        "token_type": "MANUAL",
                        "question": f"手动挂单 ({token_id[:10]}...)",
                        "min_size": 10.0,
                        "neg_risk": False,
                        "max_spread": None,
                        "volatility_sum": 0.0,
                        "source": "manual_detected",
                    }
                    current_tokens.append(manual_token)
                    known_token_ids.add(token_id)
                    if token_id not in market_states:
                        market_states[token_id] = MarketState(manual_token["question"], manual_token["token_type"])

            def _get_prices(tid: str):
                info = all_orders.get(tid)
                if info is None:
                    return None, None
                return info.get("best_bid"), info.get("best_ask")

            active_targets = [t for t in current_tokens if _get_prices(t["token_id"]) != (None, None)]

            if not active_targets:
                loop_time = time.time() - loop_start
                print(f"\r[ {timestamp} ] 🛡️ 扫描 #{scan_count} | 无活跃挂单 | 监控: {len(current_tokens)} | 耗时: {loop_time:.2f}s", end="", flush=True)
                await asyncio.sleep(dc.MONITOR_CHECK_INTERVAL)
                continue

            active_token_ids = [t["token_id"] for t in active_targets]
            all_books = await asyncio.to_thread(get_all_order_books_concurrent, backend, active_token_ids)

            for t in active_targets:
                token_id = t["token_id"]
                state = market_states[token_id]
                order_info = all_orders.get(token_id, {})
                my_bid_price = order_info.get("best_bid") if isinstance(order_info, dict) else None
                my_ask_price = order_info.get("best_ask") if isinstance(order_info, dict) else None
                book = all_books.get(token_id)
                if not book:
                    continue

                bids_sorted = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
                best_bid_price = float(bids_sorted[0].price) if bids_sorted else None
                if best_bid_price is not None and is_extreme_price_market(best_bid_price):
                    has_bid = my_bid_price is not None
                    has_ask = my_ask_price is not None
                    if has_bid != has_ask:
                        lone_side = "买单" if has_bid else "卖单"
                        print(f"\n⚠️ [孤单检测] [{t['token_type']}] {t['question'][:40]}...")
                        print(f"   极端价格市场(best_bid={best_bid_price:.3f})，仅有{lone_side}，双向缺一无奖励")
                        print(f"   🧨 撤销孤立{lone_side}...")
                        await asyncio.to_thread(_cancel_specific_token, backend, token_id, t["question"], t["token_type"])
                        state.first_run = True
                        state.reset_high_water()
                        continue

                state.my_bid_price = my_bid_price
                state.my_ask_price = my_ask_price

                if ic.ENABLE_IMBALANCE_DETECTION and not state.first_run:
                    cancel_bid, cancel_ask, imbalance_reason = check_book_imbalance(book, my_bid_price, my_ask_price)
                    if cancel_bid or cancel_ask:
                        print(f"\n\n{'⚖'*10} 买卖深度偏斜检测 {'⚖'*10}")
                        print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        print(f"🎯 目标: [{t['token_type']}] {t['question'][:45]}...")
                        print(f"   {imbalance_reason}")
                        log_event(EVENT_IMBALANCE_DETECTED, imbalance_reason or "", token_id=token_id)
                        if dc.ENABLE_AUTO_DEFENSE:
                            cached_bid_ids = order_info.get("bid_ids", []) if isinstance(order_info, dict) else []
                            cached_ask_ids = order_info.get("ask_ids", []) if isinstance(order_info, dict) else []
                            if cancel_bid:
                                ok = backend.cancel_one_side(token_id, "BUY", cached_bid_ids)
                                if ok:
                                    print(f"   ✅ 已撤销 {t['question'][:30]}... 的买单")
                                else:
                                    print(f"   ℹ️ {t['question'][:30]}... 无活跃买单可撤")
                            if cancel_ask:
                                ok = backend.cancel_one_side(token_id, "SELL", cached_ask_ids)
                                if ok:
                                    print(f"   ✅ 已撤销 {t['question'][:30]}... 的卖单")
                                else:
                                    print(f"   ℹ️ {t['question'][:30]}... 无活跃卖单可撤")
                            state.first_run = True
                            state.reset_high_water()
                        else:
                            print("   ⚠️ 防御未开启，仅报警")
                        print("⚖" * 30)
                        continue

                order_size = t.get("order_size") or t.get("min_size", 500.0)
                state.my_order_size = order_size

                bid_front, bid_same, ask_front, ask_same = calculate_layered_depth(book, my_bid_price, my_ask_price)

                if my_bid_price is not None and my_bid_price > 0:
                    bid_same = max(0, bid_same - order_size * my_bid_price)
                if my_ask_price is not None and my_ask_price > 0:
                    ask_same = max(0, ask_same - order_size * my_ask_price)

                state.bid_front_high_water = max(state.bid_front_high_water, bid_front)
                state.bid_same_high_water = max(state.bid_same_high_water, bid_same)
                state.ask_front_high_water = max(state.ask_front_high_water, ask_front)
                state.ask_same_high_water = max(state.ask_same_high_water, ask_same)

                trigger_reasons: List[str] = []
                triggered = False

                if not state.first_run:
                    bid_triggered, bid_reasons = _check_bid_threats(state, my_bid_price, bid_front, bid_same)
                    ask_triggered, ask_reasons = _check_ask_threats(state, my_ask_price, ask_front, ask_same)
                    if bid_triggered:
                        triggered = True
                        trigger_reasons.extend(bid_reasons)
                    if ask_triggered:
                        triggered = True
                        trigger_reasons.extend(ask_reasons)

                state.last_bid_front_depth = bid_front
                state.last_bid_same_depth = bid_same
                state.last_ask_front_depth = ask_front
                state.last_ask_same_depth = ask_same
                state.first_run = False

                if triggered:
                    print(f"\n\n{'!'*20} ⚡ 检测到危险信号 ⚡ {'!'*20}")
                    print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"🎯 目标: {state.question[:50]}")
                    print(f"🆔 Token: {state.token_type} ({token_id[:10]}...)")
                    for i, reason in enumerate(trigger_reasons, 1):
                        print(f"  [{i}] {reason}")
                    log_event(EVENT_DEFENSE_TRIGGERED, "; ".join(trigger_reasons), token_id=token_id, reason="depth_threats")
                    if dc.ENABLE_AUTO_DEFENSE:
                        await asyncio.to_thread(_cancel_specific_token, backend, token_id, state.question, state.token_type)
                        state.first_run = True
                        state.reset_high_water()

                        # 🔄 非阻塞：spawn 独立任务处理等待+重挂，主循环立即继续监控其他 token
                        token_info_replace = next((x for x in current_tokens if x["token_id"] == token_id), None)
                        if token_info_replace:
                            async def _delayed_replace(backend_ref, token_info, question):
                                try:
                                    print(f"   ⏳ 60s 后尝试重挂: {question[:40]}...")
                                    await asyncio.sleep(60)
                                    print(f"🔄 [防御后重挂] 正在重新检验挂单条件: {question[:40]}...")
                                    replace_result = await asyncio.to_thread(place_order_for_token, backend_ref, token_info)
                                    buy_ok = replace_result.get("buy_status") == "placed"
                                    sell_ok = replace_result.get("sell_status") == "placed"
                                    if buy_ok or sell_ok:
                                        buy_info = f"买{replace_result['buy_tier']}(${replace_result['buy_price']:.3f})" if buy_ok else "买单跳过"
                                        sell_info = f"卖{replace_result['sell_tier']}(${replace_result['sell_price']:.3f})" if sell_ok else "卖单跳过"
                                        print(f"✅ [防御后重挂] 重新挂单成功: {buy_info} | {sell_info}")
                                    else:
                                        print(f"⚠️ [防御后重挂] 条件不满足，暂不挂单（{replace_result.get('error', '深度不足')}）")
                                except Exception as e:
                                    print(f"⚠️ [防御后重挂] 异常: {e}")
                            asyncio.create_task(_delayed_replace(backend, token_info_replace, state.question))
                    else:
                        print("⚠️ 防御未开启，仅报警")
                    print("!" * 70)

            loop_time = time.time() - loop_start
            print(f"\r[ {timestamp} ] 🛡️ 扫描 #{scan_count} | 活跃: {len(active_targets)}/{len(current_tokens)} | 耗时: {loop_time:.2f}s", end="", flush=True)
            sleep_time = max(0.1, dc.MONITOR_CHECK_INTERVAL - loop_time)
            await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            print("\n🛑 [监控防御] 任务已取消")
            break
        except Exception as e:
            print(f"\n❌ [监控防御] 运行时错误: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)
