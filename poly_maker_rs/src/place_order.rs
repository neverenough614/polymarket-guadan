//! 挂单逻辑（对应 main.py place_order_for_token, run_auto_place_orders）

use std::str::FromStr;

use chrono::Utc;
use futures::future::join_all;
use polymarket_client_sdk::clob::types::{OrderType, Side};
use polymarket_client_sdk::clob::Client;
use polymarket_client_sdk::types::U256;
use rust_decimal::Decimal;

use crate::config::{
    AGGRESSIVE_MAX_ORDER_SIZE, AGGRESSIVE_SIZE_RATIO, DYNAMIC_SIZE_RATIO,
    MAX_ORDER_SIZE, NORMAL_MAX_ORDER_SIZE, NORMAL_SIZE_RATIO, PLACE_ORDER_WORKERS, RETRY_INTERVAL_SECS,
};
use crate::orderbook::{
    analyze_best_place_price_from_book, calculate_dynamic_size, get_depth_summary,
    is_extreme_price_market, OrderBookInfo,
};
use crate::state::{PLACED_ORDERS_LOG, PENDING_RETRY_TOKENS};
use crate::types::{PlaceOrderResult, TokenInfo};

type AuthClient = Client<polymarket_client_sdk::auth::state::Authenticated<polymarket_client_sdk::auth::Normal>>;

/// 获取订单簿并转为 OrderBookInfo
pub async fn fetch_orderbook(
    client: &AuthClient,
    token_id: &str,
) -> anyhow::Result<OrderBookInfo> {
    use polymarket_client_sdk::clob::types::request::OrderBookSummaryRequest;
    let tid: U256 = U256::from_str(token_id)?;
    let req = OrderBookSummaryRequest::builder().token_id(tid).build();
    let book = client.order_book(&req).await?;
    Ok(OrderBookInfo::from_response(book))
}

