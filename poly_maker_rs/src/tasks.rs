//! 后台任务（periodic_retry, spread_check, auto_close, json_sync）

use std::str::FromStr;
use std::sync::{Arc, RwLock};

use chrono::Utc;
use polymarket_client_sdk::clob::types::request::{CancelMarketOrderRequest, OrdersRequest};
use polymarket_client_sdk::clob::Client;
use polymarket_client_sdk::data::types::request::PositionsRequest;
use polymarket_client_sdk::data::Client as DataClient;
use polymarket_client_sdk::types::{Address, U256};

use crate::config::{
    CLOSE_PRICE_OFFSET, JSON_RELOAD_INTERVAL_SECS, MIN_POSITION_TO_CLOSE,
    POSITION_CHECK_INTERVAL_SECS, RETRY_INTERVAL_SECS, SPREAD_CHECK_INTERVAL_SECS,
};
use crate::place_order::{self, fetch_orderbook};
use crate::state::PENDING_RETRY_TOKENS;
use crate::strategies;
use crate::types::TokenInfo;

type AuthClient = Client<polymarket_client_sdk::auth::state::Authenticated<polymarket_client_sdk::auth::Normal>>;

/// 定期重试任务（深度不足的市场）
pub async fn periodic_retry_task(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
) {
    loop {
        tokio::time::sleep(tokio::time::Duration::from_secs(RETRY_INTERVAL_SECS)).await;
        let tokens: Vec<TokenInfo> = PENDING_RETRY_TOKENS.read().unwrap().clone();
        if tokens.is_empty() {
            println!(
                "[{}] 🔄 [重试] 无待重试 token，跳过",
                Utc::now().format("%H:%M:%S")
            );
            continue;
        }
        println!(
            "\n[{}] 🔄 [重试] 开始重试 {} 个 token...",
            Utc::now().format("%H:%M:%S"),
            tokens.len()
        );
        place_order::run_auto_place_orders(client, signer, &tokens).await;
    }
}

/// 检查并重新挂单（偏离 max_spread 时撤单重挂）
pub async fn check_and_rebalance_token(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    token_info: &TokenInfo,
    my_bid_price: Option<f64>,
    my_ask_price: Option<f64>,
) -> bool {
    let max_spread = match token_info.max_spread {
        Some(s) => s,
        None => return false,
    };
    if my_bid_price.is_none() && my_ask_price.is_none() {
        return false;
    }

    let book_info = match fetch_orderbook(client, &token_info.token_id).await {
        Ok(b) => b,
        Err(_) => return false,
    };
    let mid = match book_info.mid {
        Some(m) => m,
        None => return false,
    };

    let lower = mid - max_spread;
    let upper = mid + max_spread;

    let bid_ok = my_bid_price.map_or(true, |p| (lower..=upper).contains(&p));
    let ask_ok = my_ask_price.map_or(true, |p| (lower..=upper).contains(&p));

    if bid_ok && ask_ok {
        return false;
    }

    let mut out = Vec::new();
    if my_bid_price.map_or(false, |p| !(lower..=upper).contains(&p)) {
        out.push(format!("买单(${:.3})", my_bid_price.unwrap()));
    }
    if my_ask_price.map_or(false, |p| !(lower..=upper).contains(&p)) {
        out.push(format!("卖单(${:.3})", my_ask_price.unwrap()));
    }

    println!("\n🔄 [插队检测] [{}] {}...", token_info.token_type, &token_info.question[..token_info.question.len().min(35)]);
    println!("   mid={:.3}, 范围=[{:.3}, {:.3}]", mid, lower, upper);
    println!("   ⚠️ 偏离范围: {}", out.join(", "));
    println!("   🧨 撤单并重新挂单...");

    let tid: U256 = U256::from_str(&token_info.token_id).unwrap_or(U256::ZERO);
    let req = CancelMarketOrderRequest::builder().asset_id(tid).build();
    if let Err(e) = client.cancel_market_orders(&req).await {
        println!("   ❌ 撤单失败: {}", e);
        return false;
    }

    tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;

    let result = place_order::place_order_for_token(client, signer, token_info, &book_info).await;
    let buy_ok = result.buy_status == "placed";
    let sell_ok = result.sell_status == "placed";
    if buy_ok || sell_ok {
        let bi = if buy_ok {
            format!("买{}(${:.3})", result.buy_tier.unwrap_or(0), result.buy_price.unwrap_or(0.0))
        } else {
            "买单跳过".to_string()
        };
        let si = if sell_ok {
            format!("卖{}(${:.3})", result.sell_tier.unwrap_or(0), result.sell_price.unwrap_or(0.0))
        } else {
            "卖单跳过".to_string()
        };
        println!("   ✅ 重新挂单成功: {} | {}", bi, si);
    } else {
        println!("   ⚠️ 重新挂单失败或深度不足: 买={} | 卖={}", result.buy_status, result.sell_status);
    }
    true
}

