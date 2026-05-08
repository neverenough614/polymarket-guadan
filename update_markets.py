import time
import json
import os
import shutil
import traceback
from datetime import datetime

import pandas as pd
from gspread_dataframe import set_with_dataframe

# 引入项目模块
from data_updater.trading_utils import get_clob_client
from data_updater.google_utils import get_spreadsheet
from data_updater.find_markets import (
    get_sel_df, get_all_markets, get_all_results,
    get_markets, add_volatility_to_df, batch_fetch_volumes,
)

# ================= 配置常量 =================

# 文件路径
REWARD_SNAPSHOT_FILE = "reward_snapshot.json"
RUST_STRATEGY_JSON_PATH = os.path.join(
    os.path.dirname(__file__), "poly_maker_rs", "strategy_tokens.json"
)

# Smart LP 策略阈值
SMART_SPREAD_MIN = 0.005
SMART_SPREAD_MAX = 0.50
SMART_REWARD_MIN = 0.5

# Blue Ocean 策略阈值
BLUE_SPREAD_MIN = 0.06
BLUE_SPREAD_MAX = 0.10
BLUE_REWARD_MIN = 1.5
BLUE_VOL_MAX = 30
BLUE_DAYS_MIN = 7

# Normal LP 策略阈值
NORMAL_SPREAD_MIN = 0.01
NORMAL_SPREAD_MAX = 0.04
NORMAL_MID_REWARD_MIN = 0.5
NORMAL_BURST_MAX = 0.5
NORMAL_24H_VOL_MAX = 25
NORMAL_DAYS_MIN = 7

# High Reward Aggressive 策略阈值
AGG_DAILY_RATE_MIN = 100
AGG_REWARD_MIN = 2.0
AGG_SPREAD_MIN = 0.02
AGG_SPREAD_MAX = 0.12
AGG_DAYS_MIN = 3

# Small Edge 策略阈值：小仓位、低竞争、高 reward efficiency
SMALL_EDGE_DAILY_RATE_MIN = 3
SMALL_EDGE_DAILY_RATE_MAX = 150
SMALL_EDGE_MIN_SIZE_MAX = 200
SMALL_EDGE_EFFICIENCY_MIN = 0.5
SMALL_EDGE_SPREAD_MAX = 0.08
SMALL_EDGE_DAYS_MIN = 3
SMALL_EDGE_ORDER_SIZE = 100.0
SMALL_EDGE_MAX_ORDER_SIZE = 300.0
SMALL_EDGE_BEST_BID_MIN = 0.10
SMALL_EDGE_BEST_BID_MAX = 0.90

# 奖励变化检测阈值（USDC/天）
REWARD_CHANGE_THRESHOLD = 1.0

# Google Sheets 写入重试次数
SHEETS_MAX_RETRIES = 3
ENABLE_VOLUME_FETCH = False
EXPORT_RUST_JSON = False


# ================= 客户端初始化 =================

_cached_sel_df = None


def _get_or_create_worksheet(spreadsheet, name, rows=100, cols=20):
    """获取或创建 worksheet"""
    try:
        return spreadsheet.worksheet(name)
    except Exception:
        print(f"Creating new worksheet: {name}")
        return spreadsheet.add_worksheet(title=name, rows=rows, cols=cols)


def _init_clients():
    """
    每轮循环重新初始化客户端和 worksheet 句柄。
    避免 gspread auth token 过期导致后续 API 调用失败。
    """
    global _cached_sel_df

    spreadsheet = get_spreadsheet()
    client = get_clob_client()

    worksheets = {
        'all':        spreadsheet.worksheet("All Markets"),
        'full':       spreadsheet.worksheet("Full Markets"),
        'smart':      _get_or_create_worksheet(spreadsheet, "Smart LP Strategy"),
        'blue':       _get_or_create_worksheet(spreadsheet, "Blue Ocean Strategy"),
        'normal':     _get_or_create_worksheet(spreadsheet, "Normal LP Strategy"),
        'small_edge': _get_or_create_worksheet(spreadsheet, "Small Edge Strategy"),
        'aggressive': _get_or_create_worksheet(spreadsheet, "High Reward Aggressive"),
        'rewards':    _get_or_create_worksheet(spreadsheet, "New Rewards Alert", rows=200, cols=15),
    }

    if _cached_sel_df is None:
        _cached_sel_df = get_sel_df(spreadsheet, "Selected Markets")

    return client, worksheets, _cached_sel_df


