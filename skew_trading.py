import gc
import os
import json
import asyncio
import traceback
import pandas as pd
import math
import time

import poly_data.global_state as global_state
import poly_data.CONSTANTS as CONSTANTS

# 引入 set_order 是为了防止 WebSocket 延迟导致的重复下单
from poly_data.data_utils import get_position, get_order, set_position, set_order
from poly_data.trading_utils import get_best_bid_ask_deets, round_down, round_up

if not os.path.exists('positions/'):
    os.makedirs('positions/')

# ================= 策略参数 =================
RISK_FACTOR = 0.0004 
DEFAULT_TARGET_SPREAD = 0.04 
# ===========================================

def send_buy_order(order):
    """
    [完全复刻原作者逻辑] + [手动状态同步]
    """
    client = global_state.client
    token = order['token']
    price = order['price']
    size = order['size']
    
    # 获取现有订单状态
    existing_buy = order['orders']['buy']
    existing_size = float(existing_buy['size'])
    existing_price = float(existing_buy['price'])
    
    # 1. 计算差异 (Diff)
    price_diff = abs(existing_price - price) if existing_size > 0 else float('inf')
    size_diff = abs(existing_size - size) if existing_size > 0 else float('inf')
    
    # 2. 判断是否需要撤单 (逻辑与原作者一致)
    should_cancel = (
        price_diff > 0.005 or         # 价格变动 > 0.5分
        size_diff > size * 0.1 or     # 数量变动 > 10%
        (existing_size == 0 and size > 0) # 原本没单，现在要下单 (这里主要是为了触发下面的创建逻辑，但如果有旧单需先撤)
    )
    
    # 如果有旧单且需要变动，先撤单
    if existing_size > 0 and should_cancel:
        # print(f"Cancelling BUY for {token}...")
        try: 
            client.cancel_all_asset(token)
            # [关键同步] 撤单后，立刻告诉本地内存：单子没了
            set_order(token, 'buy', 0, 0)
        except: pass
    
    # 如果差异太小，且已有单子，直接跳过 (防抖)
    if existing_size > 0 and price_diff <= 0.005 and size_diff <= size * 0.1:
        return

    # 3. 下单逻辑
    # 检查价格安全区间
    if 0.01 <= price <= 0.99:
        print(f"➕ New BUY: {size} @ {price}")
        try:
            client.create_order(
                token, 'BUY', price, size, 
                True if order['neg_risk'] == 'TRUE' else False
            )
            # ==================================================
            # 🔥 [核心补丁] 手动写入内存 (防重复下单)
            # 既然 API 没报错，我们就认为下单成功了。
            # 强制更新本地状态，这样下一轮循环就不会以为没单子了。
            set_order(token, 'buy', size, price)
            # ==================================================
            
        except Exception as e:
            if 'not enough' in str(e).lower():
                # print("⚠️ Balance error (Buy)")
                try: client.cancel_all_asset(token)
                except: pass
            else:
                print(f"❌ Buy Failed: {e}")

def send_sell_order(order):
    """
    [完全复刻原作者逻辑] + [手动状态同步]
    """
    client = global_state.client
    token = order['token']
    price = order['price']
    size = order['size']
    
    existing_sell = order['orders']['sell']
    existing_size = float(existing_sell['size'])
    existing_price = float(existing_sell['price'])
    
    price_diff = abs(existing_price - price) if existing_size > 0 else float('inf')
    size_diff = abs(existing_size - size) if existing_size > 0 else float('inf')
    
    should_cancel = (
        price_diff > 0.005 or 
        size_diff > size * 0.1 or 
        (existing_size == 0 and size > 0)
    )
    
    if existing_size > 0 and should_cancel:
        # print(f"Cancelling SELL for {token}...")
        try: 
            client.cancel_all_asset(token)
            set_order(token, 'sell', 0, 0)
        except: pass
        
    if existing_size > 0 and price_diff <= 0.005 and size_diff <= size * 0.1:
        return

    if 0.01 <= price <= 0.99:
        print(f"➕ New SELL: {size} @ {price}")
        try:
            client.create_order(
                token, 'SELL', price, size, 
                True if order['neg_risk'] == 'TRUE' else False
            )
            # 🔥 [核心补丁] 手动写入内存
            set_order(token, 'sell', size, price)
            
        except Exception as e:
            if 'not enough' in str(e).lower():
                # print("⚠️ Balance error (Sell)")
                try: client.cancel_all_asset(token)
                except: pass
            else:
                print(f"❌ Sell Failed: {e}")

