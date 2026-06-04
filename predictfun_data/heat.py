"""predict.fun 市场热度（仿 Polymarket market_heat，按 predict.fun 适配）。

状态机 SAFE → WARM → HOT → FROZEN，主信号=防御触发计数（2h 滚动窗口）。
与 Polymarket 的差异（均有据）：
  - 去掉 reward_shock：predict.fun 无链上 sponsor 事件源，唯一信号是防御触发。
  - 热度作用从"缩量"改为"参与门控"：predict.fun 挂单量固定 min_size(=shareThreshold)
    不能缩（缩到 min_size 以下就拿不到奖励），故 FROZEN 的含义=**该市场冻结跳过**
    （plan 不选 / 监控不重挂）一段冷却期，避免在反复被攻击的市场里一次次重挂被吃。
  - 阈值重定：每次防御触发 +25，2h 内上限 3 次=75；FROZEN 需 3 次（=75），
    冷却按 2h 内真实触发次数分级：3 次→6h，4 次→12h，5 次+→24h。

JSON 原子持久化到**独立文件**（predictfun_heat_state.json），绝不碰 Polymarket 的
market_heat_state.json。now_fn / state_file 可注入便于单测。
"""
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import time as _time

# ── 分数阈值（score → 状态）──
SCORE_FROZEN = 75
SCORE_HOT = 50
SCORE_WARM = 25

# ── 防御触发信号 ──
TRIGGER_SCORE = 25            # 每次防御触发 +25
TRIGGER_MAX_COUNT = 3         # 2h 窗口内计分上限 3 次（75 封顶）
TRIGGER_WINDOW_SECS = 7200    # 2 小时滚动窗口

# ── FROZEN 冷却期（秒），按 2h 内真实触发次数分级 ──
COOLDOWN_6H = 6 * 3600
COOLDOWN_12H = 12 * 3600
COOLDOWN_24H = 24 * 3600

DEFAULT_STATE_FILE = "predictfun_heat_state.json"


def _cooldown_secs(n_triggers: int) -> int:
    if n_triggers >= 5:
        return COOLDOWN_24H
    if n_triggers >= 4:
        return COOLDOWN_12H
    return COOLDOWN_6H        # 进入 FROZEN 至少 3 次


class HeatEntry:
    __slots__ = ("state", "score", "trigger_times", "frozen_until", "question", "last_change")

    def __init__(self) -> None:
        self.state: str = "SAFE"
        self.score: int = 0
        self.trigger_times: List[float] = []
        self.frozen_until: Optional[float] = None
        self.question: str = ""
        self.last_change: float = 0.0

    def to_dict(self) -> dict:
        return {"state": self.state, "score": self.score, "trigger_times": self.trigger_times,
                "frozen_until": self.frozen_until, "question": self.question,
                "last_change": self.last_change}

    @classmethod
    def from_dict(cls, d: dict) -> "HeatEntry":
        e = cls()
        e.state = d.get("state", "SAFE")
        e.score = int(d.get("score", 0) or 0)
        e.trigger_times = list(d.get("trigger_times", []) or [])
        e.frozen_until = d.get("frozen_until")
        e.question = d.get("question", "")
        e.last_change = float(d.get("last_change", 0.0) or 0.0)
        return e


