"""predict.fun 市场发现 runner（SP3）。

拉全量 OPEN 市场 → 只挑当前有 active LP 奖励的市场 → 写本地 JSON（始终）
+ 写同一 Google spreadsheet 的 predict.fun 专属标签页（discover 模式）。
Polymarket 标签页一律不动。

用法（Windows PowerShell）：
  python scripts/predictfun_update_markets.py dryrun    # 只读发现：打印 + 写本地 JSON，不碰表（默认）
  python scripts/predictfun_update_markets.py discover   # 发现并写入 PF 标签页 + 本地 JSON

前置 .env：PLATFORM=predictfun / PREDICTFUN_NETWORK=mainnet / PREDICTFUN_PK / PREDICTFUN_API_KEY
        （写表还需 credentials.json + SPREADSHEET_URL，与 Polymarket 同一套）
"""
import os
import sys
import json
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config.bot_config import cfg
from predictfun_data.predictfun_client import PredictFunClient
from predictfun_data.market_discovery import select_reward_markets

# 写表列顺序（前 6 列对齐 sheet_loader 可解析的字段；其余为 predict.fun 专属）
COLUMNS = ["question", "token1", "token2", "neg_risk", "min_size", "max_spread",
           "volatility_sum", "hourly_rate", "fee_rate_bps", "yield_bearing", "market_id", "source"]


def _write_json(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"[JSON] 已写本地缓存 {path}（{len(rows)} 行）")


def _write_sheet(rows):
    import pandas as pd
    from gspread_dataframe import set_with_dataframe
    from data_updater.google_utils import get_spreadsheet

    import gspread
    dc = cfg.predictfun_discovery
    sh = get_spreadsheet()
    try:
        wk = sh.worksheet(dc.selected_sheet)
        wk.clear()
    except gspread.exceptions.WorksheetNotFound:
        wk = sh.add_worksheet(title=dc.selected_sheet, rows=max(100, len(rows) + 10), cols=len(COLUMNS) + 2)
        print(f"[SHEET] 新建标签页 '{dc.selected_sheet}'")
    df = pd.DataFrame(rows, columns=COLUMNS)
    set_with_dataframe(wk, df)
    print(f"[SHEET] 已写入标签页 '{dc.selected_sheet}'（{len(rows)} 行，Polymarket 标签未动）")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "dryrun"
    dc = cfg.predictfun_discovery
    print(f"=== predict.fun 市场发现 runner，模式={mode} ===")

    client = PredictFunClient(network=os.getenv("PREDICTFUN_NETWORK", "mainnet"))
    markets = client.get_all_markets(status="OPEN", page_size=dc.page_size, max_pages=dc.max_pages)
    print(f"[A] 拉取 OPEN 市场总数={len(markets)}")

    now = datetime.now(timezone.utc)
    rows = select_reward_markets(markets, now, min_hourly_rate=dc.min_hourly_rate,
                                 max_markets=dc.max_markets)
    print(f"[B] 当前有 active 奖励的市场={len(rows)}（按 hourlyRate 降序）")
    for r in rows[:10]:
        print(f"    rate={r['hourly_rate']:.0f}/h  spread≤{r['max_spread']}  min={r['min_size']:.0f}  "
              f"neg={r['neg_risk']}  {str(r['question'])[:42]}")
    if len(rows) > 10:
        print(f"    ...另有 {len(rows) - 10} 个")

    _write_json(rows, dc.strategy_json_path)

    if mode == "discover":
        try:
            _write_sheet(rows)
        except Exception as e:
            print(f"✗ 写表失败（检查 credentials.json/SPREADSHEET_URL/共享权限）：{e}")
            import traceback
            traceback.print_exc()
            return 1
    else:
        print("ℹ️ dryrun：未写表。确认上面选市无误后，运行 `discover` 写入 PF 标签页。")
    print("✓ 完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
