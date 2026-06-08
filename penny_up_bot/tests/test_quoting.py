"""quoting 核心纯逻辑测试 —— penny-up 目标价计算的全分支。"""
import pytest

from penny_up_bot.quoting import compute_target, round_to_tick


class TestRoundToTick:
    def test_cleans_float_error(self):
        # 0.64 + 0.01 的浮点结果应被规整回 0.65，不能因 0.6500000001 误判越界
        assert round_to_tick(0.64 + 0.01, 0.01) == 0.65

    def test_rounds_down_to_tick_grid(self):
        assert round_to_tick(0.123, 0.01) == 0.12

    def test_rounds_up_to_tick_grid(self):
        assert round_to_tick(0.129, 0.01) == 0.13

    def test_supports_thousandth_tick(self):
        assert round_to_tick(0.6491, 0.001) == 0.649


class TestComputeTarget:
    TICK = 0.01
    CAP = 0.65

    def test_no_competitor_returns_none(self):
        # 无对手 → 暂不挂单等待
        assert compute_target(None, self.TICK, self.CAP) is None

    def test_normal_penny_up(self):
        # 对手 0.60 → 我挂 0.61（只超一个 tick）
        assert compute_target(0.60, self.TICK, self.CAP) == 0.61

    def test_target_equal_to_cap_is_allowed(self):
        # 对手 0.64 → 0.65 == cap，允许
        assert compute_target(0.64, self.TICK, self.CAP) == 0.65

    def test_competitor_at_cap_returns_none(self):
        # 对手 0.65 → 0.66 > cap → 撤单等待
        assert compute_target(0.65, self.TICK, self.CAP) is None

    def test_competitor_above_cap_returns_none(self):
        assert compute_target(0.70, self.TICK, self.CAP) is None

    def test_float_safety_at_boundary(self):
        # 浮点边界：0.64 + 0.01 不能因浮点误差被判为 > 0.65
        result = compute_target(0.64, 0.01, 0.65)
        assert result == 0.65
        assert result is not None

    def test_thousandth_tick_market(self):
        assert compute_target(0.649, 0.001, 0.650) == 0.650
        assert compute_target(0.650, 0.001, 0.650) is None  # 0.651 > 0.650

    def test_low_competitor_repegs_down(self):
        # 对手很低时也只超一个 tick（向下跟，省钱）
        assert compute_target(0.10, self.TICK, self.CAP) == 0.11
