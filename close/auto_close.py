"""
自动清仓任务：持仓被吃后以 best_bid - offset 价格挂限价卖单清仓。
"""
import asyncio
import traceback
from datetime import datetime
from typing import Any, Dict, List

from config.bot_config import cfg
from logging_utils import log_event, EVENT_CLOSE_POSITION


async def auto_close_positions_task(backend: Any, strategy_tokens: List[Dict]) -> None:
    """每 POSITION_CHECK_INTERVAL 秒检查持仓，达到阈值则清仓。"""
    cc = cfg.close
    print(f"\n💰 [自动清仓] 任务已启动（每 {cc.POSITION_CHECK_INTERVAL}s 检查，阈值: {cc.MIN_POSITION_TO_CLOSE} shares）")

    token_map: Dict[str, Dict] = {}

    while True:
        await asyncio.sleep(cc.POSITION_CHECK_INTERVAL)

        try:
            for t in strategy_tokens:
                token_map[t["token_id"]] = t

            if not token_map:
                continue

            try:
                all_positions = backend.get_all_positions()
            except Exception as e:
                print(f"\n⚠️ [自动清仓] 获取持仓失败: {e}")
                continue

            if all_positions is None or len(all_positions) == 0:
                continue

            positions_found = []
            for _, row in all_positions.iterrows():
                asset = str(row.get("asset", ""))
                size = float(row.get("size", 0))
                if size >= cc.MIN_POSITION_TO_CLOSE and asset in token_map:
                    t = token_map[asset]
                    positions_found.append({
                        "token_id": asset,
                        "token_type": t["token_type"],
                        "question": t["question"],
                        "shares": size,
                        "neg_risk": t.get("neg_risk", False),
                    })

            if not positions_found:
                continue

            print(f"\n\n{'$'*20} 💰 发现持仓，开始清仓 {'$'*20}")
            print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"📋 发现 {len(positions_found)} 个持仓需要清仓：")

            for pos in positions_found:
                token_id = pos["token_id"]
                shares = pos["shares"]
                question = pos["question"]
                token_type = pos["token_type"]
                neg_risk = pos["neg_risk"]

                print(f"\n   🎯 [{token_type}] {question[:40]}...")
                print(f"      持仓: {shares:.2f} shares")

                try:
                    book = backend.get_order_book(token_id)
                    if not book or not book.bids:
                        print(f"      ❌ 无法获取订单簿，跳过")
                        continue

                    bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
                    best_bid = float(bids[0].price)
                    close_price = max(0.01, round(best_bid - cc.CLOSE_PRICE_OFFSET, 2))

                    print(f"      best_bid: ${best_bid:.3f} → 清仓价: ${close_price:.3f}")
                    print(f"      正在挂卖单: {shares:.2f} shares @ ${close_price:.3f}...")

                    resp = backend.create_order(token_id, "SELL", close_price, shares, neg_risk=neg_risk)

                    if resp and resp.get("status") != "error":
                        print(f"      ✅ 清仓单已提交！OrderID: {resp.get('orderID', resp)}")
                        log_event(EVENT_CLOSE_POSITION, "清仓成功", token_id=token_id, side="SELL", price=close_price, size=shares)
                    else:
                        print(f"      ❌ 清仓失败: {resp}")

                except Exception as e:
                    print(f"      ❌ 清仓出错: {e}")

            print(f"{'$'*60}\n")

        except Exception as e:
            print(f"\n❌ [自动清仓] 运行时错误: {e}")
            traceback.print_exc()
            await asyncio.sleep(5)
