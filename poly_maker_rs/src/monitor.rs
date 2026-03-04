//! 监控防御模块（对应 main.py monitor_defense_loop）

use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;

use chrono::Utc;
use polymarket_client_sdk::clob::types::request::{CancelMarketOrderRequest, OrderBookSummaryRequest, OrdersRequest};
use polymarket_client_sdk::clob::Client;
use polymarket_client_sdk::types::U256;
use rust_decimal::prelude::ToPrimitive;

use crate::config::{
    ENABLE_AUTO_DEFENSE, ENABLE_IMBALANCE_DETECTION, IMBALANCE_DEPTH_LEVELS,
    IMBALANCE_MIN_TOTAL_DEPTH, IMBALANCE_THRESHOLD, MIN_FRONT_DEPTH_ABSOLUTE,
    MIN_FRONT_DEPTH_ABSOLUTE_REF, MIN_FRONT_DEPTH_THRESHOLD, MIN_SAME_DEPTH_SAFE,
    MONITOR_CHECK_INTERVAL_SECS,
    THRESHOLD_FRONT_DEPTH_DROP, THRESHOLD_FRONT_HIGH_WATER_DROP,
    THRESHOLD_SAME_DEPTH_DROP, THRESHOLD_SAME_HIGH_WATER_DROP,
};
use crate::orderbook::{is_extreme_price_market, OrderBookInfo};
use crate::place_order;

type AuthClient = Client<polymarket_client_sdk::auth::state::Authenticated<polymarket_client_sdk::auth::Normal>>;

/// 市场状态（对应 Python MarketState）
#[derive(Debug, Clone, Default)]
pub struct MarketState {
    pub question: String,
    pub token_type: String,
    pub my_bid_price: Option<f64>,
    pub my_ask_price: Option<f64>,
    pub my_order_size: f64,
    pub last_bid_front_depth: f64,
    pub last_bid_same_depth: f64,
    pub last_ask_front_depth: f64,
    pub last_ask_same_depth: f64,
    pub bid_front_high_water: f64,
    pub bid_same_high_water: f64,
    pub ask_front_high_water: f64,
    pub ask_same_high_water: f64,
    pub first_run: bool,
}

impl MarketState {
    pub fn new(question: &str, token_type: &str) -> Self {
        Self {
            question: question.to_string(),
            token_type: token_type.to_string(),
            first_run: true,
            ..Default::default()
        }
    }

    pub fn reset_high_water(&mut self) {
        self.bid_front_high_water = 0.0;
        self.bid_same_high_water = 0.0;
        self.ask_front_high_water = 0.0;
        self.ask_same_high_water = 0.0;
    }
}

/// 订单信息（best_bid, best_ask, bid_ids, ask_ids）
#[derive(Debug, Clone, Default)]
pub struct OrderInfo {
    pub best_bid: Option<f64>,
    pub best_ask: Option<f64>,
    pub bid_ids: Vec<String>,
    pub ask_ids: Vec<String>,
}

/// 批量获取我的挂单
pub async fn get_all_my_orders_once(client: &AuthClient) -> HashMap<String, OrderInfo> {
    let mut result = HashMap::new();
    let orders = match client.orders(&OrdersRequest::default(), None).await {
        Ok(o) => o,
        Err(e) => {
            eprintln!("⚠️ 批量获取订单失败: {}", e);
            return result;
        }
    };

    for o in &orders.data {
        let status = format!("{:?}", o.status);
        if !status.to_uppercase().contains("LIVE") {
            continue;
        }
        let token_id = o.asset_id.to_string();
        let price = o.price.to_f64().unwrap_or(0.0);
        let order_id = o.id.clone();
        let side_str = format!("{:?}", o.side);

        let entry = result.entry(token_id).or_insert_with(OrderInfo::default);

        if side_str.contains("Buy") {
            entry.best_bid = Some(entry.best_bid.map_or(price, |p| p.max(price)));
            entry.bid_ids.push(order_id);
        } else if side_str.contains("Sell") {
            entry.best_ask = Some(entry.best_ask.map_or(price, |p| p.min(price)));
            entry.ask_ids.push(order_id);
        }
    }
    result
}

