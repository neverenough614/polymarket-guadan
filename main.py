import threading
import asyncio
import time
import traceback
import concurrent.futures
from typing import List, Dict, Optional, Tuple
from collections import defaultdict, deque
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import pandas as pd

from data_updater.google_utils import get_spreadsheet
from poly_data.polymarket_client import PolymarketClient
from poly_data.data_utils import update_markets, update_positions, update_orders
from poly_data.websocket_handlers import connect_market_websocket
import poly_data.global_state as global_state

# ======================================================
# ⚙️ 自动挂单配置
# ======================================================
STRATEGY_SHEET_NAME      = "Normal LP Strategy"
AGGRESSIVE_SHEET_NAME    = "High Reward Aggressive"  # 高奖励激进策略（刀口舔血）
CHAIN_REWARDS_SHEET_NAME = "Chain Rewards Alert"      # 链上奖励自动发现

# 关键词黑名单（大小写不敏感，命中则跳过该市场）
# 主要过滤：军事打击类、政治演讲单日事件、地缘政治占领/封锁类
QUESTION_BLACKLIST_KEYWORDS = [
    # 军事打击类
     "strikes", "strike", "attack", "attacks", "bomb", "missile", "nuclear strike",
    #地缘政治占领/封锁类
     "capture", "invade", "invasion", "Strait of Hormuz","aliens","Iran","Iranian","Israel","Oil",
    # 政治演讲单日事件
     "State of the Union", 'say "', "tweets", "tweet",
]

# 硬黑名单：命中关键词的市场完全不挂单（大小写不敏感）
# 与上面的 QUESTION_BLACKLIST_KEYWORDS（跳过第一档）不同，这里是彻底屏蔽
QUESTION_HARD_BLACKLIST = [
    # 在这里添加你想完全屏蔽的关键词，每个一行，例如：
    "NFL",
    "Iran","Iranian","Israel","Oil","March", "Winner", "Champion", "NBA", "Presidential", "Leader", "Nominee", "Supreme", "Bitcoin", "Democratic", "Trump", "Elon",
    "Gold",
]

DEPTH_THRESHOLD_TIER1   = 1500.0   # 第1档深度阈值（USDC），提高门槛确保只有深厚市场才挂第一档
DEPTH_THRESHOLD_TIER2   = 200.0    # 第2、3档深度阈值（USDC）
EXTREME_PRICE_THRESHOLD = 0.10     # 极端价格阈值（<0.10 或 >0.90 必须双向挂单）
RETRY_INTERVAL          = 300      # 深度不足重试间隔（秒，5分钟）
DEFENSE_RETRY_INTERVAL  = 60       # 防御撤单后快速重试间隔（秒，1分钟）
SHEET_RELOAD_INTERVAL   = 300      # 表格重载间隔（秒）
ENABLE_AUTO_PLACE       = True     # 是否启用自动挂单

# 动态挂单量配置（分策略）
# Normal LP：稳健策略，占比大、挂单量高
NORMAL_SIZE_RATIO       = 0.30     # Normal LP 占前三档总深度 30%
NORMAL_MAX_ORDER_SIZE   = 800.0    # Normal LP 最大 800 shares
# High Reward：激进策略，占比小、挂单量低
AGGRESSIVE_SIZE_RATIO   = 0.08     # High Reward 占前三档总深度 8%
AGGRESSIVE_MAX_ORDER_SIZE = 300.0  # High Reward 最大 300 shares
# Chain Rewards：链上自动发现的高奖励市场
CHAIN_REWARDS_SIZE_RATIO     = 0.10     # Chain Rewards 占前三档总深度 10%
CHAIN_REWARDS_MAX_ORDER_SIZE = 500.0    # Chain Rewards 最大 500 shares
# 兼容旧代码的默认值
DYNAMIC_SIZE_RATIO      = 0.10     # 默认（手动挂单等）
MAX_ORDER_SIZE          = 500.0    # 默认上限
# ======================================================
# ⚙️ 自动清仓配置
# ======================================================
POSITION_CHECK_INTERVAL = 3       # 持仓检查间隔（秒）
MIN_POSITION_TO_CLOSE   = 5.0      # 最小清仓阈值（shares）
CLOSE_PRICE_OFFSET        = 0.02     # 基础清仓偏移（原0.01太保守，被吃后出不掉）
CLOSE_PRICE_OFFSET_URGENT = 0.03     # 紧急清仓偏移（连续失败/极端市场用，更激进确保出掉）

# ======================================================
# ⚙️ 插队检测配置
# ======================================================
SPREAD_CHECK_INTERVAL   = 60       # 插队检测间隔（秒）

# ======================================================
# ⚙️ 监控防御配置
# ======================================================
THRESHOLD_FRONT_DEPTH_DROP      = 0.20   # 前墙单轮跌幅触发阈值（原0.20太敏感，正常波动就20-30%）
THRESHOLD_SAME_DEPTH_DROP       = 0.10   # 同档(别人的)单轮跌幅触发阈值
THRESHOLD_FRONT_HIGH_WATER_DROP = 0.50   # 前墙高水位跌幅触发阈值（原0.50稍敏感）
THRESHOLD_SAME_HIGH_WATER_DROP  = 0.50   # 同档高水位跌幅触发阈值
MIN_SAME_DEPTH_SAFE             = 200.0   # 同档安全深度（USDC，排除自己后），别人的深度低于此值触发撤单
MIN_FRONT_DEPTH_THRESHOLD       = 100.0  # 前墙有无判断阈值（USDC）
MIN_FRONT_DEPTH_ABSOLUTE        = 100.0   # 前墙绝对兜底线（USDC），低于此值直接撤单（原50太高）
MIN_FRONT_DEPTH_ABSOLUTE_REF    = 0.0    # 设为0：历史高水位>0永远成立，等于直接启用绝对兜底
MONITOR_CHECK_INTERVAL          = 1      # 扫描间隔（秒）
ENABLE_AUTO_DEFENSE             = True
MAX_CONCURRENT_WORKERS          = 10
ORDERBOOK_TIMEOUT               = 5

# ======================================================
# ⚙️ 趋势检测配置（慢刀子防御）
# ======================================================
TREND_WINDOW_SIZE              = 5      # 趋势窗口大小（轮数）
TREND_CUMULATIVE_DROP_PCT      = 0.30   # 窗口内累计跌幅阈值（30%）
TREND_MIN_CONSECUTIVE          = 3      # 最少连续下降轮数才触发

# ======================================================
# ⚙️ 极端价格市场专用风控参数（best_bid < 0.10 或 > 0.90）
# ======================================================
EXTREME_THRESHOLD_FRONT_DEPTH_DROP      = 0.12   # 前墙单轮跌幅（通用0.20→极端0.12，更敏感）
EXTREME_THRESHOLD_SAME_DEPTH_DROP       = 0.08   # 同档单轮跌幅（通用0.10→极端0.08）
EXTREME_THRESHOLD_FRONT_HIGH_WATER_DROP = 0.35   # 前墙高水位跌幅（通用0.50→极端0.35）
EXTREME_THRESHOLD_SAME_HIGH_WATER_DROP  = 0.35   # 同档高水位跌幅（通用0.50→极端0.35）
EXTREME_MIN_SAME_DEPTH_SAFE            = 300.0   # 同档安全深度（通用200→极端300）
EXTREME_MIN_FRONT_DEPTH_ABSOLUTE       = 150.0   # 前墙绝对兜底（通用100→极端150）
EXTREME_CLOSE_PRICE_OFFSET             = 0.03    # 极端市场清仓偏移（更激进）
EXTREME_TREND_CUMULATIVE_DROP_PCT      = 0.20    # 极端市场趋势阈值（通用0.30→极端0.20）
EXTREME_IMBALANCE_THRESHOLD            = 0.35    # 极端市场偏斜阈值（通用0.30→极端0.35）

# ======================================================
# ⚖️ 偏斜检测配置（买卖深度严重不对称时撤掉危险方向的单）
# ======================================================
ENABLE_IMBALANCE_DETECTION      = True       # 是否启用偏斜检测
IMBALANCE_THRESHOLD             = 0.30       # 偏斜阈值：某一边深度占比低于 30% 则触发
IMBALANCE_DEPTH_LEVELS          = 5          # 计算偏斜时使用前 N 档深度
IMBALANCE_MIN_TOTAL_DEPTH       = 500.0      # 买卖总深度低于此值时不检测（避免小市场误触发）

