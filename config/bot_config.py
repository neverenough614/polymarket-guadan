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
    # 派生字段：由 __post_init__ 依据 network 填充
    base_url: str = field(init=False, default="")
    chain_id: int = field(init=False, default=0)
    requires_api_key: bool = field(init=False, default=False)

    def __post_init__(self):
        if self.network not in PREDICTFUN_ENDPOINTS:
            raise ValueError(f"未知 network: {self.network}（应为 testnet|mainnet）")
        ep = PREDICTFUN_ENDPOINTS[self.network]
        self.base_url = ep["base_url"]
        self.chain_id = ep["chain_id"]
        self.requires_api_key = ep["requires_api_key"]


@dataclass
class PredictFunDiscoveryConfig:
    """predict.fun 市场发现/选市配置（SP3）。

    选市标准：只挑当前有 active LP 奖励的市场（对标 Polymarket 奖励捕获）。
    复用同一个 Google spreadsheet，写入 predict.fun 专属标签页（不动 Polymarket 标签）。
    """
    selected_sheet: str = "PF Selected"          # 写入的 predict.fun 标签页名
    page_size: int = 100                          # 市场分页大小（cursor 分页）
    max_pages: int = 300                           # 分页上限保护（防失控；不足会告警不静默截断）
    min_hourly_rate: float = 0.0                  # 奖励强度下限（>此值才视为 active）
    max_markets: int = 0                          # 写表市场数上限（0=不限）
    strategy_json_path: str = "predictfun_strategy_tokens.json"  # 本地缓存/兜底


@dataclass
class PredictFunSelectionConfig:
    """predict.fun 选市/打分阈值（SP4）。

    直接对齐 Polymarket update_markets.py 的 Normal LP / High Reward Aggressive 阈值，
    使 predict.fun 选出的"会挂的市场"与 Polymarket 现网口径一致。
    打分公式复用 Polymarket 原函数（predictfun_data.scoring）。

    说明：days_to_expiry / volatility 两个 Polymarket 次级护栏在 predict.fun 无数据源
    （API 不暴露到期时间与历史波动），故不参与过滤（等价于 Polymarket 的"未知到期→纳入"分支），
    runner 会打印告警。核心目标函数（reward_per_100 效率 + 价差带 + 日奖励率）完整保留。
    """
    # Normal LP
    normal_spread_min: float = 0.01
    normal_spread_max: float = 0.04
    normal_mid_reward_min: float = 0.5
    normal_days_min: float = 7.0         # 到期护栏：奖励剩余 >7 天（或未知=0）才纳入
    normal_sheet: str = "PF Normal LP"
    # High Reward Aggressive
    agg_daily_rate_min: float = 100.0
    agg_reward_min: float = 2.0          # gm_reward_per_100 下限
    agg_spread_min: float = 0.02
    agg_spread_max: float = 0.12
    agg_days_min: float = 3.0            # 到期护栏：奖励剩余 >3 天（或未知=0）才纳入
    agg_sheet: str = "PF High Reward"
    # 打分并发 & 上限
    score_workers: int = 20
    normal_json_path: str = "predictfun_normal_tokens.json"
    aggressive_json_path: str = "predictfun_aggressive_tokens.json"


