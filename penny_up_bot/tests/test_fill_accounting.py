"""position_state 测试 —— 成交记账、剩余量、完成判定。

关键不变量：用每个 order_id 的【累计】size_matched 作为真相（幂等 set，非 add），
所以重复/乱序消息、以及 re-peg 产生的新 order_id 都不会重复计数。
"""
from penny_up_bot.position_state import PositionState


def make_state(total=1000.0, cap=0.65):
    return PositionState(
        token_id="tok",
        total_size=total,
        cap_price=cap,
        tick=0.01,
        neg_risk=False,
        label="test",
    )


class TestRemaining:
    def test_fresh_state(self):
        s = make_state()
        assert s.remaining() == 1000.0
        assert not s.done


class TestOrderMatchAccounting:
    def test_single_order_match(self):
        s = make_state()
        s.record_order_match("oid1", 30.0)
        assert s.filled == 30.0
        assert s.remaining() == 970.0

    def test_duplicate_message_idempotent(self):
        s = make_state()
        s.record_order_match("oid1", 30.0)
        s.record_order_match("oid1", 30.0)  # 重复同值
        assert s.filled == 30.0

    def test_cumulative_growth_same_order(self):
        s = make_state()
        s.record_order_match("oid1", 30.0)
        s.record_order_match("oid1", 50.0)  # 同单累计增长
        assert s.filled == 50.0

    def test_out_of_order_lower_value_ignored(self):
        s = make_state()
        s.record_order_match("oid1", 50.0)
        s.record_order_match("oid1", 30.0)  # 乱序到达的旧值
        assert s.filled == 50.0

    def test_multiple_orders_sum(self):
        # re-peg 产生新 order_id，两单累加
        s = make_state()
        s.record_order_match("oid1", 50.0)
        s.record_order_match("oid2", 20.0)
        assert s.filled == 70.0


class TestDoneDetection:
    def test_done_when_target_reached(self):
        s = make_state(total=100.0)
        s.record_order_match("oid1", 100.0)
        assert s.mark_done_if_complete(min_order_size=5.0) is True
        assert s.done

    def test_done_when_remaining_below_min_order_size(self):
        s = make_state(total=100.0)
        s.record_order_match("oid1", 97.0)  # 剩 3 < 最小下单量 5
        assert s.mark_done_if_complete(min_order_size=5.0) is True

    def test_not_done_when_remaining_sufficient(self):
        s = make_state(total=100.0)
        s.record_order_match("oid1", 50.0)
        assert s.mark_done_if_complete(min_order_size=5.0) is False
        assert not s.done


class TestRestingOrder:
    def test_set_and_clear_resting(self):
        s = make_state()
        s.set_resting("oid1", 0.61, 100.0)
        assert s.order_id == "oid1" and s.order_price == 0.61 and s.order_size == 100.0
        s.clear_resting()
        assert s.order_id is None and s.order_price is None


class TestReconcile:
    def test_reconcile_bumps_filled_up_not_down(self):
        s = make_state()
        s.record_order_match("oid1", 30.0)
        s.reconcile_filled(45.0)   # REST 持仓更高 → 采纳，防止超买
        assert s.filled == 45.0
        s.reconcile_filled(40.0)   # 更低 → 不回退，保住进度
        assert s.filled == 45.0