/// 批量获取多个 token 的订单簿（单次 HTTP POST，比 Python 并发更快）
pub async fn get_all_order_books_concurrent(
    client: &AuthClient,
    token_ids: &[String],
) -> HashMap<String, polymarket_client_sdk::clob::types::response::OrderBookSummaryResponse> {
    let mut results = HashMap::new();
    if token_ids.is_empty() {
        return results;
    }
    let valid: Vec<(String, U256)> = token_ids
        .iter()
        .filter_map(|tid| U256::from_str(tid).ok().map(|u| (tid.clone(), u)))
        .collect();
    if valid.is_empty() {
        return results;
    }
    let requests: Vec<OrderBookSummaryRequest> = valid
        .iter()
        .map(|(_, u)| OrderBookSummaryRequest::builder().token_id(*u).build())
        .collect();
    match client.order_books(&requests).await {
        Ok(books) => {
            for ((tid, _), book) in valid.into_iter().zip(books.into_iter()) {
                results.insert(tid, book);
            }
        }
        Err(e) => {
            eprintln!("⚠️ 批量获取订单簿失败: {}，回退为逐条请求", e);
            for tid in token_ids {
                if let Ok(book_info) = place_order::fetch_orderbook(client, tid).await {
                    results.insert(tid.clone(), book_info.book);
                }
            }
        }
    }
    results
}

/// 计算分层深度（bid_front, bid_same, ask_front, ask_same）
pub fn calculate_layered_depth(
    book: &polymarket_client_sdk::clob::types::response::OrderBookSummaryResponse,
    my_bid_price: Option<f64>,
    my_ask_price: Option<f64>,
) -> (f64, f64, f64, f64) {
    let mut bid_front = 0.0;
    let mut bid_same = 0.0;
    let mut ask_front = 0.0;
    let mut ask_same = 0.0;

    if book.bids.is_empty() || book.asks.is_empty() {
        return (0.0, 0.0, 0.0, 0.0);
    }

    if let Some(my_bid) = my_bid_price {
        for bid in &book.bids {
            let price = bid.price.to_f64().unwrap_or(0.0);
            let size = bid.size.to_f64().unwrap_or(0.0);
            let depth = price * size;
            if price > my_bid + 0.001 {
                bid_front += depth;
            } else if (price - my_bid).abs() < 0.001 {
                bid_same += depth;
            }
        }
    }

    if let Some(my_ask) = my_ask_price {
        for ask in &book.asks {
            let price = ask.price.to_f64().unwrap_or(0.0);
            let size = ask.size.to_f64().unwrap_or(0.0);
            let depth = price * size;
            if price < my_ask - 0.001 {
                ask_front += depth;
            } else if (price - my_ask).abs() < 0.001 {
                ask_same += depth;
            }
        }
    }

    (bid_front, bid_same, ask_front, ask_same)
}

/// 精准撤单（撤指定 token 所有挂单）
pub async fn cancel_specific_token_monitor(
    client: &AuthClient,
    token_id: &str,
    question: &str,
    token_type: &str,
) -> bool {
    println!("\n🧨 正在对 [{}] 执行精准撤单...", &question[..question.len().min(30)]);
    let tid: U256 = U256::from_str(token_id).unwrap_or(U256::ZERO);
    let req = CancelMarketOrderRequest::builder().asset_id(tid).build();
    match client.cancel_market_orders(&req).await {
        Ok(_) => {
            println!("✅ 已成功撤销 {} ({:.10}...) 的所有挂单。", token_type, token_id);
            true
        }
        Err(e) => {
            eprintln!("⚠️ 撤单失败: {}", e);
            false
        }
    }
}

