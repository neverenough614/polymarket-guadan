//! 订单簿分析（对应 main.py get_orderbook_info, analyze_best_place_price_from_book, calculate_dynamic_size, is_extreme_price_market）

use polymarket_client_sdk::clob::types::response::OrderBookSummaryResponse;
use rust_decimal::prelude::ToPrimitive;

use crate::config::{
    DEPTH_THRESHOLD_TIER1, DEPTH_THRESHOLD_TIER2, EXTREME_PRICE_THRESHOLD, MAX_LEVEL_GAP,
};
use crate::types::BestPlaceResult;

/// 订单簿信息（book, best_bid, best_ask, mid）
pub struct OrderBookInfo {
    pub book: OrderBookSummaryResponse,
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
    pub mid: Option<f64>,
}

impl OrderBookInfo {
    /// 从 API 响应构建；best_bid/best_ask 显式取极值，不依赖 API 返回顺序（与 Python sorted 一致）
    pub fn from_response(book: OrderBookSummaryResponse) -> Self {
        let best_bid = book
            .bids
            .iter()
            .filter_map(|b| b.price.to_f64())
            .max_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let best_ask = book
            .asks
            .iter()
            .filter_map(|a| a.price.to_f64())
            .min_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mid = match (best_bid, best_ask) {
            (Some(b), Some(a)) => Some((b + a) / 2.0),
            _ => None,
        };
        Self {
            book,
            best_bid,
            best_ask,
            mid,
        }
    }
}

/// 极端价格市场检测（best_bid <= 0.10 或 >= 0.90）
pub fn is_extreme_price_market(best_bid: Option<f64>) -> bool {
    match best_bid {
        None => false,
        Some(b) => {
            b <= EXTREME_PRICE_THRESHOLD || b >= (1.0 - EXTREME_PRICE_THRESHOLD)
        }
    }
}

/// 从订单簿分析最优挂单价格（对应 analyze_best_place_price_from_book）
/// skip_tier1: 黑名单市场强制跳过第一档，从第二档开始挂单
pub fn analyze_best_place_price_from_book(
    book: &OrderBookSummaryResponse,
    side: &str,
    max_spread: Option<f64>,
    mid: Option<f64>,
    order_size: Option<f64>,
    skip_tier1: bool,
) -> Option<BestPlaceResult> {
    let levels: Vec<(f64, f64)> = if side == "BUY" {
        let mut v: Vec<_> = book
            .bids
            .iter()
            .filter_map(|l| Some((l.price.to_f64()?, l.size.to_f64()?)))
            .collect();
        v.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        v
    } else {
        let mut v: Vec<_> = book
            .asks
            .iter()
            .filter_map(|l| Some((l.price.to_f64()?, l.size.to_f64()?)))
            .collect();
        v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
        v
    };

    if levels.is_empty() {
        return None;
    }

    // 前置检查：第1档深度 >= 100 USDC
    let (p0, s0) = levels[0];
    if p0 * s0 < 100.0 {
        return None;
    }

    // 档位连续性检查
    let top3: Vec<f64> = levels.iter().take(3).map(|(p, _)| *p).collect();
    for j in 0..top3.len().saturating_sub(1) {
        let gap = (top3[j] - top3[j + 1]).abs();
        if gap > MAX_LEVEL_GAP + 1e-9 {
            return None;
        }
    }

    for (i, (price, size)) in levels.iter().take(3).enumerate() {
        // 🚫 黑名单市场强制跳过第一档
        if i == 0 && skip_tier1 {
            continue;
        }

        let depth = price * size;
        let threshold = if i == 0 {
            DEPTH_THRESHOLD_TIER1
        } else {
            DEPTH_THRESHOLD_TIER2
        };

        if depth < threshold {
            continue;
        }

        // ── 第一档额外安全检查 ──────────────────────────────────
        if i == 0 {
            // 孤立厚墙检测：第1档/第2档深度比 > 5，说明是大户撑场，跳过
            if levels.len() >= 2 {
                let tier2_depth = levels[1].0 * levels[1].1;
                if tier2_depth > 0.0 && depth / tier2_depth > 5.0 {
                    continue; // 第1档深度异常集中于单一档位，跳过
                }
            }

            // 占比检查：挂单价值不超过该档深度的 20%（原1/3，更保守）
            if let Some(os) = order_size {
                let my_order_value = os * price;
                if my_order_value > depth * (1.0 / 5.0) {
                    continue; // 占比超过 20%，跳过第一档，尝试第二档
                }
            }
        }

        // max_spread 范围检测
        if let (Some(ms), Some(m)) = (max_spread, mid) {
            let lower = m - ms;
            let upper = m + ms;
            if !(lower..=upper).contains(&price) {
                continue;
            }
        }

        return Some(BestPlaceResult {
            price: *price,
            tier: (i + 1) as u8,
            depth,
        });
    }

    None
}