class MarketHeatTracker:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE,
                 now_fn: Callable[[], float] = _time.time) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, HeatEntry] = {}
        self._state_file = Path(state_file)
        self._now = now_fn
        self._load()

    # ── 持久化（原子写）──
    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            text = self._state_file.read_text(encoding="utf-8")
            raw = json.loads(text) if text.strip() else {}
        except (json.JSONDecodeError, OSError):
            return
        for tid, d in raw.items():
            try:
                self._entries[tid] = HeatEntry.from_dict(d)
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        try:
            data = {tid: e.to_dict() for tid, e in self._entries.items()}
            serialized = json.dumps(data, indent=2, ensure_ascii=False)
            directory = self._state_file.parent if str(self._state_file.parent) else Path(".")
            fd, tmp = tempfile.mkstemp(prefix=self._state_file.name + ".", suffix=".tmp", dir=str(directory))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(serialized)
                os.replace(tmp, self._state_file)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError as e:
            print(f"⚠️ [predict.fun 热度] 保存失败：{e}")

    # ── 内部 ──
    def _prune(self, e: HeatEntry) -> None:
        cutoff = self._now() - TRIGGER_WINDOW_SECS
        e.trigger_times = [t for t in e.trigger_times if t > cutoff]

    def _score(self, e: HeatEntry) -> int:
        n = min(len(e.trigger_times), TRIGGER_MAX_COUNT)
        return n * TRIGGER_SCORE

    def _determine(self, e: HeatEntry, score: int) -> str:
        now = self._now()
        if e.state == "FROZEN" and e.frozen_until and now < e.frozen_until:
            return "FROZEN"          # 冷却未到期 → 保持冻结
        if score >= SCORE_FROZEN:
            return "FROZEN"
        if score >= SCORE_HOT:
            return "HOT"
        if score >= SCORE_WARM:
            return "WARM"
        return "SAFE"

    def _apply(self, e: HeatEntry) -> Tuple[str, str]:
        """剪枝→算分→定状态（冷却由 record 时按触发次数武装，此处只读 frozen_until）。

        返回 (old, new)。须持锁。被动刷新（is_frozen/get_state）绝不重设冷却，
        否则每次轮询都会续命冻结期。
        """
        self._prune(e)
        score = self._score(e)
        old = e.state
        e.score = score
        new = self._determine(e, score)
        if new != old:
            e.last_change = self._now()
        e.state = new
        return old, new

    def _get_or_create(self, tid: str, question: str) -> HeatEntry:
        e = self._entries.get(tid)
        if e is None:
            e = HeatEntry()
            e.question = question
            self._entries[tid] = e
        elif question:
            e.question = question
        return e

    # ── 公开 API ──
    def record_defense_trigger(self, token_id: str, question: str = "") -> None:
        """防御触发（深度跌幅/同档被吃/偏斜/趋势命中）时调用→升温，必要时冻结。"""
        tid = str(token_id)
        with self._lock:
            e = self._get_or_create(tid, question)
            e.trigger_times.append(self._now())
            old, new = self._apply(e)
            if new == "FROZEN":
                # 冷却按 2h 内真实触发次数分级，并随攻击持续延长（取较晚到期，绝不缩短）
                until = self._now() + _cooldown_secs(len(e.trigger_times))
                e.frozen_until = max(e.frozen_until or 0.0, until)
            self._save()
            if old != new:
                print(f"🌡️ [predict.fun 热度] {(e.question or tid)[:40]} | {old} → {new} "
                      f"(score={e.score}, 触发{len(e.trigger_times)}次/2h)")

    def is_frozen(self, token_id: str) -> bool:
        """该市场是否处于冻结期（plan 不选 / 监控不重挂）。"""
        tid = str(token_id)
        with self._lock:
            e = self._entries.get(tid)
            if e is None:
                return False
            old, new = self._apply(e)
            if old != new:
                self._save()
            return e.state == "FROZEN"

    def get_state(self, token_id: str) -> Tuple[str, int, str]:
        """(state, score, reason)。未追踪→('SAFE',0,'')。"""
        tid = str(token_id)
        with self._lock:
            e = self._entries.get(tid)
            if e is None:
                return "SAFE", 0, ""
            self._apply(e)
            n = len(e.trigger_times)
            reason = f"防御触发{n}次/2h" if n else ""
            if e.state == "FROZEN" and e.frozen_until:
                hrs = max(0.0, (e.frozen_until - self._now()) / 3600)
                reason += f"，冻结剩 {hrs:.1f}h"
            return e.state, e.score, reason


# 进程内单例（plan 与 live 同进程共享；跨进程靠 JSON 文件持久化）
tracker = MarketHeatTracker()
