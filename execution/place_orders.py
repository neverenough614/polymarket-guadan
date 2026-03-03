"""
挂单逻辑与插队检测。
使用 IExecutionBackend 执行下单/撤单，保持行为不变。
"""
import asyncio
import concurrent.futures
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.bot_config import cfg
from orderbook.analyzer import (
    get_orderbook_info,
    analyze_best_place_price_from_book,
    calculate_dynamic_size,
    is_extreme_price_market,
)
from logging_utils import log_event, EVENT_ORDER_PLACED, EVENT_SPREAD_REBALANCED, EVENT_DEPTH_INSUFFICIENT


def place_order_for_token(backend: Any, token_info: Dict) -> Dict:
    """对单个 token 执行挂单。backend 需实现 IExecutionBackend。"""
    pc = cfg.place
    token_id = token_info["token_id"]
    token_type = token_info["token_type"]
    question = token_info["question"]
    neg_risk = token_info.get("neg_risk", False)
    max_spread = token_info.get("max_spread", None)

    raw_min_size = token_info["min_size"]
    base_min_size = max(100.0, raw_min_size)

    result = {
        "token_id": token_id, "token_type": token_type, "question": question,
        "min_size": base_min_size, "buy_status": "skipped", "sell_status": "skipped",
        "buy_price": None, "sell_price": None, "buy_tier": None, "sell_tier": None,
        "extreme_price": False, "error": None, "mid": None, "max_spread": max_spread,
        "order_size": None,
    }

    try:
        book, best_bid, best_ask, mid = get_orderbook_info(backend, token_id)
        result["mid"] = mid

        source = token_info.get("source", "Normal LP")
        vol_sum = token_info.get("volatility_sum", 0.0)
        if source == "High Reward":
            sr, mos = pc.AGGRESSIVE_SIZE_RATIO, pc.AGGRESSIVE_MAX_ORDER_SIZE
        elif source == "Normal LP":
            sr, mos = pc.NORMAL_SIZE_RATIO, pc.NORMAL_MAX_ORDER_SIZE
        else:
            sr, mos = pc.DYNAMIC_SIZE_RATIO, pc.MAX_ORDER_SIZE

        order_size = calculate_dynamic_size(
            book, mid, base_min_size, volatility_sum=vol_sum,
            size_ratio=sr, max_order_size=mos,
        )
        result["order_size"] = order_size
        if order_size is None:
            result["buy_status"] = "depth_insufficient"
            result["sell_status"] = "depth_insufficient"
            result["error"] = f"前三档深度不足以支撑最小奖励挂单量 {base_min_size:.0f} shares，跳过"
            log_event(EVENT_DEPTH_INSUFFICIENT, result["error"], token_id=token_id)
            return result

        extreme = is_extreme_price_market(best_bid)
        result["extreme_price"] = extreme

        buy_result = analyze_best_place_price_from_book(book, "BUY", max_spread, mid, order_size)
        sell_result = analyze_best_place_price_from_book(book, "SELL", max_spread, mid, order_size)

        if extreme:
            if buy_result is None or sell_result is None:
                missing = []
                if buy_result is None:
                    missing.append("买单")
                if sell_result is None:
                    missing.append("卖单")
                result["buy_status"] = "extreme_skip"
                result["sell_status"] = "extreme_skip"
                result["error"] = f"极端价格市场({best_bid:.2f})，{'/'.join(missing)}深度/范围不足，跳过双向挂单"
                return result

        if buy_result:
            buy_price, buy_tier, _ = buy_result
            result["buy_price"] = buy_price
            result["buy_tier"] = buy_tier
            try:
                resp = backend.create_order(token_id, "BUY", buy_price, order_size, neg_risk=neg_risk)
                result["buy_status"] = "placed" if resp and resp.get("status") != "error" else f"failed: {str(resp)[:50]}"
                if result["buy_status"] == "placed":
                    log_event(EVENT_ORDER_PLACED, "买单成功", token_id=token_id, side="BUY", price=buy_price, size=order_size)
            except Exception as e:
                result["buy_status"] = f"error: {str(e)[:50]}"
        else:
            result["buy_status"] = "depth_insufficient"

        if sell_result:
            sell_price, sell_tier, _ = sell_result
            result["sell_price"] = sell_price
            result["sell_tier"] = sell_tier
            try:
                resp = backend.create_order(token_id, "SELL", sell_price, order_size, neg_risk=neg_risk)
                result["sell_status"] = "placed" if resp and resp.get("status") != "error" else f"failed: {str(resp)[:50]}"
                if result["sell_status"] == "placed":
                    log_event(EVENT_ORDER_PLACED, "卖单成功", token_id=token_id, side="SELL", price=sell_price, size=order_size)
            except Exception as e:
                result["sell_status"] = f"error: {str(e)[:50]}"
        else:
            result["sell_status"] = "depth_insufficient"

    except Exception as e:
        result["error"] = str(e)
        result["buy_status"] = "error"
        result["sell_status"] = "error"

    return result