/// 单边撤单（只撤 BUY 或 SELL）
pub async fn cancel_one_side(
    client: &AuthClient,
    token_id: &str,
    side: &str,
    question: &str,
    cached_order_ids: Option<&[String]>,
) -> bool {
    let side_cn = if side == "BUY" { "买单" } else { "卖单" };

    let to_cancel: Vec<String> = if let Some(ids) = cached_order_ids {
        ids.to_vec()
    } else {
        let orders = match client.orders(&OrdersRequest::default(), None).await {
            Ok(o) => o,
            Err(_) => return false,
        };
        let mut id_refs: Vec<String> = Vec::new();
        for o in &orders.data {
            let status = format!("{:?}", o.status);
            if !status.to_uppercase().contains("LIVE") {
                continue;
            }
            let tid = o.asset_id.to_string();
            if tid != token_id {
                continue;
            }
            let side_str = format!("{:?}", o.side);
            let match_side = (side == "BUY" && side_str.contains("Buy")) || (side == "SELL" && side_str.contains("Sell"));
            if match_side {
                id_refs.push(o.id.clone());
            }
        }
        id_refs
    };

    if to_cancel.is_empty() {
        println!("   ℹ️ {}... 无活跃{}可撤", &question[..question.len().min(30)], side_cn);
        return false;
    }

    let refs: Vec<&str> = to_cancel.iter().map(|s| s.as_str()).collect();
    match client.cancel_orders(&refs).await {
        Ok(_) => {
            println!("   ✅ 已撤销 {}... 的{}（{}笔）", &question[..question.len().min(30)], side_cn, refs.len());
            true
        }
        Err(e) => {
            eprintln!("   ⚠️ 单边撤单失败 ({}): {}", side_cn, e);
            false
        }
    }
}

/// 买单威胁检测
pub fn check_bid_threats(
    state: &MarketState,
    my_bid_price: Option<f64>,
    bid_front: f64,
    bid_same: f64,
) -> (bool, Vec<String>) {
    let mut reasons = Vec::new();
    let mut triggered = false;

    let _my_bid = match my_bid_price {
        Some(p) => p,
        None => return (false, reasons),
    };

    let was_behind_wall = state.last_bid_front_depth > MIN_FRONT_DEPTH_THRESHOLD;
    let now_exposed = bid_front <= MIN_FRONT_DEPTH_THRESHOLD;
    if was_behind_wall && now_exposed {
        let drop_pct = if state.last_bid_front_depth > 0.0 {
            (1.0 - bid_front / state.last_bid_front_depth) * 100.0
        } else {
            100.0
        };
        reasons.push(format!(
            "🚨 [跨分支] 买单前墙消失！前墙: ${:.0}→${:.0} (-{:.0}%)",
            state.last_bid_front_depth, bid_front, drop_pct
        ));
        triggered = true;
    }

    if bid_front < MIN_FRONT_DEPTH_ABSOLUTE && state.bid_front_high_water > MIN_FRONT_DEPTH_ABSOLUTE_REF {
        reasons.push(format!(
            "🚨 [绝对兜底] 买单前墙极度危险！当前: ${:.0} (历史最高: ${:.0})",
            bid_front, state.bid_front_high_water
        ));
        triggered = true;
    }

    if state.bid_front_high_water > MIN_FRONT_DEPTH_THRESHOLD
        && bid_front < state.bid_front_high_water * (1.0 - THRESHOLD_FRONT_HIGH_WATER_DROP)
    {
        reasons.push(format!(
            "🚨 [高水位] 买单前墙累计大幅下跌！高水位: ${:.0}→当前: ${:.0}",
            state.bid_front_high_water, bid_front
        ));
        triggered = true;
    }

    if bid_front > MIN_FRONT_DEPTH_THRESHOLD {
        if state.last_bid_front_depth > MIN_FRONT_DEPTH_THRESHOLD
            && bid_front < state.last_bid_front_depth * (1.0 - THRESHOLD_FRONT_DEPTH_DROP)
        {
            reasons.push(format!(
                "🚨 [单轮] 买单前墙塌陷！${:.0}→${:.0}",
                state.last_bid_front_depth, bid_front
            ));
            triggered = true;
        }
    } else {
        if bid_same < MIN_SAME_DEPTH_SAFE {
            reasons.push(format!("🚨 [第一档] 买单深度太薄！同档: ${:.0}", bid_same));
            triggered = true;
        } else if state.last_bid_same_depth > MIN_SAME_DEPTH_SAFE
            && bid_same < state.last_bid_same_depth * (1.0 - THRESHOLD_SAME_DEPTH_DROP)
        {
            reasons.push(format!(
                "🚨 [第一档] 买单被大量吃掉！${:.0}→${:.0}",
                state.last_bid_same_depth, bid_same
            ));
            triggered = true;
        }
        if state.bid_same_high_water > MIN_SAME_DEPTH_SAFE
            && bid_same < state.bid_same_high_water * (1.0 - THRESHOLD_SAME_HIGH_WATER_DROP)
        {
            reasons.push(format!(
                "🚨 [高水位] 第一档买单累计被吃！高水位: ${:.0}→当前: ${:.0}",
                state.bid_same_high_water, bid_same
            ));
            triggered = true;
        }
    }
    (triggered, reasons)
}

