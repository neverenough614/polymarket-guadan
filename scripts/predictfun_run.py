"""predict.fun 实盘收尾 runner（SP1-5 串联）—— 独立入口,完全不碰 Polymarket/VPS。

把五个子项目串起来：
  factory 建 PredictFunClient+Backend → 注册市场(分页) → strategy_loader 载选市结果
  → place_orders 初挂真实双边(奖励价带内) → monitor_loop 保守守护(churn 双闸防清零)

挂几个市场：默认取文件顶部常量 DEFAULT_MARKET_LIMIT（改那里即可）；命令行 --limit N 覆盖。

模式（Windows PowerShell）：
  python scripts/predictfun_run.py plan                # 只读：按默认市场数打印要挂的市场+价格+效率,不下单
  python scripts/predictfun_run.py live  --limit 1     # 实盘：挂单+守护（--limit 覆盖默认；省略=用默认）
  python scripts/predictfun_run.py once  --limit 1     # 实盘：只挂一轮,不进守护循环(便于验证)
  python scripts/predictfun_run.py cancel              # 安全：撤掉本账户所有挂单
  python scripts/predictfun_run.py close               # 一键清仓：merge/卖出所有被吃出来的持仓(不挂新单)

前置 .env：PLATFORM=predictfun / PREDICTFUN_NETWORK=mainnet / PREDICTFUN_PK / PREDICTFUN_API_KEY
        / PREDICTFUN_ACCOUNT / SPREADSHEET_URL(+credentials.json)。

⚠️ 实盘安全：live/once 会下真实单。市场数由 DEFAULT_MARKET_LIMIT（或 --limit）控制；设 0=不限(慎用)。
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

# ⚙️ 默认挂几个市场（不带 --limit 时用这个）。想改挂单市场数直接改这里；
#    命令行 --limit N 仍会临时覆盖它。设 0 = 不限（按预算挂满，慎用）。
DEFAULT_MARKET_LIMIT = 8


def _parse_args(argv):
    mode = argv[1] if len(argv) > 1 else "plan"
    limit = None
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):          # --limit 2
            try:
                limit = int(argv[i + 1])
            except ValueError:
                pass
        elif a.startswith("--limit="):                     # --limit=2
            try:
                limit = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a.startswith("--limit") and a[len("--limit"):].isdigit():  # --limit2（粘连）
            limit = int(a[len("--limit"):])
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
    print(f"\n--- 选中 {len(selected)} 个市场（按单位资本效率 PP/USDT 降序，预算守门 ≤ 余额×{SAFETY}）---")
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


def _make_backfill(backend, all_tokens):
    """造补位函数 backfill(exclude_market_keys, n_needed)->新挂的 token 列表。

    从全部候选里排除：已在挂(exclude)、热度冻结、奖励失效的市场；再按效率降序、
    受剩余预算约束，选 n_needed 个最优市场挂上。供 monitor_loop 自动轮换补位调用。
    """
    from datetime import datetime, timezone
    from predictfun_data.monitor import reward_active

    def backfill(exclude_market_keys, n_needed):
        if not n_needed or n_needed <= 0:
            return []
        now_dt = datetime.now(timezone.utc)

        def candidate_ok(toks):
            if any(heat_tracker.is_frozen(str(t["token_id"])) for t in toks):
                return False
            if any(not reward_active(t.get("reward_ends_at"), now_dt) for t in toks):
                return False
            return True

        markets = [(k, toks) for k, toks in _group_markets(backend, all_tokens)
                   if k not in exclude_market_keys and candidate_ok(toks)]
        if not markets:
            return []
        total_bal = backend.raw_client.get_usdt_balance()
        available = available_after_open_buys(total_bal, backend.get_all_orders())
        selected, _skip, _total, _dropped = build_plan(
            markets, _quote_fn(backend), _min_size, available, safety=SAFETY, max_markets=n_needed,
        )
        placed = []
        for _key, legs, _eff in selected:
            for t, price, size in legs:
                res = place_leg(backend, t, price, size)
                if res.get("status") == "placed":
                    placed.append(t)
                    print(f"  ♻️➕ 补挂 {str(t['question'])[:28]} [{t.get('token_type')}] BUY@{price:.3f}×{size:.0f}")
        return placed

    return backfill


async def _live(backend, tokens, limit):
    # _place_once 内有同步 SDK 调用(get_usdt_balance/签名)，必须在工作线程跑，
    # 否则 predict-sdk 会拒绝"async 上下文里调同步方法"。
    target = await asyncio.to_thread(_place_once, backend, tokens, limit)
    if not target:
        print("[runner] 无在挂订单，不进入守护循环。")
        return
    mc = cfg.predictfun_monitor
    churn = ChurnGuard(mc.token_cooldown_sec, mc.max_cancels_per_hour)
    target_count = limit if (limit and limit > 0) else None   # 0/不限 → 不做轮换补位
    backfill_fn = _make_backfill(backend, tokens) if target_count else None
    # 运行内重载（对齐 Polymarket sheet_sync）：重读表→刷新奖励字段、撤掉下架市场；
    # on_pool_reload 原地刷新 tokens（_make_backfill 捕获的就是它，闭包即见新候选池）。
    def _on_pool_reload(fresh):
        tokens[:] = fresh
    print("\n=== 进入守护循环（盯订单簿变化防御 + auto_close + 自动轮换补位 + 表重载；Ctrl+C 退出自动撤光清场）===")
    await monitor_loop(
        backend, target, churn, target_count=target_count, backfill_fn=backfill_fn,
        reload_fn=load_predictfun_markets, on_pool_reload=_on_pool_reload,
    )


def main() -> int:
    mode, limit = _parse_args(sys.argv)
    if limit is None:
        limit = DEFAULT_MARKET_LIMIT          # 不带 --limit → 用文件内默认；--limit N 覆盖
    src = "命令行" if "--limit" in " ".join(sys.argv) else f"默认 DEFAULT_MARKET_LIMIT"
    print(f"=== predict.fun runner 模式={mode} 挂单市场数={limit}（{src}；0=不限）===")

    if mode == "cancel":
        backend = create_execution_backend("predictfun")
        res = backend.cancel_all()
        print(f"[cancel] 已撤所有挂单：{res}")
        return 0

    if mode == "close":
        # 一键清仓：merge 完整套 / 走簿卖出单边残仓（不挂新单）。用于清掉被吃出来的卡住持仓。
        from predictfun_data.auto_close import run_auto_close
        backend = create_execution_backend("predictfun")
        print("[close] 注册市场(取 meta/簿)...")
        backend.refresh_markets(status="OPEN")
        res = run_auto_close(backend)
        print(f"[close] merge {res['merged']} / sell {res['sold']}（清仓动作 {len(res.get('actions', []))} 个）")
        return 0

    backend, tokens = _setup()

    if not tokens:
        print("✗ 无策略 token（先跑 scripts/predictfun_update_markets.py discover 生成 PF 标签页/JSON）")
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
            # 收尾清仓：撤单只清挂单，临停前成交的残仓不受影响 → 再扫一遍 merge/卖出，
            # 否则成交在 live 收尾附近的持仓会被遗留（auto_close 只在 live 跑时执行）。
            try:
                from predictfun_data.auto_close import run_auto_close
                res = run_auto_close(backend)
                print(f"[runner] ✅ 收尾清仓：merge {res['merged']} / sell {res['sold']}"
                      f"（清仓动作 {len(res.get('actions', []))} 个）")
            except Exception as e:
                print(f"[runner] ⚠️ 退出清仓失败，请手动跑 `close`：{e}")
        return 0

    print(f"未知模式 {mode}（plan|live|once|cancel|close）"); return 1


if __name__ == "__main__":
    sys.exit(main())
