"""DRY_RUN 端到端冒烟 —— book → quoting → executor → position 全链路离线跑通，零网络。

也顺带验证 ws/reconcile/run 模块能干净 import（无语法/导入错误）。
"""
import asyncio

import penny_up_bot.market_ws  # noqa: F401  导入即验证
import penny_up_bot.reconcile  # noqa: F401
import penny_up_bot.run  # noqa: F401
import penny_up_bot.user_ws  # noqa: F401
from penny_up_bot.book_state import BookState
from penny_up_bot.config import Settings
from penny_up_bot.executor import DRY_ORDER_ID, Executor
from penny_up_bot.position_state import PositionState


def book_with(bids):
    bs = BookState()
    bs.apply_snapshot(bids=[{"price": str(p), "size": str(s)} for p, s in bids], asks=[])
    return bs


def test_full_dryrun_lifecycle():
    settings = Settings(requote_min_interval_ms=0, dry_run=True)

    class FakeClient:
        created, cancelled = [], []

        def create_order(self, *a, **k):
            self.created.append(a)
            return {"orderID": "x"}

        def cancel_order(self, oid):
            self.cancelled.append(oid)

    client = FakeClient()
    ex = Executor(client, settings)
    state = PositionState("tok", total_size=50.0, cap_price=0.65, tick=0.01, neg_risk=False, label="M")

    async def scenario():
        # 1. 对手 0.60 → DRY 挂 0.61
        await ex.reconcile_token(state, book_with([(0.60, 100)]))
        assert state.order_id == DRY_ORDER_ID and state.order_price == 0.61 and state.order_size == 50.0

        # 2. 对手 0.62 → 升到 0.63
        await ex.reconcile_token(state, book_with([(0.62, 50), (0.60, 100)]))
        assert state.order_price == 0.63

        # 3. 对手顶到上限 0.65 → 撤单等待
        await ex.reconcile_token(state, book_with([(0.65, 100)]))
        assert state.order_id is None

        # 4. 对手回落 0.60 → 重新挂 0.61
        await ex.reconcile_token(state, book_with([(0.60, 100)]))
        assert state.order_price == 0.61

        # 5. 成交满目标 → 完成并撤单
        state.record_order_match("real-oid", 50.0)
        await ex.reconcile_token(state, book_with([(0.60, 100)]))
        assert state.done is True
        assert state.order_id is None

    asyncio.run(scenario())

    # 全程零真实下单（DRY 不碰网络）
    assert client.created == []
