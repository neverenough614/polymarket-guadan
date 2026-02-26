import time
import json
import os
import requests
import pandas as pd
import numpy as np
import traceback
from datetime import datetime

# 引入项目模块
from data_updater.trading_utils import get_clob_client
from data_updater.google_utils import get_spreadsheet
from data_updater.find_markets import get_sel_df, get_all_markets, get_all_results, get_markets, add_volatility_to_df
from gspread_dataframe import set_with_dataframe

# 奖励快照文件路径（用于跨轮次对比）
REWARD_SNAPSHOT_FILE = "reward_snapshot.json"

# ================= Global Setup =================
spreadsheet = get_spreadsheet()
client = get_clob_client()

# 定义 Worksheets
wk_all = spreadsheet.worksheet("All Markets")
wk_vol = spreadsheet.worksheet("Volatility Markets")
wk_full = spreadsheet.worksheet("Full Markets")

# 1. 总览表 (宽筛选)
try:
    wk_smart = spreadsheet.worksheet("Smart LP Strategy")
except:
    print("Creating new worksheet: Smart LP Strategy")
    wk_smart = spreadsheet.add_worksheet(title="Smart LP Strategy", rows=100, cols=20)

# 2. 蓝海策略表 (宽点差、高息、低波)
try:
    wk_blue = spreadsheet.worksheet("Blue Ocean Strategy")
except:
    print("Creating new worksheet: Blue Ocean Strategy")
    wk_blue = spreadsheet.add_worksheet(title="Blue Ocean Strategy", rows=100, cols=20)

# 3. 正常稳健表 (适中点差、稳健)
try:
    wk_normal = spreadsheet.worksheet("Normal LP Strategy")
except:
    print("Creating new worksheet: Normal LP Strategy")
    wk_normal = spreadsheet.add_worksheet(title="Normal LP Strategy", rows=100, cols=20)

sel_df = get_sel_df(spreadsheet, "Selected Markets")

# 4. 新增奖励监控表
try:
    wk_rewards = spreadsheet.worksheet("New Rewards Alert")
except:
    print("Creating new worksheet: New Rewards Alert")
    wk_rewards = spreadsheet.add_worksheet(title="New Rewards Alert", rows=200, cols=15)

# ================= 流动性奖励监控 =================