/// 对单个 token 执行挂单（对应 place_order_for_token）
pub async fn place_order_for_token(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    token_info: &TokenInfo,
    book_info: &OrderBookInfo,
) -> PlaceOrderResult {
    let token_id = &token_info.token_id;
    let token_type = &token_info.token_type;
    let question = &token_info.question;
    let neg_risk = token_info.neg_risk;
    let max_spread = token_info.max_spread;

    let base_min_size = token_info.min_size.max(100.0);

    let mut result = PlaceOrderResult {
        token_id: token_id.clone(),
        token_type: token_type.clone(),
        question: question.clone(),
        min_size: base_min_size,
        buy_status: "skipped".to_string(),
        sell_status: "skipped".to_string(),
        buy_price: None,
        sell_price: None,
        buy_tier: None,
        sell_tier: None,
        extreme_price: false,
        error: None,
        mid: book_info.mid,
        max_spread,
        order_size: None,
        timestamp: None,
    };

    let (size_ratio, max_order_size) = match token_info.source.as_str() {
        "High Reward" => (AGGRESSIVE_SIZE_RATIO, AGGRESSIVE_MAX_ORDER_SIZE),
        "Normal LP" => (NORMAL_SIZE_RATIO, NORMAL_MAX_ORDER_SIZE),
        "Smart High Yield" => (
            (NORMAL_SIZE_RATIO + AGGRESSIVE_SIZE_RATIO) / 2.0,
            (NORMAL_MAX_ORDER_SIZE + AGGRESSIVE_MAX_ORDER_SIZE) / 2.0,
        ),
        _ => (DYNAMIC_SIZE_RATIO, MAX_ORDER_SIZE),
    };

    let order_size = calculate_dynamic_size(
        &book_info.book,
        book_info.mid,
        base_min_size,
        token_info.volatility_sum,
        size_ratio,
        max_order_size,
    );

    result.order_size = order_size;

    if order_size.is_none() {
        result.buy_status = "depth_insufficient".to_string();
        result.sell_status = "depth_insufficient".to_string();
        let (top3_bid, top3_ask, t1_bid, t1_ask) = get_depth_summary(&book_info.book);
        result.error = Some(format!(
            "前三档深度不足(min={}): bid_t1=${:.0} bid_top3=${:.0} ask_t1=${:.0} ask_top3=${:.0} mid={:.3}",
            base_min_size as u64,
            t1_bid,
            top3_bid,
            t1_ask,
            top3_ask,
            book_info.mid.unwrap_or(0.0)
        ));
        return result;
    }

    let order_size = order_size.unwrap();
    let extreme = is_extreme_price_market(book_info.best_bid);
    result.extreme_price = extreme;

    // 🚫 黑名单市场跳过第一档，从第二档开始挂单
    let skip_tier1 = token_info.blacklisted;
    let buy_result = analyze_best_place_price_from_book(
        &book_info.book,
        "BUY",
        max_spread,
        book_info.mid,
        Some(order_size),
        skip_tier1,
    );
    let sell_result = analyze_best_place_price_from_book(
        &book_info.book,
        "SELL",
        max_spread,
        book_info.mid,
        Some(order_size),
        skip_tier1,
    );

    if extreme {
        if buy_result.is_none() || sell_result.is_none() {
            let mut missing = Vec::new();
            if buy_result.is_none() {
                missing.push("买单");
            }
            if sell_result.is_none() {
                missing.push("卖单");
            }
            result.buy_status = "extreme_skip".to_string();
            result.sell_status = "extreme_skip".to_string();
            result.error = Some(format!(
                "极端价格市场({:.2})，{}深度/范围不足，跳过双向挂单",
                book_info.best_bid.unwrap_or(0.0),
                missing.join("/")
            ));
            return result;
        }
    }

    // 执行买单
    if let Some(ref br) = buy_result {
        result.buy_price = Some(br.price);
        result.buy_tier = Some(br.tier);
        match place_single_order(client, signer, token_id, Side::Buy, br.price, order_size, neg_risk).await {
            Ok(ok) => {
                result.buy_status = if ok {
                    "placed".to_string()
                } else {
                    "failed".to_string()
                };
            }
            Err(e) => {
                result.buy_status = format!("error: {}", &e.to_string()[..e.to_string().len().min(50)]);
            }
        }
    } else {
        result.buy_status = "depth_insufficient".to_string();
        let (_, _, t1_bid, t1_ask) = get_depth_summary(&book_info.book);
        if result.error.is_none() {
            result.error = Some(format!(
                "analyze无合适档位: mid={:.3} bid_t1=${:.0} ask_t1=${:.0} max_spread={:?}",
                book_info.mid.unwrap_or(0.0),
                t1_bid,
                t1_ask,
                max_spread
            ));
        }
    }

    // 执行卖单
    if let Some(ref sr) = sell_result {
        result.sell_price = Some(sr.price);
        result.sell_tier = Some(sr.tier);
        match place_single_order(client, signer, token_id, Side::Sell, sr.price, order_size, neg_risk).await {
            Ok(ok) => {
                result.sell_status = if ok {
                    "placed".to_string()
                } else {
                    "failed".to_string()
                };
            }
            Err(e) => {
                result.sell_status = format!("error: {}", &e.to_string()[..e.to_string().len().min(50)]);
            }
        }
    } else {
        result.sell_status = "depth_insufficient".to_string();
        if result.error.is_none() {
            let (_, _, t1_bid, t1_ask) = get_depth_summary(&book_info.book);
            result.error = Some(format!(
                "analyze无合适档位: mid={:.3} bid_t1=${:.0} ask_t1=${:.0} max_spread={:?}",
                book_info.mid.unwrap_or(0.0),
                t1_bid,
                t1_ask,
                max_spread
            ));
        }
    }

    result
}

pub async fn place_single_order(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    token_id: &str,
    side: Side,
    price: f64,
    size: f64,
    neg_risk: bool,
) -> anyhow::Result<bool> {
    let tid: U256 = U256::from_str(token_id)?;
    let price_dec = Decimal::try_from(price).unwrap_or(Decimal::ZERO);
    let size_dec = Decimal::try_from(size).unwrap_or(Decimal::ZERO);

    // 预设 neg_risk 以便 fee_rate_bps 使用正确的 exchange
    client.set_neg_risk(tid, neg_risk);

    let builder = client
        .limit_order()
        .token_id(tid)
        .side(side)
        .price(price_dec)
        .size(size_dec)
        .order_type(OrderType::GTC);

    let order = builder.build().await?;
    let signed = client.sign(signer, order).await?;
    let resp = client.post_order(signed).await?;
    Ok(resp.success)
}

