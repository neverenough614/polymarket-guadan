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

# Rust 策略 JSON 导出路径
RUST_STRATEGY_JSON_PATH = os.path.join(os.path.dirname(__file__), "poly_maker_rs", "strategy_tokens.json")

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

# 4. 高奖励激进策略表 (High Reward Aggressive)
try:
    wk_aggressive = spreadsheet.worksheet("High Reward Aggressive")
except:
    print("Creating new worksheet: High Reward Aggressive")
    wk_aggressive = spreadsheet.add_worksheet(title="High Reward Aggressive", rows=100, cols=20)

sel_df = get_sel_df(spreadsheet, "Selected Markets")

# 5. 新增奖励监控表
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


# ================= Rust JSON 自动导出 =================

def export_strategy_tokens_json(normal_df: pd.DataFrame, aggressive_df: pd.DataFrame):
    """
    将 Normal LP 和 High Reward Aggressive 策略表导出为 Rust 版本使用的 strategy_tokens.json。
    与 main.py 的 _parse_sheet_tokens() 使用相同的解析逻辑。
    注意：不应用黑名单过滤（Rust 版本自行处理黑名单，标记 blacklisted 而非跳过）。
    """
    tokens = []
    seen_token_ids = {}

    def _find_col(df, name):
        for col in df.columns:
            if col.lower().replace(" ", "_") == name.lower().replace(" ", "_"):
                return col
        return None

    def _parse_df(df, source_label):
        if df is None or df.empty:
            return 0

        min_size_col = _find_col(df, "min_size")
        neg_risk_col = _find_col(df, "neg_risk")
        max_spread_col = _find_col(df, "max_spread")
        vol_col = _find_col(df, "volatility_sum")

        added = 0
        for _, row in df.iterrows():
            question = str(row.get("question", "Unknown")).strip()
            if not question or question.lower() in ("", "nan", "none"):
                continue
            # 跳过占位行
            if "当前无" in question:
                continue

            # min_size
            try:
                min_size = float(str(row.get(min_size_col, 10)).replace(",", "")) if min_size_col else 10.0
                if min_size <= 0:
                    min_size = 10.0
            except Exception:
                min_size = 10.0

            # neg_risk
            neg_risk = False
            if neg_risk_col:
                nr_val = str(row.get(neg_risk_col, "")).strip().lower()
                neg_risk = nr_val in ("true", "1", "yes")

            # max_spread（表格中单位是美分，需 /100）
            max_spread = None
            if max_spread_col:
                try:
                    ms_val = str(row.get(max_spread_col, "")).strip()
                    if ms_val and ms_val.lower() not in ("", "nan", "none", "0"):
                        raw = float(ms_val)
                        if raw > 0:
                            max_spread = raw / 100.0  # 美分 → 小数
                except Exception:
                    max_spread = None

            # volatility_sum
            vol_sum = 0.0
            if vol_col:
                try:
                    vol_sum = float(str(row.get(vol_col, 0)).replace(",", ""))
                except Exception:
                    vol_sum = 0.0

            def add_token(token_id, token_type):
                nonlocal added
                if token_id not in seen_token_ids:
                    seen_token_ids[token_id] = len(tokens)
                    tokens.append({
                        "token_id": token_id,
                        "token_type": token_type,
                        "question": question,
                        "min_size": min_size,
                        "neg_risk": neg_risk,
                        "max_spread": max_spread,
                        "volatility_sum": vol_sum,
                        "source": source_label,
                    })
                    added += 1
                else:
                    idx = seen_token_ids[token_id]
                    tokens[idx]["min_size"] = max(tokens[idx]["min_size"], min_size)
                    if max_spread is not None:
                        tokens[idx]["max_spread"] = max_spread

            t1 = str(row.get("token1", "")).strip()
            if t1 and len(t1) > 10 and t1.lower() != "nan":
                add_token(t1, "YES")

            if "token2" in df.columns:
                t2 = str(row.get("token2", "")).strip()
                if t2 and len(t2) > 10 and t2.lower() != "nan":
                    add_token(t2, "NO")

        return added

    n1 = _parse_df(normal_df, "Normal LP")
    n2 = _parse_df(aggressive_df, "High Reward")

    if not tokens:
        print("⚠️ [Rust JSON] 无 token 可导出，跳过")
        return

    try:
        with open(RUST_STRATEGY_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(tokens, f, indent=2, ensure_ascii=False)
        print(f"✅ [Rust JSON] 已导出 {len(tokens)} 个 token → {RUST_STRATEGY_JSON_PATH}")
        print(f"   Normal LP: {n1}, High Reward: {n2}")
    except Exception as e:
        print(f"⚠️ [Rust JSON] 导出失败: {e}")


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
    numeric_cols = ['spread', 'gm_reward_per_100', 'mid_reward_per_100', 'volatility_sum', 'burst_index', 'best_bid', 'best_ask', 'volume'] + vol_cols

    for col in numeric_cols:
        if col in sdf.columns:
            sdf[col] = pd.to_numeric(sdf[col], errors='coerce').fillna(0)
    
    # 计算 RV Ratio（保留原版 + 新增 mid 版本）
    if 'gm_reward_per_100' in sdf.columns and 'volatility_sum' in sdf.columns:
        sdf['rv_ratio'] = sdf['gm_reward_per_100'] / (sdf['volatility_sum'] + 0.001)
    if 'mid_reward_per_100' in sdf.columns and 'volatility_sum' in sdf.columns:
        sdf['mid_rv_ratio'] = sdf['mid_reward_per_100'] / (sdf['volatility_sum'] + 0.001)
    
    # === [修改点 2] 将详细波动率加入显示列表 ===
    priority_cols = [
        'question', 'mid_rv_ratio', 'rv_ratio', 'mid_reward_per_100', 'gm_reward_per_100', 'spread',
        'volatility_sum'] + vol_cols + [  # 把详细波动率插在这里
        'burst_index', 'volume', 'days_to_expiry',
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

    # 🔥 性能优化：提前过滤无奖励市场，避免对 3000+ 个无奖励市场调用订单簿 API
    def _has_reward(rewards):
        """检查市场是否有流动性奖励"""
        if not isinstance(rewards, dict):
            return False
        rates = rewards.get('rates', [])
        if not rates:
            return False
        for rate_info in rates:
            if rate_info.get('rewards_daily_rate', 0) > 0:
                return True
        return False

    if 'rewards' in all_df.columns:
        rewarded_df = all_df[all_df['rewards'].apply(_has_reward)].reset_index(drop=True)
        print(f"🔥 [性能优化] 有奖励的市场: {len(rewarded_df)} / {len(all_df)}（跳过 {len(all_df) - len(rewarded_df)} 个无奖励市场）")
    else:
        rewarded_df = all_df
        print(f"⚠️ 未找到 rewards 列，处理全部 {len(all_df)} 个市场")

    all_results = get_all_results(rewarded_df, client)
    print("Got all Results")
    m_data, all_markets = get_markets(all_results, sel_df, maker_reward=0.75)
    print(f"Got all orderbook. Total markets: {len(all_markets)}")

    # 2. 计算波动率（并发数从默认5提升到15，加速波动率获取）
    new_df = add_volatility_to_df(all_markets, max_workers=15)
    
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
    # 条件：mid_reward_per_100 >= 0.5；burst_index <= 0.5；24_hour <= 25（日级波动可控）
    # 保留：0.02 <= Spread <= 0.04，到期 >7天 或无到期日
    # 排序：mid_rv_ratio 降序
    _has_24h = '24_hour' in master_df.columns
    _has_burst = 'burst_index' in master_df.columns
    normal_mask = (
        (master_df['spread'] >= 0.01) &
        (master_df['spread'] <= 0.04) &
        (master_df['mid_reward_per_100'] >= 0.5) &
        ((master_df['days_to_expiry'] > 7) | (master_df['days_to_expiry'] == 0))
    )
    if _has_burst:
        normal_mask = normal_mask & (master_df['burst_index'] <= 0.5)
    if _has_24h:
        normal_mask = normal_mask & (master_df['24_hour'] <= 25)
    normal_lp_df = master_df[normal_mask].sort_values('mid_rv_ratio', ascending=False)

    # --- 策略 3: 高奖励激进策略 (High Reward Aggressive) ---
    # 目标：博收益，刀口舔血，高奖励覆盖风险，靠防御快速撤单
    # 要求：rewards_daily_rate >= 100（每日总奖励 >= $100）
    # gm_reward_per_100 >= 2.0（每$100挂单奖励 >= $2/天）
    # Spread: 2-12c（放宽，高奖励市场点差通常较宽）
    # 波动率：不限（靠超敏感防御保护）
    # 到期：>3天 或无到期日（短期也可以）
    # 排序：gm_reward_per_100 降序（奖励率最高的优先）
    # 确保 rewards_daily_rate 列存在
    if 'rewards_daily_rate' in master_df.columns:
        master_df['rewards_daily_rate'] = pd.to_numeric(master_df['rewards_daily_rate'], errors='coerce').fillna(0)
        aggressive_df = master_df[
            (master_df['rewards_daily_rate'] >= 100) &
            (master_df['gm_reward_per_100'] >= 2.0) &
            (master_df['spread'] >= 0.02) &
            (master_df['spread'] <= 0.12) &
            ((master_df['days_to_expiry'] > 3) | (master_df['days_to_expiry'] == 0))
        ].sort_values('gm_reward_per_100', ascending=False)
    else:
        aggressive_df = pd.DataFrame()

    print(f"Strategy Matches Found:")
    print(f"  - Smart LP (Master): {len(smart_df)}")
    print(f"  - Blue Ocean: {len(blue_ocean_df)}")
    print(f"  - Normal LP: {len(normal_lp_df)}")
    print(f"  - High Reward Aggressive: {len(aggressive_df)}")

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
            
            # 4. 更新 High Reward Aggressive (新表)
            if not aggressive_df.empty:
                update_sheet(aggressive_df, wk_aggressive)
                print(f"-> Updated 'High Reward Aggressive' ({len(aggressive_df)} markets)")
            else:
                # 无符合条件的市场时写入提示
                empty_df = pd.DataFrame([{'question': '当前无符合条件的高奖励市场'}])
                update_sheet(empty_df, wk_aggressive)
                print("-> Updated 'High Reward Aggressive' (无符合条件市场)")
            
            # 5. 更新其他基础表 (全量更新时才做)
            if len(master_df) > 20:
                update_sheet(master_df, wk_all)
                print("-> Updated 'All Markets'")
                update_sheet(m_data, wk_full)
                print("-> Updated 'Full Markets'")
            
            print(f"[{pd.to_datetime('now')}] Update Cycle Completed Successfully.")
            
        except Exception as e:
            print(f"Error updating sheets: {e}")
            traceback.print_exc()

        # ================== 自动导出 Rust strategy_tokens.json ==================
        try:
            export_strategy_tokens_json(normal_lp_df, aggressive_df)
        except Exception as e:
            print(f"⚠️ [Rust JSON] 导出异常: {e}")
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