@dataclass
class PredictFunPlaceConfig:
    """predict.fun 挂单/报价配置（仿 Polymarket place_order_for_token，按真实薄簿校准）。

    校准依据：top40 按日奖励抽样的实时 YES 簿统计——
      档1深度 中位 411 / p25 52；前5档 中位 1857 / p25 805；spread 中位 0.016；bid档数 中位 10。
    Polymarket 的档深>1500/前5>5000 会杀掉 85%/62% 的市场，且 predict.fun **奖励最高的市场恰最薄**
    （PP 奖励=固定 hourlyRate 按带内瓜分，与簿深无关→薄=少竞争=每刀积分更高）。
    故深度门槛大幅下调，薄市场靠"出场流动性"而非"深度墙"控风险（用户取向：薄也挂+缩量+要求能出场）。

    与 Polymarket 的差异（均有据）：
      - 砍掉"占自己≤20%档深""孤立墙比≤3.5""档距连续性"——薄簿误杀，带内成员资格才是真约束。
      - 不移植"奖励效率闸≥0.2"——predict.fun reward_per_100 高达 300+（PP），恒过=空操作。
      - 动态量在薄市场落到 floor=min_size(=shareThreshold)，即"缩量"，而非 Polymarket 的"<min 就跳过"。
    """
    min_level_depth: float = 15.0        # 单档灰尘过滤（USDT）：低于此视为无效档，跳到下一档
    min_top5_depth: float = 150.0        # 前5档累计 sanity 下限（USDT）：低于此整个市场跳过
    size_ratio: float = 0.30             # 动态量=前3档买侧深度×此比/mid（对齐 Normal LP）
    max_order_size: float = 500.0        # 挂单量上限（份）；下限=token 的 min_size(≥100)
    require_exit_liquidity: bool = True   # 薄簿风控核心：买入成交后须能卖回更低 bid 平仓
    exit_max_gap: float = 0.05           # 出场：最近更低 bid 距我买价的最大间距
    exit_depth_multiplier: float = 1.0   # 出场：更低档累计深度须 ≥ 我 notional × 此值
    min_q_score_ratio: float = 0.04      # Q-score 下限 ((v-s)/v)^2（监控判无效单用）
    extreme_price_threshold: float = 0.10  # 极端价（≤0.10 或 ≥0.90）→ 要求该市场双侧都可报价


@dataclass
class PredictFunMonitorConfig:
    """predict.fun 监控/防御配置（SP5）—— 基调：稳挂少动，避开反作弊清零。

    predict.fun 奖励=PP 积分，且会对"撤单滥用/挂不可成交单"整号清零。故防御逻辑
    与 Polymarket 现网相反：不抢档、不高频撤重挂，只在"订单脱离奖励价带"或"一侧成交"
    时克制地补救，并受冷却 + 全局撤单预算双重限流。无 WebSocket，用温和轮询。
    """
    poll_interval_sec: int = 90          # 轮询间隔（温和；240/min 限速下绰绰有余）
    token_cooldown_sec: int = 300        # 同一 token 两次撤/挂动作最小间隔（5 分钟）
    max_cancels_per_hour: int = 60       # 全局滚动 1h 撤单预算上限（反作弊护栏）
    recenter_deadband_ticks: float = 1.0 # 出带滞回：超出奖励价带 >此 tick 数才重心（防抖）
    refill_on_fill: bool = True          # 一侧完全成交后补回该侧（维持双边=最大奖励）
    min_two_sided: bool = True           # 缺一侧即视为需补单（双边才拿满奖励）


@dataclass
class BotConfig:
    """总配置，聚合各子配置"""
    sheet: SheetConfig = field(default_factory=SheetConfig)
    place: PlaceConfig = field(default_factory=PlaceConfig)
    close: CloseConfig = field(default_factory=CloseConfig)
    spread_check: SpreadCheckConfig = field(default_factory=SpreadCheckConfig)
    defense: DefenseConfig = field(default_factory=DefenseConfig)
    imbalance: ImbalanceConfig = field(default_factory=ImbalanceConfig)
    predictfun_discovery: PredictFunDiscoveryConfig = field(default_factory=PredictFunDiscoveryConfig)
    predictfun_selection: PredictFunSelectionConfig = field(default_factory=PredictFunSelectionConfig)
    predictfun_place: PredictFunPlaceConfig = field(default_factory=PredictFunPlaceConfig)
    predictfun_monitor: PredictFunMonitorConfig = field(default_factory=PredictFunMonitorConfig)


# 全局配置实例
cfg = BotConfig()
