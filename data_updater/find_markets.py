import pandas as pd
import numpy as np
import os
import requests
import time
import warnings
import concurrent.futures
import json
warnings.filterwarnings("ignore")

if not os.path.exists('data'):
    os.makedirs('data')

# 全局 session
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})
adapter = requests.adapters.HTTPAdapter(max_retries=3)
session.mount('https://', adapter)


def get_sel_df(spreadsheet, sheet_name='Selected Markets'):
    try:
        wk2 = spreadsheet.worksheet(sheet_name)
        sel_df = pd.DataFrame(wk2.get_all_records())
        if not sel_df.empty and 'question' in sel_df.columns:
            sel_df = sel_df[sel_df['question'] != ""].reset_index(drop=True)
        return sel_df
    except:
        return pd.DataFrame()
    
def get_all_markets(client):
    cursor = ""
    all_markets = []
    print("Fetching markets from Polymarket API...")
    while True:
        try:
            markets = client.get_sampling_markets(next_cursor = cursor)
            markets_df = pd.DataFrame(markets['data'])
            cursor = markets['next_cursor']
            all_markets.append(markets_df)
            if cursor is None:
                break
        except:
            break
    if not all_markets:
        return pd.DataFrame()
    all_df = pd.concat(all_markets)
    all_df = all_df.reset_index(drop=True)
    return all_df

def get_bid_ask_range(ret, TICK_SIZE):
    bid_from = ret['midpoint'] - ret['max_spread'] / 100
    bid_to = ret['best_ask'] 
    if bid_to == 0:
        bid_to = ret['midpoint']
    if bid_to - TICK_SIZE > ret['midpoint']:
        bid_to = ret['best_bid'] + (TICK_SIZE + 0.1 * TICK_SIZE)
    if bid_from > bid_to:
        bid_from = bid_to - (TICK_SIZE + 0.1 * TICK_SIZE)
    ask_to = ret['midpoint'] + ret['max_spread'] / 100
    ask_from = ret['best_bid']
    if ask_from == 0:
        ask_from = ret['midpoint']
    if ask_from + TICK_SIZE < ret['midpoint']:
        ask_from = ret['best_ask'] - (TICK_SIZE + 0.1 * TICK_SIZE)
    if ask_from > ask_to:
        ask_to = ask_from + (TICK_SIZE + 0.1 * TICK_SIZE)
    return round(bid_from, 3), round(bid_to, 3), round(ask_from, 3), round(ask_to, 3)

def generate_numbers(start, end, TICK_SIZE):
    rounded_start = (int(start * 100) + 1) / 100 if start * 100 % 1 != 0 else start + TICK_SIZE
    rounded_end = int(end * 100) / 100
    numbers = []
    current = rounded_start
    while current < end:
        numbers.append(current)
        current += TICK_SIZE
        current = round(current, len(str(TICK_SIZE).split('.')[1]))
    return numbers

def add_formula_params(curr_df, midpoint, v, daily_reward):
    curr_df['s'] = (curr_df['price'] - midpoint).abs()
    curr_df['S'] = ((v - curr_df['s']) / v) ** 2
    curr_df['100'] = 1/curr_df['price'] * 100
    curr_df['size'] = curr_df['size'] + curr_df['100']
    curr_df['Q'] = curr_df['S'] * curr_df['size']
    curr_df['reward_per_100'] = (curr_df['Q'] / curr_df['Q'].sum()) * daily_reward / 2 / curr_df['size'] * curr_df['100']
    return curr_df