# ======================================================
# 🔥 FastAPI Dashboard
# ======================================================
app = FastAPI(title="Poly-Maker Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================================================
# 📊 全局状态
# ======================================================
placed_orders_log: List[Dict] = []
placed_orders_log_lock = threading.Lock()
MAX_PLACED_ORDERS_LOG = 500  # 最多保留最近 500 条挂单日志，防止内存泄漏
pending_retry_tokens: List[Dict] = []
pending_retry_lock = threading.Lock()


# ======================================================
# 🛠️ 辅助函数
# ======================================================
def _get_top_of_book(price_dict: Dict, depth: int, reverse: bool) -> List[Dict]:
    if not price_dict:
        return []
    items = []
    for p, s in price_dict.items():
        try:
            p_f = float(p)
            s_f = float(s)
        except (ValueError, TypeError):
            continue
        if s_f > 0:
            items.append((p_f, s_f))
    items.sort(key=lambda x: x[0], reverse=reverse)
    return [{"price": p, "size": s} for p, s in items[:depth]]


def load_markets_for_dashboard():
    df = getattr(global_state, "df", None)
    all_tokens = getattr(global_state, "all_tokens", None)
    if df is None and not all_tokens:
        global_state.df = pd.DataFrame()
        global_state.all_tokens = []
        return
    if df is None:
        df = pd.DataFrame({
            "question": ["Unknown"] * len(all_tokens),
            "answer1": ["Unknown"] * len(all_tokens),
            "answer2": [""] * len(all_tokens),
            "token1": list(all_tokens),
            "token2": [None] * len(all_tokens),
        })
        global_state.df = df
    if df is not None and ("token1" not in df.columns or "token2" not in df.columns):
        if all_tokens:
            n = min(len(df), len(all_tokens))
            df = df.iloc[:n].copy()
            df["token1"] = list(all_tokens)[:n]
            df["token2"] = None
            global_state.df = df
        else:
            global_state.df = df
    if not all_tokens:
        tokens = []
        if "token1" in global_state.df.columns:
            tokens.extend(global_state.df["token1"].dropna().astype(str).tolist())
        if "token2" in global_state.df.columns:
            tokens.extend(global_state.df["token2"].dropna().astype(str).tolist())
        global_state.all_tokens = sorted(set(tokens))
    print(f"[Dashboard] Markets loaded: {len(global_state.df) if global_state.df is not None else 0}")


# ======================================================
# 🔥 一键撤单
# ======================================================
def cancel_all_orders_now(poly_client: PolymarketClient, reason: str = "手动触发"):
    print(f"\n{'='*50}")
    print(f"🛑 【一键撤单】触发原因: {reason}")
    print(f"{'='*50}")

    if not poly_client:
        print("⚠️ Client 未初始化，跳过撤单。")
        return

    try:
        open_orders = poly_client.client.get_orders()
        count = len(open_orders) if open_orders else 0
        print(f"📋 当前活跃挂单: {count} 个")
        if count == 0:
            print("✅ 账户内没有活跃挂单。")
            print("=" * 50 + "\n")
            return
        print("🔥 正在执行【全部撤单】指令...")
        resp = poly_client.client.cancel_all()
        print(f"✅ 全局撤单指令已发送！Response: {resp}")
        print("=" * 50 + "\n")
        return
    except Exception as e:
        print(f"⚠️ 全局撤单失败 ({e})，尝试逐 token 备用撤单...")

    try:
        tokens_to_cancel = []
        if hasattr(global_state, 'df') and global_state.df is not None and not global_state.df.empty:
            if 'token1' in global_state.df.columns:
                tokens_to_cancel.extend(global_state.df['token1'].dropna().astype(str).tolist())
            if 'token2' in global_state.df.columns:
                tokens_to_cancel.extend(global_state.df['token2'].dropna().astype(str).tolist())
        tokens_to_cancel = list(set(tokens_to_cancel))
        if not tokens_to_cancel:
            print("⚠️ 没有可撤单的 token 列表。")
            return
        print(f"📋 逐 token 撤单，共 {len(tokens_to_cancel)} 个...")
        count = 0
        for token in tokens_to_cancel:
            try:
                poly_client.cancel_all_asset(token)
                count += 1
            except Exception as te:
                print(f"   ❌ 撤单失败 {token[:10]}: {te}")
        print(f"✅ 备用撤单完成！已处理 {count} 个 token。")
    except Exception as e:
        print(f"❌ 备用撤单也失败: {e}")
        traceback.print_exc()
    print("=" * 50 + "\n")


# ======================================================
# 📥 读取 Google 表格策略
# ======================================================
def _parse_sheet_tokens(df: pd.DataFrame, source_label: str,
                         tokens: list, seen_token_ids: dict,
                         max_spread_unit_cents: bool = True):
    """
    从 DataFrame 解析 token 列表，追加到 tokens 并更新 seen_token_ids（去重）。
    max_spread_unit_cents: True 表示表格中 max_spread 单位是美分（需 /100），
                           False 表示已经是小数（直接使用）。
    """
    def find_col(df, name):
        for col in df.columns:
            if col.lower().replace(' ', '_') == name.lower().replace(' ', '_'):
                return col
        return None

    min_size_col   = find_col(df, 'min_size')
    neg_risk_col   = find_col(df, 'neg_risk')
    max_spread_col = find_col(df, 'max_spread')

    added = 0
    skipped_blacklist = 0
    for _, row in df.iterrows():
        question = str(row.get('question', 'Unknown')).strip()
        if not question or question.lower() in ('', 'nan', 'none'):
            continue

        # 🚫 硬黑名单：完全不挂单
        question_lower = question.lower()
        hard_matched = _is_hard_blacklisted(question)
        if hard_matched:
            print(f"   🚫 [硬黑名单] 跳过: {question[:55]}... (命中: '{hard_matched}') → 完全不挂单")
            continue

        # 关键词黑名单过滤（大小写不敏感）→ 不跳过，标记为跳过第一档
        matched_kw = next(
            (kw for kw in QUESTION_BLACKLIST_KEYWORDS if kw.lower() in question_lower),
            None
        )
        is_blacklisted = False
        if matched_kw:
            skipped_blacklist += 1
            is_blacklisted = True
            print(f"   ⚠️ [黑名单] 标记: {question[:55]}... (命中: '{matched_kw}') → 跳过第一档，从第二档开始")

        # min_size
        try:
            min_size = float(str(row.get(min_size_col, 10)).replace(',', '')) if min_size_col else 10.0
            if min_size <= 0:
                min_size = 10.0
        except (ValueError, TypeError):
            min_size = 10.0

        # neg_risk
        neg_risk = False
        if neg_risk_col:
            nr_val = str(row.get(neg_risk_col, '')).strip().lower()
            neg_risk = nr_val in ('true', '1', 'yes')

        # max_spread
        max_spread = None
        if max_spread_col:
            try:
                ms_val = str(row.get(max_spread_col, '')).strip()
                if ms_val and ms_val.lower() not in ('', 'nan', 'none', '0'):
                    raw = float(ms_val)
                    if raw > 0:
                        max_spread = raw / 100.0 if max_spread_unit_cents else raw
            except (ValueError, TypeError):
                max_spread = None

        # volatility_sum（用于波动率加权挂单量）
        vol_sum = 0.0
        vol_col = find_col(df, 'volatility_sum')
        if vol_col:
            try:
                vol_sum = float(str(row.get(vol_col, 0)).replace(',', ''))
            except (ValueError, TypeError):
                vol_sum = 0.0

        def add_token(token_id, token_type):
            nonlocal added
            if token_id not in seen_token_ids:
                seen_token_ids[token_id] = len(tokens)
                tokens.append({
                    "token_id":       token_id,
                    "token_type":     token_type,
                    "question":       question,
                    "min_size":       min_size,
                    "neg_risk":       neg_risk,
                    "max_spread":     max_spread,
                    "volatility_sum": vol_sum,
                    "source":         source_label,
                    "blacklisted":    is_blacklisted,
                })
                added += 1
            else:
                # 已存在：取较大的 min_size，更新 max_spread（如果新值不为 None）
                idx = seen_token_ids[token_id]
                tokens[idx]["min_size"] = max(tokens[idx]["min_size"], min_size)
                if max_spread is not None:
                    tokens[idx]["max_spread"] = max_spread

        t1 = str(row.get('token1', '')).strip()
        if t1 and len(t1) > 10 and t1.lower() != 'nan':
            add_token(t1, "YES")

        if 'token2' in df.columns:
            t2 = str(row.get('token2', '')).strip()
            if t2 and len(t2) > 10 and t2.lower() != 'nan':
                add_token(t2, "NO")

    return added


def load_strategy_markets() -> List[Dict]:
    print(f"\n{'='*60}")
    print(f"📥 [自动挂单] 正在读取策略表格...")

    tokens: List[Dict] = []
    seen_token_ids: Dict[str, int] = {}

    try:
        sh = get_spreadsheet()

        # ── 1. Normal LP Strategy（稳健策略）──────────────────────
        print(f"   📋 读取 '{STRATEGY_SHEET_NAME}' ...")
        try:
            wk1 = sh.worksheet(STRATEGY_SHEET_NAME)
            df1 = pd.DataFrame(wk1.get_all_records())
            if not df1.empty:
                n1 = _parse_sheet_tokens(df1, "Normal LP", tokens, seen_token_ids,
                                          max_spread_unit_cents=True)
                print(f"   ✅ '{STRATEGY_SHEET_NAME}': {len(df1)} 行 → {n1} 个新 token")
            else:
                print(f"   ⚠️ '{STRATEGY_SHEET_NAME}' 表格为空")
        except Exception as e:
            print(f"   ⚠️ 读取 '{STRATEGY_SHEET_NAME}' 失败: {e}")

        # ── 2. High Reward Aggressive（已禁用）──────────────
        # print(f"   📋 读取 '{AGGRESSIVE_SHEET_NAME}' ...")
        # try:
        #     wk2 = sh.worksheet(AGGRESSIVE_SHEET_NAME)
        #     df2 = pd.DataFrame(wk2.get_all_records())
        #     if not df2.empty:
        #         df2 = df2[~df2['question'].astype(str).str.contains('当前无', na=False)]
        #         if not df2.empty:
        #             n2 = _parse_sheet_tokens(df2, "High Reward", tokens, seen_token_ids,
        #                                       max_spread_unit_cents=True)
        #             print(f"   ✅ '{AGGRESSIVE_SHEET_NAME}': {len(df2)} 行 → {n2} 个新 token")
        #         else:
        #             print(f"   ⚠️ '{AGGRESSIVE_SHEET_NAME}' 无符合条件的市场")
        #     else:
        #         print(f"   ⚠️ '{AGGRESSIVE_SHEET_NAME}' 表格为空")
        # except Exception as e:
        #     print(f"   ⚠️ 读取 '{AGGRESSIVE_SHEET_NAME}' 失败（可能尚未创建）: {e}")

        # ── 3. Chain Rewards Alert（链上自动发现的高奖励市场）──────
        print(f"   📋 读取 '{CHAIN_REWARDS_SHEET_NAME}' ...")
        try:
            wk3 = sh.worksheet(CHAIN_REWARDS_SHEET_NAME)
            df3 = pd.DataFrame(wk3.get_all_records())
            if not df3.empty:
                # 列名适配：max_spread_c → max_spread（让 _parse_sheet_tokens 能识别）
                if 'max_spread_c' in df3.columns and 'max_spread' not in df3.columns:
                    df3 = df3.rename(columns={'max_spread_c': 'max_spread'})
                n3 = _parse_sheet_tokens(df3, "Chain Rewards", tokens, seen_token_ids,
                                          max_spread_unit_cents=True)
                print(f"   ✅ '{CHAIN_REWARDS_SHEET_NAME}': {len(df3)} 行 → {n3} 个新 token")
            else:
                print(f"   ⚠️ '{CHAIN_REWARDS_SHEET_NAME}' 表格为空")
        except Exception as e:
            print(f"   ⚠️ 读取 '{CHAIN_REWARDS_SHEET_NAME}' 失败（可能尚未创建）: {e}")

        # 统计各策略来源
        normal_count = sum(1 for t in tokens if t.get("source") == "Normal LP")
        aggressive_count = sum(1 for t in tokens if t.get("source") == "High Reward")
        chain_count = sum(1 for t in tokens if t.get("source") == "Chain Rewards")
        print(f"   ✅ 合并后共 {len(tokens)} 个 token（Normal LP: {normal_count}, High Reward: {aggressive_count}, Chain Rewards: {chain_count}）")
        print(f"{'='*60}\n")
        return tokens

    except Exception as e:
        print(f"   ❌ 读取表格失败: {e}")
        traceback.print_exc()
        return []


# ======================================================
# 📊 订单簿分析工具
# ======================================================
def get_orderbook_info(poly_client: PolymarketClient, token_id: str):
    """
    获取订单簿，返回 (book, best_bid, best_ask, mid_price) 或 None
    """
    try:
        book = poly_client.client.get_order_book(token_id)
        if not book:
            return None, None, None, None
        bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        asks = sorted(book.asks, key=lambda x: float(x.price), reverse=False)
        best_bid = float(bids[0].price) if bids else None
        best_ask = float(asks[0].price) if asks else None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2.0
        else:
            mid = None
        return book, best_bid, best_ask, mid
    except Exception as e:
        return None, None, None, None


# 档位连续性检查阈值（相邻档位价差超过此值则认为流动性不连续）
MAX_LEVEL_GAP = 0.02  # 2c（Normal LP 市场 spread 2-6c，档位间距 2c 是正常的）


def analyze_best_place_price_from_book(book, side: str,
                                        max_spread: Optional[float] = None,
                                        mid: Optional[float] = None,
                                        order_size: Optional[float] = None,
                                        skip_tier1: bool = False):
    """
    从已有的订单簿对象分析最优挂单价格（不再重复请求 API）。
    - 第1档需要深度 >= DEPTH_THRESHOLD_TIER1 (200 USDC)
    - 第2、3档需要深度 >= DEPTH_THRESHOLD_TIER2 (100 USDC)
    - 如果提供了 max_spread 和 mid，则只选择在 [mid-max_spread, mid+max_spread] 范围内的档位
    - 前三档任意相邻档位价差 > MAX_LEVEL_GAP (2c) 则跳过（流动性不连续，被吃损失过大）
    - 如果提供了 order_size，第1档额外检查：挂单价值不超过该档深度的 50%，否则跳到第2档
    - skip_tier1: 黑名单市场强制跳过第一档，从第二档开始挂单
    返回 (price, tier, depth) 或 None
    """
    try:
        if not book:
            return None

        if side == "BUY":
            levels = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        else:
            levels = sorted(book.asks, key=lambda x: float(x.price), reverse=False)

        if not levels:
            return None

        # 前置检查：第1档深度必须 ≥ 100 USDC，否则整个市场跳过
        first_level = levels[0]
        first_depth = float(first_level.price) * float(first_level.size)
        if first_depth < 100.0:
            return None

        # ── 档位连续性检查（方案A：前三档任意相邻间距 > 1c 则跳过）──────────
        top3_prices = [float(lv.price) for lv in levels[:3]]
        if len(top3_prices) >= 2:
            for j in range(len(top3_prices) - 1):
                gap = abs(top3_prices[j] - top3_prices[j + 1])
                if gap > MAX_LEVEL_GAP + 1e-9:  # 容忍浮点误差
                    # 流动性不连续，跳过
                    return None

        for i, level in enumerate(levels[:3]):
            # 🚫 黑名单市场强制跳过第一档
            if i == 0 and skip_tier1:
                continue

            price = float(level.price)
            size = float(level.size)
            depth = price * size
            threshold = DEPTH_THRESHOLD_TIER1 if i == 0 else DEPTH_THRESHOLD_TIER2

            if depth < threshold:
                continue

            # ── 第一档额外安全检查 ──────────────────────────────────
            if i == 0:
                # 孤立厚墙检测：第1档/第2档深度比 > 5，说明是大户撑场，跳过
                if len(levels) >= 2:
                    tier2_depth = float(levels[1].price) * float(levels[1].size)
                    if tier2_depth > 0 and depth / tier2_depth > 3.5:
                        continue  # 第1档深度异常集中于单一档位，跳过

                # 占比检查：挂单价值不超过该档深度的 20%（原1/3，更保守）
                if order_size is not None:
                    my_order_value = order_size * price
                    if my_order_value > depth * (1/5):
                        continue  # 占比超过 20%，跳过第一档，尝试第二档

            # max_spread 范围检测
            if max_spread is not None and mid is not None:
                lower = mid - max_spread
                upper = mid + max_spread
                if not (lower <= price <= upper):
                    continue  # 此档位超出范围，跳过

            return price, i + 1, depth

        return None

    except Exception as e:
        print(f"   ⚠️ 分析订单簿失败: {e}")
        return None


def is_extreme_price_market(best_bid: Optional[float]) -> bool:
    if best_bid is None:
        return False
    # 使用 <= / >= 确保恰好 10c 和 90c 也被识别为极端价格市场
    return best_bid <= EXTREME_PRICE_THRESHOLD or best_bid >= (1.0 - EXTREME_PRICE_THRESHOLD)

def calculate_dynamic_size(book, mid: Optional[float], min_size: float,
                           volatility_sum: float = 0.0,
                           size_ratio: float = DYNAMIC_SIZE_RATIO,
                           max_order_size: float = MAX_ORDER_SIZE) -> Optional[float]:
    """
    根据市场前三档深度和波动率动态计算挂单量。

    逻辑：
      1. 分别计算买单前三档深度和卖单前三档深度（USDC）
      2. 分别计算对应方向的目标挂单量（shares）
      3. 取两者中的较小值，确保任何一个方向都不会占比过大
      4. 应用波动率折扣因子：高波动市场挂小单，低波动市场挂大单
      5. 如果 target_size < min_size（奖励门槛），返回 None（深度不足，跳过不挂）
      6. 否则返回 min(target_size, MAX_ORDER_SIZE)，取整

    波动率折扣因子：
      volatility_sum <= 10  → factor = 1.0（满额挂单）
      volatility_sum = 25   → factor = 0.75
      volatility_sum = 50   → factor = 0.33（大幅缩减）

    返回 None 表示深度不足以支撑最小奖励挂单量，应跳过该市场。
    """
    if not book or mid is None or mid <= 0:
        return None

    try:
        # 买单前三档深度（USDC）
        bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
        top3_bid_depth = sum(float(b.price) * float(b.size) for b in bids[:3])

        # 卖单前三档深度（USDC）
        asks = sorted(book.asks, key=lambda x: float(x.price), reverse=False)
        top3_ask_depth = sum(float(a.price) * float(a.size) for a in asks[:3])

        if top3_bid_depth <= 0 and top3_ask_depth <= 0:
            return None

        # 分别计算各方向目标挂单量（shares）
        bid_target = (top3_bid_depth * size_ratio / mid) if top3_bid_depth > 0 else 0
        ask_target = (top3_ask_depth * size_ratio / mid) if top3_ask_depth > 0 else 0

        # 取较小值：确保两个方向都不会占比过大
        target_size = min(bid_target, ask_target) if (bid_target > 0 and ask_target > 0) else max(bid_target, ask_target)

        # 🔥 波动率折扣因子：高波动市场挂小单，低波动市场挂大单
        if volatility_sum <= 10:
            vol_factor = 1.0
        else:
            vol_factor = max(0.2, 1.0 - (volatility_sum - 10) / 60)
        target_size = target_size * vol_factor

        # 如果 target_size < min_size（奖励门槛），说明深度不足以支撑最小奖励挂单量，跳过
        if target_size < min_size:
            return None

        # 限制在 [min_size, max_order_size] 范围内，取整
        final_size = min(target_size, max_order_size)
        final_size = round(final_size)

        return float(final_size)

    except Exception:
        return None


# ======================================================
# 🚀 对单个 token 执行挂单
# ======================================================
def place_order_for_token(poly_client: PolymarketClient, token_info: Dict) -> Dict:
    token_id   = token_info["token_id"]
    token_type = token_info["token_type"]
    question   = token_info["question"]
    neg_risk   = token_info.get("neg_risk", False)
    max_spread = token_info.get("max_spread", None)

    # 基础最小挂单量（保底）
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
        # 只调用一次订单簿 API，同时用于 mid price 计算和买卖档位分析
        book, best_bid, best_ask, mid = get_orderbook_info(poly_client, token_id)
        result["mid"] = mid

        # 🔥 动态计算挂单量（基于前三档总深度 + 波动率加权）
        # 根据策略来源使用不同的占比和上限
        source = token_info.get("source", "Normal LP")
        vol_sum = token_info.get("volatility_sum", 0.0)
        if source == "High Reward":
            sr, mos = AGGRESSIVE_SIZE_RATIO, AGGRESSIVE_MAX_ORDER_SIZE
        elif source == "Normal LP":
            sr, mos = NORMAL_SIZE_RATIO, NORMAL_MAX_ORDER_SIZE
        elif source == "Chain Rewards":
            sr, mos = CHAIN_REWARDS_SIZE_RATIO, CHAIN_REWARDS_MAX_ORDER_SIZE
        else:
            sr, mos = DYNAMIC_SIZE_RATIO, MAX_ORDER_SIZE
        order_size = calculate_dynamic_size(book, mid, base_min_size, volatility_sum=vol_sum,
                                            size_ratio=sr, max_order_size=mos)
        result["order_size"] = order_size
        if order_size is None:
            result["buy_status"] = "depth_insufficient"
            result["sell_status"] = "depth_insufficient"
            result["error"] = f"前三档深度不足以支撑最小奖励挂单量 {base_min_size:.0f} shares，跳过"
            return result

        # 极端价格检测
        extreme = is_extreme_price_market(best_bid)
        result["extreme_price"] = extreme

        # 复用同一个 book 对象分析买卖最优档位（不再重复请求 API）
        # 🚫 黑名单市场跳过第一档，从第二档开始挂单
        blacklisted = token_info.get("blacklisted", False)
        buy_result  = analyze_best_place_price_from_book(book, "BUY",  max_spread, mid, order_size, skip_tier1=blacklisted)
        sell_result = analyze_best_place_price_from_book(book, "SELL", max_spread, mid, order_size, skip_tier1=blacklisted)

        if extreme:
            # 极端价格市场：必须买卖双向都满足深度条件，否则整个跳过
            if buy_result is None or sell_result is None:
                missing = []
                if buy_result is None: missing.append("买单")
                if sell_result is None: missing.append("卖单")
                result["buy_status"] = "extreme_skip"
                result["sell_status"] = "extreme_skip"
                result["error"] = f"极端价格市场({best_bid:.2f})，{'/'.join(missing)}深度/范围不足，跳过双向挂单"
                return result

        # 执行买单
        if buy_result:
            buy_price, buy_tier, _ = buy_result
            result["buy_price"] = buy_price
            result["buy_tier"] = buy_tier
            try:
                resp = poly_client.create_order(token_id, "BUY", buy_price, order_size, neg_risk=neg_risk)
                result["buy_status"] = "placed" if resp and resp.get('status') != 'error' else f"failed: {str(resp)[:50]}"
            except Exception as e:
                result["buy_status"] = f"error: {str(e)[:50]}"
        else:
            result["buy_status"] = "depth_insufficient"

        # 执行卖单
        if sell_result:
            sell_price, sell_tier, _ = sell_result
            result["sell_price"] = sell_price
            result["sell_tier"] = sell_tier
            try:
                resp = poly_client.create_order(token_id, "SELL", sell_price, order_size, neg_risk=neg_risk)
                result["sell_status"] = "placed" if resp and resp.get('status') != 'error' else f"failed: {str(resp)[:50]}"
            except Exception as e:
                result["sell_status"] = f"error: {str(e)[:50]}"
        else:
            result["sell_status"] = "depth_insufficient"

    except Exception as e:
        result["error"] = str(e)
        result["buy_status"] = "error"
        result["sell_status"] = "error"

    return result


# ======================================================
# 🚀 批量自动挂单（并发版）
# ======================================================
PLACE_ORDER_WORKERS = 8  # 并发挂单线程数

def _is_hard_blacklisted(question: str) -> Optional[str]:
    """检查 question 是否命中硬黑名单，返回命中的关键词或 None"""
    question_lower = question.lower()
    return next(
        (kw for kw in QUESTION_HARD_BLACKLIST if kw.lower() in question_lower),
        None
    )


def _cleanup_blacklisted_orders(poly_client: PolymarketClient):
    """
    启动时扫描所有已有挂单，撤销命中硬黑名单的市场。
    解决历史遗留挂单（黑名单生效前挂上去的）不会被自动清理的问题。
    """
    print(f"\n{'='*60}")
    print(f"🚫 [启动清理] 扫描已有挂单，撤销命中硬黑名单的市场...")
    print(f"{'='*60}")

    if not poly_client:
        print("   ⚠️ Client 未初始化，跳过清理")
        return

    try:
        orders = poly_client.client.get_orders()
        if not orders:
            print("   ✅ 无活跃挂单，无需清理")
            print(f"{'='*60}\n")
            return

        # 只处理 LIVE 状态的订单
        live_orders = [o for o in orders if str(o.get('status', '')).upper() == 'LIVE']
        if not live_orders:
            print("   ✅ 无活跃挂单，无需清理")
            print(f"{'='*60}\n")
            return

        # 收集所有活跃挂单的 token_id 和对应的 market 信息
        # 注意：订单中可能没有 question 字段，需要从 market slug 或其他字段推断
        # 但 Polymarket API 的订单通常包含 asset_id/token_id，不一定有 question
        # 所以我们需要从 Google 表格中建立 token_id → question 的映射
        print(f"   📋 发现 {len(live_orders)} 个活跃挂单，正在匹配黑名单...")

        # 从表格加载 token_id → question 映射
        token_to_question: Dict[str, str] = {}
        try:
            sh = get_spreadsheet()
            for sheet_name in [STRATEGY_SHEET_NAME, CHAIN_REWARDS_SHEET_NAME]:
                try:
                    wk = sh.worksheet(sheet_name)
                    df = pd.DataFrame(wk.get_all_records())
                    if not df.empty:
                        for _, row in df.iterrows():
                            q = str(row.get('question', '')).strip()
                            if not q or q.lower() in ('', 'nan', 'none'):
                                continue
                            t1 = str(row.get('token1', '')).strip()
                            if t1 and len(t1) > 10 and t1.lower() != 'nan':
                                token_to_question[t1] = q
                            if 'token2' in df.columns:
                                t2 = str(row.get('token2', '')).strip()
                                if t2 and len(t2) > 10 and t2.lower() != 'nan':
                                    token_to_question[t2] = q
                except Exception:
                    pass
        except Exception as e:
            print(f"   ⚠️ 加载表格映射失败: {e}，将仅基于已有信息清理")

        # 检查每个活跃挂单
        tokens_to_cancel: Dict[str, str] = {}  # token_id → question
        for o in live_orders:
            token_id = o.get('token_id') or o.get('asset_id')
            if not token_id:
                continue
            # 尝试从映射中获取 question
            question = token_to_question.get(token_id, '')
            if not question:
                # 尝试从订单的其他字段获取（如 market slug）
                market = o.get('market', '') or o.get('description', '') or ''
                question = market
            if question:
                hard_kw = _is_hard_blacklisted(question)
                if hard_kw and token_id not in tokens_to_cancel:
                    tokens_to_cancel[token_id] = question
                    print(f"   🚫 发现黑名单挂单: {question[:55]}... (命中: '{hard_kw}')")

        if not tokens_to_cancel:
            print("   ✅ 所有活跃挂单均未命中硬黑名单，无需清理")
            print(f"{'='*60}\n")
            return

        # 撤销命中黑名单的挂单
        print(f"\n   🧨 正在撤销 {len(tokens_to_cancel)} 个黑名单市场的挂单...")
        cancelled = 0
        for token_id, question in tokens_to_cancel.items():
            try:
                poly_client.cancel_all_asset(token_id)
                cancelled += 1
                print(f"      ✅ 已撤销: {question[:45]}... ({token_id[:10]}...)")
            except Exception as e:
                print(f"      ❌ 撤销失败: {question[:45]}... → {e}")

        print(f"\n   🚫 [启动清理] 完成！撤销了 {cancelled}/{len(tokens_to_cancel)} 个黑名单市场的挂单")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"   ❌ [启动清理] 扫描失败: {e}")
        traceback.print_exc()
        print(f"{'='*60}\n")


