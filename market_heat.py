"""
动态市场热度追踪器 (Dynamic Market Heat Tracker)

状态机: SAFE → WARM → HOT → FROZEN
主信号: 防御触发计数（实时，2h 滚动窗口）
辅助信号: reward_shock（新 sponsor 事件）

设计要点:
  - 进入 HOT/FROZEN 快（防御触发即升级）
  - 退出慢（FROZEN 有强制冷却期，依触发强度 6-24h）
  - 与 vol_factor 独立：vol_factor 管"历史波动缩单"，heat_multiplier 管"实时危险降仓"
  - JSON 持久化：重启后热度状态不丢失
"""

import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ======================================================
# 配置参数
# ======================================================

# 状态阈值（分数 → 状态）
SCORE_FROZEN = 80
SCORE_HOT    = 50
SCORE_WARM   = 20

# ── 防御触发信号（主信号）──
DEFENSE_TRIGGER_SCORE       = 25     # 每次防御触发 +25 分
DEFENSE_TRIGGER_MAX_COUNT   = 3      # 2h 窗口内最多计 3 次（75 分封顶）
DEFENSE_TRIGGER_WINDOW_SECS = 7200   # 2 小时滚动窗口

# ── Reward shock 信号（辅助）──
REWARD_SHOCK_SCORE      = 30      # 新 sponsor 事件 +30 分
REWARD_SHOCK_DECAY_SECS = 43200   # 12 小时后自然失效

# ── 热度乘数（影响挂单量）──
HEAT_SIZE_MULTIPLIER = {
    "SAFE":   1.0,
    "WARM":   0.5,
    "HOT":    0.15,
    "FROZEN": 0.0,
}

# ── FROZEN 冷却期（秒）──
FROZEN_COOLDOWN_MAP = {
    "low":    6 * 3600,    # 无防御触发、仅 reward_shock → 6h
    "medium": 12 * 3600,   # 1-2 次防御触发 → 12h
    "high":   24 * 3600,   # 3+ 次防御触发 → 24h
}

# ── 持久化文件 ──
DEFAULT_STATE_FILE = "market_heat_state.json"


# ======================================================
# 数据结构
# ======================================================

class MarketHeatEntry:
    __slots__ = (
        "state", "score", "defense_trigger_times", "reward_shock_time",
        "frozen_until", "question", "last_state_change",
    )

    def __init__(self):
        self.state: str = "SAFE"
        self.score: int = 0
        self.defense_trigger_times: List[float] = []
        self.reward_shock_time: Optional[float] = None
        self.frozen_until: Optional[float] = None
        self.question: str = ""
        self.last_state_change: float = time.time()

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "score": self.score,
            "defense_trigger_times": self.defense_trigger_times,
            "reward_shock_time": self.reward_shock_time,
            "frozen_until": self.frozen_until,
            "question": self.question,
            "last_state_change": self.last_state_change,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MarketHeatEntry":
        e = cls()
        e.state = d.get("state", "SAFE")
        e.score = d.get("score", 0)
        e.defense_trigger_times = d.get("defense_trigger_times", [])
        e.reward_shock_time = d.get("reward_shock_time")
        e.frozen_until = d.get("frozen_until")
        e.question = d.get("question", "")
        e.last_state_change = d.get("last_state_change", time.time())
        return e


# ======================================================
# 核心 Tracker
# ======================================================