def process_single_row(row, client):
    ret = {}
    ret['question'] = row.get('question', '')
    ret['neg_risk'] = row.get('neg_risk', False)

    tokens = row.get('tokens', [])
    if len(tokens) >= 2:
        ret['answer1'] = tokens[0].get('outcome', '')
        ret['answer2'] = tokens[1].get('outcome', '')
        token1 = tokens[0].get('token_id', '')
        token2 = tokens[1].get('token_id', '')
    else:
        ret['answer1'] = ''
        ret['answer2'] = ''
        token1 = ''
        token2 = ''

    condition_id = row.get('condition_id', '')

    rewards = row.get('rewards', {})
    ret['min_size'] = rewards.get('min_size', 0)
    ret['max_spread'] = rewards.get('max_spread', 0)

    rate = 0
    rates = rewards.get('rates', [])
    if rates:
        for rate_info in rates:
            if rate_info.get('asset_address', '').lower() == '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'.lower():
                rate = rate_info.get('rewards_daily_rate', 0)
                break
    ret['rewards_daily_rate'] = rate

    if token1:
        try:
            book = client.get_order_book(token1)
            bids = pd.DataFrame(book.bids).astype(float) if book.bids else pd.DataFrame()
            asks = pd.DataFrame(book.asks).astype(float) if book.asks else pd.DataFrame()
        except:
            bids = pd.DataFrame()
            asks = pd.DataFrame()
    else:
        bids = pd.DataFrame()
        asks = pd.DataFrame()

    # Best Bid/Ask Fix
    try:
        ret['best_bid'] = bids['price'].max() if not bids.empty else 0
    except:
        ret['best_bid'] = 0

    try:
        ret['best_ask'] = asks['price'].min() if not asks.empty else 0 
    except:
        ret['best_ask'] = 0

    ret['midpoint'] = (ret['best_bid'] + ret['best_ask']) / 2
    TICK_SIZE = row.get('minimum_tick_size', 0.01)
    ret['tick_size'] = TICK_SIZE

    bid_from, bid_to, ask_from, ask_to = get_bid_ask_range(ret, TICK_SIZE)
    v = round((ret['max_spread'] / 100), 2) if ret['max_spread'] else 0.01

    bids_df = pd.DataFrame()
    bids_df['price'] = generate_numbers(bid_from, bid_to, TICK_SIZE)
    asks_df = pd.DataFrame()
    asks_df['price'] = generate_numbers(ask_from, ask_to, TICK_SIZE)

    if not bids.empty:
        bids_df = bids_df.merge(bids, on='price', how='left').fillna(0)
    if not asks.empty:
        asks_df = asks_df.merge(asks, on='price', how='left').fillna(0)

    best_bid_reward = 0
    try:
        ret_bid = add_formula_params(bids_df, ret['midpoint'], v, rate)
        best_bid_reward = round(ret_bid['reward_per_100'].max(), 2)
    except:
        pass

    best_ask_reward = 0
    try:
        ret_ask = add_formula_params(asks_df, ret['midpoint'], v, rate)
        best_ask_reward = round(ret_ask['reward_per_100'].max(), 2)
    except:
        pass

    ret['bid_reward_per_100'] = best_bid_reward
    ret['ask_reward_per_100'] = best_ask_reward
    ret['sm_reward_per_100'] = round((best_bid_reward + best_ask_reward) / 2, 2)
    ret['gm_reward_per_100'] = round((best_bid_reward * best_ask_reward) ** 0.5, 2)

    # ── mid_reward_per_100: midpoint ±3 档的平均奖励（更贴合实际挂单位置）──
    N_LEVELS = 3
    mid_bid_reward = 0
    mid_ask_reward = 0
    try:
        if not bids_df.empty and 'reward_per_100' in ret_bid.columns:
            mid_bid_mask = ret_bid['price'].between(ret['midpoint'] - N_LEVELS * TICK_SIZE, ret['midpoint'])
            mid_bid_rows = ret_bid[mid_bid_mask]
            if not mid_bid_rows.empty:
                mid_bid_reward = round(mid_bid_rows['reward_per_100'].mean(), 4)
    except:
        pass
    try:
        if not asks_df.empty and 'reward_per_100' in ret_ask.columns:
            mid_ask_mask = ret_ask['price'].between(ret['midpoint'], ret['midpoint'] + N_LEVELS * TICK_SIZE)
            mid_ask_rows = ret_ask[mid_ask_mask]
            if not mid_ask_rows.empty:
                mid_ask_reward = round(mid_ask_rows['reward_per_100'].mean(), 4)
    except:
        pass
    ret['mid_reward_per_100'] = round((mid_bid_reward * mid_ask_reward) ** 0.5, 2) if (mid_bid_reward > 0 and mid_ask_reward > 0) else 0

    ret['end_date_iso'] = row.get('end_date_iso', '')
    ret['end_date'] = row.get('end_date_iso', '') 
    
    # volume 由 batch_fetch_volumes() 批量填充，此处不再逐个请求
    ret['volume'] = 0.0

    ret['market_slug'] = row.get('market_slug', '')
    ret['token1'] = token1
    ret['token2'] = token2
    ret['condition_id'] = condition_id

    return ret

