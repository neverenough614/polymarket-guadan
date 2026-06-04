"""只读诊断：把真实持仓的原始结构 + 解析结果 + 清仓决策整条链打出来。

用于排查"被吃单后持仓没自动卖出"。不下任何单、不撤单，纯 GET。

用法：python scripts/predictfun_inspect_positions.py
"""
import os
import sys
import json

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from execution.factory import create_execution_backend
from predictfun_data.auto_close import positions_to_map, decide_close_actions
from config.bot_config import cfg


def main():
    backend = create_execution_backend("predictfun")
    client = backend.raw_client
    print(f"[inspect] 账户={client.address}")

    # 1) 原始 REST 响应（最关键：看真实字段名/单位）
    print("\n===== 1) 原始 /v1/positions 响应 =====")
    try:
        raw = client.rest.get_positions()
        print(json.dumps(raw, ensure_ascii=False, indent=2, default=str)[:4000])
    except Exception as e:
        print(f"  ✗ rest.get_positions 失败: {e}")
        raw = None

    # 2) client.get_positions()（取 data 后）
    print("\n===== 2) client.get_positions() =====")
    rows = client.get_positions()
    print(f"  类型={type(rows).__name__}  条数={len(rows) if hasattr(rows,'__len__') else '?'}")
    if rows:
        first = rows[0]
        print(f"  首行类型={type(first).__name__}")
        if isinstance(first, dict):
            print(f"  首行字段={list(first.keys())}")
            print(f"  首行内容={json.dumps(first, ensure_ascii=False, default=str)[:1500]}")

    # 3) positions_to_map 解析结果（看是否解析出份额）
    print("\n===== 3) positions_to_map 解析 =====")
    rows2 = backend.get_all_positions()
    if hasattr(rows2, "to_dict"):
        rows2 = rows2.to_dict("records")
    pmap = positions_to_map(rows2, min_close=0.0)
    print(f"  解析出 {len(pmap)} 个持仓: {pmap}")
    if not pmap:
        print("  ⚠️ 解析为空 → 这就是没自动卖出的原因（字段名/单位对不上，见上面原始结构）")

    # 4) 注册市场后看 meta + 簿 + 清仓决策
    print("\n===== 4) 注册市场 + 清仓决策 =====")
    if pmap:
        backend.refresh_markets(status="OPEN")
        for tid in pmap:
            meta = backend.meta_for(tid)
            try:
                book = backend.get_order_book(tid)
                nb = len(book.bids or []) if book else 0
            except Exception as e:
                nb = f"取簿失败 {e}"
            print(f"  token {tid[:14]}… 持仓={pmap[tid]:.2f}  meta={'有' if meta else '无'}"
                  f"  condition={getattr(meta,'condition_id',None)}  comp={getattr(meta,'complement_token_id',None)}"
                  f"  bid档数={nb}")

        def bids_of(t):
            try:
                b = backend.get_order_book(t)
                return sorted(((float(x.price), float(x.size)) for x in (b.bids or [])),
                              key=lambda z: z[0], reverse=True) if b else []
            except Exception:
                return []

        mc = cfg.predictfun_monitor
        actions = decide_close_actions(pmap, backend.meta_for, bids_of,
                                       min_close=mc.close_min_position, sell_offset=mc.close_offset,
                                       max_drop=mc.close_max_drop)
        print(f"\n  decide_close_actions → {len(actions)} 个动作:")
        for a in actions:
            print(f"    {a.kind} token={str(a.token_id)[:14] if a.token_id else '-'} "
                  f"cond={str(a.condition_id)[:14] if a.condition_id else '-'} "
                  f"size={a.size:.2f} price={a.price:.3f} reason={a.reason}")
        if not actions:
            print("    ⚠️ 无动作 → 可能 min_close 过滤 / 无 meta / 无买档")


if __name__ == "__main__":
    main()
