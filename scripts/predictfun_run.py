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
from predictfun_data.placer import compute_bid, place_bid
from orderbook.analyzer import get_orderbook_info


def _parse_args(argv):
    mode = argv[1] if len(argv) > 1 else "plan"
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            limit = None
    return mode, limit


def _limit_markets(backend, tokens, n_markets):
    """按市场分组(YES+NO 两个 token 同属一市场),取前 n_markets 个市场的全部 token。

    predict.fun 双边=买 YES + 买 NO(买 NO 即卖 YES),故每市场两个 outcome 都要挂买单。
    n_markets<=0 表示不限。返回展平后的 token 列表(每市场两张买单)。
    """
    by_market = {}      # market_id -> [tokens]，保持出现顺序
    order = []
    for t in tokens:
        meta = backend.meta_for(t["token_id"])
        mk = meta.market_id if meta is not None else ("u", t["token_id"])
        if mk not in by_market:
            by_market[mk] = []
            order.append(mk)
        by_market[mk].append(t)
    if n_markets and n_markets > 0:
        order = order[:n_markets]
    out = []
    for mk in order:
        out.extend(by_market[mk])
    return out, len(order)


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


def _plan(backend, tokens):
    print(f"\n=== PLAN（只读,不下单）{len(tokens)} 个 token（每 token 一张买单,YES+NO 构成双边）===")
    quotable = 0
    est_capital = 0.0
    for i, t in enumerate(tokens):
        tid = t["token_id"]
        _book, bb, ba, mid = get_orderbook_info(backend, tid)
        ms = t.get("max_spread")
        meta = backend.meta_for(tid)
        tick = getattr(meta, "tick_size", 0.01) if meta else 0.01
        size = max(100.0, float(t.get("min_size", 0) or 0))
        buy = compute_bid(bb or 0, ba or 0, ms, tick) if (bb and ba) else None
        if buy is not None:
            quotable += 1
            est_capital += buy * size
            tag = f"买 {buy:.3f}  占用≈{buy*size:.1f} USDT"
        else:
            tag = "—（单侧簿/交叉,跳过）"
        print(f"  [{i+1}/{len(tokens)}] {str(t['question'])[:34]} [{t['token_type']}] "
              f"mid={mid if mid is None else round(mid,3)} band±{ms} → {tag}  size≈{size:.0f}")
    print(f"=== PLAN 结束：{quotable}/{len(tokens)} 可报价，预计占用合计≈{est_capital:.1f} USDT。"
          f"确认后用 live/once + --limit 实盘 ===")


def _place_once(backend, tokens):
    print(f"\n=== 初挂（实盘,{len(tokens)} 个 token,各一张买单）===")
    ok = skip = 0
    for i, t in enumerate(tokens):
        _book, bb, ba, _mid = get_orderbook_info(backend, t["token_id"])
        res = place_bid(backend, t, bb or 0, ba or 0)
        if res.get("status") == "placed":
            ok += 1
            print(f"  [{i+1}/{len(tokens)}] ✅ {str(t['question'])[:32]} [{t['token_type']}] BUY@{res['price']:.3f}×{res['size']:.0f}")
        else:
            skip += 1
            print(f"  [{i+1}/{len(tokens)}] ⚠️ {str(t['question'])[:32]} [{t['token_type']}] → {res.get('status')}")
    print(f"=== 初挂完成：成功 {ok}，跳过/失败 {skip} ===")
    return ok, skip


async def _live(backend, tokens):
    _place_once(backend, tokens)
    mc = cfg.predictfun_monitor
    churn = ChurnGuard(mc.token_cooldown_sec, mc.max_cancels_per_hour)
    print("\n=== 进入守护循环（Ctrl+C 退出；退出不自动撤单,需要可单独跑 cancel）===")
    await monitor_loop(backend, tokens, churn)


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
        print("✗ 实盘必须带 --limit N 控制市场数（防敞口失控）。例：live --limit 1")
        return 1
    if limit is not None:
        tokens, n_mk = _limit_markets(backend, tokens, limit)
        print(f"[runner] 限定前 {n_mk} 个市场 → {len(tokens)} 个 token（每市场买 YES+买 NO）")

    if mode == "plan":
        _plan(backend, tokens); return 0
    if mode == "once":
        _place_once(backend, tokens); return 0
    if mode == "live":
        try:
            asyncio.run(_live(backend, tokens))
        except KeyboardInterrupt:
            print("\n[runner] 收到中断,退出守护循环。挂单仍在（如需清场跑 `cancel`）。")
        return 0

    print(f"未知模式 {mode}（plan|live|once|cancel）"); return 1


if __name__ == "__main__":
    sys.exit(main())