def batch_fetch_volumes(condition_ids, batch_size=50):
    """批量获取 24h 成交量，避免逐个请求 Gamma API。
    将 N 次请求压缩为 N/batch_size 次，大幅减少网络延迟。"""
    volumes = {}
    if not condition_ids:
        return volumes

    # 去重
    unique_ids = list(set(cid for cid in condition_ids if cid))
    print(f"📊 [批量 Volume] 正在获取 {len(unique_ids)} 个市场的成交量（每批 {batch_size} 个）...")

    def _fetch_batch(batch):
        batch_vols = {}
        try:
            ids_str = ",".join(batch)
            r = session.get(
                "https://gamma-api.polymarket.com/markets",
                params={"condition_ids": ids_str, "limit": len(batch)},
                timeout=10
            )
            data = r.json()
            markets_list = data if isinstance(data, list) else data.get("data", [])
            for m in markets_list:
                cid = str(m.get("conditionId") or m.get("condition_id", "")).strip()
                vol = float(m.get("volume24hr") or m.get("volumeNum") or m.get("volume") or 0)
                if cid:
                    batch_vols[cid] = vol
        except Exception as e:
            print(f"   ⚠️ 批量 Volume 请求失败: {e}")
        return batch_vols

    # 并发请求各批次
    batches = [unique_ids[i:i+batch_size] for i in range(0, len(unique_ids), batch_size)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(batches))) as executor:
        futures = [executor.submit(_fetch_batch, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            volumes.update(future.result())

    print(f"   ✅ 获取到 {len(volumes)} 个市场的成交量")
    return volumes


def get_all_results(all_df, client, max_workers=40):
    all_results = []
    
    def process_with_progress(args):
        idx, row = args
        try:
            return process_single_row(row, client)
        except:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_with_progress, (idx, row)) for idx, row in all_df.iterrows()]
        
        count = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not None:
                all_results.append(result)
            
            count += 1
            if count % 50 == 0:
                print(f'Processed {count} of {len(all_df)} markets...')

    return all_results

def get_combined_markets(new_df, new_markets, sel_df):
    if len(sel_df) > 0:
        old_markets = new_df[new_df['question'].isin(sel_df['question'])]
        all_markets = pd.concat([old_markets, new_markets])
    else:
        all_markets = new_markets
    all_markets = all_markets.drop_duplicates('question')
    all_markets = all_markets.sort_values('gm_reward_per_100', ascending=False)
    return all_markets

def calculate_annualized_volatility(df, hours):
    if df.empty: return 0
    end_time = df['t'].max()
    start_time = end_time - pd.Timedelta(hours=hours)
    window_df = df[df['t'] >= start_time]
    if len(window_df) < 2: return 0
    volatility = window_df['log_return'].std()
    annualized_volatility = volatility * np.sqrt(60 * 24 * 252)
    return round(annualized_volatility, 2)