class MarketHeatTracker:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self._lock = threading.Lock()
        self._entries: Dict[str, MarketHeatEntry] = {}
        self._state_file = Path(state_file)
        self._load()

    # ── 持久化 ──────────────────────────────────────────

    def _load(self):
        if not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
            for token_id, d in raw.items():
                self._entries[token_id] = MarketHeatEntry.from_dict(d)
            print(f"🌡️ [热度] 从 {self._state_file} 加载了 {len(self._entries)} 个市场状态")
        except Exception as e:
            print(f"⚠️ [热度] 加载状态文件失败: {e}，将从空白开始")

    def _save(self):
        try:
            data = {tid: e.to_dict() for tid, e in self._entries.items()}
            self._state_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"⚠️ [热度] 保存状态文件失败: {e}")

    # ── 内部工具 ────────────────────────────────────────

    def _get_or_create(self, token_id: str, question: str = "") -> MarketHeatEntry:
        if token_id not in self._entries:
            e = MarketHeatEntry()
            e.question = question
            self._entries[token_id] = e
        elif question:
            self._entries[token_id].question = question
        return self._entries[token_id]

    def _calc_score(self, e: MarketHeatEntry) -> int:
        """从原始信号重新计算分数（无状态副作用）"""
        now = time.time()

        # 1. 防御触发（滚动 2h 窗口）
        cutoff = now - DEFENSE_TRIGGER_WINDOW_SECS
        e.defense_trigger_times = [t for t in e.defense_trigger_times if t > cutoff]
        n = min(len(e.defense_trigger_times), DEFENSE_TRIGGER_MAX_COUNT)
        score = n * DEFENSE_TRIGGER_SCORE

        # 2. Reward shock（12h 内有效）
        if e.reward_shock_time and (now - e.reward_shock_time) < REWARD_SHOCK_DECAY_SECS:
            score += REWARD_SHOCK_SCORE

        return min(score, 100)

    def _determine_state(self, e: MarketHeatEntry, score: int) -> str:
        now = time.time()
        # FROZEN 有强制冷却期：未到期则保持 FROZEN
        if e.state == "FROZEN" and e.frozen_until and now < e.frozen_until:
            return "FROZEN"
        # 按分数判定
        if score >= SCORE_FROZEN:
            return "FROZEN"
        if score >= SCORE_HOT:
            return "HOT"
        if score >= SCORE_WARM:
            return "WARM"
        return "SAFE"

    def _set_frozen_cooldown(self, e: MarketHeatEntry):
        n = len(e.defense_trigger_times)
        if n >= 3:
            secs = FROZEN_COOLDOWN_MAP["high"]
        elif n >= 1:
            secs = FROZEN_COOLDOWN_MAP["medium"]
        else:
            secs = FROZEN_COOLDOWN_MAP["low"]
        e.frozen_until = time.time() + secs

    def _apply_state_change(self, e: MarketHeatEntry, new_score: int) -> Tuple[str, str]:
        """更新 entry 的 score/state，返回 (old_state, new_state)"""
        old_state = e.state
        e.score = new_score
        new_state = self._determine_state(e, new_score)
        if new_state == "FROZEN" and old_state != "FROZEN":
            self._set_frozen_cooldown(e)
        if new_state != old_state:
            e.last_state_change = time.time()
        e.state = new_state
        return old_state, new_state

    # ── 公开接口：记录事件 ──────────────────────────────

    def record_defense_trigger(self, token_id: str, question: str = ""):
        """防御系统触发时调用（深度跌幅/偏斜/趋势检测命中）"""
        with self._lock:
            e = self._get_or_create(token_id, question)
            e.defense_trigger_times.append(time.time())
            score = self._calc_score(e)
            old, new = self._apply_state_change(e, score)
            self._save()
            if old != new:
                _log_state_change(e.question, old, new, score, "防御触发")

    def record_reward_shock(self, token_id: str, question: str = ""):
        """链上检测到新 sponsor/reward 事件时调用"""
        with self._lock:
            e = self._get_or_create(token_id, question)
            e.reward_shock_time = time.time()
            score = self._calc_score(e)
            old, new = self._apply_state_change(e, score)
            self._save()
            if old != new:
                _log_state_change(e.question, old, new, score, "奖励冲击")

    # ── 公开接口：查询状态 ──────────────────────────────

    def get_heat_state(self, token_id: str) -> Tuple[str, int, str]:
        """
        返回 (state, score, reason_str)
        如果 token 未被追踪，返回 ("SAFE", 0, "")
        """
        with self._lock:
            if token_id not in self._entries:
                return "SAFE", 0, ""
            e = self._entries[token_id]
            score = self._calc_score(e)
            old, new = self._apply_state_change(e, score)
            if old != new:
                self._save()
                _log_state_change(e.question, old, new, score, "自然衰减")
            return e.state, e.score, _build_reason(e)

    def get_heat_multiplier(self, token_id: str) -> float:
        """获取挂单量乘数：SAFE=1.0, WARM=0.5, HOT=0.15, FROZEN=0.0"""
        state, _, _ = self.get_heat_state(token_id)
        return HEAT_SIZE_MULTIPLIER[state]

    def should_skip_tier1(self, token_id: str) -> bool:
        """WARM/HOT 状态下跳过第一档"""
        state, _, _ = self.get_heat_state(token_id)
        return state in ("WARM", "HOT")

    # ── 公开接口：跨进程同步 ────────────────────────────

    def sync_from_disk(self):
        """
        从 JSON 文件合并外部进程写入的更新（如 reward_monitor.py 的 reward_shock）。
        只合并 reward_shock_time（取较新的值），不覆盖内存中的防御触发数据。
        """
        if not self._state_file.exists():
            return
        with self._lock:
            try:
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
                merged = 0
                for token_id, d in raw.items():
                    disk_shock = d.get("reward_shock_time")
                    if not disk_shock:
                        continue
                    if token_id in self._entries:
                        mem_shock = self._entries[token_id].reward_shock_time
                        if mem_shock is None or disk_shock > mem_shock:
                            self._entries[token_id].reward_shock_time = disk_shock
                            if d.get("question"):
                                self._entries[token_id].question = d["question"]
                            merged += 1
                    else:
                        self._entries[token_id] = MarketHeatEntry.from_dict(d)
                        merged += 1
                if merged > 0:
                    print(f"🌡️ [热度·同步] 从磁盘合并了 {merged} 个 reward_shock 更新")
            except Exception as e:
                print(f"⚠️ [热度·同步] 读取磁盘状态失败: {e}")

    # ── 公开接口：定期维护 ──────────────────────────────

    def decay_all(self):
        """
        周期性调用（建议每 30 分钟）：
        - 检查 FROZEN 冷却到期的市场，重新评估状态
        - 清理长时间 SAFE 的 stale 条目
        """
        with self._lock:
            changed = False
            stale_ids = []

            for token_id, e in self._entries.items():
                score = self._calc_score(e)
                old, new = self._apply_state_change(e, score)
                if old != new:
                    changed = True
                    _log_state_change(e.question, old, new, score, "定期衰减")

                # 清理：SAFE 且 48h 无任何信号的条目
                if e.state == "SAFE" and not e.defense_trigger_times and not e.reward_shock_time:
                    if time.time() - e.last_state_change > 48 * 3600:
                        stale_ids.append(token_id)

            for tid in stale_ids:
                del self._entries[tid]
                changed = True

            if changed:
                self._save()

    # ── 公开接口：Dashboard ─────────────────────────────

    def get_all_states_summary(self) -> List[dict]:
        """返回所有非 SAFE 市场的热度摘要（给 Dashboard 用）"""
        with self._lock:
            result = []
            for token_id, e in self._entries.items():
                # 刷新分数
                score = self._calc_score(e)
                e.score = score
                state = self._determine_state(e, score)
                if state == "SAFE":
                    continue
                remaining = ""
                if state == "FROZEN" and e.frozen_until:
                    secs_left = max(0, e.frozen_until - time.time())
                    hours_left = secs_left / 3600
                    remaining = f"{hours_left:.1f}h"
                result.append({
                    "token_id":  token_id[:16] + "...",
                    "question":  e.question[:50],
                    "state":     state,
                    "score":     score,
                    "triggers":  len(e.defense_trigger_times),
                    "remaining": remaining,
                    "reason":    _build_reason(e),
                })
            return sorted(result, key=lambda x: x["score"], reverse=True)


# ======================================================
# 辅助函数（模块级）
# ======================================================

def _build_reason(e: MarketHeatEntry) -> str:
    parts = []
    n = len(e.defense_trigger_times)
    if n > 0:
        parts.append(f"防御触发{n}次/2h")
    if e.reward_shock_time:
        hours_ago = (time.time() - e.reward_shock_time) / 3600
        if hours_ago < 12:
            parts.append(f"新奖励({hours_ago:.1f}h前)")
    return ", ".join(parts) if parts else ""


def _log_state_change(question: str, old: str, new: str, score: int, source: str):
    label = question[:40] if question else "unknown"
    print(f"\n🌡️ [热度·{source}] {label} | {old} → {new} (score={score})")


# ======================================================
# 全局单例
# ======================================================
tracker = MarketHeatTracker()