def run_auto_place_orders(strategy_tokens: List[Dict]) -> Tuple[int, int]:
    global placed_orders_log, pending_retry_tokens

    poly_client = global_state.client
    if not poly_client:
        print("[AutoPlace] ❌ PolymarketClient 未初始化，跳过挂单")
        return 0, len(strategy_tokens)

    # 🚫 硬黑名单二次检查（兜底：防止重试队列等路径绕过黑名单）
    filtered_tokens = []
    for t in strategy_tokens:
        hard_kw = _is_hard_blacklisted(t["question"])
        if hard_kw:
            print(f"   🚫 [硬黑名单·兜底] 跳过: {t['question'][:55]}... (命中: '{hard_kw}') → 完全不挂单")
        else:
            filtered_tokens.append(t)
    if len(filtered_tokens) < len(strategy_tokens):
        print(f"   🚫 硬黑名单兜底过滤: {len(strategy_tokens) - len(filtered_tokens)} 个被拦截")
    strategy_tokens = filtered_tokens

    print(f"\n{'='*60}")
    print(f"🔍 [自动挂单] 并发分析 {len(strategy_tokens)} 个 token（{PLACE_ORDER_WORKERS} 线程）...")
    print(f"{'='*60}")

    results_map: Dict[str, Dict] = {}

    def _place_one(token_info):
        result = place_order_for_token(poly_client, token_info)
        result["timestamp"] = datetime.now().strftime("%H:%M:%S")
        return token_info["token_id"], result

    # 并发执行挂单
    with concurrent.futures.ThreadPoolExecutor(max_workers=PLACE_ORDER_WORKERS) as executor:
        futures = {executor.submit(_place_one, t): t for t in strategy_tokens}
        for future in concurrent.futures.as_completed(futures):
            try:
                token_id, result = future.result()
                results_map[token_id] = result
            except Exception as e:
                token_info = futures[future]
                results_map[token_info["token_id"]] = {
                    "token_id": token_info["token_id"],
                    "token_type": token_info["token_type"],
                    "question": token_info["question"],
                    "buy_status": "error", "sell_status": "error",
                    "error": str(e), "timestamp": datetime.now().strftime("%H:%M:%S"),
                }

    # 按原始顺序打印结果
    success_count = 0
    skip_count = 0
    new_pending = []

    for i, token_info in enumerate(strategy_tokens):
        result = results_map.get(token_info["token_id"], {})
        with placed_orders_log_lock:
            placed_orders_log.append(result)
            if len(placed_orders_log) > MAX_PLACED_ORDERS_LOG:
                placed_orders_log[:] = placed_orders_log[-MAX_PLACED_ORDERS_LOG:]

        buy_ok    = result.get("buy_status") == "placed"
        sell_ok   = result.get("sell_status") == "placed"
        buy_skip  = result.get("buy_status") in ("depth_insufficient", "extreme_skip")
        sell_skip = result.get("sell_status") in ("depth_insufficient", "extreme_skip")

        label = f"   [{i+1}/{len(strategy_tokens)}] {token_info['question'][:35]}... [{token_info['token_type']}]"

        if buy_ok or sell_ok:
            success_count += 1
            # 🔥 保存实际挂单量到 token_info，供防御模块排除自己
            if result.get("order_size"):
                token_info["order_size"] = result["order_size"]
            buy_info  = f"买{result['buy_tier']}(${result['buy_price']:.3f})"  if buy_ok  else "买单跳过"
            sell_info = f"卖{result['sell_tier']}(${result['sell_price']:.3f})" if sell_ok else "卖单跳过"
            extreme_tag = " [极端价格✓]" if result.get("extreme_price") else ""
            spread_tag  = f" [mid={result['mid']:.3f}±{result['max_spread']}]" if result.get("max_spread") and result.get("mid") else ""
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
        # 本次重试涉及的 token ID（避免覆盖防御撤单新加入的 token）
        retried_ids = set(results_map.keys())
        # 保留不在本次重试范围内的 token（比如防御撤单期间新加入的）
        kept = [t for t in pending_retry_tokens if t["token_id"] not in retried_ids]
        # 合并：本次重试仍失败的 + 其他保留的
        pending_retry_tokens[:] = new_pending + kept

    print(f"\n{'='*60}")
    print(f"📊 [自动挂单] 完成！成功: {success_count} 个，跳过/失败: {skip_count} 个")
    if new_pending:
        print(f"   🔄 {len(new_pending)} 个 token 将在 {RETRY_INTERVAL//60} 分钟后重试")
    print(f"{'='*60}\n")
    return success_count, skip_count