/// 动态计算挂单量（对应 calculate_dynamic_size）
pub fn calculate_dynamic_size(
    book: &OrderBookSummaryResponse,
    mid: Option<f64>,
    min_size: f64,
    volatility_sum: f64,
    size_ratio: f64,
    max_order_size: f64,
) -> Option<f64> {
    let mid = mid?;
    if mid <= 0.0 {
        return None;
    }

    let mut bids: Vec<(f64, f64)> = book
        .bids
        .iter()
        .filter_map(|l| Some((l.price.to_f64()?, l.size.to_f64()?)))
        .collect();
    bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    let mut asks: Vec<(f64, f64)> = book
        .asks
        .iter()
        .filter_map(|l| Some((l.price.to_f64()?, l.size.to_f64()?)))
        .collect();
    asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));

    let top3_bid_depth: f64 = bids.iter().take(3).map(|(p, s)| p * s).sum();
    let top3_ask_depth: f64 = asks.iter().take(3).map(|(p, s)| p * s).sum();

    if top3_bid_depth <= 0.0 && top3_ask_depth <= 0.0 {
        return None;
    }

    let bid_target = if top3_bid_depth > 0.0 {
        top3_bid_depth * size_ratio / mid
    } else {
        0.0
    };
    let ask_target = if top3_ask_depth > 0.0 {
        top3_ask_depth * size_ratio / mid
    } else {
        0.0
    };

    let target_size = if bid_target > 0.0 && ask_target > 0.0 {
        bid_target.min(ask_target)
    } else {
        bid_target.max(ask_target)
    };

    // 波动率折扣因子
    let vol_factor = if volatility_sum <= 10.0 {
        1.0
    } else {
        (1.0 - (volatility_sum - 10.0) / 60.0).max(0.2)
    };
    let target_size = target_size * vol_factor;

    if target_size < min_size {
        return None;
    }

    let final_size = target_size.min(max_order_size).round();
    Some(final_size)
}

/// 诊断用：返回订单簿深度摘要 (top3_bid, top3_ask, tier1_bid_depth, tier1_ask_depth)
pub fn get_depth_summary(book: &OrderBookSummaryResponse) -> (f64, f64, f64, f64) {
    let mut bids: Vec<(f64, f64)> = book
        .bids
        .iter()
        .filter_map(|l| Some((l.price.to_f64()?, l.size.to_f64()?)))
        .collect();
    bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut asks: Vec<(f64, f64)> = book
        .asks
        .iter()
        .filter_map(|l| Some((l.price.to_f64()?, l.size.to_f64()?)))
        .collect();
    asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let top3_bid: f64 = bids.iter().take(3).map(|(p, s)| p * s).sum();
    let top3_ask: f64 = asks.iter().take(3).map(|(p, s)| p * s).sum();
    let t1_bid = bids.first().map(|(p, s)| p * s).unwrap_or(0.0);
    let t1_ask = asks.first().map(|(p, s)| p * s).unwrap_or(0.0);
    (top3_bid, top3_ask, t1_bid, t1_ask)
}
