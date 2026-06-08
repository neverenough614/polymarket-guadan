"""REST 兜底 —— 周期性用持仓校正 filled，并重新触发 requote，捕捉 ws 漏掉的盘口/成交。"""
from __future__ import annotations

import asyncio
from typing import Dict

from .book_state import BookState
from .executor import Executor
from .position_state import PositionState


async def run_reconcile(
    client,
    states: Dict[str, PositionState],
    books: Dict[str, BookState],
    executor: Executor,
    interval_s: int,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            positions = await asyncio.to_thread(client.get_positions_by_token)
            for tid, state in states.items():
                if tid in positions:
                    # 只把【启动后】新增持仓计入，避免既有持仓污染目标
                    delta = positions[tid] - state.baseline_position
                    if delta > 0:
                        state.reconcile_filled(delta)
                state.mark_done_if_complete(executor.min_order_size)
            # 重新评估每个 token，补回 ws 可能漏触发的 requote
            for tid, state in states.items():
                if tid in books:
                    await executor.reconcile_token(state, books[tid])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[reconcile] 跳过本轮: {e}")