def load_reward_snapshot() -> dict:
    """从本地文件加载上一次的奖励快照 {condition_id: reward_per_100}"""
    if os.path.exists(REWARD_SNAPSHOT_FILE):
        try:
            with open(REWARD_SNAPSHOT_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_reward_snapshot(snapshot: dict):
    """保存本次奖励快照到本地文件"""
    try:
        with open(REWARD_SNAPSHOT_FILE, 'w') as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        print(f"⚠️ 保存奖励快照失败: {e}")


def fetch_reward_changes(m_data: pd.DataFrame) -> pd.DataFrame:
    """
    使用原始全量数据 m_data（含 rewards_daily_rate、condition_id 等字段），
    与上次快照对比，返回每日奖励总量增加的市场 DataFrame。
    
    监控目标：用户向已存在的市场追加了流动性奖励（rewards_daily_rate 增加）。
    rewards_daily_rate 是每日奖励总量（USDC），由市场发起者或用户追加设定，
    不随订单簿深度变化，是判断"是否有人追加奖励"的最直接指标。
    """
    print("\n🔍 [奖励监控] 正在分析每日奖励总量变化...")

    if m_data is None or m_data.empty:
        print("   ⚠️ m_data 为空，跳过奖励监控")
        return pd.DataFrame()

    reward_col = 'rewards_daily_rate'
    if reward_col not in m_data.columns:
        print(f"   ⚠️ 找不到 '{reward_col}' 列，跳过奖励监控")
        return pd.DataFrame()

    # 只处理有奖励的市场（rewards_daily_rate > 0）
    rewarded_df = m_data[pd.to_numeric(m_data[reward_col], errors='coerce').fillna(0) > 0].copy()
    if rewarded_df.empty:
        print("   ℹ️ 当前没有任何有奖励的市场")
        save_reward_snapshot({})
        return pd.DataFrame()

    print(f"   📊 本轮共有 {len(rewarded_df)} 个有奖励市场")

    # 构建本次快照 {condition_id: rewards_daily_rate}
    current_snapshot = {}
    rows = []
    for _, row in rewarded_df.iterrows():
        cid = str(row.get('condition_id', '')).strip()
        if not cid:
            continue
        daily_rate = float(pd.to_numeric(row.get(reward_col, 0), errors='coerce') or 0)
        current_snapshot[cid] = daily_rate
        rows.append({
            'condition_id':      cid,
            'question':          str(row.get('question', '')),
            'new_daily_rate':    daily_rate,
            'gm_reward_per_100': float(pd.to_numeric(row.get('gm_reward_per_100', 0), errors='coerce') or 0),
            'spread':            float(pd.to_numeric(row.get('spread', 0), errors='coerce') or 0),
            'volume':            float(pd.to_numeric(row.get('volume', 0), errors='coerce') or 0),
            'token1':            str(row.get('token1', '')),
            'token2':            str(row.get('token2', '')),
        })

    # 加载上次快照
    prev_snapshot = load_reward_snapshot()

    # 找出每日奖励总量增加的市场（阈值：增加超过 1 USDC/天）
    CHANGE_THRESHOLD = 1.0
    changes = []
    for row in rows:
        cid            = row['condition_id']
        new_daily_rate = row['new_daily_rate']
        prev_daily_rate = prev_snapshot.get(cid, 0)

        if new_daily_rate > prev_daily_rate + CHANGE_THRESHOLD:
            row['prev_daily_rate']  = prev_daily_rate
            row['reward_added']     = round(new_daily_rate - prev_daily_rate, 2)
            row['detected_at']      = datetime.now().strftime('%Y-%m-%d %H:%M')
            changes.append(row)

    # 保存本次快照（无论有没有变化都更新）
    save_reward_snapshot(current_snapshot)

    if not changes:
        print(f"   ✅ 本轮无追加奖励（阈值: +{CHANGE_THRESHOLD} USDC/天）")
        return pd.DataFrame()

    print(f"   🎉 发现 {len(changes)} 个奖励追加！")
    for c in changes:
        print(f"      💰 {c['question'][:40]}... "
              f"每日奖励: {c['prev_daily_rate']:.1f} → {c['new_daily_rate']:.1f} USDC/天 (+{c['reward_added']:.1f})")

    df = pd.DataFrame(changes)
    df = df.sort_values('reward_added', ascending=False)
    col_order = ['question', 'prev_daily_rate', 'new_daily_rate', 'reward_added',
                 'gm_reward_per_100', 'spread', 'volume', 'token1', 'token2', 'condition_id', 'detected_at']
    col_order = [c for c in col_order if c in df.columns]
    return df[col_order]


# ================= Helper Functions =================

def update_sheet(data, worksheet):
    all_values = worksheet.get_all_values()
    existing_num_rows = len(all_values)
    existing_num_cols = len(all_values[0]) if all_values else 0
    num_rows, num_cols = data.shape
    max_rows = max(num_rows, existing_num_rows)
    max_cols = max(num_cols, existing_num_cols)
    padded_data = pd.DataFrame('', index=range(max_rows), columns=range(max_cols))
    padded_data.iloc[:num_rows, :num_cols] = data.values
    padded_data.columns = list(data.columns) + [''] * (max_cols - num_cols)
    set_with_dataframe(worksheet, padded_data, include_index=False, include_column_header=True, resize=True)

def clean_and_prepare_data(df):
    """
    统一的数据清洗和类型转换
    """
    sdf = df.copy()
    
    # === [修改点 1] 定义所有波动率列名 ===
    vol_cols = ['1_hour', '3_hour', '6_hour', '12_hour', '24_hour', '7_day', '30_day']
    
    # 确保关键指标是数值型 (加入 vol_cols 以便排序)
    numeric_cols = ['spread', 'gm_reward_per_100', 'volatility_sum', 'best_bid', 'best_ask', 'volume'] + vol_cols
    
    for col in numeric_cols:
        if col in sdf.columns:
            sdf[col] = pd.to_numeric(sdf[col], errors='coerce').fillna(0)
    
    # 计算 RV Ratio
    if 'gm_reward_per_100' in sdf.columns and 'volatility_sum' in sdf.columns:
        sdf['rv_ratio'] = sdf['gm_reward_per_100'] / (sdf['volatility_sum'] + 0.001)
    
    # === [修改点 2] 将详细波动率加入显示列表 ===
    priority_cols = [
        'question', 'rv_ratio', 'gm_reward_per_100', 'spread', 
        'volatility_sum'] + vol_cols + [ # 把详细波动率插在这里
        'volume', 'days_to_expiry', 
        'best_bid', 'best_ask', 'answer1', 'market_slug', 'end_date'
    ]
    
    # 确保只取存在的列
    final_cols = [c for c in priority_cols if c in sdf.columns]
    remaining_cols = [c for c in sdf.columns if c not in final_cols]
    
    return sdf[final_cols + remaining_cols]

def fetch_and_process_data():
    global spreadsheet, client, wk_all, wk_vol, wk_smart, wk_blue, wk_normal, wk_full, wk_rewards, sel_df
    
    print(f"[{pd.to_datetime('now')}] Starting fetch cycle...")

    # 1. 获取数据
    all_df = get_all_markets(client)
    print(f"Got all Markets: {len(all_df)}")
    all_results = get_all_results(all_df, client)
    print("Got all Results")
    m_data, all_markets = get_markets(all_results, sel_df, maker_reward=0.75)
    print(f"Got all orderbook. Total markets: {len(all_markets)}")

    # 2. 计算波动率
    new_df = add_volatility_to_df(all_markets)
    
    # 3. 基础数据处理
    for col in ['24_hour', '7_day', '14_day']:
        if col in new_df.columns:
            new_df[col] = pd.to_numeric(new_df[col], errors='coerce').fillna(0)
    new_df['volatility_sum'] = new_df['24_hour'] + new_df['7_day'] + new_df['14_day']
    
    # 计算日期
    if 'end_date' in new_df.columns:
        try:
            new_df['end_date'] = pd.to_datetime(new_df['end_date'], errors='coerce')
            now = pd.Timestamp.now()
            # 处理时区
            if new_df['end_date'].dt.tz is None:
                new_df['days_to_expiry'] = (new_df['end_date'] - now).dt.days
            else:
                new_df['days_to_expiry'] = (new_df['end_date'].dt.tz_localize(None) - now).dt.days
        except:
            new_df['days_to_expiry'] = 0

    # 4. 准备用于筛选的 Clean DataFrame
    master_df = clean_and_prepare_data(new_df)

    # ================== 执行三大策略筛选 ==================

    print("Applying filters...")

    # --- 策略 0: Smart LP 总览 (宽口径，用于查漏补缺) ---
    # 只要不是死盘(Volume>0)且点差不过分(Spread<0.5)都放进来
    smart_df = master_df[
        (master_df['spread'] >= 0.005) & 
        (master_df['spread'] <= 0.50) &
        (master_df['gm_reward_per_100'] > 0.5)
    ].sort_values('rv_ratio', ascending=False)

    # --- 策略 1: 蓝海策略 (Blue Ocean) ---
    # 要求：0.06 < Spread <= 0.10, Reward > 1.5, Volatility < 30
    # 到期：>7天 或无到期日（days_to_expiry==0 表示无到期日/解析失败，应放进来）
    blue_ocean_df = master_df[
        (master_df['spread'] > 0.06) & 
        (master_df['spread'] <= 0.10) & 
        (master_df['gm_reward_per_100'] > 1.5) & 
        (master_df['volatility_sum'] < 30) &
        ((master_df['days_to_expiry'] > 7) | (master_df['days_to_expiry'] == 0))
    ].sort_values('gm_reward_per_100', ascending=False)

    # --- 策略 2: 正常稳健策略 (Normal LP) ---
    # 要求：0.02 <= Spread <= 0.06, Volatility < 50, Reward > 0.5
    # 到期：>7天 或无到期日
    # 排序：rv_ratio 降序（奖励/波动率性价比最高的优先）
    normal_lp_df = master_df[
        (master_df['spread'] >= 0.02) & 
        (master_df['spread'] <= 0.06) & 
        (master_df['volatility_sum'] < 50) & 
        (master_df['gm_reward_per_100'] > 0.5) &
        ((master_df['days_to_expiry'] > 7) | (master_df['days_to_expiry'] == 0))
    ].sort_values('rv_ratio', ascending=False)

    print(f"Strategy Matches Found:")
    print(f"  - Smart LP (Master): {len(smart_df)}")
    print(f"  - Blue Ocean: {len(blue_ocean_df)}")
    print(f"  - Normal LP: {len(normal_lp_df)}")

    # ================== 更新 Google Sheets ==================
    if len(master_df) > 0:
        try:
            print("Updating Sheets...")
            # 1. 更新 Smart LP (总表)
            update_sheet(smart_df, wk_smart)
            print("-> Updated 'Smart LP Strategy'")
            
            # 2. 更新 Blue Ocean (新表)
            update_sheet(blue_ocean_df, wk_blue)
            print("-> Updated 'Blue Ocean Strategy'")
            
            # 3. 更新 Normal LP (新表)
            update_sheet(normal_lp_df, wk_normal)
            print("-> Updated 'Normal LP Strategy'")
            
            # 4. 更新其他基础表 (全量更新时才做)
            if len(master_df) > 20:
                update_sheet(master_df, wk_all)
                print("-> Updated 'All Markets'")
                update_sheet(m_data, wk_full)
                print("-> Updated 'Full Markets'")
            
            print(f"[{pd.to_datetime('now')}] Update Cycle Completed Successfully.")
            
        except Exception as e:
            print(f"Error updating sheets: {e}")
            traceback.print_exc()
    else:
        print("No data found to update.")

    # ================== 流动性奖励监控 ==================
    # 注意：使用 m_data（原始全量数据，含 rewards_daily_rate 字段）
    # 而非 master_df（master_df 经过 clean_and_prepare_data 处理，可能丢失该字段）
    try:
        reward_changes_df = fetch_reward_changes(m_data)
        if not reward_changes_df.empty:
            update_sheet(reward_changes_df, wk_rewards)
            print(f"-> Updated 'New Rewards Alert' ({len(reward_changes_df)} 条变化)")
        else:
            # 无变化时清空表格，只保留标题行提示
            no_change_df = pd.DataFrame([{
                'question': '✅ 本轮未发现新增/追加奖励',
                'prev_daily_rate': '',
                'new_daily_rate': '',
                'reward_added': '',
                'gm_reward_per_100': '',
                'spread': '',
                'volume': '',
                'token1': '',
                'token2': '',
                'condition_id': '',
                'detected_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
            }])
            update_sheet(no_change_df, wk_rewards)
            print("-> Updated 'New Rewards Alert' (无变化)")
    except Exception as e:
        print(f"⚠️ 奖励监控更新失败: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    print("Starting Data Updater with Specific Strategies...")
    while True:
        try:
            fetch_and_process_data()
            print("Sleeping for 1 hour...")
            time.sleep(60 * 60) 
        except KeyboardInterrupt:
            print("Stopping...")
            break
        except Exception as e:
            traceback.print_exc()
            print(str(e))
            print("Error encountered. Retrying in 60 seconds...")
            time.sleep(60)