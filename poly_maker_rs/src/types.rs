//! 类型定义（对应 main.py 中的数据结构）

use serde::{Deserialize, Serialize};

/// 策略 token 信息（对应 Python token_info）
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TokenInfo {
    pub token_id: String,
    pub token_type: String,  // "YES" | "NO" | "MANUAL"
    pub question: String,
    #[serde(default = "default_min_size")]
    pub min_size: f64,
    #[serde(default)]
    pub neg_risk: bool,
    #[serde(default)]
    pub max_spread: Option<f64>,
    #[serde(default)]
    pub volatility_sum: f64,
    #[serde(default = "default_source")]
    pub source: String,
    #[serde(default)]
    pub blacklisted: bool,
    #[serde(skip)]
    pub order_size: Option<f64>,
}

fn default_min_size() -> f64 {
    100.0
}

fn default_source() -> String {
    "Normal LP".to_string()
}

/// 挂单结果（对应 Python place_order_for_token 返回值）
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PlaceOrderResult {
    pub token_id: String,
    pub token_type: String,
    pub question: String,
    pub min_size: f64,
    pub buy_status: String,
    pub sell_status: String,
    pub buy_price: Option<f64>,
    pub sell_price: Option<f64>,
    pub buy_tier: Option<u8>,
    pub sell_tier: Option<u8>,
    pub extreme_price: bool,
    pub error: Option<String>,
    pub mid: Option<f64>,
    pub max_spread: Option<f64>,
    pub order_size: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
}

/// 最优挂单档位分析结果 (price, tier, depth)
#[derive(Debug, Clone)]
pub struct BestPlaceResult {
    pub price: f64,
    pub tier: u8,
    pub depth: f64,
}