/// 卖单威胁检测
pub fn check_ask_threats(
    state: &MarketState,
    my_ask_price: Option<f64>,
    ask_front: f64,
    ask_same: f64,
) -> (bool, Vec<String>) {
    let mut reasons = Vec::new();
    let mut triggered = false;

    let _my_ask = match my_ask_price {
        Some(p) => p,
        None => return (false, reasons),
    };

    let was_behind_wall = state.last_ask_front_depth > MIN_FRONT_DEPTH_THRESHOLD;
    let now_exposed = ask_front <= MIN_FRONT_DEPTH_THRESHOLD;
    if was_behind_wall && now_exposed {
        let drop_pct = if state.last_ask_front_depth > 0.0 {
            (1.0 - ask_front / state.last_ask_front_depth) * 100.0
        } else {
            100.0
        };
        reasons.push(format!(
            "🚨 [跨分支] 卖单前墙消失！前墙: ${:.0}→${:.0} (-{:.0}%)",
            state.last_ask_front_depth, ask_front, drop_pct
        ));
        triggered = true;
    }

    if ask_front < MIN_FRONT_DEPTH_ABSOLUTE && state.ask_front_high_water > MIN_FRONT_DEPTH_ABSOLUTE_REF {
        reasons.push(format!(
            "🚨 [绝对兜底] 卖单前墙极度危险！当前: ${:.0} (历史最高: ${:.0})",
            ask_front, state.ask_front_high_water
        ));
        triggered = true;
    }

    if state.ask_front_high_water > MIN_FRONT_DEPTH_THRESHOLD
        && ask_front < state.ask_front_high_water * (1.0 - THRESHOLD_FRONT_HIGH_WATER_DROP)
    {
        reasons.push(format!(
            "🚨 [高水位] 卖单前墙累计大幅下跌！高水位: ${:.0}→当前: ${:.0}",
            state.ask_front_high_water, ask_front
        ));
        triggered = true;
    }

    if ask_front > MIN_FRONT_DEPTH_THRESHOLD {
        if state.last_ask_front_depth > MIN_FRONT_DEPTH_THRESHOLD
            && ask_front < state.last_ask_front_depth * (1.0 - THRESHOLD_FRONT_DEPTH_DROP)
        {
            reasons.push(format!(
                "🚨 [单轮] 卖单前墙塌陷！${:.0}→${:.0}",
                state.last_ask_front_depth, ask_front
            ));
            triggered = true;
        }
    } else {
        if ask_same < MIN_SAME_DEPTH_SAFE {
            reasons.push(format!("🚨 [第一档] 卖单深度太薄！同档: ${:.0}", ask_same));
            triggered = true;
        } else if state.last_ask_same_depth > MIN_SAME_DEPTH_SAFE
            && ask_same < state.last_ask_same_depth * (1.0 - THRESHOLD_SAME_DEPTH_DROP)
        {
            reasons.push(format!(
                "🚨 [第一档] 卖单被大量吃掉！${:.0}→${:.0}",
                state.last_ask_same_depth, ask_same
            ));
            triggered = true;
        }
        if state.ask_same_high_water > MIN_SAME_DEPTH_SAFE
            && ask_same < state.ask_same_high_water * (1.0 - THRESHOLD_SAME_HIGH_WATER_DROP)
        {
            reasons.push(format!(
                "🚨 [高水位] 第一档卖单累计被吃！高水位: ${:.0}→当前: ${:.0}",
                state.ask_same_high_water, ask_same
            ));
            triggered = true;
        }
    }
    (triggered, reasons)
}

