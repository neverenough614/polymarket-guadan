"""predict.fun 策略市场加载（SP3）。

从同一 Google spreadsheet 的 predict.fun 专属标签页读取选市结果 → 归一化为
策略 token 列表（与 Polymarket 的 token schema 一致），供 execution.place_orders 消费。
表不可用时回退本地 JSON（由 scripts/predictfun_update_markets.py 同步写出）。

复用 strategy_io.sheet_loader._parse_sheet_tokens（列名/去重/黑名单逻辑通用），
但 predict.fun 的 max_spread 已是小数（spreadThreshold），故 max_spread_unit_cents=False。
"""
import json
import os
import traceback
from typing import Dict, List

import pandas as pd

from config.bot_config import cfg
from strategy_io.sheet_loader import _parse_sheet_tokens


def _tokens_from_df(df: pd.DataFrame) -> List[Dict]:
    tokens: List[Dict] = []
    seen: Dict[str, int] = {}
    if df is not None and not df.empty:
        _parse_sheet_tokens(df, "High Reward", tokens, seen, max_spread_unit_cents=False)
    return tokens


def load_from_sheet() -> List[Dict]:
    """从 predict.fun 专属标签页读取 → token 列表。失败抛异常（由调用方决定是否回退）。"""
    from data_updater.google_utils import get_spreadsheet
    dc = cfg.predictfun_discovery
    sh = get_spreadsheet()
    wk = sh.worksheet(dc.selected_sheet)
    df = pd.DataFrame(wk.get_all_records())
    return _tokens_from_df(df)


def load_from_json(path: str = None) -> List[Dict]:
    """从本地 JSON 读取选市行 → token 列表。文件不存在返回空列表。"""
    dc = cfg.predictfun_discovery
    path = path or dc.strategy_json_path
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️ [predict.fun] 本地 JSON 读取失败({path})，返回空列表：{e}")
        return []
    return _tokens_from_df(pd.DataFrame(rows))


def load_predictfun_markets(prefer_sheet: bool = True) -> List[Dict]:
    """加载 predict.fun 策略市场：优先表格，失败回退本地 JSON。"""
    if prefer_sheet:
        try:
            tokens = load_from_sheet()
            print(f"📥 [predict.fun] 表格 '{cfg.predictfun_discovery.selected_sheet}' → {len(tokens)} 个 token")
            return tokens
        except Exception as e:
            print(f"⚠️ [predict.fun] 读取表格失败，回退本地 JSON：{e}")
            traceback.print_exc()
    tokens = load_from_json()
    print(f"📥 [predict.fun] 本地 JSON → {len(tokens)} 个 token")
    return tokens