# ======================================================
# 🔄 定期重试任务（深度不足 / 防御撤单）
# ======================================================
async def periodic_retry_task():
    """
    每 DEFENSE_RETRY_INTERVAL 秒检查一次重试队列。
    - 防御撤单的 token（带 _retry_at 标记）：到时间即重试
    - 深度不足的 token（无标记）：等满 RETRY_INTERVAL 才重试
    """
    while True:
        await asyncio.sleep(DEFENSE_RETRY_INTERVAL)
        now = time.time()
        with pending_retry_lock:
            # 区分：防御撤单（有 _retry_at）vs 深度不足（无 _retry_at）
            ready = []
            still_waiting = []
            for t in pending_retry_tokens:
                retry_at = t.get("_retry_at", 0)
                if retry_at > 0:
                    # 防御撤单：到时间即重试
                    if now >= retry_at:
                        ready.append(t)
                    else:
                        still_waiting.append(t)
                else:
                    # 深度不足：用更长的间隔，检查 _added_at
                    added_at = t.get("_added_at", 0)
                    if added_at == 0:
                        t["_added_at"] = now
                        still_waiting.append(t)
                    elif now - added_at >= RETRY_INTERVAL:
                        ready.append(t)
                    else:
                        still_waiting.append(t)
            pending_retry_tokens[:] = still_waiting

        if not ready:
            continue
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 [重试] 开始重试 {len(ready)} 个 token...")
        await asyncio.to_thread(run_auto_place_orders, ready)


