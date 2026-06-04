"""predict.fun 实盘收尾 runner（SP1-5 串联）—— 独立入口,完全不碰 Polymarket/VPS。

把五个子项目串起来：
  factory 建 PredictFunClient+Backend → 注册市场(分页) → strategy_loader 载选市结果
  → place_orders 初挂真实双边(奖励价带内) → monitor_loop 保守守护(churn 双闸防清零)

模式（Windows PowerShell）：
  python scripts/predictfun_run.py plan  --limit 5     # 只读：打印要挂的市场与价格,不下单
  python scripts/predictfun_run.py live  --limit 1     # 实盘：挂单+守护(限 N 个市场,控制敞口)
  python scripts/predictfun_run.py once  --limit 1     # 实盘：只挂一轮,不进守护循环(便于验证)
  python scripts/predictfun_run.py cancel              # 安全：撤掉本账户所有挂单

前置 .env：PLATFORM=predictfun / PREDICTFUN_NETWORK=mainnet / PREDICTFUN_PK / PREDICTFUN_API_KEY
        / PREDICTFUN_ACCOUNT / SPREADSHEET_URL(+credentials.json)。

⚠️ 实盘安全：live/once 会下真实单,务必用 --limit 控制市场数。资金不足时多数单会被拒。
"""
import os
import sys
import asyncio

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from config.bot_config import cfg
from execution.factory import create_execution_backend
from execution.predictfun_monitor_loop import monitor_loop
from predictfun_data.strategy_loader import load_predictfun_markets
from predictfun_data.churn_guard import ChurnGuard
from predictfun_data.placer import compute_quote, place_leg, order_efficiency
from predictfun_data.budget import build_plan, available_after_open_buys
from predictfun_data.heat import tracker as heat_tracker
from orderbook.analyzer import get_orderbook_info

SAFETY = 0.95   # 预算缓冲：累计预扣 ≤ 余额×此值（防边界 + 留 gas/滑点余量）


def _parse_args(argv):
    mode = argv[1] if len(argv) > 1 else "plan"
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            limit = None
    return mode, limit


def _group_markets(backend, tokens):
    """按市场分组(YES+NO 同属一市场),按该市场日奖励降序排列。

    返回 [(market_key, [outcome_token,...])]，每市场恰两个 outcome（buy YES + buy NO）。
    """
    by_market = {}
    for t in tokens:
        meta = backend.meta_for(t["token_id"])
        mk = meta.market_id if meta is not None else ("u", t["token_id"])
        by_market.setdefault(mk, []).append(t)

    def reward_of(toks):
        return max((float(t.get("rewards_daily_rate", 0) or 0) for t in toks), default=0.0)

    return sorted(by_market.items(), key=lambda kv: reward_of(kv[1]), reverse=True)


def _min_size(tokens):
    if not tokens:
        return 100.0
    return max(100.0, *(float(t.get("min_size", 0) or 0) for t in tokens))


def _quote_fn(backend):
    """返回 quote_fn(token)->(token, price, size, reason, eff)，内部拉簿 + compute_quote + 算效率。

    eff=该腿预期日奖励(PP/日)，用 placer.order_efficiency（复用 Polymarket 公式）算，
    供 build_plan 按"挂单效率"降序选市。无法报价时 eff=0。
    """
    def fn(t):
        book, _bb, _ba, mid = get_orderbook_info(backend, t["token_id"])
        meta = backend.meta_for(t["token_id"])
        tick = getattr(meta, "tick_size", 0.01) if meta else 0.01
        min_size = max(100.0, float(t.get("min_size", 0) or 0))
        price, size, reason = compute_quote(book, t.get("max_spread"), tick, min_size)
        eff = 0.0
        if price is not None and book is not None and mid:
            daily = float(t.get("rewards_daily_rate", 0) or 0)
            eff = order_efficiency(book.bids, price, size, mid,
                                   t.get("max_spread"), daily)["expected_daily_reward"]
        return (t, price, size, reason, eff)
    return fn


def _plan_markets(backend, tokens, n_markets):
    """分组→排序→（按市场数上限截断）→报价配对→预算守门。返回 (selected, skip_reasons, total)。"""
    markets = _group_markets(backend, tokens)
    # 跳过热度冻结的市场（反复被攻击→冷却期内不再选，避免重挂被吃）
    before = len(markets)
    markets = [(k, toks) for k, toks in markets
               if not any(heat_tracker.is_frozen(str(t["token_id"])) for t in toks)]
    if before - len(markets) > 0:
        print(f"[plan] 跳过 {before - len(markets)} 个热度冻结市场")
    # 可用抵押=链上总额−已挂买单预扣（重启/重复下单也不会超额授信）
    total_bal = backend.raw_client.get_usdt_balance()
    available = available_after_open_buys(total_bal, backend.get_all_orders())
    # n_markets=目标挂满的市场数（懒求值：扫到选满即停，自动跳过单边/预算不足的市场）
    cap = n_markets if (n_markets and n_markets > 0) else None
    selected, skip_reasons, total, _dropped = build_plan(
        markets, _quote_fn(backend), _min_size, available, safety=SAFETY, max_markets=cap,
    )
    return selected, skip_reasons, total, available


def _setup():
    """建 backend、注册全量市场、载入策略 token(YES+NO 都保留)。"""
    backend = create_execution_backend("predictfun")
    client = backend.raw_client
    print(f"[setup] 账户={client.address}  USDT={client.get_usdt_balance()}")
    print("[setup] 注册市场(分页拉全量,稍候)...")
    n = backend.refresh_markets(status="OPEN")
    print(f"[setup] 已注册 {n} 个 outcome token")
    tokens = load_predictfun_markets()
    print(f"[setup] 载入策略 token={len(tokens)}（YES+NO 各一,双边=买两个 outcome）")
    return backend, tokens


