"""penny_up_bot 入口 —— asyncio 编排市场 ws / 用户 ws / REST 兜底，全部 token 完成或 Ctrl+C 退出。

运行：
    python -m penny_up_bot.run
先在 penny_up_bot/config.py 填 TOKENS，并把 penny_up_bot/.env 填成【另一个号】。
"""
from __future__ import annotations

import asyncio
import sys
from typing import Dict, List

# Windows 控制台可能是 GBK，输出非 GBK 字符会崩溃；统一改 UTF-8 且永不因编码报错。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — 老环境无 reconfigure 时忽略
        pass

from .book_state import BookState
from .client import PennyUpClient
from .config import TOKENS, Settings
from .executor import Executor
from .market_ws import run_market_ws
from .position_state import PositionState
from .reconcile import run_reconcile
from .resolver import resolve
from .user_ws import run_user_ws


def _print_confirmation(state: PositionState, settings: Settings) -> None:
    print(
        f"市场: {state.label}   买入方向: {state.outcome}   token_id: {state.token_id}\n"
        f"  上限价: {state.cap_price}   目标量: {state.total_size} shares   "
        f"tick: {state.tick}   neg_risk: {state.neg_risk}   既有持仓基线: {state.baseline_position}\n"
        f"  DRY_RUN: {settings.dry_run}"
    )


async def main() -> None:
    settings = Settings.from_env()

    if not TOKENS:
        print("[!] 请先在 penny_up_bot/config.py 填写 TOKENS 再运行。")
        return

    print("初始化客户端…")
    client = PennyUpClient()

    # 解析 token + 读取持仓基线
    resolved = [resolve(cfg, client, settings) for cfg in TOKENS]
    base_positions = await asyncio.to_thread(client.get_positions_by_token)

    states: Dict[str, PositionState] = {}
    books: Dict[str, BookState] = {}
    print("\n================ 启动确认（请核对方向）================")
    for r in resolved:
        st = PositionState(
            r.token_id, r.total_size, r.cap_price, r.tick, r.neg_risk, r.label, r.outcome
        )
        st.baseline_position = base_positions.get(r.token_id, 0.0)
        states[r.token_id] = st
        books[r.token_id] = BookState()
        _print_confirmation(st, settings)
    print("=====================================================\n")

    executor = Executor(client, settings)
    token_ids: List[str] = list(states.keys())

    async def on_change(tid: str) -> None:
        asyncio.create_task(executor.reconcile_token(states[tid], books[tid]))

    async def on_fill(tid: str) -> None:
        st = states[tid]
        print(f"[fill] {st.label} 已成交 {st.filled}/{st.total_size}（剩余 {st.remaining()}）")
        if st.mark_done_if_complete(executor.min_order_size):
            print(f"[done] {st.label} 建仓完成")
        asyncio.create_task(executor.reconcile_token(st, books[tid]))

    done_event = asyncio.Event()

    async def watch_done() -> None:
        while True:
            if all(s.done for s in states.values()):
                print("所有 token 建仓完成，准备退出…")
                done_event.set()
                return
            await asyncio.sleep(2)

    tasks = [
        asyncio.create_task(run_market_ws(token_ids, books, on_change)),
        asyncio.create_task(run_user_ws(client, states, on_fill)),
        asyncio.create_task(run_reconcile(client, states, books, executor, settings.reconcile_interval_s)),
        asyncio.create_task(watch_done()),
    ]

    try:
        await done_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n收到中断，撤掉本工具的所有挂单…")
    finally:
        for t in tasks:
            t.cancel()
        await executor.cancel_all_own(list(states.values()))
        print("已退出（只撤了本工具自己的单）。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
