"""predict.fun SP1 验收：testnet 跑通 鉴权→取市场+簿→挂极小单→查到→撤掉。

用法：
  PLATFORM=predictfun PREDICTFUN_NETWORK=testnet python scripts/predictfun_smoke.py
前置：pip install -e ".[predictfun]"；.env 配好 PREDICTFUN_PK。
"""
import sys
import time

from predictfun_data.predictfun_client import PredictFunClient


def main() -> int:
    c = PredictFunClient(network="testnet")
    print(f"[1] 鉴权 OK，address={c.address}")

    markets = c.get_markets(status="OPEN", first=5)
    if not markets:
        print("✗ 没取到 OPEN 市场"); return 1
    m = markets[0]
    market_id = m.get("id")
    print(f"[2] 取到市场 id={market_id} q={str(m.get('question'))[:50]}")

    book = c.get_orderbook(market_id)
    print(f"[3] 簿：bids={len(book.bids)} asks={len(book.asks)} "
          f"best_bid={book.bids[0].price if book.bids else None} "
          f"best_ask={book.asks[0].price if book.asks else None}")

    # 取 YES outcome 的 token_id（字段名以实际为准；VERIFY）
    outcomes = m.get("outcomes") or []
    token_id = (outcomes[0].get("tokenId") if outcomes else None)
    if not token_id:
        print("✗ 未找到 outcome token_id（对照 OpenAPI 调整）"); return 1

    # 挂一张远离 mid 的极小买单（避免成交）：价 0.01，量 5
    place = c.create_order(token_id, "BUY", 0.01, 5,
                           neg_risk=bool(m.get("isNegRisk")),
                           is_yield_bearing=bool(m.get("isYieldBearing")))
    print(f"[4] 下单结果：{place}")
    if place.get("status") != "live":
        print("✗ 下单失败"); return 1

    time.sleep(2)
    mine = c.get_open_orders(market_id=market_id)
    print(f"[5] 我的单：{[o['id'] for o in mine]}")
    ids = [o["id"] for o in mine] or [place["order_id"]]

    rm = c.remove_orders(ids)
    print(f"[6] 撤单：removed={rm['removed']} noop={rm['noop']}")

    time.sleep(2)
    left = c.get_open_orders(market_id=market_id)
    print(f"[7] 撤后剩余我的单：{[o['id'] for o in left]}")
    print("✓ SP1 smoke 通过" if not left else "⚠ 仍有残留单，检查撤单逻辑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
