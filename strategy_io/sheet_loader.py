"""
读取 Google 表格策略、解析 token、黑名单过滤。
"""
import traceback
from typing import Dict, List

import pandas as pd

from data_updater.google_utils import get_spreadsheet
from config.bot_config import cfg


def _find_col(df: pd.DataFrame, name: str):
    for col in df.columns:
        if col.lower().replace(" ", "_") == name.lower().replace(" ", "_"):
            return col
    return None


def _parse_sheet_tokens(
    df: pd.DataFrame,
    source_label: str,
    tokens: list,
    seen_token_ids: dict,
    max_spread_unit_cents: bool = True,
) -> int:
    """
    从 DataFrame 解析 token 列表，追加到 tokens 并更新 seen_token_ids（去重）。
    max_spread_unit_cents: True 表示表格中 max_spread 单位是美分（需 /100），
                           False 表示已经是小数（直接使用）。
    返回新增 token 数量。
    """
    sc = cfg.sheet
    min_size_col = _find_col(df, "min_size")
    neg_risk_col = _find_col(df, "neg_risk")
    max_spread_col = _find_col(df, "max_spread")

    added = 0
    for _, row in df.iterrows():
        question = str(row.get("question", "Unknown")).strip()
        if not question or question.lower() in ("", "nan", "none"):
            continue

        # 关键词黑名单过滤（大小写不敏感）
        question_lower = question.lower()
        matched_kw = next(
            (kw for kw in sc.QUESTION_BLACKLIST_KEYWORDS if kw.lower() in question_lower),
            None,
        )
        if matched_kw:
            print(f"   🚫 [黑名单] 跳过: {question[:55]}... (命中: '{matched_kw}')")
            continue

        try:
            min_size = float(str(row.get(min_size_col, 10)).replace(",", "")) if min_size_col else 10.0
            if min_size <= 0:
                min_size = 10.0
        except Exception:
            min_size = 10.0

        neg_risk = False
        if neg_risk_col:
            nr_val = str(row.get(neg_risk_col, "")).strip().lower()
            neg_risk = nr_val in ("true", "1", "yes")

        max_spread = None
        if max_spread_col:
            try:
                ms_val = str(row.get(max_spread_col, "")).strip()
                if ms_val and ms_val.lower() not in ("", "nan", "none", "0"):
                    raw = float(ms_val)
                    if raw > 0:
                        max_spread = raw / 100.0 if max_spread_unit_cents else raw
            except Exception:
                max_spread = None

        vol_sum = 0.0
        vol_col = _find_col(df, "volatility_sum")
        if vol_col:
            try:
                vol_sum = float(str(row.get(vol_col, 0)).replace(",", ""))
            except Exception:
                vol_sum = 0.0

        def add_token(token_id: str, token_type: str):
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


def load_strategy_markets() -> List[Dict]:
    """从 Google 表格加载策略市场（Normal LP + High Reward）。"""
    sc = cfg.sheet
    tokens: List[Dict] = []
    seen_token_ids: Dict[str, int] = {}

    print(f"\n{'='*60}")
    print(f"📥 [自动挂单] 正在读取策略表格...")

    try:
        sh = get_spreadsheet()

        print(f"   📋 读取 '{sc.STRATEGY_SHEET_NAME}' ...")
        try:
            wk1 = sh.worksheet(sc.STRATEGY_SHEET_NAME)
            df1 = pd.DataFrame(wk1.get_all_records())
            if not df1.empty:
                n1 = _parse_sheet_tokens(df1, "Normal LP", tokens, seen_token_ids, max_spread_unit_cents=True)
                print(f"   ✅ '{sc.STRATEGY_SHEET_NAME}': {len(df1)} 行 → {n1} 个新 token")
            else:
                print(f"   ⚠️ '{sc.STRATEGY_SHEET_NAME}' 表格为空")
        except Exception as e:
            print(f"   ⚠️ 读取 '{sc.STRATEGY_SHEET_NAME}' 失败: {e}")

        print(f"   📋 读取 '{sc.AGGRESSIVE_SHEET_NAME}' ...")
        try:
            wk2 = sh.worksheet(sc.AGGRESSIVE_SHEET_NAME)
            df2 = pd.DataFrame(wk2.get_all_records())
            if not df2.empty:
                df2 = df2[~df2["question"].astype(str).str.contains("当前无", na=False)]
                if not df2.empty:
                    n2 = _parse_sheet_tokens(df2, "High Reward", tokens, seen_token_ids, max_spread_unit_cents=True)
                    print(f"   ✅ '{sc.AGGRESSIVE_SHEET_NAME}': {len(df2)} 行 → {n2} 个新 token")
                else:
                    print(f"   ⚠️ '{sc.AGGRESSIVE_SHEET_NAME}' 无符合条件的市场")
            else:
                print(f"   ⚠️ '{sc.AGGRESSIVE_SHEET_NAME}' 表格为空")
        except Exception as e:
            print(f"   ⚠️ 读取 '{sc.AGGRESSIVE_SHEET_NAME}' 失败（可能尚未创建）: {e}")

        normal_count = sum(1 for t in tokens if t.get("source") == "Normal LP")
        aggressive_count = sum(1 for t in tokens if t.get("source") == "High Reward")
        print(f"   ✅ 合并后共 {len(tokens)} 个 token（Normal LP: {normal_count}, High Reward: {aggressive_count}）")
        print(f"{'='*60}\n")
        return tokens

    except Exception as e:
        print(f"   ❌ 读取表格失败: {e}")
        traceback.print_exc()
        return []