/// 买卖深度偏斜检测
pub fn check_book_imbalance(
    book: &polymarket_client_sdk::clob::types::response::OrderBookSummaryResponse,
    my_bid_price: Option<f64>,
    my_ask_price: Option<f64>,
) -> (bool, bool, Option<String>) {
    if book.bids.is_empty() || book.asks.is_empty() {
        return (false, false, None);
    }

    let mut bids: Vec<(f64, f64)> = book
        .bids
        .iter()
        .filter_map(|b| Some((b.price.to_f64()?, b.size.to_f64()?)))
        .collect();
    bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));

    let mut asks: Vec<(f64, f64)> = book
        .asks
        .iter()
        .filter_map(|a| Some((a.price.to_f64()?, a.size.to_f64()?)))
        .collect();
    asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));

    let bid_depth: f64 = bids.iter().take(IMBALANCE_DEPTH_LEVELS).map(|(p, s)| p * s).sum();
    let ask_depth: f64 = asks.iter().take(IMBALANCE_DEPTH_LEVELS).map(|(p, s)| p * s).sum();
    let total = bid_depth + ask_depth;

    if total < IMBALANCE_MIN_TOTAL_DEPTH {
        return (false, false, None);
    }

    let bid_ratio = bid_depth / total;
    let ask_ratio = ask_depth / total;

    let mut cancel_bid = false;
    let mut cancel_ask = false;
    let mut reasons = Vec::new();

    if bid_ratio < IMBALANCE_THRESHOLD && my_bid_price.is_some() {
        cancel_bid = true;
        reasons.push(format!(
            "🚨 [偏斜] 买方深度严重不足！买/卖={:.0}%/{:.0}% (${:.0}/${:.0})，价格可能下跌 → 撤买单",
            bid_ratio * 100.0, ask_ratio * 100.0, bid_depth, ask_depth
        ));
    }
    if ask_ratio < IMBALANCE_THRESHOLD && my_ask_price.is_some() {
        cancel_ask = true;
        reasons.push(format!(
            "🚨 [偏斜] 卖方深度严重不足！买/卖={:.0}%/{:.0}% (${:.0}/${:.0})，价格可能上涨 → 撤卖单",
            bid_ratio * 100.0, ask_ratio * 100.0, bid_depth, ask_depth
        ));
    }

    let reason_str = if reasons.is_empty() {
        None
    } else {
        Some(reasons.join("\n"))
    };
    (cancel_bid, cancel_ask, reason_str)
}