# ======================================================
# 🔄 表格同步任务（每小时）
# ======================================================
async def sheet_sync_task(strategy_tokens_ref: list):
    while True:
        await asyncio.sleep(SHEET_RELOAD_INTERVAL)
        print(f"\n\n{'='*60}")
        print(f"🔄 [{datetime.now().strftime('%H:%M:%S')}] 正在重载表格: '{STRATEGY_SHEET_NAME}'...")
        print(f"{'='*60}")

        new_tokens = await asyncio.to_thread(load_strategy_markets)
        if not new_tokens:
            print("   ⚠️ 重载失败或表格为空，保持原有配置")
            continue

        old_ids = {t["token_id"] for t in strategy_tokens_ref}
        new_ids = {t["token_id"] for t in new_tokens}
        added_ids   = new_ids - old_ids
        removed_ids = old_ids - new_ids

        print(f"   📊 变化: ➕ 新增 {len(added_ids)} | 🗑️ 移除 {len(removed_ids)}（撤单+移除）| 不变 {len(new_ids & old_ids)}")

        if removed_ids:
            poly_client = global_state.client
            for token_id in removed_ids:
                old_token = next((t for t in strategy_tokens_ref if t["token_id"] == token_id), None)
                label = old_token["question"][:40] if old_token else token_id[:10]
                print(f"      🗑️ {label}... (撤单并移除监控)")
                if poly_client:
                    try:
                        await asyncio.to_thread(poly_client.cancel_all_asset, token_id)
                        print(f"         ✅ 撤单成功: {token_id[:10]}...")
                    except Exception as e:
                        print(f"         ⚠️ 撤单失败: {e}")
            # 从监控列表中删除已移除的 token
            strategy_tokens_ref[:] = [t for t in strategy_tokens_ref if t["token_id"] not in removed_ids]

        if added_ids:
            added_tokens = [t for t in new_tokens if t["token_id"] in added_ids]
            strategy_tokens_ref.extend(added_tokens)
            print(f"   🚀 正在对 {len(added_tokens)} 个新增市场执行挂单...")
            await asyncio.to_thread(run_auto_place_orders, added_tokens)
        else:
            print(f"   ✅ 无新增市场")

        print(f"   📋 当前监控总数: {len(strategy_tokens_ref)} 个 token")
        print(f"{'='*60}\n")


# ======================================================
# 🔍 插队检测任务（定期检查挂单是否偏离 max_spread 范围）
# ======================================================
def check_and_rebalance_token(poly_client: PolymarketClient, token_info: Dict,
                               my_bid_price: Optional[float], my_ask_price: Optional[float]) -> bool:
    """
    检查我的挂单是否还在 mid ± max_spread 范围内。
    如果偏离，撤单并重新挂单。
    返回 True 表示执行了重新挂单，False 表示无需操作。
    """
    token_id   = token_info["token_id"]
    max_spread = token_info.get("max_spread", None)
    question   = token_info["question"]
    token_type = token_info["token_type"]

    if max_spread is None:
        return False  # 没有 max_spread 限制，跳过

    if my_bid_price is None and my_ask_price is None:
        return False  # 没有挂单，跳过

    # 只调用一次订单簿 API，同时用于 mid price 计算和重新挂单分析
    book, best_bid, best_ask, mid = get_orderbook_info(poly_client, token_id)
    if mid is None:
        return False

    lower = mid - max_spread
    upper = mid + max_spread

    bid_out_of_range  = my_bid_price  is not None and not (lower <= my_bid_price  <= upper)
    ask_out_of_range  = my_ask_price  is not None and not (lower <= my_ask_price  <= upper)

    if not bid_out_of_range and not ask_out_of_range:
        return False  # 都在范围内，无需操作

    # 有挂单偏离范围，撤单重挂
    out_sides = []
    if bid_out_of_range:  out_sides.append(f"买单(${my_bid_price:.3f})")
    if ask_out_of_range:  out_sides.append(f"卖单(${my_ask_price:.3f})")

    print(f"\n🔄 [插队检测] [{token_type}] {question[:35]}...")
    print(f"   mid={mid:.3f}, 范围=[{lower:.3f}, {upper:.3f}]")
    print(f"   ⚠️ 偏离范围: {', '.join(out_sides)}")
    print(f"   🧨 撤单并重新挂单...")

    try:
        poly_client.cancel_all_asset(token_id)
        time.sleep(0.5)
        # 复用已有 book 对象，不再重复请求 API
        result = place_order_for_token(poly_client, token_info)
        buy_ok  = result["buy_status"] == "placed"
        sell_ok = result["sell_status"] == "placed"
        if buy_ok or sell_ok:
            buy_info  = f"买{result['buy_tier']}(${result['buy_price']:.3f})"  if buy_ok  else "买单跳过"
            sell_info = f"卖{result['sell_tier']}(${result['sell_price']:.3f})" if sell_ok else "卖单跳过"
            print(f"   ✅ 重新挂单成功: {buy_info} | {sell_info}")
        else:
            print(f"   ⚠️ 重新挂单失败或深度不足: 买={result['buy_status']} | 卖={result['sell_status']}")
        return True
    except Exception as e:
        print(f"   ❌ 插队重挂失败: {e}")
        return False


async def spread_check_task(strategy_tokens: list):
    """
    每 SPREAD_CHECK_INTERVAL 秒检查一次所有有 max_spread 的挂单，
    如果挂单价格偏离 mid ± max_spread，则撤单重挂。
    """
    # 等待一段时间再开始（避免刚挂完单就检测）
    await asyncio.sleep(SPREAD_CHECK_INTERVAL)
    print(f"\n🔍 [插队检测] 任务已启动（每 {SPREAD_CHECK_INTERVAL}s 检查一次）")

    while True:
        try:
            poly_client = global_state.client
            if not poly_client:
                await asyncio.sleep(SPREAD_CHECK_INTERVAL)
                continue

            current_tokens = list(strategy_tokens)
            # 只检查有 max_spread 的 token
            spread_tokens = [t for t in current_tokens if t.get("max_spread") is not None]

            if not spread_tokens:
                await asyncio.sleep(SPREAD_CHECK_INTERVAL)
                continue

            # 批量获取我的挂单
            all_orders = await asyncio.to_thread(get_all_my_orders_once, poly_client)

            rebalanced = 0
            for t in spread_tokens:
                token_id = t["token_id"]
                my_bid_price, my_ask_price = all_orders.get(token_id, (None, None))

                if my_bid_price is None and my_ask_price is None:
                    continue  # 没有挂单，跳过

                did_rebalance = await asyncio.to_thread(
                    check_and_rebalance_token, poly_client, t, my_bid_price, my_ask_price
                )
                if did_rebalance:
                    rebalanced += 1
                    await asyncio.sleep(0.5)

            if rebalanced > 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 [插队检测] 本轮重新挂单: {rebalanced} 个")

        except Exception as e:
            print(f"\n❌ [插队检测] 运行时错误: {e}")
            traceback.print_exc()

        await asyncio.sleep(SPREAD_CHECK_INTERVAL)


# ======================================================
# 💰 自动清仓任务（持仓被吃后市价卖出）
# ======================================================
async def auto_close_positions_task(strategy_tokens: list):
    """
    每 POSITION_CHECK_INTERVAL 秒检查一次持仓（通过 API，更实时）。
    如果发现持仓 >= MIN_POSITION_TO_CLOSE shares，
    立即以 best_bid - CLOSE_PRICE_OFFSET 价格挂限价单卖出清仓。
    """
    print(f"\n💰 [自动清仓] 任务已启动（每 {POSITION_CHECK_INTERVAL}s 检查，阈值: {MIN_POSITION_TO_CLOSE} shares）")

    token_map: Dict[str, Dict] = {}
    # 清仓失败冷却：token_id → 下次可重试的时间戳（避免 404 等错误无限刷屏）
    close_fail_cooldown: Dict[str, float] = {}
    CLOSE_FAIL_COOLDOWN_SECONDS = 300  # 清仓失败后冷却 5 分钟再重试
    # 清仓升级计数器：连续多轮都有持仓 → 说明清仓单没成交 → 用更激进价格
    close_attempt_count: Dict[str, int] = {}

    while True:
        await asyncio.sleep(POSITION_CHECK_INTERVAL)

        poly_client = global_state.client
        if not poly_client:
            continue

        try:
            # 更新 token_map（支持动态新增）
            for t in strategy_tokens:
                token_map[t["token_id"]] = t

            if not token_map:
                continue

            # 使用 API 获取所有持仓
            try:
                all_positions = poly_client.get_all_positions()
            except Exception as e:
                print(f"\n⚠️ [自动清仓] 获取持仓失败: {e}")
                continue

            if all_positions is None or len(all_positions) == 0:
                continue

            # 只清仓策略列表中的 token（Normal LP + High Reward），不清仓手动买入的持仓
            positions_found = []
            for _, row in all_positions.iterrows():
                asset = str(row.get('asset', ''))
                size  = float(row.get('size', 0))
                if size >= MIN_POSITION_TO_CLOSE:
                    if asset in token_map:
                        # 列表中的 token：清仓
                        t = token_map[asset]
                        positions_found.append({
                            "token_id":   asset,
                            "token_type": t["token_type"],
                            "question":   t["question"],
                            "shares":     size,
                            "neg_risk":   t.get("neg_risk", False),
                        })
                    else:
                        # 手动持仓（网页端买入等）：跳过，不自动清仓
                        pass

            if not positions_found:
                # 没有持仓 → 清空升级计数器
                close_attempt_count.clear()
                continue

            # 清理已不在持仓列表中的 token 的计数器
            found_ids = {p["token_id"] for p in positions_found}
            for tid in list(close_attempt_count.keys()):
                if tid not in found_ids:
                    close_attempt_count.pop(tid, None)

            print(f"\n\n{'$'*20} 💰 发现持仓，开始清仓 {'$'*20}")
            print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"📋 发现 {len(positions_found)} 个持仓需要清仓：")

            for pos in positions_found:
                token_id   = pos["token_id"]
                shares     = pos["shares"]
                question   = pos["question"]
                token_type = pos["token_type"]
                neg_risk   = pos["neg_risk"]

                # 🕐 冷却检查：清仓失败后等待一段时间再重试，避免无限刷屏
                now_ts = time.time()
                cooldown_until = close_fail_cooldown.get(token_id, 0)
                if now_ts < cooldown_until:
                    continue  # 仍在冷却中，静默跳过

                print(f"\n   🎯 [{token_type}] {question[:40]}...")
                print(f"      持仓: {shares:.2f} shares")

                try:
                    # 🔥 清仓前先撤掉该 token 的买单，避免"边清边买"循环
                    try:
                        poly_client.cancel_all_asset(token_id)
                        print(f"      🧹 已撤销该 token 的所有挂单（防止边清边买）")
                    except Exception as cancel_e:
                        print(f"      ⚠️ 撤单失败（继续清仓）: {cancel_e}")

                    book = poly_client.client.get_order_book(token_id)
                    if not book or not book.bids:
                        print(f"      ❌ 无法获取订单簿，跳过")
                        close_fail_cooldown[token_id] = now_ts + CLOSE_FAIL_COOLDOWN_SECONDS
                        continue

                    bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
                    asks = sorted(book.asks, key=lambda x: float(x.price), reverse=False) if book.asks else []
                    best_bid = float(bids[0].price)
                    best_ask = float(asks[0].price) if asks else None

                    # 清仓价格分级：极端市场/连续失败 → 更激进
                    attempts = close_attempt_count.get(token_id, 0)
                    if is_extreme_price_market(best_bid):
                        offset = EXTREME_CLOSE_PRICE_OFFSET
                        tag = "极端市场"
                    elif attempts >= 2:
                        offset = CLOSE_PRICE_OFFSET_URGENT
                        tag = f"连续{attempts}次未清仓"
                    else:
                        offset = CLOSE_PRICE_OFFSET
                        tag = ""

                    # 🔥 动态偏移：基于 spread 调整，避免窄 spread 市场偏移过大
                    if best_ask is not None and best_ask > best_bid:
                        spread = best_ask - best_bid
                        spread_based_offset = max(offset, spread * 0.3)
                        if spread_based_offset > offset:
                            offset = round(spread_based_offset, 3)
                            tag = f"{tag}，spread动态偏移" if tag else "spread动态偏移"

                    close_price = max(0.01, round(best_bid - offset, 2))
                    close_attempt_count[token_id] = attempts + 1

                    if tag:
                        print(f"      ⚠️ {tag}，使用偏移 -{offset}")
                    print(f"      best_bid: ${best_bid:.3f} → 清仓价: ${close_price:.3f}")
                    print(f"      正在挂卖单: {shares:.2f} shares @ ${close_price:.3f}...")

                    resp = poly_client.create_order(token_id, "SELL", close_price, shares, neg_risk=neg_risk)

                    if resp and resp.get('status') != 'error':
                        print(f"      ✅ 清仓单已提交！OrderID: {resp.get('orderID', resp)}")
                    else:
                        print(f"      ❌ 清仓失败: {resp}")
                        close_fail_cooldown[token_id] = now_ts + CLOSE_FAIL_COOLDOWN_SECONDS

                except Exception as e:
                    print(f"      ❌ 清仓出错: {e}")
                    close_fail_cooldown[token_id] = time.time() + CLOSE_FAIL_COOLDOWN_SECONDS

            print(f"{'$'*60}\n")

        except Exception as e:
            print(f"\n❌ [自动清仓] 运行时错误: {e}")
            traceback.print_exc()
            await asyncio.sleep(5)


