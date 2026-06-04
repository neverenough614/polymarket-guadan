"""predict.fun 策略市场加载（SP3/SP4）。

从同一 Google spreadsheet 的 predict.fun 专属标签页读取**已打分分流**的两张策略表
（PF Normal LP / PF High Reward，对标 Polymarket 的 Normal LP Strategy / High Reward
Aggressive），归一化为策略 token 列表（与 Polymarket token schema 一致），跨表去重，
供 execution.place_orders 消费。表不可用时回退本地 JSON（runner 同步写出）。

复用 strategy_io.sheet_loader._parse_sheet_tokens；predict.fun 的 max_spread 已是小数
（spreadThreshold），故 max_spread_unit_cents=False。
"""
import json
import os
import traceback
from typing import Dict, List

import pandas as pd

from config.bot_config import cfg
from strategy_io.sheet_loader import _parse_sheet_tokens


def _accumulate(df: pd.DataFrame, source_label: str, tokens: List[Dict], seen: Dict[str, int]) -> int:
    if df is None or df.empty:
        return 0
    return _parse_sheet_tokens(df, source_label, tokens, seen, max_spread_unit_cents=False)


def _collect_reward_ends(df: pd.DataFrame, ends_map: Dict[str, str]) -> None:
    """从 df 收集 token_id → reward_ends_at（_parse_sheet_tokens 不携带此列，单独补）。"""
    if df is None or getattr(df, "empty", True) or "reward_ends_at" not in df.columns:
        return
    for _, row in df.iterrows():
        ends = row.get("reward_ends_at")
        if ends is None or str(ends).strip().lower() in ("", "nan", "none"):
            continue
        for col in ("token1", "token2"):
            tid = str(row.get(col, "")).strip()
            if tid and len(tid) > 10 and tid.lower() != "nan":
                ends_map[tid] = ends


def _apply_reward_ends(tokens: List[Dict], ends_map: Dict[str, str]) -> None:
    """把 reward_ends_at 写回每个 token（监控据此做奖励失效撤单）。"""
    for t in tokens:
        e = ends_map.get(str(t.get("token_id", "")))
        if e is not None:
            t["reward_ends_at"] = e


def load_from_sheet() -> List[Dict]:
    """读 PF Normal LP + PF High Reward 两标签 → 去重后的 token 列表。失败抛异常。"""
    from data_updater.google_utils import get_spreadsheet
    sc = cfg.predictfun_selection
    sh = get_spreadsheet()
    tokens: List[Dict] = []
    seen: Dict[str, int] = {}
    ends_map: Dict[str, str] = {}
    for tab, label in ((sc.normal_sheet, "Normal LP"), (sc.agg_sheet, "High Reward")):
        try:
            wk = sh.worksheet(tab)
            df = pd.DataFrame(wk.get_all_records())
            n = _accumulate(df, label, tokens, seen)
            _collect_reward_ends(df, ends_map)
            print(f"   📋 '{tab}': {len(df)} 行 → {n} 个新 token")
        except Exception as e:
            print(f"   ⚠️ 读取 '{tab}' 失败（可能尚未创建）：{e}")
    _apply_reward_ends(tokens, ends_map)
    return tokens


def load_from_json() -> List[Dict]:
    """从本地两个 JSON（normal/aggressive）读取 → 去重后的 token 列表。"""
    sc = cfg.predictfun_selection
    tokens: List[Dict] = []
    seen: Dict[str, int] = {}
    ends_map: Dict[str, str] = {}
    for path, label in ((sc.normal_json_path, "Normal LP"), (sc.aggressive_json_path, "High Reward")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"⚠️ [predict.fun] 本地 JSON 读取失败({path})：{e}")
            continue
        df = pd.DataFrame(rows)
        _accumulate(df, label, tokens, seen)
        _collect_reward_ends(df, ends_map)
    _apply_reward_ends(tokens, ends_map)
    return tokens


def load_predictfun_markets(prefer_sheet: bool = True) -> List[Dict]:
    """加载 predict.fun 策略市场：优先表格，失败回退本地 JSON。"""
    if prefer_sheet:
        try:
            tokens = load_from_sheet()
            if tokens:
                print(f"📥 [predict.fun] 表格 → {len(tokens)} 个 token")
                return tokens
            print("⚠️ [predict.fun] 表格读到 0 token，回退本地 JSON")
        except Exception as e:
            print(f"⚠️ [predict.fun] 读取表格失败，回退本地 JSON：{e}")
            traceback.print_exc()
    tokens = load_from_json()
    print(f"📥 [predict.fun] 本地 JSON → {len(tokens)} 个 token")
    return tokens
