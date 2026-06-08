"""penny_up_bot 配置。

用户主要编辑下方的 TOKENS 列表。每个 token 指定要建仓的市场、方向、上限价、总目标量。
方向用「市场 + YES/NO」填写，工具启动时自动解析成 token_id（见 resolver.py）；
也兼容直接填 token_id。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


# ============ 单个 token 的建仓配置 ============

@dataclass(frozen=True)
class TokenConfig:
    """一个要建仓的市场 outcome。

    指定市场三选一（优先级 token_id > condition_id > slug）：
      - token_id:     直接给 outcome token id（给了就跳过解析）
      - condition_id: 市场 condition id + outcome，由 CLOB get_market 解析
      - slug:         市场 slug + outcome，由 Gamma API 解析 condition_id 再走 CLOB
    """
    cap_price: float                      # 硬上限买价，下单价永不 > 此值
    total_size: float                     # 总目标建仓量（shares）
    outcome: str = "YES"                  # "YES" 或 "NO"（用 token_id 直填时仅作显示核对）
    token_id: Optional[str] = None
    condition_id: Optional[str] = None
    slug: Optional[str] = None
    neg_risk: Optional[bool] = None       # None = 让 resolver 自动判定
    label: str = ""                       # 可选：人类可读市场名，仅用于日志

    def validate(self) -> None:
        if not (0.0 < self.cap_price < 1.0):
            raise ValueError(f"cap_price 必须在 (0,1)：{self.cap_price}")
        if self.total_size <= 0:
            raise ValueError(f"total_size 必须 > 0：{self.total_size}")
        if self.outcome.strip().upper() not in {"YES", "NO"}:
            raise ValueError(f"outcome 必须是 YES 或 NO：{self.outcome}")
        if not (self.token_id or self.condition_id or self.slug):
            raise ValueError("必须指定 token_id、condition_id 或 slug 之一")


# ============ 用户编辑区：要建仓的市场 ============

TOKENS: list[TokenConfig] = [
    # 示例（请替换后再运行）：
    # TokenConfig(
    #     slug="will-x-happen-by-2026",
    #     outcome="YES",
    #     cap_price=0.65,
    #     total_size=1000.0,
    #     label="Will X happen by 2026?",
    # ),
]


# ============ 全局参数（带默认值，可改） ============

@dataclass(frozen=True)
class Settings:
    requote_min_interval_ms: int = 300    # 每 token 最小重挂间隔（去抖）
    default_tick: float = 0.01            # 查不到 tick 时的默认值
    default_min_order_size: float = 5.0   # 剩余 < 此值视为完成，不再挂
    reconcile_interval_s: int = 10        # REST 兜底同步周期
    dry_run: bool = True                  # 由 .env 的 DRY_RUN 覆盖

    @staticmethod
    def from_env() -> "Settings":
        dry = os.getenv("DRY_RUN", "true").strip().lower() != "false"
        return Settings(dry_run=dry)


HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
