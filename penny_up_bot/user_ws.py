"""用户成交 websocket —— 订阅本账户订单/成交，用累计 size_matched 扣减剩余目标量。"""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Awaitable, Callable, Dict

import websockets

from .config import USER_WS_URL
from .position_state import PositionState


async def run_user_ws(
    client,
    states: Dict[str, PositionState],
    on_fill: Callable[[str], Awaitable[None]],
) -> None:
    """订阅用户成交；断线自动重连。on_fill(token_id) 在记账更新后调用。"""
    while True:
        try:
            async with websockets.connect(USER_WS_URL, ping_interval=5, ping_timeout=None) as ws:
                auth = {
                    "type": "user",
                    "auth": {
                        "apiKey": client.creds.api_key,
                        "secret": client.creds.api_secret,
                        "passphrase": client.creds.api_passphrase,
                    },
                }
                await ws.send(json.dumps(auth))
                print("[user_ws] 已订阅本账户成交")
                async for raw in ws:
                    rows = json.loads(raw)
                    if not isinstance(rows, list):
                        rows = [rows]
                    for row in rows:
                        if row.get("event_type") != "order":
                            continue
                        tid = row.get("asset_id")
                        oid = row.get("id")
                        if tid in states and oid:
                            size_matched = float(row.get("size_matched", 0) or 0)
                            states[tid].record_order_match(oid, size_matched)
                            await on_fill(tid)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[user_ws] 断开重连: {e}")
            print(traceback.format_exc())
            await asyncio.sleep(5)
