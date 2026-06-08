"""市场盘口 websocket —— 订阅 token 列表，维护每 token 的 BookState，盘口变动即触发 requote。"""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Awaitable, Callable, Dict, List

import websockets

from .book_state import BookState
from .config import MARKET_WS_URL


def apply_market_message(books: Dict[str, BookState], msg: dict):
    """把一条市场 ws 消息应用到对应 BookState；返回变动的 token_id，无关消息返回 None。"""
    et = msg.get("event_type")
    tid = msg.get("asset_id")
    if not tid or tid not in books:
        return None
    if et == "book":
        books[tid].apply_snapshot(msg.get("bids", []), msg.get("asks", []))
        return tid
    if et == "price_change":
        for ch in msg.get("price_changes", msg.get("changes", [])):
            books[tid].apply_price_change(ch["side"], float(ch["price"]), float(ch["size"]))
        return tid
    return None


async def run_market_ws(
    token_ids: List[str],
    books: Dict[str, BookState],
    on_change: Callable[[str], Awaitable[None]],
) -> None:
    """订阅市场盘口；断线自动重连。on_change(token_id) 在该 token 盘口变动后调用。"""
    while True:
        try:
            async with websockets.connect(MARKET_WS_URL, ping_interval=5, ping_timeout=None) as ws:
                await ws.send(json.dumps({"assets_ids": token_ids}))
                print(f"[market_ws] 已订阅 {len(token_ids)} 个 token")
                async for raw in ws:
                    msgs = json.loads(raw)
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    changed = set()
                    for m in msgs:
                        tid = apply_market_message(books, m)
                        if tid:
                            changed.add(tid)
                    for tid in changed:
                        await on_change(tid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[market_ws] 断开重连: {e}")
            print(traceback.format_exc())
            await asyncio.sleep(5)
