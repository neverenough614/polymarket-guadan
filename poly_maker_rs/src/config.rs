//! 配置常量（对应 main.py 中的配置）

// 自动挂单
pub const DEPTH_THRESHOLD_TIER1: f64 = 1500.0;  // 第1档深度阈值 USDC（提高门槛确保只有深厚市场才挂第一档）
pub const DEPTH_THRESHOLD_TIER2: f64 = 200.0;   // 第2、3档深度阈值 USDC
pub const EXTREME_PRICE_THRESHOLD: f64 = 0.10;  // 极端价格阈值 <0.10 或 >0.90
pub const RETRY_INTERVAL_SECS: u64 = 300;       // 深度不足重试间隔 5分钟
pub const ENABLE_AUTO_PLACE: bool = true;

// 动态挂单量
pub const NORMAL_SIZE_RATIO: f64 = 0.30;
pub const NORMAL_MAX_ORDER_SIZE: f64 = 700.0;
pub const AGGRESSIVE_SIZE_RATIO: f64 = 0.08;
pub const AGGRESSIVE_MAX_ORDER_SIZE: f64 = 300.0;
pub const DYNAMIC_SIZE_RATIO: f64 = 0.10;
pub const MAX_ORDER_SIZE: f64 = 500.0;

// 订单簿分析
pub const MAX_LEVEL_GAP: f64 = 0.02;  // 档位连续性 2c

// 自动清仓
pub const POSITION_CHECK_INTERVAL_SECS: u64 = 3;
pub const MIN_POSITION_TO_CLOSE: f64 = 5.0;
pub const CLOSE_PRICE_OFFSET: f64 = 0.01;

// 插队检测
pub const SPREAD_CHECK_INTERVAL_SECS: u64 = 60;

// 策略 JSON 定期重载（对应 Python sheet_sync_task）
pub const JSON_RELOAD_INTERVAL_SECS: u64 = 300;  // 5分钟

// 监控防御
pub const THRESHOLD_FRONT_DEPTH_DROP: f64 = 0.20;   // 前墙单轮跌幅触发阈值
pub const THRESHOLD_SAME_DEPTH_DROP: f64 = 0.10;    // 同档(别人的)单轮跌幅触发阈值
pub const THRESHOLD_FRONT_HIGH_WATER_DROP: f64 = 0.50;
pub const THRESHOLD_SAME_HIGH_WATER_DROP: f64 = 0.50; // 同档高水位跌幅触发阈值
pub const MIN_SAME_DEPTH_SAFE: f64 = 200.0;   // 同档安全深度（USDC，排除自己后）
pub const MIN_FRONT_DEPTH_THRESHOLD: f64 = 100.0;
pub const MIN_FRONT_DEPTH_ABSOLUTE: f64 = 100.0;
pub const MIN_FRONT_DEPTH_ABSOLUTE_REF: f64 = 0.0;
pub const MONITOR_CHECK_INTERVAL_SECS: u64 = 2;
pub const ENABLE_AUTO_DEFENSE: bool = true;
pub const MAX_CONCURRENT_WORKERS: usize = 20;
pub const ORDERBOOK_TIMEOUT_SECS: u64 = 3;

// 买卖深度偏斜检测
pub const IMBALANCE_THRESHOLD: f64 = 0.30;
pub const IMBALANCE_DEPTH_LEVELS: usize = 5;
pub const IMBALANCE_MIN_TOTAL_DEPTH: f64 = 500.0;
pub const ENABLE_IMBALANCE_DETECTION: bool = true;

// 并发
pub const PLACE_ORDER_WORKERS: usize = 8;
