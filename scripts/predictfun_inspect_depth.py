"""诊断：拉取已选 predict.fun 市场的实时订单簿，统计真实流动性/深度分布。

目的：predict.fun 是小盘市场，Polymarket 的深度阈值（档深>1500、前5档>5000）很可能不适用。
先看真实数据，再据此校准 placer 阈值。

用法：
  python scripts/predictfun_inspect_depth.py            # 默认抽样前 40 个市场（按日奖励降序）
  python scripts/predictfun_inspect_depth.py 80         # 抽样前 80 个市场
"""
import os
import sys
import json
import statistics

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from execution.factory import create_execution_backend

NORMAL_JSON = "predictfun_normal_tokens.json"
AGG_JSON = "predictfun_aggressive_tokens.json"


def _load_rows():
    rows = []
    seen = set()
    for path in (NORMAL_JSON, AGG_JSON):
        if not os.path.exists(path):
            continue
        for r in json.load(open(path, encoding="utf-8")):
            mid = r.get("market_id")
            if mid and mid not in seen:
                seen.add(mid)
                rows.append(r)
    # 按日奖励降序（最值得做的市场优先看）
    rows.sort(key=lambda r: float(r.get("rewards_daily_rate", 0) or 0), reverse=True)
    return rows


def _depths(levels):
    """[BookLevel] → [(price, size, notional)] 已按贴近盘口排序（簿已排好）。"""
    out = []
    for lv in levels or []:
        p, s = float(lv.price), float(lv.size)
        out.append((p, s, p * s))
    return out


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    backend = create_execution_backend("predictfun")
    print(f"[inspect] 注册市场(分页拉全量)...")
    backend.refresh_markets(status="OPEN")
    rows = _load_rows()[:n]
    print(f"[inspect] 抽样 {len(rows)} 个市场（按日奖励降序），拉实时 YES 簿...\n")

    hdr = (f"{'#':>3} {'日奖励':>8} {'spread':>6} {'mid':>6} {'tick':>6} "
           f"{'bids':>4} {'asks':>4} {'档1深':>8} {'档2深':>8} {'前5档深':>9}  问题")
    print(hdr)
    print("-" * len(hdr))

    t1_depths, t2_depths, top5_depths = [], [], []
    spreads, n_bid_levels = [], []
    quotable_tier1_1500 = 0
    quotable_top5_5000 = 0

    for i, r in enumerate(rows):
        tid = str(r.get("token1") or "").strip()
        if not tid:
            continue
        try:
            book = backend.get_order_book(tid)
        except Exception as e:
            print(f"{i+1:>3} 拉簿失败: {str(e)[:40]}")
            continue
        meta = backend.meta_for(tid)
        tick = getattr(meta, "tick_size", 0.01) if meta else 0.01
        bids = _depths(book.bids) if book else []
        asks = _depths(book.asks) if book else []
        bb = bids[0][0] if bids else 0.0
        ba = asks[0][0] if asks else 0.0
        mid = (bb + ba) / 2 if (bb and ba) else 0.0
        spread = (ba - bb) if (bb and ba) else 0.0
        t1 = bids[0][2] if bids else 0.0
        t2 = bids[1][2] if len(bids) > 1 else 0.0
        top5 = sum(d[2] for d in bids[:5])

        t1_depths.append(t1)
        t2_depths.append(t2)
        top5_depths.append(top5)
        spreads.append(spread)
        n_bid_levels.append(len(bids))
        if t1 >= 1500:
            quotable_tier1_1500 += 1
        if top5 >= 5000:
            quotable_top5_5000 += 1

        print(f"{i+1:>3} {float(r.get('rewards_daily_rate',0)):>8.0f} {spread:>6.3f} "
              f"{mid:>6.3f} {tick:>6.3f} {len(bids):>4} {len(asks):>4} "
              f"{t1:>8.0f} {t2:>8.0f} {top5:>9.0f}  {str(r.get('question',''))[:40]}")

    def pct(arr, p):
        if not arr:
            return 0.0
        arr = sorted(arr)
        k = max(0, min(len(arr) - 1, int(round((p / 100) * (len(arr) - 1)))))
        return arr[k]

    m = len(top5_depths)
    print("\n" + "=" * 60)
    print(f"统计（{m} 个市场，USDT 计）：")
    print(f"  档1深度   中位 {pct(t1_depths,50):.0f}  p25 {pct(t1_depths,25):.0f}  p75 {pct(t1_depths,75):.0f}  max {max(t1_depths or [0]):.0f}")
    print(f"  档2深度   中位 {pct(t2_depths,50):.0f}  p25 {pct(t2_depths,25):.0f}  p75 {pct(t2_depths,75):.0f}")
    print(f"  前5档深   中位 {pct(top5_depths,50):.0f}  p25 {pct(top5_depths,25):.0f}  p75 {pct(top5_depths,75):.0f}  max {max(top5_depths or [0]):.0f}")
    print(f"  spread    中位 {pct(spreads,50):.3f}  p25 {pct(spreads,25):.3f}  p75 {pct(spreads,75):.3f}")
    print(f"  bid档数   中位 {pct(n_bid_levels,50):.0f}  p25 {pct(n_bid_levels,25):.0f}  max {max(n_bid_levels or [0])}")
    print("-" * 60)
    print(f"  能过 Polymarket 档1≥1500 的市场: {quotable_tier1_1500}/{m} ({100*quotable_tier1_1500/max(1,m):.0f}%)")
    print(f"  能过 Polymarket 前5≥5000 的市场: {quotable_top5_5000}/{m} ({100*quotable_top5_5000/max(1,m):.0f}%)")
    print("=" * 60)


if __name__ == "__main__":
    main()
