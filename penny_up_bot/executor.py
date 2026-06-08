"""撤+挂执行引擎 —— 把「这一刻该挂什么」落地为真实订单，并发安全。

防御历史 bug（重复挂单 / 补位竞态）：
  - 每 token 一把 asyncio.Lock，同一 token 任一时刻只允许一个撤+挂事务在途。
  - lock 已被占用时直接丢弃本次触发（最新盘口会再次触发；reconcile 周期兜底）。
  - 去抖：每 token 最小重挂间隔。
  - 空操作短路：目标价/量没变就不动。
  - 同 token 至多一张 live 单：先撤旧再挂新。
所有网络调用走 asyncio.to_thread，不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from .book_state import BookState
from .position_state import PositionState
from .quoting import compute_target

DRY_ORDER_ID = "DRY_RUN"
_SIZE_EPS = 1e-6


def _extract_order_id(resp: Dict[str, Any]) -> Optional[str]:
    if not isinstance(resp, dict):
        return None
    for key in ("orderID", "orderId", "id", "order_id"):
        if resp.get(key):
            return str(resp[key])
    return None


class Executor:
    def __init__(self, client, settings) -> None:
        self.client = client
        self.settings = settings
        self.interval_s = settings.requote_min_interval_ms / 1000.0
        self.min_order_size = getattr(settings, "default_min_order_size", 5.0)
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_action: Dict[str, float] = {}

    def _lock_for(self, token_id: str) -> asyncio.Lock:
        return self._locks.setdefault(token_id, asyncio.Lock())

    def _desired_size(self, state: PositionState) -> float:
        return round(state.remaining(), 2)

    async def reconcile_token(self, state: PositionState, book: BookState) -> None:
        """根据当前盘口，把该 token 的挂单调整到位（penny-up 或撤单）。"""
        token_id = state.token_id

        if state.done:
            async with self._lock_for(token_id):
                await self._ensure_no_order(state)
            return

        lock = self._lock_for(token_id)
        if lock.locked():
            return  # 已有在途事务，丢弃本次

        async with lock:
            if state.mark_done_if_complete(self.min_order_size):
                await self._ensure_no_order(state)
                return

            best_comp = book.best_competing_bid(state.order_price, state.order_size, state.tick)
            target = compute_target(best_comp, state.tick, state.cap_price)

            if target is None:
                await self._ensure_no_order(state)
                return

            desired_size = self._desired_size(state)
            if desired_size <= self.min_order_size - _SIZE_EPS:
                # 剩余太小不值得挂
                state.mark_done_if_complete(self.min_order_size)
                await self._ensure_no_order(state)
                return

            # 空操作短路：价、量都没变
            if (
                state.order_id is not None
                and state.order_price is not None
                and abs(state.order_price - target) < state.tick / 2.0
                and abs(state.order_size - desired_size) < _SIZE_EPS
            ):
                return

            # 去抖
            now = time.monotonic()
            if now - self._last_action.get(token_id, 0.0) < self.interval_s:
                return

            await self._replace(state, target, desired_size)
            self._last_action[token_id] = time.monotonic()

    async def _ensure_no_order(self, state: PositionState) -> None:
        if state.order_id is None:
            return
        await self._cancel(state)

    async def _cancel(self, state: PositionState) -> None:
        oid = state.order_id
        if oid is None:
            return
        if self.settings.dry_run or oid == DRY_ORDER_ID:
            print(f"[DRY] 撤单 {state.label} {state.order_size}@{state.order_price}")
        else:
            await asyncio.to_thread(self.client.cancel_order, oid)
        state.clear_resting()

    async def _replace(self, state: PositionState, price: float, size: float) -> None:
        # 先撤旧
        await self._cancel(state)
        # 再挂新
        if self.settings.dry_run:
            print(f"[DRY] 挂单 {state.label} BUY {size}@{price}  (剩余目标 {state.remaining()})")
            state.set_resting(DRY_ORDER_ID, price, size)
            return
        resp = await asyncio.to_thread(
            self.client.create_order, state.token_id, "BUY", price, size, state.neg_risk
        )
        oid = _extract_order_id(resp)
        if oid:
            state.set_resting(oid, price, size)
            print(f"[LIVE] 挂单 {state.label} BUY {size}@{price} -> {oid}")
        else:
            print(f"[LIVE] 挂单失败 {state.label} BUY {size}@{price} resp={resp}")

    async def cancel_all_own(self, states) -> None:
        """优雅退出：只撤本工具记录的单。"""
        for state in states:
            async with self._lock_for(state.token_id):
                await self._ensure_no_order(state)