/// 插队检测任务（定期检查 max_spread 偏离）
pub async fn spread_check_task(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    strategy_tokens: Arc<RwLock<Vec<TokenInfo>>>,
) {
    tokio::time::sleep(tokio::time::Duration::from_secs(SPREAD_CHECK_INTERVAL_SECS)).await;
    println!("\n🔍 [插队检测] 任务已启动（每 {}s 检查一次）", SPREAD_CHECK_INTERVAL_SECS);

    loop {
        let spread_tokens: Vec<_> = strategy_tokens.read().unwrap()
            .iter()
            .filter(|t| t.max_spread.is_some())
            .cloned()
            .collect();
        if spread_tokens.is_empty() {
            tokio::time::sleep(tokio::time::Duration::from_secs(SPREAD_CHECK_INTERVAL_SECS)).await;
            continue;
        }

        let orders = match client.orders(&OrdersRequest::default(), None).await {
            Ok(o) => o,
            Err(_) => {
                tokio::time::sleep(tokio::time::Duration::from_secs(SPREAD_CHECK_INTERVAL_SECS)).await;
                continue;
            }
        };

        // 按 asset_id 分组，取 best_bid, best_ask
        let mut order_map: std::collections::HashMap<String, (Option<f64>, Option<f64>)> = std::collections::HashMap::new();
        for o in &orders.data {
            let aid = o.asset_id.to_string();
            let price = o.price.to_string().parse::<f64>().ok();
            let entry = order_map.entry(aid).or_insert((None, None));
            let side_str = format!("{:?}", o.side);
            if side_str.contains("Buy") {
                if let Some(pr) = price {
                    entry.0 = Some(entry.0.map_or(pr, |e| e.max(pr)));
                }
            } else if side_str.contains("Sell") {
                if let Some(pr) = price {
                    entry.1 = Some(entry.1.map_or(pr, |e| e.min(pr)));
                }
            }
        }

        let mut rebalanced = 0;
        for t in &spread_tokens {
            let (my_bid, my_ask) = order_map.get(&t.token_id).copied().unwrap_or((None, None));
            if my_bid.is_none() && my_ask.is_none() {
                continue;
            }
            if check_and_rebalance_token(client, signer, t, my_bid, my_ask).await {
                rebalanced += 1;
                tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
            }
        }
        if rebalanced > 0 {
            println!("[{}] 🔍 [插队检测] 本轮重新挂单: {} 个", Utc::now().format("%H:%M:%S"), rebalanced);
        }

        tokio::time::sleep(tokio::time::Duration::from_secs(SPREAD_CHECK_INTERVAL_SECS)).await;
    }
}

/// 自动清仓任务（持仓被吃后市价卖出）
/// funder_address: 必须传入（BROWSER_ADDRESS），Data API 用此地址查持仓
pub async fn auto_close_positions_task(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    strategy_tokens: Arc<RwLock<Vec<TokenInfo>>>,
    funder_address: Address,
) {
    println!(
        "\n💰 [自动清仓] 任务已启动（每 {}s 检查，阈值: {} shares）",
        POSITION_CHECK_INTERVAL_SECS, MIN_POSITION_TO_CLOSE
    );

    let data_client = DataClient::default();
    loop {
        let token_map: std::collections::HashMap<_, _> = strategy_tokens.read().unwrap()
        .iter()
        .map(|t| (t.token_id.clone(), t.clone()))
        .collect();

        tokio::time::sleep(tokio::time::Duration::from_secs(POSITION_CHECK_INTERVAL_SECS)).await;

        let req = PositionsRequest::builder().user(funder_address).build();
        let positions = match data_client.positions(&req).await {
            Ok(p) => p,
            Err(e) => {
                println!("\n⚠️ [自动清仓] 获取持仓失败: {}", e);
                continue;
            }
        };

        for p in &positions {
            let asset = p.asset.to_string();
            let size: f64 = p.size.to_string().parse().unwrap_or(0.0);
            if size < MIN_POSITION_TO_CLOSE {
                continue;
            }
            let Some(t) = token_map.get(&asset) else { continue };

            println!("\n\n$$$$$$$$$$$$$$$$$$$$ 💰 发现持仓，开始清仓 $$$$$$$$$$$$$$$$$$$$");
            println!("⏰ 时间: {}", Utc::now().format("%Y-%m-%d %H:%M:%S"));
            println!("📋 [{}] {}...", t.token_type, &t.question[..t.question.len().min(40)]);
            println!("   持仓: {:.2} shares", size);

            let book_info = match fetch_orderbook(client, &asset).await {
                Ok(b) => b,
                Err(_) => {
                    println!("   ❌ 无法获取订单簿，跳过");
                    continue;
                }
            };
            let best_bid = match book_info.best_bid {
                Some(b) => b,
                None => {
                    println!("   ❌ 无买单，跳过");
                    continue;
                }
            };
            let close_price = (best_bid - CLOSE_PRICE_OFFSET).max(0.01);
            let close_price = (close_price * 100.0).round() / 100.0;

            println!("   best_bid: ${:.3} → 清仓价: ${:.3}", best_bid, close_price);
            println!("   正在挂卖单: {:.2} shares @ ${:.3}...", size, close_price);

            match place_order::place_single_order(
                client,
                signer,
                &asset,
                polymarket_client_sdk::clob::types::Side::Sell,
                close_price,
                size,
                t.neg_risk,
            )
            .await
            {
                Ok(true) => println!("   ✅ 清仓单已提交！"),
                Ok(false) => println!("   ❌ 清仓失败"),
                Err(e) => println!("   ❌ 清仓出错: {}", e),
            }
        }
    }
}

