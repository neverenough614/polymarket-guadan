"""predict.fun 市场发现 + 选市打分 runner（SP3+SP4）。

流程：拉全量 OPEN 市场 → 只挑当前有 active LP 奖励的市场（SP3）
     → 并发取每个市场的 Yes 簿、用 Polymarket 同款公式打分（SP4）
     → 按 Polymarket Normal LP / High Reward Aggressive 阈值分流
     → 写同一 Google spreadsheet 的 predict.fun 专属标签页 + 本地 JSON。
Polymarket 标签页一律不动。

用法（Windows PowerShell）：
  python scripts/predictfun_update_markets.py dryrun    # 只读：打印 + 写本地 JSON，不碰表（默认）
  python scripts/predictfun_update_markets.py discover   # 发现+打分并写入 PF 标签页 + 本地 JSON

前置 .env：PLATFORM=predictfun / PREDICTFUN_NETWORK=mainnet / PREDICTFUN_PK / PREDICTFUN_API_KEY
        （写表还需 credentials.json + SPREADSHEET_URL，与 Polymarket 同一套）
"""
import os
import sys
import json
import concurrent.futures
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config.bot_config import cfg
from predictfun_data.predictfun_client import PredictFunClient
from predictfun_data.market_discovery import select_reward_markets, days_to_expiry
from predictfun_data.scoring import score_discovery_row
from predictfun_data.selection import split_strategies

# 原始奖励市场表（SP3）列
RAW_COLUMNS = ["question", "token1", "token2", "neg_risk", "min_size", "max_spread",
               "volatility_sum", "hourly_rate", "fee_rate_bps", "yield_bearing", "market_id", "source"]
# 打分后策略表（SP4）列：前 7 列对齐 sheet_loader 可解析字段
SCORED_COLUMNS = ["question", "token1", "token2", "neg_risk", "min_size", "max_spread", "volatility_sum",
                  "spread", "mid_reward_per_100", "gm_reward_per_100", "sm_reward_per_100",
                  "best_bid", "best_ask", "rewards_daily_rate", "hourly_rate", "days_to_expiry",
                  "fee_rate_bps", "yield_bearing", "market_id", "source"]


def _write_json(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"[JSON] 已写 {path}（{len(rows)} 行）")


def _write_sheet(tab_name, rows, columns):
    import pandas as pd
    import gspread
    from gspread_dataframe import set_with_dataframe
    from data_updater.google_utils import get_spreadsheet

    sh = get_spreadsheet()
    try:
        wk = sh.worksheet(tab_name)
        wk.clear()
    except gspread.exceptions.WorksheetNotFound:
        wk = sh.add_worksheet(title=tab_name, rows=max(100, len(rows) + 10), cols=len(columns) + 2)
        print(f"[SHEET] 新建标签页 '{tab_name}'")
    df = pd.DataFrame(rows, columns=columns)
    set_with_dataframe(wk, df)
    print(f"[SHEET] 已写入 '{tab_name}'（{len(rows)} 行，Polymarket 标签未动）")


def _score_markets(client, rows, workers):
    """并发取每个市场 Yes 簿并打分。取簿失败的市场跳过并计数。"""
    scored, failed = [], 0

    def _one(row):
        book = client.get_orderbook(row["market_id"])   # NormalizedBook（Yes 口径）
        return score_discovery_row(row, book.bids, book.asks)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(_one, r): r for r in rows}
        for fut in concurrent.futures.as_completed(futs):
            try:
                scored.append(fut.result())
            except Exception:
                failed += 1
    if failed:
        print(f"⚠️ [打分] {failed} 个市场取簿/打分失败，已跳过")
    return scored


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "dryrun"
    dc, sc = cfg.predictfun_discovery, cfg.predictfun_selection
    print(f"=== predict.fun 发现+打分 runner，模式={mode} ===")
    print("ℹ️ 注：predict.fun 无到期时间/历史波动数据，Polymarket 的 days_to_expiry 与 "
          "volatility 护栏不参与过滤（等价于其'未知到期→纳入'分支）。")

    client = PredictFunClient(network=os.getenv("PREDICTFUN_NETWORK", "mainnet"))
    markets = client.get_all_markets(status="OPEN", page_size=dc.page_size, max_pages=dc.max_pages)
    print(f"[A] 拉取 OPEN 市场总数={len(markets)}")

    now = datetime.now(timezone.utc)
    raw = select_reward_markets(markets, now, min_hourly_rate=dc.min_hourly_rate, max_markets=dc.max_markets)
    print(f"[B] active 奖励市场={len(raw)}（待打分）")

    scored = _score_markets(client, raw, sc.score_workers)
    # 到期护栏：用 rewards.endsAt 算剩余天数（未知=0→纳入），供 selection 过滤
    for r in scored:
        r["days_to_expiry"] = round(days_to_expiry(r.get("reward_ends_at"), now), 3)
    print(f"[C] 完成打分={len(scored)}")

    normal, aggressive = split_strategies(scored, sc)
    print(f"[D] 选市分流：Normal LP={len(normal)}  High Reward={len(aggressive)}")
    for tag, lst, key in (("Normal LP", normal, "mid_reward_per_100"),
                          ("High Reward", aggressive, "gm_reward_per_100")):
        for r in lst[:5]:
            print(f"    [{tag}] {key}={r.get(key)}  spread={r.get('spread')}  "
                  f"rate={r.get('rewards_daily_rate'):.0f}/d  {str(r.get('question'))[:38]}")

    _write_json(raw, dc.strategy_json_path)
    _write_json(normal, sc.normal_json_path)
    _write_json(aggressive, sc.aggressive_json_path)

    if mode == "discover":
        try:
            _write_sheet(dc.selected_sheet, raw, RAW_COLUMNS)         # PF Selected（全量奖励市场）
            _write_sheet(sc.normal_sheet, normal, SCORED_COLUMNS)     # PF Normal LP
            _write_sheet(sc.agg_sheet, aggressive, SCORED_COLUMNS)    # PF High Reward
        except Exception as e:
            print(f"✗ 写表失败（检查 credentials.json/SPREADSHEET_URL/共享权限）：{e}")
            import traceback
            traceback.print_exc()
            return 1
    else:
        print("ℹ️ dryrun：未写表。确认上面分流无误后，运行 `discover` 写入 PF 标签页。")
    print("✓ 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