def run_auto_place_orders(
    backend: Any,
    strategy_tokens: List[Dict],
    placed_orders_log: List[Dict],
    pending_retry_tokens: List[Dict],
    pending_retry_lock: threading.Lock,
) -> Tuple[int, int]:
    """批量自动挂单（并发版）。"""
    pc = cfg.place

    print(f"\n{'='*60}")
    print(f"🔍 [自动挂单] 并发分析 {len(strategy_tokens)} 个 token（{pc.PLACE_ORDER_WORKERS} 线程）...")
    print(f"{'='*60}")

    results_map: Dict[str, Dict] = {}

    def _place_one(token_info: Dict):
        result = place_order_for_token(backend, token_info)
        result["timestamp"] = datetime.now().strftime("%H:%M:%S")
        return token_info["token_id"], result

    with concurrent.futures.ThreadPoolExecutor(max_workers=pc.PLACE_ORDER_WORKERS) as executor:
        futures = {executor.submit(_place_one, t): t for t in strategy_tokens}
        for future in concurrent.futures.as_completed(futures):
            try:
                tid, result = future.result()
                results_map[tid] = result
            except Exception as e:
                token_info = futures[future]
                results_map[token_info["token_id"]] = {
                    "token_id": token_info["token_id"],
                    "token_type": token_info["token_type"],
                    "question": token_info["question"],
                    "buy_status": "error", "sell_status": "error",
                    "error": str(e), "timestamp": datetime.now().strftime("%H:%M:%S"),
                }

    success_count = 0
    skip_count = 0
    new_pending = []

    for i, token_info in enumerate(strategy_tokens):
        result = results_map.get(token_info["token_id"], {})
        placed_orders_log.append(result)

        buy_ok = result.get("buy_status") == "placed"
        sell_ok = result.get("sell_status") == "placed"
        buy_skip = result.get("buy_status") in ("depth_insufficient", "extreme_skip")
        sell_skip = result.get("sell_status") in ("depth_insufficient", "extreme_skip")

        label = f"   [{i+1}/{len(strategy_tokens)}] {token_info['question'][:35]}... [{token_info['token_type']}]"

        if buy_ok or sell_ok:
            success_count += 1
            if result.get("order_size"):
                token_info["order_size"] = result["order_size"]
            buy_info = f"买{result['buy_tier']}(${result['buy_price']:.3f})" if buy_ok else "买单跳过"
            sell_info = f"卖{result['sell_tier']}(${result['sell_price']:.3f})" if sell_ok else "卖单跳过"
            extreme_tag = " [极端价格✓]" if result.get("extreme_price") else ""
            spread_tag = f" [mid={result['mid']:.3f}±{result['max_spread']}]" if result.get("max_spread") and result.get("mid") else ""
            print(f"{label} ✅ {buy_info} | {sell_info}{extreme_tag}{spread_tag}")
        elif result.get("error") and "极端价格" in str(result.get("error", "")):
            skip_count += 1
            print(f"{label} ⛔ {result['error']}")
            new_pending.append(token_info)
        elif buy_skip and sell_skip:
            skip_count += 1
            print(f"{label} ⚠️ 深度/范围不足，跳过（等待重试）")
            new_pending.append(token_info)
        else:
            skip_count += 1
            print(f"{label} ❌ 买={result.get('buy_status','?')[:25]} | 卖={result.get('sell_status','?')[:25]}")

    with pending_retry_lock:
        pending_retry_tokens.clear()
        pending_retry_tokens.extend(new_pending)

    print(f"\n{'='*60}")
    print(f"📊 [自动挂单] 完成！成功: {success_count} 个，跳过/失败: {skip_count} 个")
    if new_pending:
        print(f"   🔄 {len(new_pending)} 个 token 将在 {pc.RETRY_INTERVAL//60} 分钟后重试")
    print(f"{'='*60}\n")
    return success_count, skip_count


async def periodic_retry_task(
    backend: Any,
    pending_retry_tokens: List[Dict],
    pending_retry_lock: threading.Lock,
    placed_orders_log: List[Dict],
) -> None:
    """定期重试深度不足的市场。"""
    pc = cfg.place
    while True:
        await asyncio.sleep(pc.RETRY_INTERVAL)
        with pending_retry_lock:
            tokens_to_retry = list(pending_retry_tokens)
        if not tokens_to_retry:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 [重试] 无待重试 token，跳过")
            continue
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 [重试] 开始重试 {len(tokens_to_retry)} 个 token...")
        await asyncio.to_thread(
            run_auto_place_orders,
            backend,
            tokens_to_retry,
            placed_orders_log,
            pending_retry_tokens,
            pending_retry_lock,
        )