# ======================================================
# 🛡️ 监控防御模块
# ======================================================
class MarketState:
    def __init__(self, question, token_type):
        self.question = question
        self.token_type = token_type
        self.my_bid_price = None
        self.my_ask_price = None
        self.my_order_size = 0.0  # 我的挂单量（shares），用于从同档深度中排除自己
        self.last_bid_front_depth = 0
        self.last_bid_same_depth = 0
        self.last_ask_front_depth = 0
        self.last_ask_same_depth = 0
        self.bid_front_high_water = 0
        self.bid_same_high_water = 0
        self.ask_front_high_water = 0
        self.ask_same_high_water = 0
        self.first_run = True
        # 趋势追踪：记录最近 N 轮深度值（慢刀子检测）
        self.bid_front_history = deque(maxlen=TREND_WINDOW_SIZE)
        self.bid_same_history = deque(maxlen=TREND_WINDOW_SIZE)
        self.ask_front_history = deque(maxlen=TREND_WINDOW_SIZE)
        self.ask_same_history = deque(maxlen=TREND_WINDOW_SIZE)

    def reset_high_water(self):
        self.bid_front_high_water = 0
        self.bid_same_high_water = 0
        self.ask_front_high_water = 0
        self.ask_same_high_water = 0
        self.bid_front_history.clear()
        self.bid_same_history.clear()
        self.ask_front_history.clear()
        self.ask_same_history.clear()


def get_all_my_orders_once(poly_client: PolymarketClient):
    try:
        orders = poly_client.client.get_orders()
        grouped = defaultdict(lambda: {'bids': [], 'asks': []})
        for o in orders:
            # 🔥 只处理 LIVE 状态的订单，过滤掉已取消/已成交的历史订单
            status = str(o.get('status', '')).upper()
            if status and status != 'LIVE':
                continue
            token_id = o.get('token_id') or o.get('asset_id')
            if not token_id:
                continue
            price = float(o['price'])
            side  = o.get('side')
            if side == 'BUY':
                grouped[token_id]['bids'].append(price)
            elif side == 'SELL':
                grouped[token_id]['asks'].append(price)
        result = {}
        for token_id, sides in grouped.items():
            best_bid = max(sides['bids']) if sides['bids'] else None
            best_ask = min(sides['asks']) if sides['asks'] else None
            result[token_id] = (best_bid, best_ask)
        return result
    except Exception as e:
        print(f"⚠️ 批量获取订单失败: {e}")
        return {}


def get_order_book_safe_monitor(poly_client: PolymarketClient, token_id: str):
    try:
        book = poly_client.client.get_order_book(token_id)
        return token_id, book, None
    except Exception as e:
        return token_id, None, str(e)


def get_all_order_books_concurrent(poly_client: PolymarketClient, token_ids: List[str]):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        future_to_token = {
            executor.submit(get_order_book_safe_monitor, poly_client, tid): tid
            for tid in token_ids
        }
        try:
            for future in concurrent.futures.as_completed(future_to_token, timeout=ORDERBOOK_TIMEOUT + 2):
                token_id = future_to_token[future]
                try:
                    returned_id, book, error = future.result()
                    if book:
                        results[returned_id] = book
                except Exception:
                    pass
        except concurrent.futures.TimeoutError:
            # 部分任务超时，已完成的结果已收集，继续使用
            unfinished = sum(1 for f in future_to_token if not f.done())
            if unfinished > 0:
                print(f"\n   ⚠️ {unfinished} 个盘口查询超时，已跳过（网络延迟）")
    return results


def calculate_layered_depth(book, my_bid_price, my_ask_price):
    bid_front = bid_same = ask_front = ask_same = 0
    if not book or not book.bids or not book.asks:
        return 0, 0, 0, 0
    if my_bid_price is not None:
        for bid in book.bids:
            price = float(bid.price)
            size  = float(bid.size)
            depth = price * size
            if price > my_bid_price + 0.001:
                bid_front += depth
            elif abs(price - my_bid_price) < 0.001:
                bid_same += depth
    if my_ask_price is not None:
        for ask in book.asks:
            price = float(ask.price)
            size  = float(ask.size)
            depth = price * size
            if price < my_ask_price - 0.001:
                ask_front += depth
            elif abs(price - my_ask_price) < 0.001:
                ask_same += depth
    return bid_front, bid_same, ask_front, ask_same


def cancel_specific_token_monitor(poly_client: PolymarketClient, token_id: str, question: str, token_type: str):
    print(f"\n🧨 正在对 [{question[:30]}] 执行精准撤单...")
    try:
        poly_client.cancel_all_asset(token_id)
        print(f"✅ 已成功撤销 {token_type} ({token_id[:10]}...) 的所有挂单。")
        return True
    except Exception as e:
        print(f"⚠️ 撤单失败: {e}")
        return False


def cancel_one_side_orders(poly_client: PolymarketClient, token_id: str, side: str, question: str):
    """
    只撤指定方向（BUY/SELL）的挂单，保留另一方向。
    """
    side_cn = "买单" if side == "BUY" else "卖单"
    print(f"\n🧨 正在撤销 [{question[:30]}] 的{side_cn}...")
    try:
        orders = poly_client.client.get_orders()
        to_cancel = []
        for o in orders:
            if str(o.get("status", "")).upper() != "LIVE":
                continue
            tid = o.get("token_id") or o.get("asset_id")
            if tid == token_id and o.get("side") == side:
                oid = o.get("id")
                if oid:
                    to_cancel.append(oid)
        if not to_cancel:
            print(f"   无活跃{side_cn}需要撤销")
            return False
        for oid in to_cancel:
            poly_client.client.cancel(oid)
        print(f"✅ 已撤销 {len(to_cancel)} 个{side_cn}")
        return True
    except Exception as e:
        print(f"⚠️ 单方向撤单失败: {e}，回退全撤")
        try:
            poly_client.cancel_all_asset(token_id)
            return True
        except Exception:
            return False


def emergency_close_position(poly_client: PolymarketClient, token_id: str,
                              all_books: dict, neg_risk: bool = False):
    """
    紧急清仓：检查指定 token 是否有持仓，如有则立即挂卖单清仓。
    同步函数，在 asyncio.to_thread 中调用。
    返回 True 表示已提交清仓单。
    """
    try:
        all_positions = poly_client.get_all_positions()
        if all_positions is None:
            return False
        for _, pos_row in all_positions.iterrows():
            if str(pos_row.get('asset', '')) == token_id:
                pos_shares = float(pos_row.get('size', 0))
                if pos_shares >= MIN_POSITION_TO_CLOSE:
                    close_book = all_books.get(token_id)
                    if close_book and close_book.bids:
                        close_bids = sorted(close_book.bids, key=lambda x: float(x.price), reverse=True)
                        close_best_bid = float(close_bids[0].price)
                        close_price = max(0.01, round(close_best_bid - CLOSE_PRICE_OFFSET_URGENT, 2))
                        print(f"   💰 [紧急清仓] 发现持仓 {pos_shares:.1f} shares，立即清仓 @ ${close_price:.3f}")
                        poly_client.create_order(token_id, "SELL", close_price, pos_shares, neg_risk=neg_risk)
                        return True
    except Exception as close_e:
        print(f"   ⚠️ [紧急清仓] 失败: {close_e}")
    return False


def _check_trend_threat(history: deque, label: str, is_extreme: bool = False) -> tuple:
    """检查深度历史是否呈连续下降趋势（慢刀子检测）。返回 (triggered, reason)"""
    if len(history) < TREND_MIN_CONSECUTIVE + 1:
        return False, ""
    # 从最新往回检查连续下降轮数
    consecutive_drops = 0
    for i in range(len(history) - 1, 0, -1):
        if history[i] < history[i - 1]:
            consecutive_drops += 1
        else:
            break
    if consecutive_drops < TREND_MIN_CONSECUTIVE:
        return False, ""
    # 计算累计跌幅
    start_idx = len(history) - 1 - consecutive_drops
    window_start = history[start_idx]
    current = history[-1]
    if window_start <= 0:
        return False, ""
    cum_drop = 1 - current / window_start
    threshold = EXTREME_TREND_CUMULATIVE_DROP_PCT if is_extreme else TREND_CUMULATIVE_DROP_PCT
    if cum_drop >= threshold:
        return True, f"🚨 [慢刀子] {label}连续{consecutive_drops}轮下降！${window_start:.0f}→${current:.0f} (累计-{cum_drop:.0%})"
    return False, ""