/// 批量自动挂单（对应 run_auto_place_orders，并发数=PLACE_ORDER_WORKERS）
pub async fn run_auto_place_orders(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    strategy_tokens: &[TokenInfo],
) -> (usize, usize) {

    println!("\n============================================================");
    println!(
        "🔍 [自动挂单] 并发分析 {} 个 token（{} 线程）...",
        strategy_tokens.len(),
        PLACE_ORDER_WORKERS
    );
    println!("============================================================");

    let total = strategy_tokens.len();
    let mut results: Vec<(usize, TokenInfo, PlaceOrderResult)> = Vec::with_capacity(total);
    let chunk_size = PLACE_ORDER_WORKERS;
    for chunk_start in (0..total).step_by(chunk_size) {
        let chunk: Vec<_> = strategy_tokens[chunk_start..]
            .iter()
            .take(chunk_size)
            .enumerate()
            .map(|(j, ti)| (chunk_start + j, ti.clone()))
            .collect();
        let futs: Vec<_> = chunk
            .into_iter()
            .map(|(i, token_info)| async move {
                let book_result = fetch_orderbook(client, &token_info.token_id).await;
                let result = match book_result {
                    Ok(book_info) => place_order_for_token(client, signer, &token_info, &book_info).await,
                    Err(e) => {
                        let mut r = PlaceOrderResult::default();
                        r.token_id = token_info.token_id.clone();
                        r.token_type = token_info.token_type.clone();
                        r.question = token_info.question.clone();
                        r.buy_status = "error".to_string();
                        r.sell_status = "error".to_string();
                        r.error = Some(e.to_string());
                        r
                    }
                };
                (i, token_info, result)
            })
            .collect();
        let chunk_results = join_all(futs).await;
        results.extend(chunk_results);
    }
    results.sort_by_key(|(i, _, _)| *i);

    let mut success_count = 0;
    let mut skip_count = 0;
    let mut depth_count = 0;
    let mut extreme_count = 0;
    let mut api_err_count = 0;
    let mut new_pending = Vec::new();

    for (i, token_info, mut result) in results {
        if result.buy_status == "depth_insufficient" && result.sell_status == "depth_insufficient" {
            depth_count += 1;
        }
        if result.error.as_ref().map(|e| e.contains("极端价格")).unwrap_or(false) {
            extreme_count += 1;
        }
        if result.buy_status == "error" || result.sell_status.starts_with("error") {
            api_err_count += 1;
        }
        result.timestamp = Some(Utc::now().format("%H:%M:%S").to_string());

        PLACED_ORDERS_LOG.write().unwrap().push(result.clone());

        let buy_ok = result.buy_status == "placed";
        let sell_ok = result.sell_status == "placed";
        let buy_skip = result.buy_status == "depth_insufficient" || result.buy_status == "extreme_skip";
        let sell_skip = result.sell_status == "depth_insufficient" || result.sell_status == "extreme_skip";

        let q: String = token_info.question.chars().take(35).collect();
        let label = format!("   [{}/{}] {}... [{}]", i + 1, strategy_tokens.len(), q, token_info.token_type);

        if buy_ok || sell_ok {
            success_count += 1;
            if let Some(os) = result.order_size {
                let mut ti = token_info.clone();
                ti.order_size = Some(os);
                // token_info 在策略列表中是引用，这里需要更新 - 通过 new_pending 不包含成功的不需要
            }
            let buy_info = if buy_ok {
                format!("买{}(${:.3})", result.buy_tier.unwrap_or(0), result.buy_price.unwrap_or(0.0))
            } else {
                "买单跳过".to_string()
            };
            let sell_info = if sell_ok {
                format!("卖{}(${:.3})", result.sell_tier.unwrap_or(0), result.sell_price.unwrap_or(0.0))
            } else {
                "卖单跳过".to_string()
            };
            let extreme_tag = if result.extreme_price { " [极端价格✓]" } else { "" };
            let spread_tag = if result.max_spread.is_some() && result.mid.is_some() {
                format!(" [mid={:.3}±{:?}]", result.mid.unwrap_or(0.0), result.max_spread)
            } else {
                String::new()
            };
            println!("{label} ✅ {buy_info} | {sell_info}{extreme_tag}{spread_tag}");
        } else if result.error.as_ref().map(|e| e.contains("极端价格")).unwrap_or(false) {
            skip_count += 1;
            println!("{label} ⛔ {}", result.error.as_deref().unwrap_or(""));
            new_pending.push(token_info);
        } else if buy_skip && sell_skip {
            skip_count += 1;
            let err_detail = result.error.as_deref().unwrap_or("深度/范围不足");
            println!("{label} ⚠️ {}", err_detail);
            new_pending.push(token_info);
        } else {
            skip_count += 1;
            println!(
                "{label} ❌ 买={} | 卖={}",
                &result.buy_status[..result.buy_status.len().min(25)],
                &result.sell_status[..result.sell_status.len().min(25)]
            );
        }
    }

    *PENDING_RETRY_TOKENS.write().unwrap() = new_pending.clone();

    println!("\n============================================================");
    println!("📊 [自动挂单] 完成！成功: {success_count} 个，跳过/失败: {skip_count} 个");
    if skip_count > 0 {
        println!("   📋 诊断: 深度不足={depth_count}, 极端价格跳过={extreme_count}, API错误={api_err_count}");
    }
    if !new_pending.is_empty() {
        println!("   🔄 {} 个 token 将在 {} 分钟后重试", new_pending.len(), RETRY_INTERVAL_SECS / 60);
    }
    println!("============================================================\n");

    (success_count, skip_count)
}
