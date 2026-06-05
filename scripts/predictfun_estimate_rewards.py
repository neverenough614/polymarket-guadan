"""只读：按 order_efficiency(选市同款公式)估算当前挂单一天能farming多少 PP。

纯 GET，不下单不撤单。估算前提：当前簿构成与挂单维持一整天（实际随簿变化）。

用法：python scripts/predictfun_estimate_rewards.py
"""
import sys
import json

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import os

from execution.factory import create_execution_backend
from predictfun_data.placer import order_efficiency
from predictfun_data import units


def _reward_params():
    """token_id → (max_spread, rewards_daily_rate)。直接读 discover 写的 JSON（表格那列常空）。"""
    out = {}
    for path in ("predictfun_normal_tokens.json", "predictfun_aggressive_tokens.json"):
        if not os.path.exists(path):
            continue
        try:
            rows = json.load(open(path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for r in rows:
            ms = float(r.get("max_spread") or 0) or None
            daily = float(r.get("rewards_daily_rate") or 0)
            for col in ("token1", "token2"):
                tid = str(r.get(col, "")).strip()
                if tid and len(tid) > 10:
                    out[tid] = (ms, daily)
    return out


def _size(o):
    core = o.get("order") if isinstance(o.get("order"), dict) else {}
    raw = (o.get("size") or o.get("quantity") or o.get("amount")
           or core.get("size") or core.get("makerAmount") or o.get("makerAmount"))
    if raw is None:
        return 0.0
    s = str(raw).strip()
    try:
        if s.lstrip("-").isdigit():
            iv = int(s)
            return units.from_wei(iv) if iv >= 10 ** 10 else float(iv)
        v = float(s)
        return units.from_wei(int(v)) if v >= 1e10 else v
    except (ValueError, TypeError):
        return 0.0


def main():
    backend = create_execution_backend("predictfun")
    print("[est] 注册市场(取 meta/簿)...")
    backend.refresh_markets(status="OPEN")
    params = _reward_params()

    orders = [o for o in backend.get_all_orders()
              if str(o.get("status", "")).upper() in ("", "LIVE")
              and str(o.get("side", "")).upper() == "BUY"]
    print(f"[est] 当前活跃买单 {len(orders)} 张\n")
    if orders:
        print("原始订单样例:", json.dumps(orders[0], ensure_ascii=False, default=str)[:600], "\n")

    print(f"{'token':>14} {'腿':>4} {'挂价':>6} {'量':>6} {'mid':>6} {'带':>6} {'日率PP':>7} {'占比':>6} {'≈PP/日':>8}")
    total = 0.0
    for o in orders:
        tid = backend._order_tid(o)
        ms, daily = params.get(tid, (None, 0.0))
        price = float(o.get("price", 0) or 0)
        size = _size(o)
        try:
            book = backend.get_order_book(tid)
        except Exception:
            book = None
        bids = list(book.bids or []) if book else []
        asks = list(book.asks or []) if book else []
        bb = max((float(b.price) for b in bids), default=0.0)
        ba = min((float(a.price) for a in asks), default=0.0)
        mid = (bb + ba) / 2 if (bb and ba) else 0.0
        meta = backend.meta_for(tid)
        leg = getattr(meta, "name", "?") if meta else "?"
        # 竞争簿排除我自己那张（同价档减去我的 size），避免双算
        comp = []
        for b in bids:
            bp, bs = float(b.price), float(b.size)
            if abs(bp - price) < 1e-9:
                bs -= size
            if bs > 0:
                comp.append((bp, bs))
        eff = order_efficiency(comp, price, size, mid, ms, daily)
        pp = eff["expected_daily_reward"]
        total += pp
        print(f"{tid[:14]:>14} {str(leg):>4} {price:>6.3f} {size:>6.0f} "
              f"{mid:>6.3f} {str(ms):>6} {daily:>7.0f} {eff['my_q_share']*100:>5.1f}% {pp:>8.1f}")

    print(f"\n  ====== 合计 ≈ {total:.0f} PP/日 ======")
    print("  注：估算假设当前簿与挂单维持一整天；实际随他人进出、你被吃单/撤挂而变。")


if __name__ == "__main__":
    main()
