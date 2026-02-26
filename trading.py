import gc
import os
import json
import asyncio
import traceback
import pandas as pd
import math

import poly_data.global_state as global_state
import poly_data.CONSTANTS as CONSTANTS

# 我们不再从 utils 导入定价函数，而是直接在这里重写它，以注入你的逻辑
from poly_data.trading_utils import get_best_bid_ask_deets, get_buy_sell_amount, round_down, round_up
from poly_data.data_utils import get_position, get_order, set_position

if not os.path.exists('positions/'):
    os.makedirs('positions/')

# ==============================================================================
# 🔥 [核心修改] 重写定价函数：实现你的 Fixed Spread (95-93=2) 逻辑
# ==============================================================================
def get_my_fixed_spread_prices(best_bid, best_ask, row):
    """
    只做一件事：根据中间价和设定的 spread，算出买一和卖一
    """
    # 1. 读取你表格里的 spread (比如 0.04)
    # 优先读 max_spread，没有就读 spread_threshold
    target_spread = float(row.get('max_spread', row.get('spread_threshold', 0.04)))
    
    # 智能修正：如果你填了 4，自动转成 0.04
    if target_spread > 0.9: target_spread /= 100.0
    if target_spread < 0.01: target_spread = 0.01 # 最小保护

    # 2. 计算中间价
    mid = (best_bid + best_ask) / 2
    half = target_spread / 2

    # 3. 计算目标价
    my_bid = mid - half
    my_ask = mid + half

    # 4. 防吃单锁 (Maker Only)
    tick = 0.01
    # 买单最高 = 卖一 - 1分
    if my_bid >= best_ask: my_bid = best_ask - tick
    # 卖单最低 = 买一 + 1分
    if my_ask <= best_bid: my_ask = best_bid + tick

    return my_bid, my_ask

# ==============================================================================
# 下面的代码 99% 是原作者的原始逻辑，只改了调用定价函数的那一行
# ==============================================================================

def send_buy_order(order):
    client = global_state.client
    existing_buy_size = order['orders']['buy']['size']
    existing_buy_price = order['orders']['buy']['price']
    
    price_diff = abs(existing_buy_price - order['price']) if existing_buy_size > 0 else float('inf')
    size_diff = abs(existing_buy_size - order['size']) if existing_buy_size > 0 else float('inf')
    
    should_cancel = (
        price_diff > 0.005 or 
        size_diff > order['size'] * 0.1 or 
        existing_buy_size == 0
    )
    
    if should_cancel and existing_buy_size > 0:
        try: client.cancel_all_asset(order['token'])
        except: pass
    elif not should_cancel:
        return 

    # 这里的 incentive_start 逻辑稍微放宽一点，防止因为 spread 紧而不下单
    incentive_start = order['mid_price'] - 0.1 
    if order['price'] < incentive_start: return

    if 0.01 <= order['price'] <= 0.99:
        print(f"➕ New BUY: {order['size']} @ {order['price']}")
        try:
            client.create_order(
                order['token'], 'BUY', order['price'], order['size'], 
                True if order['neg_risk'] == 'TRUE' else False
            )
        except Exception as e:
            print(f"❌ Buy Failed: {e}")

def send_sell_order(order):
    client = global_state.client
    existing_sell_size = order['orders']['sell']['size']
    existing_sell_price = order['orders']['sell']['price']
    
    price_diff = abs(existing_sell_price - order['price']) if existing_sell_size > 0 else float('inf')
    size_diff = abs(existing_sell_size - order['size']) if existing_sell_size > 0 else float('inf')
    
    should_cancel = (
        price_diff > 0.005 or 
        size_diff > order['size'] * 0.1 or 
        existing_sell_size == 0
    )
    
    if should_cancel and existing_sell_size > 0:
        try: client.cancel_all_asset(order['token'])
        except: pass
    elif not should_cancel:
        return 

    if 0.01 <= order['price'] <= 0.99:
        print(f"➕ New SELL: {order['size']} @ {order['price']}")
        try:
            client.create_order(
                order['token'], 'SELL', order['price'], order['size'], 
                True if order['neg_risk'] == 'TRUE' else False
            )
        except Exception as e:
            pass

market_locks = {}