def check_side_threats(state, side: str, my_price, front_depth, same_depth, is_extreme=False):
    """
    通用的单方向威胁检测（合并原 check_bid_threats / check_ask_threats）。
    side: "bid" 或 "ask"
    """
    reasons = []
    triggered = False
    if my_price is None:
        return False, []

    label = "买单" if side == "bid" else "卖单"

    # 根据是否极端价格市场选择参数集
    t_front_drop    = EXTREME_THRESHOLD_FRONT_DEPTH_DROP if is_extreme else THRESHOLD_FRONT_DEPTH_DROP
    t_same_drop     = EXTREME_THRESHOLD_SAME_DEPTH_DROP if is_extreme else THRESHOLD_SAME_DEPTH_DROP
    t_front_hw_drop = EXTREME_THRESHOLD_FRONT_HIGH_WATER_DROP if is_extreme else THRESHOLD_FRONT_HIGH_WATER_DROP
    t_same_hw_drop  = EXTREME_THRESHOLD_SAME_HIGH_WATER_DROP if is_extreme else THRESHOLD_SAME_HIGH_WATER_DROP
    min_same_safe   = EXTREME_MIN_SAME_DEPTH_SAFE if is_extreme else MIN_SAME_DEPTH_SAFE
    min_front_abs   = EXTREME_MIN_FRONT_DEPTH_ABSOLUTE if is_extreme else MIN_FRONT_DEPTH_ABSOLUTE

    # 从 state 中获取对应方向的历史数据
    last_front = getattr(state, f'last_{side}_front_depth')
    last_same  = getattr(state, f'last_{side}_same_depth')
    hw_front   = getattr(state, f'{side}_front_high_water')
    hw_same    = getattr(state, f'{side}_same_high_water')
    hist_front = getattr(state, f'{side}_front_history')
    hist_same  = getattr(state, f'{side}_same_history')

    was_behind_wall = last_front > MIN_FRONT_DEPTH_THRESHOLD
    now_exposed     = front_depth <= MIN_FRONT_DEPTH_THRESHOLD
    if was_behind_wall and now_exposed:
        drop_pct = (1 - front_depth / last_front) * 100 if last_front > 0 else 100
        reasons.append(f"🚨 [跨分支] {label}前墙消失！前墙: ${last_front:.0f}→${front_depth:.0f} (-{drop_pct:.0f}%)")
        triggered = True
    if front_depth < min_front_abs and hw_front > MIN_FRONT_DEPTH_ABSOLUTE_REF:
        reasons.append(f"🚨 [绝对兜底] {label}前墙极度危险！当前: ${front_depth:.0f} (历史最高: ${hw_front:.0f})")
        triggered = True
    if hw_front > MIN_FRONT_DEPTH_THRESHOLD and front_depth < hw_front * (1 - t_front_hw_drop):
        reasons.append(f"🚨 [高水位] {label}前墙累计大幅下跌！高水位: ${hw_front:.0f}→当前: ${front_depth:.0f}")
        triggered = True
    if front_depth > MIN_FRONT_DEPTH_THRESHOLD:
        if last_front > MIN_FRONT_DEPTH_THRESHOLD and front_depth < last_front * (1 - t_front_drop):
            reasons.append(f"🚨 [单轮] {label}前墙塌陷！${last_front:.0f}→${front_depth:.0f}")
            triggered = True
    else:
        if same_depth < min_same_safe:
            reasons.append(f"🚨 [第一档] {label}深度太薄！同档: ${same_depth:.0f}")
            triggered = True
        elif last_same > min_same_safe and same_depth < last_same * (1 - t_same_drop):
            reasons.append(f"🚨 [第一档] {label}被大量吃掉！${last_same:.0f}→${same_depth:.0f}")
            triggered = True
        if hw_same > min_same_safe and same_depth < hw_same * (1 - t_same_hw_drop):
            reasons.append(f"🚨 [高水位] 第一档{label}累计被吃！高水位: ${hw_same:.0f}→当前: ${same_depth:.0f}")
            triggered = True
    # 趋势检测（慢刀子）
    trend_t, trend_r = _check_trend_threat(hist_front, f"{label}前墙", is_extreme)
    if trend_t:
        triggered = True
        reasons.append(trend_r)
    trend_t2, trend_r2 = _check_trend_threat(hist_same, f"{label}同档", is_extreme)
    if trend_t2:
        triggered = True
        reasons.append(trend_r2)
    return triggered, reasons


# 兼容旧调用名（防御主循环中使用）
def check_bid_threats(state, my_bid_price, bid_front, bid_same, is_extreme=False):
    return check_side_threats(state, "bid", my_bid_price, bid_front, bid_same, is_extreme)

def check_ask_threats(state, my_ask_price, ask_front, ask_same, is_extreme=False):
    return check_side_threats(state, "ask", my_ask_price, ask_front, ask_same, is_extreme)


# ======================================================
# 🛡️ 监控防御主循环
# ======================================================
async def monitor_defense_loop(strategy_tokens: list):
    print(f"\n{'='*60}")
    print(f"🛡️  [监控防御] 启动中...")
    print(f"    ⚙️  自动防御: {ENABLE_AUTO_DEFENSE}")
    print(f"    ⏱️  扫描间隔: {MONITOR_CHECK_INTERVAL}秒")
    print(f"{'='*60}\n")

    market_states: Dict[str, MarketState] = {}
    scan_count = 0

    while True:
        try:
            poly_client = global_state.client
            if not poly_client:
                await asyncio.sleep(5)
                continue

            timestamp  = datetime.now().strftime("%H:%M:%S")
            scan_count += 1
            loop_start = time.time()
            current_tokens = list(strategy_tokens)

            for t in current_tokens:
                if t["token_id"] not in market_states:
                    market_states[t["token_id"]] = MarketState(t["question"], t["token_type"])

            all_orders = await asyncio.to_thread(get_all_my_orders_once, poly_client)

            # 🔥 自动发现所有挂单（包括手动挂单），不限于 strategy_tokens 列表
            known_token_ids = {t["token_id"] for t in current_tokens}
            for token_id in all_orders:
                if token_id not in known_token_ids:
                    # 手动挂单的 token：自动创建临时 token_info 并加入监控
                    manual_token = {
                        "token_id":       token_id,
                        "token_type":     "MANUAL",
                        "question":       f"手动挂单 ({token_id[:10]}...)",
                        "min_size":       10.0,
                        "neg_risk":       False,
                        "max_spread":     None,
                        "volatility_sum": 0.0,
                        "source":         "manual_detected",
                    }
                    current_tokens.append(manual_token)
                    known_token_ids.add(token_id)
                    # 为手动挂单创建 MarketState
                    if token_id not in market_states:
                        market_states[token_id] = MarketState(manual_token["question"], manual_token["token_type"])

            active_targets = [t for t in current_tokens if all_orders.get(t["token_id"], (None, None)) != (None, None)]

            if not active_targets:
                loop_time = time.time() - loop_start
                print(f"\r[ {timestamp} ] 🛡️ 扫描 #{scan_count} | 无活跃挂单 | 监控: {len(current_tokens)} | 耗时: {loop_time:.2f}s", end="", flush=True)
                await asyncio.sleep(MONITOR_CHECK_INTERVAL)
                continue

            active_token_ids = [t["token_id"] for t in active_targets]
            all_books = await asyncio.to_thread(get_all_order_books_concurrent, poly_client, active_token_ids)

            for t in active_targets:
                token_id = t["token_id"]
                state    = market_states[token_id]
                my_bid_price, my_ask_price = all_orders.get(token_id, (None, None))
                book = all_books.get(token_id)
                if not book:
                    continue

                # ── 极端价格孤单检测 ──────────────────────────────────
                # 对于极端价格市场（YES < 10c 或 YES > 90c），必须双向挂单才有奖励。
                # 如果只有一边挂单（孤立），立即撤掉，避免无效占用资金。
                bids_sorted = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
                best_bid_price = float(bids_sorted[0].price) if bids_sorted else None
                if best_bid_price is not None and is_extreme_price_market(best_bid_price):
                    has_bid = my_bid_price is not None
                    has_ask = my_ask_price is not None
                    if has_bid != has_ask:  # 只有一边（XOR）
                        lone_side = "买单" if has_bid else "卖单"
                        print(f"\n⚠️ [孤单检测] [{t['token_type']}] {t['question'][:40]}...")
                        print(f"   极端价格市场(best_bid={best_bid_price:.3f})，仅有{lone_side}，双向缺一无奖励")
                        print(f"   🧨 撤销孤立{lone_side}...")
                        await asyncio.to_thread(
                            cancel_specific_token_monitor, poly_client, token_id,
                            t["question"], t["token_type"]
                        )
                        state.first_run = True
                        state.reset_high_water()
                        continue  # 已撤单，跳过本轮防御检测

                state.my_bid_price = my_bid_price
                state.my_ask_price = my_ask_price

                # ── ⚖️ 偏斜检测（买卖深度严重不对称时撤单）──────────────
                if ENABLE_IMBALANCE_DETECTION and not state.first_run:
                    bids_top = sorted(book.bids, key=lambda x: float(x.price), reverse=True)[:IMBALANCE_DEPTH_LEVELS]
                    asks_top = sorted(book.asks, key=lambda x: float(x.price), reverse=False)[:IMBALANCE_DEPTH_LEVELS]
                    imb_bid_depth = sum(float(b.price) * float(b.size) for b in bids_top)
                    imb_ask_depth = sum(float(a.price) * float(a.size) for a in asks_top)
                    imb_total = imb_bid_depth + imb_ask_depth

                    if imb_total >= IMBALANCE_MIN_TOTAL_DEPTH:
                        bid_ratio = imb_bid_depth / imb_total
                        ask_ratio = imb_ask_depth / imb_total
                        _imb_threshold = EXTREME_IMBALANCE_THRESHOLD if (best_bid_price is not None and is_extreme_price_market(best_bid_price)) else IMBALANCE_THRESHOLD

                        imbalance_triggered = False
                        danger_side = None  # 危险方向：需要撤掉的一边
                        if bid_ratio < _imb_threshold and my_bid_price is not None:
                            imbalance_triggered = True
                            danger_side = "BUY"  # 买方深度不足 → 价格可能下跌 → 买单危险
                            print(f"\n\n{'⚖'*10} 买卖深度偏斜检测 {'⚖'*10}")
                            print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                            print(f"🎯 目标: [{t['token_type']}] {t['question'][:45]}...")
                            print(f"   🚨 [偏斜] 买方深度严重不足！买/卖={bid_ratio:.0%}/{ask_ratio:.0%} (${imb_bid_depth:.0f}/${imb_ask_depth:.0f})，价格可能下跌 → 撤买单")
                        elif ask_ratio < _imb_threshold and my_ask_price is not None:
                            imbalance_triggered = True
                            danger_side = "SELL"  # 卖方深度不足 → 价格可能上涨 → 卖单危险
                            print(f"\n\n{'⚖'*10} 买卖深度偏斜检测 {'⚖'*10}")
                            print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                            print(f"🎯 目标: [{t['token_type']}] {t['question'][:45]}...")
                            print(f"   🚨 [偏斜] 卖方深度严重不足！买/卖={bid_ratio:.0%}/{ask_ratio:.0%} (${imb_bid_depth:.0f}/${imb_ask_depth:.0f})，价格可能上涨 → 撤卖单")

                        if imbalance_triggered and ENABLE_AUTO_DEFENSE and danger_side:
                            # 只撤危险方向的单，保留安全方向
                            await asyncio.to_thread(cancel_one_side_orders, poly_client, token_id, danger_side, t["question"])
                            state.first_run = True
                            state.reset_high_water()
                            # 🔄 加入重试队列
                            token_info_imb = next((x for x in current_tokens if x["token_id"] == token_id), None)
                            if token_info_imb:
                                with pending_retry_lock:
                                    existing_ids = {x["token_id"] for x in pending_retry_tokens}
                                    if token_info_imb["token_id"] not in existing_ids:
                                        token_info_imb["_retry_at"] = time.time() + DEFENSE_RETRY_INTERVAL
                                        pending_retry_tokens.append(token_info_imb)
                                print(f"   📋 已加入快速重试队列（{DEFENSE_RETRY_INTERVAL}s 后重试）")
                            # 💰 紧急清仓检查（仅当买单被撤时才需要，因为持仓是买入的结果）
                            if danger_side == "BUY":
                                await asyncio.to_thread(
                                    emergency_close_position, poly_client, token_id, all_books, t.get("neg_risk", False)
                                )
                            print("⚖" * 30)
                            continue  # 已撤单，跳过本轮深度检测

                # 获取我的挂单量（用于从同档深度中排除自己）
                order_size = t.get("order_size") or t.get("min_size", 500.0)
                state.my_order_size = order_size

                bid_front, bid_same, ask_front, ask_same = calculate_layered_depth(book, my_bid_price, my_ask_price)

                # 🔥 从同档深度中排除自己的挂单量，只看"别人的深度"
                if my_bid_price is not None and my_bid_price > 0:
                    my_bid_value = order_size * my_bid_price
                    bid_same = max(0, bid_same - my_bid_value)
                if my_ask_price is not None and my_ask_price > 0:
                    my_ask_value = order_size * my_ask_price
                    ask_same = max(0, ask_same - my_ask_value)

                state.bid_front_high_water = max(state.bid_front_high_water, bid_front)
                state.bid_same_high_water  = max(state.bid_same_high_water,  bid_same)
                state.ask_front_high_water = max(state.ask_front_high_water, ask_front)
                state.ask_same_high_water  = max(state.ask_same_high_water,  ask_same)

                # 记录趋势历史（慢刀子检测用）
                state.bid_front_history.append(bid_front)
                state.bid_same_history.append(bid_same)
                state.ask_front_history.append(ask_front)
                state.ask_same_history.append(ask_same)

                trigger_reasons = []
                triggered = False
                _is_ext = best_bid_price is not None and is_extreme_price_market(best_bid_price)

                if not state.first_run:
                    bid_triggered, bid_reasons = check_bid_threats(state, my_bid_price, bid_front, bid_same, is_extreme=_is_ext)
                    ask_triggered, ask_reasons = check_ask_threats(state, my_ask_price, ask_front, ask_same, is_extreme=_is_ext)
                    if bid_triggered:
                        triggered = True
                        trigger_reasons.extend(bid_reasons)
                    if ask_triggered:
                        triggered = True
                        trigger_reasons.extend(ask_reasons)

                state.last_bid_front_depth = bid_front
                state.last_bid_same_depth  = bid_same
                state.last_ask_front_depth = ask_front
                state.last_ask_same_depth  = ask_same
                state.first_run = False

                if triggered:
                    print(f"\n\n{'!'*20} ⚡ 检测到危险信号 ⚡ {'!'*20}")
                    print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"🎯 目标: {state.question[:50]}")
                    print(f"🆔 Token: {state.token_type} ({token_id[:10]}...)")
                    for i, reason in enumerate(trigger_reasons, 1):
                        print(f"  [{i}] {reason}")
                    if ENABLE_AUTO_DEFENSE:
                        await asyncio.to_thread(cancel_specific_token_monitor, poly_client, token_id, state.question, state.token_type)
                        state.first_run = True
                        state.reset_high_water()

                        # 🔄 加入重试队列，由 periodic_retry_task 统一处理重挂
                        token_info_replace = next((t for t in current_tokens if t["token_id"] == token_id), None)
                        if token_info_replace:
                            with pending_retry_lock:
                                existing_ids = {t["token_id"] for t in pending_retry_tokens}
                                if token_info_replace["token_id"] not in existing_ids:
                                    token_info_replace["_retry_at"] = time.time() + DEFENSE_RETRY_INTERVAL
                                    pending_retry_tokens.append(token_info_replace)
                            print(f"   📋 已加入快速重试队列（{DEFENSE_RETRY_INTERVAL}s 后重试）")

                        # 💰 防御撤单后立即清仓检查
                        await asyncio.to_thread(
                            emergency_close_position, poly_client, token_id, all_books, t.get("neg_risk", False)
                        )
                    else:
                        print("⚠️ 防御未开启，仅报警")
                    print("!" * 70)

            loop_time  = time.time() - loop_start
            # 动态监控间隔：活跃 token 越多，间隔越长，降低 API 压力
            active_count = len(active_targets)
            if active_count <= 10:
                dynamic_interval = MONITOR_CHECK_INTERVAL
            elif active_count <= 30:
                dynamic_interval = MONITOR_CHECK_INTERVAL * 2
            else:
                dynamic_interval = MONITOR_CHECK_INTERVAL * 3
            print(f"\r[ {timestamp} ] 🛡️ 扫描 #{scan_count} | 活跃: {active_count}/{len(current_tokens)} | 耗时: {loop_time:.2f}s | 间隔: {dynamic_interval}s", end="", flush=True)
            sleep_time = max(0.1, dynamic_interval - loop_time)
            await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            print("\n🛑 [监控防御] 任务已取消")
            break
        except Exception as e:
            print(f"\n❌ [监控防御] 运行时错误: {e}")
            await asyncio.sleep(5)