def _print_selection(selected, skip_reasons, total, available):
    print(f"\n--- 选中 {len(selected)} 个市场（按挂单效率降序，预算守门 ≤ 余额×{SAFETY}）---")
    for i, (_key, legs, eff) in enumerate(selected):
        q = str(legs[0][0].get("question", ""))[:34]
        cost = sum(p * s for _t, p, s in legs)
        legs_str = " + ".join(f"{t.get('token_type')} {p:.3f}×{s:.0f}" for t, p, s in legs)
        print(f"  [{i+1}] {q} → 买 {legs_str}  预扣≈{cost:.1f} USDT  效率≈{eff:.0f} PP/日")
    print(f"--- 预扣合计≈{total:.1f} / 余额 {available:.1f} USDT（缓冲后上限 {available*SAFETY:.1f}）---")
    if skip_reasons:
        print("   跳过原因：" + ", ".join(f"{k}={v}" for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1])))


def _plan(backend, tokens, limit):
    selected, skip_reasons, total, available = _plan_markets(backend, tokens, limit)
    print(f"\n=== PLAN（只读,不下单）候选 {len(tokens)//2} 市场 ===")
    _print_selection(selected, skip_reasons, total, available)
    print("   确认后用 live/once + --limit 实盘 ===")


def _live_buy_token_ids(backend):
    """已有 live 买单的 token 集合（幂等下单用：已挂的不重复挂）。"""
    try:
        grouped = backend.get_all_my_orders_grouped()
        return {tid for tid, info in grouped.items() if info.get("best_bid") is not None}
    except Exception:
        return set()


def _place_once(backend, tokens, limit):
    selected, skip_reasons, total, available = _plan_markets(backend, tokens, limit)
    print(f"\n=== 初挂（实盘，幂等：已挂的不重复挂）===")
    _print_selection(selected, skip_reasons, total, available)
    existing = _live_buy_token_ids(backend)
    ok = fail = kept = 0
    target_tokens = []          # 目标在挂的全部 token（已存在 + 新挂成功）→ 交给监控
    for mi, (_key, legs, _eff) in enumerate(selected):
        for t, price, size in legs:
            tid = str(t["token_id"])
            if tid in existing:
                kept += 1
                target_tokens.append(t)
                print(f"  ⏭️ [{mi+1}] {str(t['question'])[:30]} [{t.get('token_type')}] 已有挂单，跳过")
                continue
            res = place_leg(backend, t, price, size)
            if res.get("status") == "placed":
                ok += 1
                target_tokens.append(t)
                print(f"  ✅ [{mi+1}] {str(t['question'])[:30]} [{t.get('token_type')}] BUY@{price:.3f}×{size:.0f}")
            else:
                fail += 1
                err = str(res.get("resp", res))[:70]
                print(f"  ❌ [{mi+1}] {str(t['question'])[:30]} [{t.get('token_type')}] → {err}")
    print(f"=== 初挂完成：新挂 {ok}，已存在 {kept}，失败 {fail}（在挂 token {len(target_tokens)}）===")
    return target_tokens


async def _live(backend, tokens, limit):
    # _place_once 内有同步 SDK 调用(get_usdt_balance/签名)，必须在工作线程跑，
    # 否则 predict-sdk 会拒绝"async 上下文里调同步方法"。
    target = await asyncio.to_thread(_place_once, backend, tokens, limit)
    if not target:
        print("[runner] 无在挂订单，不进入守护循环。")
        return
    mc = cfg.predictfun_monitor
    churn = ChurnGuard(mc.token_cooldown_sec, mc.max_cancels_per_hour)
    print("\n=== 进入守护循环（盯订单簿变化防御 + auto_close；Ctrl+C 退出会自动撤掉所有挂单清场）===")
    await monitor_loop(backend, target, churn)


def main() -> int:
    mode, limit = _parse_args(sys.argv)
    print(f"=== predict.fun runner 模式={mode} limit={limit} ===")

    if mode == "cancel":
        backend = create_execution_backend("predictfun")
        res = backend.cancel_all()
        print(f"[cancel] 已撤所有挂单：{res}")
        return 0

    backend, tokens = _setup()

    if not tokens:
        print("✗ 无策略 token（先跑 scripts/predictfun_update_markets.py discover 生成 PF 标签页/JSON）")
        return 1

    if mode in ("live", "once") and limit is None:
        print("✗ 实盘必须带 --limit N 控制市场数（防敞口失控）。例：once --limit 3")
        return 1

    if mode == "plan":
        _plan(backend, tokens, limit); return 0
    if mode == "once":
        _place_once(backend, tokens, limit); return 0
    if mode == "live":
        try:
            asyncio.run(_live(backend, tokens, limit))
        except KeyboardInterrupt:
            print("\n[runner] 收到中断,正在撤掉本账户所有挂单（清场）...")
            try:
                res = backend.cancel_all()
                print(f"[runner] ✅ 已撤所有挂单：{res}")
            except Exception as e:
                print(f"[runner] ⚠️ 退出撤单失败，请手动跑 `cancel`：{e}")
        return 0

    print(f"未知模式 {mode}（plan|live|once|cancel）"); return 1


if __name__ == "__main__":
    sys.exit(main())
