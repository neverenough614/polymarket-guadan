"""
集中配置 poly-maker 机器人所有核心参数。
按功能分组，便于按模块引用和单元测试覆盖。
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class SheetConfig:
    """策略表格与黑名单配置"""
    STRATEGY_SHEET_NAME: str = "Normal LP Strategy"
    AGGRESSIVE_SHEET_NAME: str = "High Reward Aggressive"
    QUESTION_BLACKLIST_KEYWORDS: List[str] = field(default_factory=list)


@dataclass
class PlaceConfig:
    """自动挂单配置"""
    DEPTH_THRESHOLD_TIER1: float = 500.0      # 第1档深度阈值（USDC）
    DEPTH_THRESHOLD_TIER2: float = 200.0      # 第2、3档深度阈值（USDC）
    EXTREME_PRICE_THRESHOLD: float = 0.10     # 极端价格阈值（<0.10 或 >0.90 必须双向挂单）
    MAX_LEVEL_GAP: float = 0.02               # 档位连续性检查阈值（相邻档位价差超过此值则认为流动性不连续）
    MIN_FIRST_TIER_DEPTH: float = 100.0       # 第1档最低深度（USDC），低于此值整个市场跳过

    # 动态挂单量（分策略）
    NORMAL_SIZE_RATIO: float = 0.30
    NORMAL_MAX_ORDER_SIZE: float = 700.0
    AGGRESSIVE_SIZE_RATIO: float = 0.08
    AGGRESSIVE_MAX_ORDER_SIZE: float = 300.0
    DYNAMIC_SIZE_RATIO: float = 0.10          # 默认（手动挂单等）
    MAX_ORDER_SIZE: float = 500.0
    TIER1_ORDER_RATIO: float = 1/5            # 第1档挂单价值不超过该档深度的比例（保守策略，原1/3）
    TIER1_MAX_DEPTH_RATIO: float = 5.0        # 第1档/第2档深度比上限，超过则视为孤立厚墙，跳过第1档

    RETRY_INTERVAL: int = 300                 # 深度不足重试间隔（秒）
    SHEET_RELOAD_INTERVAL: int = 300          # 表格重载间隔（秒）
    PLACE_ORDER_WORKERS: int = 8              # 并发挂单线程数
    ENABLE_AUTO_PLACE: bool = True


@dataclass
class CloseConfig:
    """自动清仓配置"""
    POSITION_CHECK_INTERVAL: int = 5          # 持仓检查间隔（秒）
    MIN_POSITION_TO_CLOSE: float = 5.0        # 最小清仓阈值（shares）
    CLOSE_PRICE_OFFSET: float = 0.01          # 清仓价格偏移（best_bid - 此值，确保成交）


@dataclass
class SpreadCheckConfig:
    """插队检测配置"""
    SPREAD_CHECK_INTERVAL: int = 60           # 插队检测间隔（秒）


@dataclass
class DefenseConfig:
    """监控防御配置"""
    THRESHOLD_FRONT_DEPTH_DROP: float = 0.30
    THRESHOLD_SAME_DEPTH_DROP: float = 0.50
    THRESHOLD_FRONT_HIGH_WATER_DROP: float = 0.50
    THRESHOLD_SAME_HIGH_WATER_DROP: float = 0.60
    MIN_SAME_DEPTH_SAFE: float = 300.0
    MIN_FRONT_DEPTH_THRESHOLD: float = 100.0
    MIN_FRONT_DEPTH_ABSOLUTE: float = 100.0
    MIN_FRONT_DEPTH_ABSOLUTE_REF: float = 0.0
    MONITOR_CHECK_INTERVAL: int = 2
    MAX_CONCURRENT_WORKERS: int = 20
    ORDERBOOK_TIMEOUT: int = 3
    ENABLE_AUTO_DEFENSE: bool = True


@dataclass
class ImbalanceConfig:
    """买卖深度偏斜检测配置"""
    IMBALANCE_THRESHOLD: float = 0.30
    IMBALANCE_DEPTH_LEVELS: int = 5
    IMBALANCE_MIN_TOTAL_DEPTH: float = 500.0
    ENABLE_IMBALANCE_DETECTION: bool = True


PREDICTFUN_ENDPOINTS = {
    "testnet": {"base_url": "https://api-testnet.predict.fun", "chain_id": 97, "requires_api_key": False},
    "mainnet": {"base_url": "https://api.predict.fun", "chain_id": 56, "requires_api_key": True},
}


@dataclass
class PredictFunConfig:
    """predict.fun 平台配置（BNB 链）。network 决定 URL/链/是否需 API key。"""
    network: str = "testnet"
    tick_size: float = 0.01
    default_yield_bearing: bool = False
    rate_limit_per_min: int = 240

    def __post_init__(self):
        if self.network not in PREDICTFUN_ENDPOINTS:
            raise ValueError(f"未知 network: {self.network}（应为 testnet|mainnet）")
        ep = PREDICTFUN_ENDPOINTS[self.network]
        self.base_url = ep["base_url"]
        self.chain_id = ep["chain_id"]
        self.requires_api_key = ep["requires_api_key"]


@dataclass
class BotConfig:
    """总配置，聚合各子配置"""
    sheet: SheetConfig = field(default_factory=SheetConfig)
    place: PlaceConfig = field(default_factory=PlaceConfig)
    close: CloseConfig = field(default_factory=CloseConfig)
    spread_check: SpreadCheckConfig = field(default_factory=SpreadCheckConfig)
    defense: DefenseConfig = field(default_factory=DefenseConfig)
    imbalance: ImbalanceConfig = field(default_factory=ImbalanceConfig)


# 全局配置实例
cfg = BotConfig()