/// 监控防御主循环
pub async fn monitor_defense_loop(
    client: &AuthClient,
    _signer: &impl alloy::signers::Signer,
    strategy_tokens: Arc<std::sync::RwLock<Vec<crate::types::TokenInfo>>>,
) {
    println!("\n============================================================");
    println!("🛡️  [监控防御] 启动中...");
    println!("    ⚙️  自动防御: {}", ENABLE_AUTO_DEFENSE);
    println!("    ⚖️  偏斜检测: {} (阈值: {:.0}%)", ENABLE_IMBALANCE_DETECTION, IMBALANCE_THRESHOLD * 100.0);
    println!("    ⏱️  扫描间隔: {}秒", MONITOR_CHECK_INTERVAL_SECS);
    println!("============================================================\n");

    let mut market_states: HashMap<String, MarketState> = HashMap::new();
    let mut scan_count: u64 = 0;

    loop {
        let loop_start = std::time::Instant::now();

        let mut current_tokens = strategy_tokens.read().unwrap().clone();

        for t in &current_tokens {
            market_states
                .entry(t.token_id.clone())
                .or_insert_with(|| MarketState::new(&t.question, &t.token_type));
        }

        let all_orders = get_all_my_orders_once(client).await;

        for (token_id, _order_info) in &all_orders {
            if !current_tokens.iter().any(|t| t.token_id == *token_id) {
                let question = format!("手动挂单 ({:.10}...)", token_id);
                let token_type = "MANUAL".to_string();
                current_tokens.push(crate::types::TokenInfo {
                    token_id: token_id.clone(),
                    token_type: token_type.clone(),
                    question: question.clone(),
                    min_size: 10.0,
                    neg_risk: false,
                    max_spread: None,
                    volatility_sum: 0.0,
                    source: "manual_detected".to_string(),
                    blacklisted: false,
                    order_size: None,
                });
                market_states
                    .entry(token_id.clone())
                    .or_insert_with(|| MarketState::new(&question, &token_type));
            }
        }

        let active_targets: Vec<_> = current_tokens
            .iter()
            .filter(|t| {
                let info = all_orders.get(&t.token_id);
                match info {
                    Some(o) => o.best_bid.is_some() || o.best_ask.is_some(),
                    None => false,
                }
            })
            .cloned()
            .collect();

        if active_targets.is_empty() {
            let elapsed = loop_start.elapsed().as_secs_f64();
            println!(
                "\r[ {} ] 🛡️ 扫描 #{} | 无活跃挂单 | 监控: {} | 耗时: {:.2}s",
                Utc::now().format("%H:%M:%S"),
                scan_count,
                current_tokens.len(),
                elapsed
            );
            tokio::time::sleep(tokio::time::Duration::from_secs(MONITOR_CHECK_INTERVAL_SECS)).await;
            scan_count += 1;
            continue;
        }

        let active_ids: Vec<String> = active_targets.iter().map(|t| t.token_id.clone()).collect();
        let all_books = get_all_order_books_concurrent(client, &active_ids).await;

        for t in &active_targets {
            let token_id = &t.token_id;
            let state = market_states.get_mut(token_id).unwrap();
            let order_info = all_orders.get(token_id).unwrap();
            let my_bid_price = order_info.best_bid;
            let my_ask_price = order_info.best_ask;

            let book = match all_books.get(token_id) {
                Some(b) => b,
                None => continue,
            };

            let book_info = OrderBookInfo::from_response(book.clone());

            // 极端价格孤单检测
            if let Some(best_bid_price) = book_info.best_bid {
                if is_extreme_price_market(Some(best_bid_price)) {
                    let has_bid = my_bid_price.is_some();
                    let has_ask = my_ask_price.is_some();
                    if has_bid != has_ask {
                        let lone_side = if has_bid { "买单" } else { "卖单" };
                        println!("\n⚠️ [孤单检测] [{}] {}...", t.token_type, &t.question[..t.question.len().min(40)]);
                        println!("   极端价格市场(best_bid={:.3})，仅有{}，双向缺一无奖励", best_bid_price, lone_side);
                        println!("   🧨 撤销孤立{}...", lone_side);
                        cancel_specific_token_monitor(client, token_id, &t.question, &t.token_type).await;
                        state.first_run = true;
                        state.reset_high_water();
                        continue;
                    }
                }
            }

            state.my_bid_price = my_bid_price;
            state.my_ask_price = my_ask_price;

            // 偏斜检测
            if ENABLE_IMBALANCE_DETECTION && !state.first_run {
                let (cancel_bid, cancel_ask, imbalance_reason) = check_book_imbalance(book, my_bid_price, my_ask_price);
                if cancel_bid || cancel_ask {
                    if let Some(reason) = imbalance_reason {
                        println!("\n\n{}\n⏰ 时间: {}", "⚖".repeat(10), Utc::now().format("%Y-%m-%d %H:%M:%S"));
                        println!("🎯 目标: [{}] {}...", t.token_type, &t.question[..t.question.len().min(45)]);
                        println!("   {}", reason);
                        if ENABLE_AUTO_DEFENSE {
                            if cancel_bid && !order_info.bid_ids.is_empty() {
                                cancel_one_side(client, token_id, "BUY", &t.question, Some(&order_info.bid_ids)).await;
                            }
                            if cancel_ask && !order_info.ask_ids.is_empty() {
                                cancel_one_side(client, token_id, "SELL", &t.question, Some(&order_info.ask_ids)).await;
                            }
                            state.first_run = true;
                            state.reset_high_water();
                        } else {
                            println!("   ⚠️ 防御未开启，仅报警");
                        }
                        println!("{}", "⚖".repeat(30));
                    }
                    continue;
                }
            }

            let order_size = t.order_size.unwrap_or(t.min_size.max(500.0));
            state.my_order_size = order_size;

            let (bid_front, bid_same, ask_front, ask_same) = calculate_layered_depth(book, my_bid_price, my_ask_price);

            let mut bid_same_adj = bid_same;
            let mut ask_same_adj = ask_same;
            if let Some(my_bid) = my_bid_price {
                if my_bid > 0.0 {
                    bid_same_adj = (bid_same - order_size * my_bid).max(0.0);
                }
            }
            if let Some(my_ask) = my_ask_price {
                if my_ask > 0.0 {
                    ask_same_adj = (ask_same - order_size * my_ask).max(0.0);
                }
            }

            state.bid_front_high_water = state.bid_front_high_water.max(bid_front);
            state.bid_same_high_water = state.bid_same_high_water.max(bid_same_adj);
            state.ask_front_high_water = state.ask_front_high_water.max(ask_front);
            state.ask_same_high_water = state.ask_same_high_water.max(ask_same_adj);

            let mut trigger_reasons = Vec::new();
            let mut triggered = false;

            if !state.first_run {
                let (bid_trig, bid_reasons) = check_bid_threats(state, my_bid_price, bid_front, bid_same_adj);
                let (ask_trig, ask_reasons) = check_ask_threats(state, my_ask_price, ask_front, ask_same_adj);
                if bid_trig {
                    triggered = true;
                    trigger_reasons.extend(bid_reasons);
                }
                if ask_trig {
                    triggered = true;
                    trigger_reasons.extend(ask_reasons);
                }
            }

            // 与 Python 一致：last_* 存原始深度，威胁检测用 *_adj（排除自己）
            state.last_bid_front_depth = bid_front;
            state.last_bid_same_depth = bid_same;
            state.last_ask_front_depth = ask_front;
            state.last_ask_same_depth = ask_same;
            state.first_run = false;

            if triggered {
                println!("\n\n{} ⚡ 检测到危险信号 ⚡ {}", "!".repeat(20), "!".repeat(20));
                println!("⏰ 时间: {}", Utc::now().format("%Y-%m-%d %H:%M:%S"));
                println!("🎯 目标: {}", &state.question[..state.question.len().min(50)]);
                println!("🆔 Token: {} ({:.10}...)", state.token_type, token_id);
                for (i, r) in trigger_reasons.iter().enumerate() {
                    println!("  [{}] {}", i + 1, r);
                }
                if ENABLE_AUTO_DEFENSE {
                    cancel_specific_token_monitor(client, token_id, &state.question, &state.token_type).await;
                    state.first_run = true;
                    state.reset_high_water();

                    // 🔄 非阻塞：将被撤单的 token 加入待重试列表，由 periodic_retry_task 在 60s 后重挂
                    // 这样主监控循环不会被阻塞，其他市场继续受到保护
                    let token_info_replace = current_tokens.iter().find(|x| x.token_id == *token_id).cloned();
                    if let Some(ti) = token_info_replace {
                        println!("   ⏳ 已加入待重试队列，将由 periodic_retry_task 在下一轮重挂: {}...", &state.question[..state.question.len().min(40)]);
                        crate::state::PENDING_RETRY_TOKENS.write().unwrap().push(ti);
                    }
                } else {
                    println!("⚠️ 防御未开启，仅报警");
                }
                println!("{}", "!".repeat(70));
            }
        }

        let elapsed = loop_start.elapsed().as_secs_f64();
        let sleep_secs = (MONITOR_CHECK_INTERVAL_SECS as f64 - elapsed).max(0.1);
        println!(
            "\r[ {} ] 🛡️ 扫描 #{} | 活跃: {}/{} | 耗时: {:.2}s",
            Utc::now().format("%H:%M:%S"),
            scan_count,
            active_targets.len(),
            current_tokens.len(),
            elapsed
        );
        tokio::time::sleep(tokio::time::Duration::from_secs_f64(sleep_secs)).await;
        scan_count += 1;
    }
}
