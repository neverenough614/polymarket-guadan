"""只读：投影"挂 N 个市场"一天大约能 farming 多少 PP。

复用选市同款链路（build_plan + order_efficiency），但把每个 token 的 rewards_daily_rate
从 discover 写的 JSON 文件补回来（表格那列常空，导致 plan 里 eff 全 0）。

用法：python scripts/predictfun_project_rewards.py            # 默认对比 4/6/8/10/12 市场
      python scripts/predictfun_project_rewards.py 8         # 只看 8 市场
纯 GET，不下单不撤单。
"""
import sys
import os
import json

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from scripts.predictfun_run import (
    _setup, _group_markets, _quote_fn, _min_size, SAFETY, heat_tracker,
)
from predictfun_data.budget import build_plan, available_after_open_buys


def _daily_by_token():
    """token_id → rewards_daily_rate，从 discover JSON 读（表格那列常空）。"""
    out = {}
    for path in ("predictfun_normal_tokens.json", "predictfun_aggressive_tokens.json"):
        if not os.path.exists(path):
            continue
        try:
            rows = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for r in rows:
            daily = float(r.get("rewards_daily_rate") or 0)
            if daily <= 0:
                continue
            for col in ("token1", "token2"):
                tid = str(r.get(col, "")).strip()
                if tid and len(tid) > 10:
                    out[tid] = daily
    return out


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    counts = [int(args[0])] if args else [4, 6, 8, 10, 12]

    backend, tokens = _setup()
    daily_map = _daily_by_token()
    print(f"[proj] JSON 含日率的 token={len(daily_map)}")
    # 把日率补回 token（quote 路径只认 token['rewards_daily_rate']）
    for t in tokens:
        d = daily_map.get(str(t.get("token_id", "")))
        if d:
            t["rewards_daily_rate"] = d

    markets = _group_markets(backend, tokens)
    markets = [(k, toks) for k, toks in markets
               if not any(heat_tracker.is_frozen(str(x["token_id"])) for x in toks)]
    total_bal = backend.raw_client.get_usdt_balance()
    available = available_after_open_buys(total_bal, backend.get_all_orders())
    print(f"[proj] 候选市场 {len(markets)}，可用抵押≈{available:.1f} USDT\n")

    qfn = _quote_fn(backend)
    print(f"{'目标市场':>8} {'实选':>5} {'预扣USDT':>9} {'≈PP/日':>8}")
    for n in counts:
        selected, _skip, total, _dropped = build_plan(
            markets, qfn, _min_size, available, safety=SAFETY, max_markets=n,
        )
        pp = sum(eff for _k, _legs, eff in selected)
        print(f"{n:>8} {len(selected):>5} {total:>9.1f} {pp:>8.0f}")

    print("\n  注：投影假设当前簿与挂单维持一整天；实选可能少于目标（预算/单边/极端价跳过）。")
    print("  日率来自 discover JSON；簿外或带外腿不计奖励。")


if __name__ == "__main__":
    main()
