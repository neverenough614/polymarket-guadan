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
from predictfun_data.placer import compute_quotes, place_for_token
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


def _one_per_market(backend, tokens):
    """predict.fun 每市场只有一本簿 → 每市场只报价一个 outcome(优先 book 原生/YES 侧),
    两侧(bid+ask)挂在这一本簿上即满足"双边"奖励要求。避免同时报价 YES+NO 造成
    双倍资金占用与同簿自成交风险。未注册的 token 原样保留(异常兜底)。"""
    chosen = {}            # market_id -> token
    is_native = {}         # market_id -> bool
    extras = []            # 未注册的 token（异常兜底）
    for t in tokens:
        meta = backend.meta_for(t["token_id"])
        if meta is None:
            extras.append(t)
            continue
        mk = meta.market_id
        native = not meta.is_complement
        if mk not in chosen:
            chosen[mk] = t
            is_native[mk] = native
        elif native and not is_native[mk]:
            chosen[mk] = t     # native(YES) 覆盖先前的 complement(NO)
            is_native[mk] = True
    return list(chosen.values()) + extras


def _setup(one_per_market=True):
    """建 backend、注册全量市场、载入策略 token（默认每市场只留一个 outcome）。"""
    backend = create_execution_backend("predictfun")
    client = backend.raw_client
    print(f"[setup] 账户={client.address}  USDT={client.get_usdt_balance()}")
    print("[setup] 注册市场(分页拉全量,稍候)...")
    n = backend.refresh_markets(status="OPEN")
    print(f"[setup] 已注册 {n} 个 outcome token")
    tokens = load_predictfun_markets()
    print(f"[setup] 载入策略 token={len(tokens)}")
    if one_per_market:
        tokens = _one_per_market(backend, tokens)
        print(f"[setup] 每市场单 outcome 去重后={len(tokens)}（每市场两侧挂在同一本簿）")
    return backend, tokens


def _plan(backend, tokens):
    print(f"\n=== PLAN（只读,不下单）共 {len(tokens)} 个 token ===")
    quotable = 0
    for i, t in enumerate(tokens):
        tid = t["token_id"]
        _book, bb, ba, mid = get_orderbook_info(backend, tid)
        ms = t.get("max_spread")
        meta = backend.meta_for(tid)
        tick = getattr(meta, "tick_size", 0.01) if meta else 0.01
        size = max(100.0, float(t.get("min_size", 0) or 0))
        q = compute_quotes(bb or 0, ba or 0, ms, tick) if (bb and ba) else None
        if q:
            quotable += 1
            tag = f"买 {q[0]:.3f} / 卖 {q[1]:.3f}  价值≈{q[0]*size:.1f}+{q[1]*size:.1f} USDT"
        else:
            tag = "—（单侧簿/交叉,跳过）"
        print(f"  [{i+1}/{len(tokens)}] {str(t['question'])[:36]} [{t['token_type']}] "
              f"mid={mid if mid is None else round(mid,3)} band±{ms} → {tag}  size≈{size:.0f}")
    print(f"=== PLAN 结束：{quotable}/{len(tokens)} 可双边报价。确认后用 live/once + --limit 实盘 ===")


def _place_once(backend, tokens):
    print(f"\n=== 初挂双边（实盘,{len(tokens)} 个 token）===")
    ok = skip = 0
    for i, t in enumerate(tokens):
        _book, bb, ba, _mid = get_orderbook_info(backend, t["token_id"])
        placed = place_for_token(backend, t, bb or 0, ba or 0, {"BUY", "SELL"})
        live = [p for p in placed if p.get("status") == "placed"]
        if live:
            ok += 1
            info = " ".join(f"{p['side']}@{p['price']:.3f}" for p in live)
            print(f"  [{i+1}/{len(tokens)}] ✅ {str(t['question'])[:34]} → {info}")
        else:
            skip += 1
            print(f"  [{i+1}/{len(tokens)}] ⚠️ {str(t['question'])[:34]} → {placed[0].get('status') if placed else 'none'}")
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
        tokens = tokens[:limit]
        print(f"[runner] 限定前 {len(tokens)} 个市场")

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
