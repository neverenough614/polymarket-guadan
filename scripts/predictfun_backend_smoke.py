"""SP2 主网 smoke：验证 PredictFunBackend 全链路 + my-orders 字段形状 + No 侧写路。

只下一张极小、远离盘口、不会成交的单，随即撤掉（沿用 SP1 已验证的安全做法）。
绝不打印私钥。用法：python scripts/predictfun_backend_smoke.py
"""
import sys
import json

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from predictfun_data.predictfun_client import PredictFunClient
from execution.predictfun_backend import PredictFunBackend


def main() -> int:
    client = PredictFunClient(network="mainnet")
    be = PredictFunBackend(client)
    n = be.refresh_markets(first=40)
    print(f"[A] 注册表 token 数={n}")

    # 选一个有 No 侧挂单(=Yes 有 bids)的市场，验证 No 侧 complement
    market = None
    for m in client.get_markets(status="OPEN", first=40):
        outs = m.get("outcomes") or []
        if len(outs) == 2:
            market = m
            break
    if not market:
        print("✗ 没有二元市场"); return 1

    outs = market["outcomes"]
    yes = next((o for o in outs if str(o.get("name")).lower() == "yes"), outs[0])
    no = next((o for o in outs if str(o.get("name")).lower() == "no"), outs[1])
    yes_id, no_id = str(yes["onChainId"]), str(no["onChainId"])
    print(f"[B] 市场 id={market['id']} q='{str(market.get('question') or market.get('categorySlug'))[:45]}'")
    print(f"    Yes onChainId=...{yes_id[-8:]}  No onChainId=...{no_id[-8:]}")

    # backend 取簿：Yes 原生 vs No 复合，对照 outcome 自带 bestBid/bestAsk
    yb = be.get_order_book(yes_id)
    nb = be.get_order_book(no_id)
    print(f"[C] backend Yes 簿: best_bid={yb.bids[0].price if yb.bids else None} "
          f"best_ask={yb.asks[0].price if yb.asks else None}  "
          f"(outcome 自带 bestBid={yes.get('bestBid')} bestAsk={yes.get('bestAsk')})")
    print(f"[D] backend No  簿: best_bid={nb.bids[0].price if nb.bids else None} "
          f"best_ask={nb.asks[0].price if nb.asks else None}  "
          f"(outcome 自带 bestBid={no.get('bestBid')} bestAsk={no.get('bestAsk')})")
    print("    ↑ backend No 簿 best 应与 outcome 自带 No bestBid/bestAsk 相等 → complement 正确")

    # 在 No 侧下一张极小、远低于盘口的 BUY（0.02×50=1.0 USD，不会成交），验证写路用 No onChainId
    print("[E] 在 No 侧下单：BUY 0.02 x 50（价值 1.0 USD，远离盘口、不会成交）...")
    place = be.create_order(no_id, "BUY", 0.02, 50)
    print(f"    下单返回={json.dumps(place, ensure_ascii=False)[:300]}")
    if place.get("status") != "live":
        print("✗ 下单失败（看 error）"); return 1

    # 拉我的挂单，dump 原始字段形状（确认 token_id 字段名，撤单过滤依赖它）
    orders = be.get_all_orders()
    print(f"[F] 我的挂单数={len(orders)}")
    for o in orders[:3]:
        print(f"    normalized: id={o.get('id')} token_id={o.get('token_id')} side={o.get('side')} "
              f"price={o.get('price')} status={o.get('status')}")
        print(f"    RAW keys={list((o.get('raw') or {}).keys())}")
        print(f"    RAW={json.dumps(o.get('raw'), ensure_ascii=False, default=str)[:400]}")

    matched = [o for o in orders if str(o.get("token_id")) == no_id]
    print(f"[G] 按 No token_id 过滤到 {len(matched)} 张（撤单将依赖此匹配）")

    # 撤单（按 token：只撤 No）
    be.cancel_all_asset(no_id)
    after = [o for o in be.get_all_orders() if str(o.get("token_id")) == no_id]
    print(f"[H] cancel_all_asset(No) 后剩余 No 挂单={len(after)}")
    print("✓ SP2 smoke 完成。若 [G] 匹配到 ≥1 且 [H]=0，则 token_id 字段名正确、撤单按 id 工作正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