# 锁机制
market_locks = {}

async def perform_trade(market):
    # 使用锁，确保同一时间只有一个逻辑在跑
    if market not in market_locks:
        market_locks[market] = asyncio.Lock()

    if market_locks[market].locked():
        return

    async with market_locks[market]:
        try:
            client = global_state.client
            try:
                row = global_state.df[global_state.df['condition_id'] == market].iloc[0]
            except: return

            # Tick Size 计算
            tick_size_str = str(row.get('tick_size', '0.01'))
            if "." in tick_size_str:
                round_length = len(tick_size_str.split(".")[1])
                tick_size = 1 / (10 ** round_length)
            else:
                round_length = 2
                tick_size = 0.01

            deets = [
                {'name': 'token1', 'token': row['token1'], 'answer': row['answer1']}, 
                {'name': 'token2', 'token': row['token2'], 'answer': row['answer2']}
            ]
            
            # 读取参数
            current_risk_factor = float(row.get('risk_factor', RISK_FACTOR)) if not pd.isna(row.get('risk_factor')) else RISK_FACTOR
            target_size = float(row.get('trade_size', 100.0)) if not pd.isna(row.get('trade_size')) else 100.0
            max_pos_limit = float(row.get('max_size', 500.0)) if not pd.isna(row.get('max_size')) else 500.0

            # 仓位合并 (复用原逻辑)
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
            except:
                pos_1, pos_2 = 0, 0

            # ------- 核心循环 -------
            for i, detail in enumerate(deets):
                token = int(detail['token'])
                token_name = detail['name']
                
                # 1. 获取最新数据
                current_inventory = pos_1 if i == 0 else pos_2
                orders = get_order(token) # 这里会读取我们手动 set_order 更新过的数据
                
                ob_data = get_best_bid_ask_deets(market, token_name, 100, 0.1)
                if ob_data['best_bid'] is None or ob_data['best_ask'] is None:
                    continue

                best_bid = float(ob_data['best_bid'])
                best_ask = float(ob_data['best_ask'])
                mid_price = (best_bid + best_ask) / 2
                
                # 2. 计算 Skew (库存倾斜)
                skew = current_inventory * current_risk_factor
                half_spread = DEFAULT_TARGET_SPREAD / 2

                target_bid = mid_price - half_spread - skew
                target_ask = mid_price + half_spread - skew

                # 3. 防吃单安全锁 (Maker Only)
                target_bid = min(target_bid, best_ask - tick_size)
                target_ask = max(target_ask, best_bid + tick_size)

                target_bid = round(target_bid, round_length)
                target_ask = round(target_ask, round_length)

                print(f"[{detail['answer']}] Inv:{current_inventory:.0f} | Bid:{target_bid} | Ask:{target_ask}")

                # 4. 构造 Order 字典 (仿照原作者格式)
                # 注意：这里我们只把必要的字段传进去
                buy_order_info = {
                    'token': token,
                    'price': target_bid,
                    'size': target_size,
                    'orders': orders, # 传入包含 'buy' 和 'sell' 详情的字典
                    'neg_risk': row['neg_risk']
                }
                
                sell_order_info = {
                    'token': token,
                    'price': target_ask,
                    'size': target_size,
                    'orders': orders,
                    'neg_risk': row['neg_risk']
                }

                # 5. 执行下单
                # 买单：库存未超限 + 价格合理
                if current_inventory + target_size <= max_pos_limit:
                    if target_bid < best_ask:
                        send_buy_order(buy_order_info)
                else:
                    # 超限撤单
                    if float(orders['buy']['size']) > 0:
                        client.cancel_all_asset(token)
                        set_order(token, 'buy', 0, 0)

                # 卖单：有库存 + 价格合理
                if current_inventory > 0:
                    if target_ask > best_bid:
                        send_sell_order(sell_order_info)
                else:
                    # 无库存撤单
                    if float(orders['sell']['size']) > 0:
                        client.cancel_all_asset(token)
                        set_order(token, 'sell', 0, 0)

        except Exception as ex:
            print(f"❌ Error: {ex}")
            traceback.print_exc()

        gc.collect()
        
        # [核心] 强制休眠 2 秒
        # 这是"别人的代码"里最有效的防抖机制，不要删！
        await asyncio.sleep(2.0)