/// 策略 JSON 定期重载任务（对应 Python sheet_sync_task）
/// 每 5 分钟重载 strategy_tokens.json，同步新增/移除的 token
pub async fn json_sync_task(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    strategy_tokens: Arc<RwLock<Vec<TokenInfo>>>,
    json_path: String,
) {
    tokio::time::sleep(tokio::time::Duration::from_secs(JSON_RELOAD_INTERVAL_SECS)).await;
    println!("\n🔄 [策略重载] 任务已启动（每 {}s 检查 {}）", JSON_RELOAD_INTERVAL_SECS, json_path);

    loop {
        tokio::time::sleep(tokio::time::Duration::from_secs(JSON_RELOAD_INTERVAL_SECS)).await;

        let new_tokens = match strategies::load_strategy_tokens(&json_path) {
            Ok(t) if !t.is_empty() => t,
            Ok(_) => {
                println!("   ⚠️ [{}] 重载失败或文件为空，保持原配置", Utc::now().format("%H:%M:%S"));
                continue;
            }
            Err(e) => {
                println!("   ⚠️ [{}] 重载失败: {}，保持原配置", Utc::now().format("%H:%M:%S"), e);
                continue;
            }
        };

        let old_ids: std::collections::HashSet<_> = strategy_tokens.read().unwrap().iter().map(|t| t.token_id.clone()).collect();
        let new_ids: std::collections::HashSet<_> = new_tokens.iter().map(|t| t.token_id.clone()).collect();
        let removed_ids: Vec<_> = old_ids.difference(&new_ids).cloned().collect();
        let added_ids: Vec<_> = new_ids.difference(&old_ids).cloned().collect();
        let unchanged = old_ids.intersection(&new_ids).count();

        if removed_ids.is_empty() && added_ids.is_empty() {
            continue;
        }

        println!("\n{}", "=".repeat(60));
        println!("🔄 [{}] 正在重载策略...", Utc::now().format("%H:%M:%S"));
        println!("   📊 变化: ➕ 新增 {} | 🗑️ 移除 {} | 不变 {}", added_ids.len(), removed_ids.len(), unchanged);
        println!("{}", "=".repeat(60));

        if !removed_ids.is_empty() {
            for token_id in &removed_ids {
                let tid: U256 = U256::from_str(token_id).unwrap_or(U256::ZERO);
                let req = CancelMarketOrderRequest::builder().asset_id(tid).build();
                if let Err(e) = client.cancel_market_orders(&req).await {
                    println!("      ⚠️ 撤单失败 {}: {}", &token_id[..token_id.len().min(16)], e);
                } else {
                    println!("      ✅ 已撤单并移除: {}...", &token_id[..token_id.len().min(16)]);
                }
                tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
            }
        }

        if !added_ids.is_empty() {
            let added_tokens: Vec<_> = new_tokens.iter().filter(|t| added_ids.contains(&t.token_id)).cloned().collect();
            println!("   🚀 正在对 {} 个新增市场执行挂单...", added_tokens.len());
            place_order::run_auto_place_orders(client, signer, &added_tokens).await;
            println!("   ✅ 新增挂单完成");
        }

        {
            let mut tokens = strategy_tokens.write().unwrap();
            *tokens = new_tokens;
            println!("   📋 当前监控总数: {} 个 token\n", tokens.len());
        }
    }
}