# ======================================================
# 🔥 优雅退出
# ======================================================
@app.on_event("shutdown")
async def shutdown_event():
    poly_client = getattr(global_state, 'client', None)
    await asyncio.to_thread(cancel_all_orders_now, poly_client, "程序关闭（Ctrl+C）")


# ======================================================
# 📡 Dashboard API
# ======================================================
@app.get("/markets")
def list_markets():
    if not hasattr(global_state, "df") or global_state.df is None:
        raise HTTPException(500, "Markets not loaded")
    markets = []
    for _, row in global_state.df.iterrows():
        try:
            t1 = row.get("token1")
            t2 = row.get("token2")
            label = f"{row.get('question', '')} - {row.get('answer1', '')}"
            if t1 and str(t1) != "nan":
                markets.append({"asset_id": str(t1), "label": label})
            if t2 and str(t2) != "nan":
                markets.append({"asset_id": str(t2), "label": label.replace(str(row.get('answer1')), str(row.get('answer2')))})
        except Exception:
            continue
    seen = set()
    unique = []
    for m in markets:
        if m["asset_id"] not in seen:
            seen.add(m["asset_id"])
            unique.append(m)
    return unique


@app.get("/orderbook/{asset_id}")
def get_orderbook(asset_id: str, depth: int = 10):
    if not hasattr(global_state, "all_data") or global_state.all_data is None:
        raise HTTPException(500, "Orderbook not loaded")
    ob = global_state.all_data.get(asset_id)
    if not ob:
        raise HTTPException(404, "No orderbook")
    return {
        "asset_id": asset_id,
        "bids": _get_top_of_book(ob.get("bids", {}), depth, True),
        "asks": _get_top_of_book(ob.get("asks", {}), depth, False),
    }


@app.get("/orders/log")
def get_orders_log():
    with placed_orders_log_lock:
        log_snapshot = list(placed_orders_log[-50:])
        total = len(placed_orders_log)
        placed_count = sum(1 for o in placed_orders_log if o.get("buy_status") == "placed" or o.get("sell_status") == "placed")
    return {
        "total": total,
        "placed_count": placed_count,
        "pending_retry": len(pending_retry_tokens),
        "log": log_snapshot,
    }


@app.post("/cancel_all")
async def api_cancel_all():
    poly_client = getattr(global_state, 'client', None)
    await asyncio.to_thread(cancel_all_orders_now, poly_client, "手动 API 触发")
    return {"status": "ok", "message": "撤单指令已发送"}


# ======================================================
# 🚀 主启动流程
# ======================================================
def init_state_and_markets():
    try:
        print("[System] Initializing Polymarket client...")
        global_state.client = PolymarketClient()
        print("[System] Updating markets from config...")
        update_markets()
        update_positions()
        update_orders()
    except Exception as e:
        print("[System] ❌ Init Error:", e)
        traceback.print_exc()
    load_markets_for_dashboard()


async def market_ws_loop():
    while not getattr(global_state, "all_tokens", None):
        print("[WS] Waiting for markets...")
        await asyncio.sleep(5)
    while True:
        try:
            print("[WS] Connecting...")
            await connect_market_websocket(global_state.all_tokens)
        except Exception as e:
            print("[WS] Error:", e)
            traceback.print_exc()
        await asyncio.sleep(5)


async def auto_place_and_monitor():
    if not ENABLE_AUTO_PLACE:
        print("[AutoPlace] ⚠️ 自动挂单已禁用（ENABLE_AUTO_PLACE=False）")
        return

    while not hasattr(global_state, 'client') or global_state.client is None:
        print("[AutoPlace] ⏳ 等待 PolymarketClient 初始化...")
        await asyncio.sleep(2)

    print("[AutoPlace] ✅ PolymarketClient 已就绪")

    # 🚫 启动时扫描已有挂单，撤销命中硬黑名单的市场（清理历史遗留）
    await asyncio.to_thread(_cleanup_blacklisted_orders, global_state.client)

    strategy_tokens = await asyncio.to_thread(load_strategy_markets)

    if not strategy_tokens:
        print("[AutoPlace] ❌ 未读取到任何市场，退出自动挂单")
        return

    print(f"\n🚀 [自动挂单系统] 开始执行初始挂单...")
    await asyncio.to_thread(run_auto_place_orders, strategy_tokens)

    print(f"\n🛡️  [系统] 启动所有后台任务...")
    await asyncio.gather(
        periodic_retry_task(),                      # 深度不足重试
        monitor_defense_loop(strategy_tokens),      # 监控防御
        sheet_sync_task(strategy_tokens),           # 每小时表格同步
        auto_close_positions_task(strategy_tokens), # 自动清仓
        spread_check_task(strategy_tokens),         # 插队检测
    )


async def background_services():
    init_state_and_markets()
    await asyncio.gather(
        # asyncio.create_task(market_ws_loop()),
        asyncio.create_task(auto_place_and_monitor()),
    )


def start_background_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(background_services())


if __name__ == "__main__":
    threading.Thread(target=start_background_thread, daemon=True).start()
    print("🚀 Dashboard: http://0.0.0.0:8000")
    print("📋 挂单日志: http://0.0.0.0:8000/orders/log")
    print("🛑 一键撤单: POST http://0.0.0.0:8000/cancel_all")
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except KeyboardInterrupt:
        pass