def add_volatility(row):
    try:
        res = session.get(f'https://clob.polymarket.com/prices-history?interval=1m&market={row["token1"]}&fidelity=10', timeout=5)
        history = res.json().get('history', [])
        if not history:
            row_dict = row.copy()
            stats = {k: 0 for k in ['1_hour', '3_hour', '6_hour', '12_hour', '24_hour', '7_day', '14_day', '30_day', 'volatility_price', 'burst_index']}
            return {**row_dict, **stats}
            
        price_df = pd.DataFrame(history)
        price_df['t'] = pd.to_datetime(price_df['t'], unit='s')
        price_df['p'] = price_df['p'].round(2)
        price_df['log_return'] = np.log(price_df['p'] / price_df['p'].shift(1))

        # 计算 burst_index：仅在「非零变动」上统计集中度，避免大量 0 导致指数全接近 1
        rets = price_df['log_return'].dropna().values
        burst_index = 0.0
        abs_rets = np.abs(rets)
        abs_rets_nz = abs_rets[abs_rets > 1e-6]
        N_min = 20
        alpha = 0.8
        if len(abs_rets_nz) < N_min:
            burst_index = 0.0
        else:
            S = abs_rets_nz.sum()
            if S > 0:
                sorted_abs = np.sort(abs_rets_nz)[::-1]
                cumsum = np.cumsum(sorted_abs)
                threshold = alpha * S
                k = int((cumsum >= threshold).argmax()) + 1
                f = k / len(abs_rets_nz)
                burst_index = float(max(0.0, min(1.0, 1.0 - f)))

        row_dict = row.copy()
        stats = {
            '1_hour': calculate_annualized_volatility(price_df, 1),
            '3_hour': calculate_annualized_volatility(price_df, 3),
            '6_hour': calculate_annualized_volatility(price_df, 6),
            '12_hour': calculate_annualized_volatility(price_df, 12),
            '24_hour': calculate_annualized_volatility(price_df, 24),
            '7_day': calculate_annualized_volatility(price_df, 24 * 7),
            '14_day': calculate_annualized_volatility(price_df, 24 * 14),
            '30_day': calculate_annualized_volatility(price_df, 24 * 30),
            'volatility_price': price_df['p'].iloc[-1] if not price_df.empty else 0,
            'burst_index': burst_index,
        }
        return {**row_dict, **stats}
    except:
        return row

def add_volatility_to_df(df, max_workers=5):
    results = []
    df = df.reset_index(drop=True)
    def process_volatility_with_progress(args):
        idx, row = args
        try:
            return add_volatility(row.to_dict())
        except:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_volatility_with_progress, (idx, row)) for idx, row in df.iterrows()]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result is not None:
                results.append(result)
            if len(results) % (max_workers * 2) == 0:
                print(f'Volatility: {len(results)} of {len(df)}')
    return pd.DataFrame(results)

def get_markets(all_results, sel_df, maker_reward=1):
    new_df = pd.DataFrame(all_results)
    if not new_df.empty:
        new_df['spread'] = abs(new_df['best_ask'] - new_df['best_bid'])
        new_df = new_df.sort_values('rewards_daily_rate', ascending=False)
        new_df[' '] = ''
        
        cols = ['question', 'answer1', 'answer2', 'neg_risk', 'spread', 'best_bid', 'best_ask', 'rewards_daily_rate', 'bid_reward_per_100', 'ask_reward_per_100', 'gm_reward_per_100', 'mid_reward_per_100', 'sm_reward_per_100', 'min_size', 'max_spread', 'tick_size', 'market_slug', 'token1', 'token2', 'condition_id', 'volume', 'end_date']
        for c in cols:
            if c not in new_df.columns:
                new_df[c] = 0 if c == 'volume' else ''
                
        new_df = new_df[cols]
        new_df = new_df.replace([np.inf, -np.inf], 0)
    
    all_data = new_df.copy()
    s_df = new_df.copy()
    
    making_markets = pd.DataFrame()
    if not s_df.empty:
        making_markets = s_df[~new_df['question'].isin(sel_df['question'])]
        making_markets = making_markets.sort_values('gm_reward_per_100', ascending=False)
        making_markets = making_markets[making_markets['gm_reward_per_100'] >= maker_reward]
        
    all_markets = get_combined_markets(new_df, making_markets, sel_df)    
    return all_data, all_markets