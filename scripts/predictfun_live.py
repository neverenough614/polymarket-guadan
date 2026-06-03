"""predict.fun 主网分段实盘 runner（智能账户模式）。

用法（Windows PowerShell）：
  python scripts/predictfun_live.py readonly   # 阶段1：只读，不下单（零风险）
  python scripts/predictfun_live.py approve     # 一次性：链上授权（烧 gas）
  python scripts/predictfun_live.py order        # 阶段2：挂一张极小单 + 立即撤（极小风险）

前置 .env：
  PLATFORM=predictfun
  PREDICTFUN_NETWORK=mainnet
  PREDICTFUN_PK=0x...            # 智能账户 owner/signer 私钥
  PREDICTFUN_API_KEY=...         # 主网必需
  PREDICTFUN_ACCOUNT=0x...       # predict.fun 智能账户地址（资金所在）
"""
import sys
import json
import traceback

# Windows 终端常为 GBK，强制 stdout/stderr 用 UTF-8，避免 ✓/✗/中文 打印崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from predictfun_data.predictfun_client import PredictFunClient


def _stage_readonly(c):
    print(f"[A] address(签名/账户)={c.address}")
    bal = c.get_usdt_balance()
    print(f"[B] USDT 余额={bal}")
    markets = c.get_markets(status="OPEN", first=5)
    print(f"[C] 取到 {len(markets)} 个 OPEN 市场")
    for i, m in enumerate(markets[:3]):
        print(f"    市场[{i}] id={m.get('id')} negRisk={m.get('isNegRisk')} "
              f"yield={m.get('isYieldBearing')} q={str(m.get('question'))[:40]}")
        print(f"            outcomes={json.dumps(m.get('outcomes'), ensure_ascii=False)[:300]}")
    if markets:
        mid = markets[0].get("id")
        book = c.get_orderbook(mid)
        print(f"[D] 市场 {mid} 簿：bids={len(book.bids)} asks={len(book.asks)} "
              f"best_bid={book.bids[0].price if book.bids else None} "
              f"best_ask={book.asks[0].price if book.asks else None}")
    mine = c.get_open_orders()
    print(f"[E] 当前我的挂单数={len(mine)}")
    print("✓ 阶段1 只读完成。把以上输出贴回，确认字段无误后再 approve / order。")


def _stage_approve(c):
    print("[A] 执行链上授权 set_approvals（会烧 gas，可能要等几秒）...")
    res = c.set_approvals()
    print(f"[B] 授权结果：{res}")
    print("✓ 授权完成（一次性）。")


def _stage_order(c):
    markets = c.get_markets(status="OPEN", first=5)
    if not markets:
        print("✗ 没取到市场"); return 1
    m = markets[0]
    market_id = m.get("id")
    book = c.get_orderbook(market_id)
    outcomes = m.get("outcomes") or []
    token_id = outcomes[0].get("tokenId") if outcomes else None
    print(f"[A] 目标市场 id={market_id} token_id={token_id} "
          f"best_bid={book.bids[0].price if book.bids else None}")
    if not token_id:
        print("✗ 未拿到 token_id（看阶段1 outcomes 字段名，对照调整）"); return 1
    # 极小买单，价格远离 mid（0.02）以免立即成交
    print("[B] 准备下单：BUY 0.02 x 5（极小、远离盘口）")
    place = c.create_order(token_id, "BUY", 0.02, 5,
                           neg_risk=bool(m.get("isNegRisk")),
                           is_yield_bearing=bool(m.get("isYieldBearing")))
    print(f"[C] 下单返回：{json.dumps(place, ensure_ascii=False)[:500]}")
    if place.get("status") != "live":
        print("✗ 下单失败（看上面 error，多半是 # VERIFY 字段，贴回修）"); return 1
    mine = c.get_open_orders(market_id=market_id)
    ids = [o["id"] for o in mine] or [place["order_id"]]
    print(f"[D] 我的挂单 ids={ids}")
    rm = c.remove_orders(ids)
    print(f"[E] 撤单：{json.dumps(rm, ensure_ascii=False)[:300]}")
    print("✓ 阶段2 完成（已撤）。确认资金无误后即可放量。")
    return 0


def main() -> int:
    stage = sys.argv[1] if len(sys.argv) > 1 else "readonly"
    print(f"=== predict.fun 主网 runner，阶段={stage} ===")
    try:
        c = PredictFunClient(network="mainnet")
    except Exception as e:
        print(f"✗ 客户端初始化/鉴权失败：{e}")
        traceback.print_exc()
        return 1
    try:
        if stage == "readonly":
            _stage_readonly(c); return 0
        if stage == "approve":
            _stage_approve(c); return 0
        if stage == "order":
            return _stage_order(c) or 0
        print(f"未知阶段 {stage}（readonly|approve|order）"); return 1
    except Exception as e:
        print(f"✗ 阶段 {stage} 出错：{e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
