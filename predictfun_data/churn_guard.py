"""撤单/重挂频率护栏（SP5）—— 反 predict.fun 反作弊清零的核心闸门。

两道闸：
  ① 每 token 冷却：同一市场两次撤/挂动作之间至少间隔 cooldown 秒；
  ② 全局滚动窗口预算：过去 1 小时内的撤单动作数不得超过 max_cancels_per_hour。
任一不满足即拒绝本次动作（宁可不动，也不触发"撤单滥用"）。

纯逻辑、注入 now，便于测试；线程不敏感（监控循环单线程顺序调用）。
"""
from collections import deque
from typing import Deque, Dict


class ChurnGuard:
    def __init__(self, token_cooldown_sec: float, max_cancels_per_hour: int):
        self._cooldown = float(token_cooldown_sec)
        self._max_per_hour = int(max_cancels_per_hour)
        self._last_action: Dict[str, float] = {}
        self._recent: Deque[float] = deque()   # 全局动作时间戳（滚动 1h）

    def _prune(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()

    def allow(self, token_id: str, now: float, count_as_cancel: bool = True) -> bool:
        """是否允许对该 token 现在执行一次动作。

        副作用：会 _prune 过期的全局时间戳（监控循环单线程顺序调用，安全）。
        count_as_cancel=True（重心=含撤单）才校验全局撤单预算；
        纯补单（place-only，不撤）只受冷却约束（撤单滥用针对的是撤，不是挂）。
        """
        self._prune(now)
        if count_as_cancel and len(self._recent) >= self._max_per_hour:
            return False
        last = self._last_action.get(str(token_id))
        if last is not None and (now - last) < self._cooldown:
            return False
        return True

    def budget_ok(self, now: float) -> bool:
        """仅校验全局小时撤单预算（不看 token 冷却）。

        用于**保护性撤单**：撤单是保命动作，绝不能被"该 token 刚重挂过"的冷却挡住
        （否则会拿着危险单干等冷却）。撤单滥用的闸放在"重挂"侧（allow 的 token 冷却）。
        这里只保留高位小时预算作安全网，正常永不触及。
        """
        self._prune(now)
        return len(self._recent) < self._max_per_hour

    def record(self, token_id: str, now: float, count_as_cancel: bool = True) -> None:
        """记录一次已执行的动作（更新冷却；含撤单时计入全局预算）。"""
        self._last_action[str(token_id)] = now
        if count_as_cancel:
            self._recent.append(now)
        self._prune(now)

    def remaining_budget(self, now: float) -> int:
        self._prune(now)
        return max(0, self._max_per_hour - len(self._recent))
