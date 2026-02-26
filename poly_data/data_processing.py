import json
from sortedcontainers import SortedDict
import poly_data.global_state as global_state
import poly_data.CONSTANTS as CONSTANTS

try:
    from trading import perform_trade
except ImportError:
    from trading import perform_trade

import time 
import asyncio
from poly_data.data_utils import set_position, set_order, update_positions

def process_book_data(asset, json_data):
    global_state.all_data[asset] = {
        'asset_id': json_data['asset_id'],
        'bids': SortedDict(),
        'asks': SortedDict()
    }
    global_state.all_data[asset]['bids'].update({float(entry['price']): float(entry['size']) for entry in json_data['bids']})
    global_state.all_data[asset]['asks'].update({float(entry['price']): float(entry['size']) for entry in json_data['asks']})

def process_price_change(asset, side, price_level, new_size, msg_asset_id):
    if asset not in global_state.all_data:
        return
    tracked_asset_id = global_state.all_data[asset].get('asset_id')
    if msg_asset_id and tracked_asset_id and msg_asset_id != tracked_asset_id:
        return 
    if side == 'bids':
        book = global_state.all_data[asset]['bids']
    else:
        book = global_state.all_data[asset]['asks']
    if new_size == 0:
        if price_level in book:
            del book[price_level]
    else:
        book[price_level] = new_size

def process_data(json_datas, trade=True):
    if isinstance(json_datas, str):
        try:
            json_datas = json.loads(json_datas)
        except:
            return
    if not isinstance(json_datas, list):
        json_datas = [json_datas]

    # [新增] 记录哪些市场发生了变动，最后统一触发，而不是变动一次触发一次
    markets_to_trade = set()

    for json_data in json_datas:
        event_type = json_data.get('event_type')
        asset = json_data.get('market')
        if not asset or not event_type:
            continue

        if event_type == 'book':
            process_book_data(asset, json_data)
            if trade:
                markets_to_trade.add(asset)
                
        elif event_type == 'price_change':
            msg_asset_id = json_data.get('asset_id')
            if 'price_changes' in json_data:
                for data in json_data['price_changes']:
                    side = 'bids' if data['side'] == 'BUY' else 'asks'
                    price_level = float(data['price'])
                    new_size = float(data['size'])
                    process_price_change(asset, side, price_level, new_size, msg_asset_id)
                
                if trade:
                    markets_to_trade.add(asset)

    # [新增] 统一触发交易，防止并发洪水
    for market in markets_to_trade:
        asyncio.create_task(perform_trade(market))

# --- 以下部分保持不变 ---
def add_to_performing(col, id):
    if col not in global_state.performing:
        global_state.performing[col] = set()
    if col not in global_state.performing_timestamps:
        global_state.performing_timestamps[col] = {}
    global_state.performing[col].add(id)
    global_state.performing_timestamps[col][id] = time.time()

def remove_from_performing(col, id):
    if col in global_state.performing:
        global_state.performing[col].discard(id)
    if col in global_state.performing_timestamps:
        global_state.performing_timestamps[col].pop(id, None)

def process_user_data(rows):
    if isinstance(rows, str):
        try: rows = json.loads(rows)
        except: return
    if not isinstance(rows, list): rows = [rows]

    for row in rows:
        market = row.get('market')
        if not market: continue
        token = row.get('asset_id')
        side = row.get('side', '').lower()
            
        if token in global_state.REVERSE_TOKENS:     
            col = token + "_" + side
            if row['event_type'] == 'trade':
                size = 0; price = 0; maker_outcome = ""; taker_outcome = row.get('outcome')
                is_user_maker = False
                if 'maker_orders' in row:
                    for maker_order in row['maker_orders']:
                        if maker_order.get('maker_address', '').lower() == global_state.client.browser_wallet.lower():
                            size = float(maker_order.get('matched_amount', 0))
                            price = float(maker_order.get('price', 0))
                            is_user_maker = True
                            maker_outcome = maker_order.get('outcome')
                            if maker_outcome == taker_outcome: side = 'buy' if side == 'sell' else 'sell' 
                            else: token = global_state.REVERSE_TOKENS[token]
                if not is_user_maker:
                    size = float(row.get('size', 0))
                    price = float(row.get('price', 0))

                print(f"TRADE: {market} STATUS: {row.get('status')} SIZE: {size}") 
                status = row.get('status')
                
                if status == 'CONFIRMED' or status == 'FAILED':
                    if status == 'FAILED':
                        asyncio.create_task(asyncio.sleep(2))
                        update_positions()
                    else:
                        remove_from_performing(col, row.get('id'))
                        asyncio.create_task(perform_trade(market))
                elif status == 'MATCHED':
                    add_to_performing(col, row.get('id'))
                    set_position(token, side, size, price)
                    asyncio.create_task(perform_trade(market))
                elif status == 'MINED':
                    remove_from_performing(col, row.get('id'))
            elif row['event_type'] == 'order':
                original_size = float(row.get('original_size', 0))
                size_matched = float(row.get('size_matched', 0))
                set_order(token, side, original_size - size_matched, row.get('price'))
                asyncio.create_task(perform_trade(market))