# ================= 流动性奖励监控 =================

def load_reward_snapshot() -> dict:
    """从本地文件加载上一次的奖励快照 {condition_id: reward_per_100}"""
    if os.path.exists(REWARD_SNAPSHOT_FILE):
        try:
            with open(REWARD_SNAPSHOT_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            print(f"⚠️ reward_snapshot.json 格式异常（期望 dict，实际 {type(data).__name__}），重置")
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ 加载奖励快照失败: {e}，重置")
    return {}


def save_reward_snapshot(snapshot: dict):
    """原子写入：先写临时文件再 rename，避免写入中途崩溃损坏文件"""
    try:
        tmp_path = REWARD_SNAPSHOT_FILE + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(snapshot, f, indent=2)
        shutil.move(tmp_path, REWARD_SNAPSHOT_FILE)
    except OSError as e:
        print(f"⚠️ 保存奖励快照失败: {e}")


def _has_reward(rewards) -> bool:
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

    # 找出每日奖励总量增加的市场
    changes = []
    for row in rows:
        cid            = row['condition_id']
        new_daily_rate = row['new_daily_rate']
        prev_daily_rate = prev_snapshot.get(cid, 0)

        if new_daily_rate > prev_daily_rate + REWARD_CHANGE_THRESHOLD:
            row['prev_daily_rate']  = prev_daily_rate
            row['reward_added']     = round(new_daily_rate - prev_daily_rate, 2)
            row['detected_at']      = datetime.now().strftime('%Y-%m-%d %H:%M')
            changes.append(row)

    # 保存本次快照（无论有没有变化都更新）
    save_reward_snapshot(current_snapshot)

    if not changes:
        print(f"   ✅ 本轮无追加奖励（阈值: +{REWARD_CHANGE_THRESHOLD} USDC/天）")
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

def export_strategy_tokens_json(normal_df: pd.DataFrame, aggressive_df: pd.DataFrame, small_edge_df: pd.DataFrame = None):
    """
    将 Normal LP 和 High Reward Aggressive 策略表导出为 Rust 版本使用的 strategy_tokens.json。
    与 main.py 的 _parse_sheet_tokens() 使用相同的解析逻辑。
    注意：不应用黑名单过滤（Rust 版本自行处理黑名单，标记 blacklisted 而非跳过）。
    """
    tokens_by_id = {}  # token_id -> token dict

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
        order_size_col = _find_col(df, "small_edge_order_size") or _find_col(df, "order_size")
        reward_rate_col = _find_col(df, "rewards_daily_rate")

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
            except (ValueError, TypeError):
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
                except (ValueError, TypeError):
                    max_spread = None

            # volatility_sum
            vol_sum = 0.0
            if vol_col:
                try:
                    vol_sum = float(str(row.get(vol_col, 0)).replace(",", ""))
                except (ValueError, TypeError):
                    vol_sum = 0.0

            order_size = None
            if order_size_col:
                try:
                    raw_order_size = float(str(row.get(order_size_col, "")).replace(",", ""))
                    if raw_order_size > 0:
                        order_size = raw_order_size
                except (ValueError, TypeError):
                    order_size = None

            rewards_daily_rate = 0.0
            if reward_rate_col:
                try:
                    rewards_daily_rate = float(str(row.get(reward_rate_col, 0)).replace(",", ""))
                except (ValueError, TypeError):
                    rewards_daily_rate = 0.0

            def add_token(token_id, token_type):
                nonlocal added
                if token_id not in tokens_by_id:
                    token = {
                        "token_id": token_id,
                        "token_type": token_type,
                        "question": question,
                        "min_size": min_size,
                        "neg_risk": neg_risk,
                        "max_spread": max_spread,
                        "volatility_sum": vol_sum,
                        "source": source_label,
                    }
                    if order_size is not None:
                        token["small_edge_order_size"] = order_size
                    if rewards_daily_rate > 0:
                        token["rewards_daily_rate"] = rewards_daily_rate
                    tokens_by_id[token_id] = token
                    added += 1
                else:
                    existing = tokens_by_id[token_id]
                    existing["min_size"] = max(existing["min_size"], min_size)
                    if max_spread is not None:
                        existing["max_spread"] = max_spread
                    if order_size is not None:
                        existing["small_edge_order_size"] = order_size
                    if rewards_daily_rate > 0:
                        existing["rewards_daily_rate"] = rewards_daily_rate

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
    n3 = _parse_df(small_edge_df, "Small Edge")

    tokens = list(tokens_by_id.values())

    if not tokens:
        print("⚠️ [Rust JSON] 无 token 可导出，跳过")
        return

    try:
        with open(RUST_STRATEGY_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(tokens, f, indent=2, ensure_ascii=False)
        print(f"✅ [Rust JSON] 已导出 {len(tokens)} 个 token → {RUST_STRATEGY_JSON_PATH}")
        print(f"   Normal LP: {n1}, High Reward: {n2}, Small Edge: {n3}")
    except OSError as e:
        print(f"⚠️ [Rust JSON] 导出失败: {e}")


# ================= Helper Functions =================

def update_sheet(data, worksheet, retries=SHEETS_MAX_RETRIES):
    """带重试的 Sheet 写入，避免 clear 成功但 write 失败导致表格被清空"""
    for attempt in range(retries):
        try:
            worksheet.clear()
            set_with_dataframe(worksheet, data, include_index=False, include_column_header=True, resize=True)
            return
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"   ⚠️ Sheet 写入失败 (attempt {attempt + 1}/{retries}): {e}，{wait}s 后重试...")
                time.sleep(wait)
            else:
                raise


def clean_and_prepare_data(df):
    """统一的数据清洗和类型转换"""
    sdf = df.copy()

    vol_cols = ['1_hour', '3_hour', '6_hour', '12_hour', '24_hour', '7_day', '30_day']

    # 确保关键指标是数值型（含 rewards_daily_rate，避免后续重复转换）
    numeric_cols = [
        'spread', 'gm_reward_per_100', 'mid_reward_per_100', 'volatility_sum',
        'burst_index', 'best_bid', 'best_ask', 'volume', 'rewards_daily_rate',
    ] + vol_cols

    for col in numeric_cols:
        if col in sdf.columns:
            sdf[col] = pd.to_numeric(sdf[col], errors='coerce').fillna(0)

    # 计算 RV Ratio（保留原版 + 新增 mid 版本）
    if 'gm_reward_per_100' in sdf.columns and 'volatility_sum' in sdf.columns:
        sdf['rv_ratio'] = sdf['gm_reward_per_100'] / (sdf['volatility_sum'] + 0.001)
    if 'mid_reward_per_100' in sdf.columns and 'volatility_sum' in sdf.columns:
        sdf['mid_rv_ratio'] = sdf['mid_reward_per_100'] / (sdf['volatility_sum'] + 0.001)

    # 列排序（rewards_daily_rate 插入 spread 之后）
    priority_cols = [
        'question', 'mid_rv_ratio', 'rv_ratio', 'mid_reward_per_100', 'gm_reward_per_100', 'spread',
        'rewards_daily_rate', 'volatility_sum',
    ] + vol_cols + [
        'burst_index', 'volume', 'days_to_expiry',
        'best_bid', 'best_ask', 'answer1', 'market_slug', 'end_date',
    ]

    # 确保只取存在的列
    final_cols = [c for c in priority_cols if c in sdf.columns]
    remaining_cols = [c for c in sdf.columns if c not in final_cols]

    return sdf[final_cols + remaining_cols]


# ================= 策略筛选 =================

def _apply_strategy_filters(master_df):
    """对 master_df 应用四大策略筛选，返回各策略 DataFrame 的 dict"""
    master_df = master_df.copy()
    if 'rv_ratio' not in master_df.columns and {'gm_reward_per_100', 'volatility_sum'}.issubset(master_df.columns):
        master_df['rv_ratio'] = master_df['gm_reward_per_100'] / (master_df['volatility_sum'] + 0.001)
    if 'mid_rv_ratio' not in master_df.columns and {'mid_reward_per_100', 'volatility_sum'}.issubset(master_df.columns):
        master_df['mid_rv_ratio'] = master_df['mid_reward_per_100'] / (master_df['volatility_sum'] + 0.001)

    # --- 策略 0: Smart LP 总览 (宽口径，用于查漏补缺) ---
    smart_df = master_df[
        (master_df['spread'] >= SMART_SPREAD_MIN) &
        (master_df['spread'] <= SMART_SPREAD_MAX) &
        (master_df['gm_reward_per_100'] > SMART_REWARD_MIN)
    ].sort_values('rv_ratio', ascending=False)

    # --- 策略 1: 蓝海策略 (Blue Ocean) ---
    # 到期：>7天 或无到期日（days_to_expiry==0 表示无到期日/解析失败，应放进来）
    blue_ocean_df = master_df[
        (master_df['spread'] > BLUE_SPREAD_MIN) &
        (master_df['spread'] <= BLUE_SPREAD_MAX) &
        (master_df['gm_reward_per_100'] > BLUE_REWARD_MIN) &
        (master_df['volatility_sum'] < BLUE_VOL_MAX) &
        ((master_df['days_to_expiry'] > BLUE_DAYS_MIN) | (master_df['days_to_expiry'] == 0))
    ].sort_values('gm_reward_per_100', ascending=False)

    # --- 策略 2: 正常稳健策略 (Normal LP) ---
    _has_24h = '24_hour' in master_df.columns
    _has_burst = 'burst_index' in master_df.columns
    normal_mask = (
        (master_df['spread'] >= NORMAL_SPREAD_MIN) &
        (master_df['spread'] <= NORMAL_SPREAD_MAX) &
        (master_df['mid_reward_per_100'] >= NORMAL_MID_REWARD_MIN) &
        ((master_df['days_to_expiry'] > NORMAL_DAYS_MIN) | (master_df['days_to_expiry'] == 0))
    )
    if _has_burst:
        normal_mask = normal_mask & (master_df['burst_index'] <= NORMAL_BURST_MAX)
    if _has_24h:
        normal_mask = normal_mask & (master_df['24_hour'] <= NORMAL_24H_VOL_MAX)
    normal_lp_df = master_df[normal_mask].sort_values('mid_rv_ratio', ascending=False)

    # --- 策略 3: 高奖励激进策略 (High Reward Aggressive) ---
    if 'rewards_daily_rate' in master_df.columns:
        aggressive_df = master_df[
            (master_df['rewards_daily_rate'] >= AGG_DAILY_RATE_MIN) &
            (master_df['gm_reward_per_100'] >= AGG_REWARD_MIN) &
            (master_df['spread'] >= AGG_SPREAD_MIN) &
            (master_df['spread'] <= AGG_SPREAD_MAX) &
            ((master_df['days_to_expiry'] > AGG_DAYS_MIN) | (master_df['days_to_expiry'] == 0))
        ].sort_values('gm_reward_per_100', ascending=False)
    else:
        aggressive_df = pd.DataFrame()

    # --- 策略 4: Small Edge（小仓位高效率） ---
    small_edge_df = pd.DataFrame()
    small_edge_required = {
        'rewards_daily_rate', 'min_size', 'spread', 'best_bid', 'best_ask',
        'bid_reward_per_100', 'ask_reward_per_100', 'max_spread',
    }
    if small_edge_required.issubset(master_df.columns):
        small_edge_work = master_df.copy()
        small_edge_work['small_edge_efficiency'] = small_edge_work[
            ['bid_reward_per_100', 'ask_reward_per_100', 'mid_reward_per_100']
            if 'mid_reward_per_100' in small_edge_work.columns
            else ['bid_reward_per_100', 'ask_reward_per_100']
        ].max(axis=1)
        small_edge_work['small_edge_order_size'] = small_edge_work['min_size'].clip(
            lower=SMALL_EDGE_ORDER_SIZE,
            upper=SMALL_EDGE_MAX_ORDER_SIZE,
        )
        small_edge_work['small_edge_notional_est'] = (
            small_edge_work['small_edge_order_size'] * small_edge_work['best_bid'].clip(lower=0.01)
        ).round(2)
        small_edge_work['small_edge_score'] = (
            small_edge_work['small_edge_efficiency'] / (small_edge_work.get('volatility_sum', 0) + 1.0)
        ).round(4)
        small_edge_mask = (
            (small_edge_work['rewards_daily_rate'] >= SMALL_EDGE_DAILY_RATE_MIN) &
            (small_edge_work['rewards_daily_rate'] <= SMALL_EDGE_DAILY_RATE_MAX) &
            (small_edge_work['min_size'] <= SMALL_EDGE_MIN_SIZE_MAX) &
            (small_edge_work['small_edge_efficiency'] >= SMALL_EDGE_EFFICIENCY_MIN) &
            (small_edge_work['spread'] <= SMALL_EDGE_SPREAD_MAX) &
            (small_edge_work['best_bid'] > SMALL_EDGE_BEST_BID_MIN) &
            (small_edge_work['best_bid'] < SMALL_EDGE_BEST_BID_MAX) &
            ((small_edge_work['days_to_expiry'] > SMALL_EDGE_DAYS_MIN) | (small_edge_work['days_to_expiry'] == 0))
        )
        small_edge_df = small_edge_work[small_edge_mask].sort_values(
            ['small_edge_efficiency', 'small_edge_score'],
            ascending=False,
        )

    print(f"Strategy Matches Found:")
    print(f"  - Smart LP (Master): {len(smart_df)}")
    print(f"  - Blue Ocean: {len(blue_ocean_df)}")
    print(f"  - Normal LP: {len(normal_lp_df)}")
    print(f"  - High Reward Aggressive: {len(aggressive_df)}")
    print(f"  - Small Edge: {len(small_edge_df)}")

    return {
        'smart': smart_df,
        'blue': blue_ocean_df,
        'normal': normal_lp_df,
        'aggressive': aggressive_df,
        'small_edge': small_edge_df,
    }


# ================= Sheets 写入 =================

def _write_all_sheets(strategies, master_df, m_data, worksheets):
    """串行写入 Google Sheets（避免共享 gspread 会话的线程安全问题）"""
    agg_data = strategies['aggressive']
    if agg_data.empty:
        agg_data = pd.DataFrame([{'question': '当前无符合条件的高奖励市场'}])

    sheet_tasks = [
        (strategies['smart'],  worksheets['smart'],      'Smart LP Strategy'),
        (strategies['blue'],   worksheets['blue'],       'Blue Ocean Strategy'),
        (strategies['normal'], worksheets['normal'],     'Normal LP Strategy'),
        (strategies['small_edge'], worksheets['small_edge'], 'Small Edge Strategy'),
        (agg_data,             worksheets['aggressive'], 'High Reward Aggressive'),
    ]
    # 全量更新时加入基础表
    if len(master_df) > 20:
        sheet_tasks.append((master_df, worksheets['all'],  'All Markets'))
        sheet_tasks.append((m_data,    worksheets['full'], 'Full Markets'))

    for data, ws, name in sheet_tasks:
        try:
            update_sheet(data, ws)
            print(f"-> Updated '{name}' ({len(data)} rows)")
        except Exception as e:
            print(f"⚠️ Failed '{name}': {e}")


# ================= 奖励监控写入 =================

def _run_reward_monitor(m_data, wk_rewards):
    """执行奖励监控并更新 Google Sheet"""
    reward_changes_df = fetch_reward_changes(m_data)
    if not reward_changes_df.empty:
        update_sheet(reward_changes_df, wk_rewards)
        print(f"-> Updated 'New Rewards Alert' ({len(reward_changes_df)} 条变化)")
    else:
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


# ================= 主流程 =================

def fetch_and_process_data():
    print(f"[{datetime.now()}] Starting fetch cycle...")

    # 每轮重新初始化客户端（避免 token 过期）
    client, worksheets, sel_df = _init_clients()

    # 1. 获取数据
    all_df = get_all_markets(client)
    print(f"Got all Markets: {len(all_df)}")

    # 🔥 性能优化：提前过滤无奖励市场，避免对 3000+ 个无奖励市场调用订单簿 API
    if 'rewards' in all_df.columns:
        rewarded_df = all_df[all_df['rewards'].apply(_has_reward)].reset_index(drop=True)
        print(f"🔥 [性能优化] 有奖励的市场: {len(rewarded_df)} / {len(all_df)}（跳过 {len(all_df) - len(rewarded_df)} 个无奖励市场）")
    else:
        rewarded_df = all_df
        print(f"⚠️ 未找到 rewards 列，处理全部 {len(all_df)} 个市场")

    all_results = get_all_results(rewarded_df, client)
    print("Got all Results")

    # Volume 当前不参与策略筛选；默认关闭，避免无效 Gamma API 请求。
    if ENABLE_VOLUME_FETCH:
        condition_ids = [r.get('condition_id', '') for r in all_results if r]
        volumes_map = batch_fetch_volumes(condition_ids)
        for r in all_results:
            if r:
                cid = r.get('condition_id', '')
                r['volume'] = volumes_map.get(cid, 0.0)
    else:
        print("📊 [批量 Volume] 已关闭（不参与当前策略筛选）")

    m_data, all_markets = get_markets(all_results, sel_df, maker_reward=0.75)
    print(f"Got all orderbook. Total markets: {len(all_markets)}")

    # 🔥 跳过无效市场（best_bid=0 且 best_ask=0 表示无订单簿，波动率无意义）
    valid_markets = all_markets[
        (all_markets['best_bid'] > 0) | (all_markets['best_ask'] > 0)
    ].copy()
    skipped = len(all_markets) - len(valid_markets)
    if skipped > 0:
        print(f"🔥 [性能优化] 跳过 {skipped} 个无订单簿市场的波动率计算")

    # 2. 计算波动率（并发数提升到 25，加速波动率获取）
    new_df = add_volatility_to_df(valid_markets, max_workers=25)

    # 3. 基础数据处理
    for col in ['24_hour', '7_day', '14_day']:
        if col in new_df.columns:
            new_df[col] = pd.to_numeric(new_df[col], errors='coerce').fillna(0)
    new_df['volatility_sum'] = new_df['24_hour'] + new_df['7_day'] + new_df['14_day']

    # 计算到期天数
    if 'end_date' in new_df.columns:
        try:
            new_df['end_date'] = pd.to_datetime(new_df['end_date'], errors='coerce')
            now = pd.Timestamp.now()
            # 处理时区
            if new_df['end_date'].dt.tz is None:
                new_df['days_to_expiry'] = (new_df['end_date'] - now).dt.days
            else:
                new_df['days_to_expiry'] = (new_df['end_date'].dt.tz_localize(None) - now).dt.days
        except (ValueError, TypeError) as e:
            print(f"⚠️ 日期解析异常: {e}")
            new_df['days_to_expiry'] = 0

    # 4. 清洗数据
    master_df = clean_and_prepare_data(new_df)

    # 5. 策略筛选
    print("Applying filters...")
    strategies = _apply_strategy_filters(master_df)

    # 6. 更新 Google Sheets
    if len(master_df) > 0:
        try:
            print("Updating Sheets...")
            _write_all_sheets(strategies, master_df, m_data, worksheets)
            print(f"[{datetime.now()}] Update Cycle Completed Successfully.")
        except Exception as e:
            print(f"Error updating sheets: {e}")
            traceback.print_exc()

        if EXPORT_RUST_JSON:
            try:
                export_strategy_tokens_json(strategies['normal'], strategies['aggressive'], strategies['small_edge'])
            except Exception as e:
                print(f"⚠️ [Rust JSON] 导出异常: {e}")
                traceback.print_exc()
        else:
            print("🦀 [Rust JSON] 已关闭（Python main.py 不依赖该文件）")
    else:
        print("No data found to update.")

    # 7. 流动性奖励监控
    # 注意：使用 m_data（原始全量数据，含 rewards_daily_rate 字段）
    try:
        _run_reward_monitor(m_data, worksheets['rewards'])
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