async def perform_trade(market):
    if market not in market_locks:
        market_locks[market] = asyncio.Lock()

    if market_locks[market].locked(): return

    async with market_locks[market]:
        try:
            client = global_state.client
            try:
                row = global_state.df[global_state.df['condition_id'] == market].iloc[0]
            except: return
            
            round_length = len(str(row.get('tick_size', '0.01')).split(".")[1]) if "." in str(row.get('tick_size', '0.01')) else 2

            # 读取配置 (兼容性处理)
            p_type = str(row.get('param_type', 'mid')).strip()
            if not p_type or p_type == 'nan' or p_type not in global_state.params: p_type = 'mid'
            params = global_state.params[p_type]
            
            # 强制读取 trade_size
            trade_size = float(row.get('trade_size', 100.0))
            max_pos = float(row.get('max_size', 500.0))

            deets = [
                {'name': 'token1', 'token': row['token1'], 'answer': row['answer1']}, 
                {'name': 'token2', 'token': row['token2'], 'answer': row['answer2']}
            ]
            
            # 仓位合并 (原版逻辑)
            try:
                pos_1 = float(client.get_position(row['token1'])[0])
                pos_2 = float(client.get_position(row['token2'])[0])
                amt_merge = min(pos_1, pos_2) / 10**6
                if amt_merge > CONSTANTS.MIN_MERGE_SIZE:
                    client.merge_positions(amt_merge, market, row['neg_risk'] == 'TRUE')
                    pos_1 = float(client.get_position(row['token1'])[0]) / 10**6
                    pos_2 = float(client.get_position(row['token2'])[0]) / 10**6
                else:
                    pos_1 /= 10**6
                    pos_2 /= 10**6
            except: pos_1, pos_2 = 0, 0

            for detail in deets:
                token = int(detail['token'])
                orders = get_order(token)
                
                deets_ob = get_best_bid_ask_deets(market, detail['name'], 100, 0.1)
                if deets_ob['best_bid'] is None or deets_ob['best_ask'] is None: continue

                best_bid = round(deets_ob['best_bid'], round_length)
                best_ask = round(deets_ob['best_ask'], round_length)
                
                # ======================================================
                # 🔥 [修改点] 调用我们自定义的定价函数
                # ======================================================
                bid_price, ask_price = get_my_fixed_spread_prices(best_bid, best_ask, row)
                
                # 精度修正
                bid_price = round(bid_price, round_length)
                ask_price = round(ask_price, round_length)
                mid_price = (best_bid + best_ask) / 2
                # ======================================================

                pos_info = get_position(token)
                position = round_down(pos_info['size'], 2)
                avgPrice = pos_info['avgPrice']

                # 计算买卖数量 (这里我们简化逻辑，直接用 trade_size，更可控)
                buy_amount = trade_size
                sell_amount = trade_size

                order = {
                    "token": token, "mid_price": mid_price, "neg_risk": row['neg_risk'],
                    "max_spread": float(row.get('max_spread', 0.04))*100, 
                    'orders': orders, 'token_name': detail['name'],
                    'row': row, 'price': 0, 'size': 0
                }

                # ------- 卖单 (止盈/止损/风控) -------
                # 只有当有持仓时才考虑卖
                if position > 0:
                    order['size'] = sell_amount
                    order['price'] = ask_price
                    
                    # 止盈逻辑 (Take Profit)
                    if avgPrice > 0:
                        tp_price = round_up(avgPrice * (1 + params['take_profit_threshold']/100), round_length)
                        # 卖价不能低于止盈价 (除非止损)
                        if ask_price < tp_price: order['price'] = max(ask_price, tp_price)

                    # 止损逻辑
                    pnl = (mid_price - avgPrice) / avgPrice * 100 if avgPrice > 0 else 0
                    spread = best_ask - best_bid
                    
                    if (pnl < params['stop_loss_threshold'] and spread <= params['spread_threshold']):
                        print(f"🚨 Stop Loss: {pnl:.2f}%")
                        order['price'] = best_bid
                        send_sell_order(order)
                        continue
                    
                    # 正常做市卖单
                    # 检查是否需要更新
                    curr_ord_price = float(orders['sell']['price'])
                    if curr_ord_price == 0 or abs(curr_ord_price - order['price']) > 0.005:
                        send_sell_order(order)

                # ------- 买单 -------
                # 只有当库存未超限时才买
                if position + buy_amount <= max_pos:
                    if row.get('3_hour', 0) <= params['volatility_threshold']:
                        order['size'] = buy_amount
                        order['price'] = bid_price
                        
                        # 检查是否需要更新
                        curr_buy_price = float(orders['buy']['price'])
                        if curr_buy_price == 0 or abs(curr_buy_price - bid_price) > 0.005:
                             send_buy_order(order)
                else:
                    # 超限撤单
                    if float(orders['buy']['size']) > 0:
                        client.cancel_all_asset(token)

        except Exception as ex:
            print(f"❌ Error: {ex}")
            traceback.print_exc()

        gc.collect()
        await asyncio.sleep(2)