def check_and_rebalance_token(
    backend: Any,
    token_info: Dict,
    my_bid_price: Optional[float],
    my_ask_price: Optional[float],
) -> bool:
    """检查挂单是否在 mid ± max_spread 范围内，偏离则撤单重挂。"""
    token_id = token_info["token_id"]
    max_spread = token_info.get("max_spread", None)
    question = token_info["question"]
    token_type = token_info["token_type"]

    if max_spread is None:
        return False
    if my_bid_price is None and my_ask_price is None:
        return False

    book, _, _, mid = get_orderbook_info(backend, token_id)
    if mid is None:
        return False

    lower = mid - max_spread
    upper = mid + max_spread

    bid_out_of_range = my_bid_price is not None and not (lower <= my_bid_price <= upper)
    ask_out_of_range = my_ask_price is not None and not (lower <= my_ask_price <= upper)

    if not bid_out_of_range and not ask_out_of_range:
        return False

    out_sides = []
    if bid_out_of_range:
        out_sides.append(f"买单(${my_bid_price:.3f})")
    if ask_out_of_range:
        out_sides.append(f"卖单(${my_ask_price:.3f})")

    print(f"\n🔄 [插队检测] [{token_type}] {question[:35]}...")
    print(f"   mid={mid:.3f}, 范围=[{lower:.3f}, {upper:.3f}]")
    print(f"   ⚠️ 偏离范围: {', '.join(out_sides)}")
    print(f"   🧨 撤单并重新挂单...")

    try:
        backend.cancel_all_asset(token_id)
        time.sleep(0.5)
        result = place_order_for_token(backend, token_info)
        buy_ok = result["buy_status"] == "placed"
        sell_ok = result["sell_status"] == "placed"
        if buy_ok or sell_ok:
            buy_info = f"买{result['buy_tier']}(${result['buy_price']:.3f})" if buy_ok else "买单跳过"
            sell_info = f"卖{result['sell_tier']}(${result['sell_price']:.3f})" if sell_ok else "卖单跳过"
            print(f"   ✅ 重新挂单成功: {buy_info} | {sell_info}")
            log_event(EVENT_SPREAD_REBALANCED, "插队检测重挂成功", token_id=token_id)
        else:
            print(f"   ⚠️ 重新挂单失败或深度不足: 买={result['buy_status']} | 卖={result['sell_status']}")
        return True
    except Exception as e:
        print(f"   ❌ 插队重挂失败: {e}")
        return False


async def spread_check_task(
    backend: Any,
    strategy_tokens: List[Dict],
) -> None:
    """定期检查挂单是否偏离 mid ± max_spread，偏离则撤单重挂。"""
    scc = cfg.spread_check
    await asyncio.sleep(scc.SPREAD_CHECK_INTERVAL)
    print(f"\n🔍 [插队检测] 任务已启动（每 {scc.SPREAD_CHECK_INTERVAL}s 检查一次）")

    while True:
        try:
            current_tokens = list(strategy_tokens)
            spread_tokens = [t for t in current_tokens if t.get("max_spread") is not None]

            if not spread_tokens:
                await asyncio.sleep(scc.SPREAD_CHECK_INTERVAL)
                continue

            if hasattr(backend, "get_all_my_orders_grouped"):
                all_orders = backend.get_all_my_orders_grouped()
            else:
                raw = backend.get_all_orders()
                grouped = defaultdict(lambda: {"bids": [], "asks": []})
                for o in raw or []:
                    if str(o.get("status", "")).upper() != "LIVE":
                        continue
                    tid = o.get("token_id") or o.get("asset_id")
                    if not tid:
                        continue
                    price = float(o.get("price", 0))
                    side = o.get("side")
                    if side == "BUY":
                        grouped[tid]["bids"].append(price)
                    elif side == "SELL":
                        grouped[tid]["asks"].append(price)
                all_orders = {}
                for tid, g in grouped.items():
                    all_orders[tid] = {
                        "best_bid": max(g["bids"]) if g["bids"] else None,
                        "best_ask": min(g["asks"]) if g["asks"] else None,
                    }

            rebalanced = 0
            for t in spread_tokens:
                token_id = t["token_id"]
                order_info = all_orders.get(token_id, {})
                my_bid_price = order_info.get("best_bid") if isinstance(order_info, dict) else None
                my_ask_price = order_info.get("best_ask") if isinstance(order_info, dict) else None

                if my_bid_price is None and my_ask_price is None:
                    continue

                did_rebalance = await asyncio.to_thread(
                    check_and_rebalance_token,
                    backend,
                    t,
                    my_bid_price,
                    my_ask_price,
                )
                if did_rebalance:
                    rebalanced += 1
                    await asyncio.sleep(0.5)

            if rebalanced > 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 [插队检测] 本轮重新挂单: {rebalanced} 个")

        except Exception as e:
            print(f"\n❌ [插队检测] 运行时错误: {e}")
            import traceback
            traceback.print_exc()

        await asyncio.sleep(scc.SPREAD_CHECK_INTERVAL)
