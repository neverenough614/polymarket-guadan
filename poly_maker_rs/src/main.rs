//! Polymarket 交互的 Rust 迁移版
//!
//! 与 main.py 中的 Polymarket 交互逻辑对应，不含 update_markets（Google 表格）相关功能。
//! 策略 token 从 strategy_tokens.json 加载（代替 Google 表格）。

use std::str::FromStr;

use alloy::signers::Signer as _;
use anyhow::{Context, Result};
use polymarket_client_sdk::auth::Normal;
use polymarket_client_sdk::clob::types::request::{CancelMarketOrderRequest, OrdersRequest};
use polymarket_client_sdk::clob::types::{OrderType, Side};
use polymarket_client_sdk::clob::{Client, Config};
use polymarket_client_sdk::data::types::request::PositionsRequest;
use polymarket_client_sdk::data::Client as DataClient;
use polymarket_client_sdk::types::{Address, U256};
use polymarket_client_sdk::{POLYGON, PRIVATE_KEY_VAR};

use poly_maker_rs::api;
use poly_maker_rs::monitor;
use poly_maker_rs::place_order;
use poly_maker_rs::strategies;
use poly_maker_rs::tasks;

fn get_private_key() -> Result<String> {
    std::env::var("PK")
        .or_else(|_| std::env::var(PRIVATE_KEY_VAR))
        .context("请设置 PK 或 POLYMARKET_PRIVATE_KEY 环境变量")
}

/// 已认证的 CLOB 客户端类型
type AuthClient = Client<polymarket_client_sdk::auth::state::Authenticated<Normal>>;

/// 创建已认证的 CLOB 客户端（与 Python PolymarketClient 一致）
async fn create_clob_client() -> Result<AuthClient> {
    dotenvy::dotenv().ok();
    let pk = get_private_key()?;
    let signer = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
        .with_chain_id(Some(POLYGON));

    let config = Config::builder().use_server_time(true).build();
    let client = Client::new("https://clob.polymarket.com", config)?
        .authentication_builder(&signer)
        .signature_type(polymarket_client_sdk::clob::types::SignatureType::GnosisSafe)
        .authenticate()
        .await?;

    println!("✅ Polymarket CLOB 客户端已连接");
    Ok(client)
}

/// 一键撤单（对应 Python cancel_all_orders_now）
async fn cancel_all_orders(client: &AuthClient) -> Result<()> {
    println!("\n==================================================");
    println!("🛑 【一键撤单】");
    println!("==================================================");

    let count = match client.orders(&OrdersRequest::default(), None).await {
        Ok(orders) => orders.data.len(),
        Err(_) => 0,
    };
    println!("📋 当前活跃挂单: {count} 个");

    if count == 0 {
        println!("✅ 账户内没有活跃挂单。");
        return Ok(());
    }

    println!("🔥 正在执行【全部撤单】指令...");
    let resp = client.cancel_all_orders().await?;
    println!("✅ 撤单完成！Response: {:?}", resp);
    Ok(())
}

/// 撤销指定 token 的所有挂单（对应 Python cancel_all_asset）
async fn cancel_asset_orders(client: &AuthClient, asset_id: &str) -> Result<()> {
    let token_id: U256 = U256::from_str(asset_id)?;
    let req = CancelMarketOrderRequest::builder()
        .asset_id(token_id)
        .build();
    client.cancel_market_orders(&req).await?;
    println!("✅ 已撤销 token {} 的所有挂单", &asset_id[..asset_id.len().min(16)]);
    Ok(())
}

/// 获取订单簿（对应 Python get_orderbook_info）
async fn get_orderbook(client: &AuthClient, token_id: &str) -> Result<poly_maker_rs::orderbook::OrderBookInfo> {
    let info = place_order::fetch_orderbook(client, token_id).await?;
    Ok(info)
}

/// 挂限价单（对应 Python create_order）
async fn place_limit_order(
    client: &AuthClient,
    signer: &impl alloy::signers::Signer,
    token_id: &str,
    side: Side,
    price: rust_decimal::Decimal,
    size: rust_decimal::Decimal,
) -> Result<()> {
    let tid: U256 = U256::from_str(token_id)?;

    let builder = client
        .limit_order()
        .token_id(tid)
        .side(side)
        .price(price)
        .size(size)
        .order_type(OrderType::GTC);

    let order = builder.build().await?;
    let signed = client.sign(signer, order).await?;
    let resp = client.post_order(signed).await?;

    if resp.success {
        println!("✅ 挂单成功！OrderID: {:?}", resp.order_id);
    } else {
        println!("⚠️ 挂单响应: {:?}", resp);
    }
    Ok(())
}

/// 获取持仓（对应 Python get_all_positions，通过 Data API）
async fn get_positions(funder_address: &str) -> Result<()> {
    let addr: Address = funder_address.parse()?;
    let client = DataClient::default();
    let req = PositionsRequest::builder().user(addr).limit(100)?.build();
    let positions = client.positions(&req).await?;

    println!("\n📊 持仓 ({}) 个:", positions.len());
    for p in &positions {
        println!("   - {}: size={:?}", p.asset, p.size);
    }
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();

    // 解析命令行
    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(String::as_str).unwrap_or("help");

    match cmd {
        "cancel_all" => {
            let client = create_clob_client().await?;
            cancel_all_orders(&client).await?;
        }
        "cancel_asset" => {
            let asset_id = args
                .get(2)
                .context("用法: poly_maker_rs cancel_asset <token_id>")?;
            let client = create_clob_client().await?;
            cancel_asset_orders(&client, asset_id).await?;
        }
        "orderbook" => {
            let token_id = args
                .get(2)
                .context("用法: poly_maker_rs orderbook <token_id>")?;
            let client = create_clob_client().await?;
            let info = get_orderbook(&client, token_id).await?;
            println!("best_bid: {:?}, best_ask: {:?}, mid: {:?}, bids: {}, asks: {}",
                info.best_bid, info.best_ask, info.mid, info.book.bids.len(), info.book.asks.len());
        }
        "orders" => {
            let client = create_clob_client().await?;
            let orders = client.orders(&OrdersRequest::default(), None).await?;
            println!("\n📋 当前挂单 ({} 个):", orders.data.len());
            for o in &orders.data {
                println!("   - {} {} @ {} size={:?}", o.side, o.asset_id, o.price, o.original_size);
            }
        }
        "positions" => {
            let addr = args
                .get(2)
                .context("用法: poly_maker_rs positions <funder_address>")?;
            get_positions(addr).await?;
        }
        "serve" | "server" => {
            // 服务器模式（对应 Python main）：HTTP API + daemon，完全替代 Python 版本
            let path = args.get(2).map(String::as_str).unwrap_or("strategy_tokens.json").to_string();
            let tokens = strategies::load_strategy_tokens(&path)
                .context("加载策略 token 失败")?;
            if tokens.is_empty() {
                anyhow::bail!("策略列表为空");
            }
            let tokens = std::sync::Arc::new(std::sync::RwLock::new(tokens));
            let pk = get_private_key()?;
            let signer = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let client = create_clob_client().await?;
            let client = std::sync::Arc::new(client);

            let funder_addr: Option<Address> = std::env::var("BROWSER_ADDRESS")
                .ok()
                .and_then(|s| s.parse().ok());

            println!("\n🚀 [Poly-Maker] 启动服务器模式（替代 Python 版本）");
            println!("   HTTP API: http://0.0.0.0:8000");
            println!("   挂单日志: http://0.0.0.0:8000/orders/log");
            println!("   一键撤单: POST http://0.0.0.0:8000/cancel_all\n");

            // 初始挂单
            place_order::run_auto_place_orders(client.as_ref(), &signer, &tokens.read().unwrap()).await;

            // 后台任务
            let client2 = std::sync::Arc::clone(&client);
            let signer2 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            tokio::spawn(async move {
                tasks::periodic_retry_task(client2.as_ref(), &signer2).await;
            });

            if let Some(addr) = funder_addr {
                let client3 = create_clob_client().await?;
                let signer3 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                    .with_chain_id(Some(POLYGON));
                let tokens3 = std::sync::Arc::clone(&tokens);
                tokio::spawn(async move {
                    tasks::auto_close_positions_task(&client3, &signer3, tokens3, addr).await;
                });
            }

            let client4 = create_clob_client().await?;
            let signer4 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let tokens4 = std::sync::Arc::clone(&tokens);
            tokio::spawn(async move {
                tasks::spread_check_task(&client4, &signer4, tokens4).await;
            });

            let client5 = create_clob_client().await?;
            let signer5 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let tokens5 = std::sync::Arc::clone(&tokens);
            tokio::spawn(async move {
                monitor::monitor_defense_loop(&client5, &signer5, tokens5).await;
            });

            let client6 = std::sync::Arc::clone(&client);
            let signer6 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let tokens6 = std::sync::Arc::clone(&tokens);
            let path6 = path.clone();
            tokio::spawn(async move {
                tasks::json_sync_task(client6.as_ref(), &signer6, tokens6, path6).await;
            });

            // HTTP API
            let app_state = api::AppState {
                client: std::sync::Arc::clone(&client),
                strategy_tokens: std::sync::Arc::clone(&tokens),
            };
            let app = api::api_router(app_state);

            let listener = tokio::net::TcpListener::bind("0.0.0.0:8000").await?;
            println!("✅ Dashboard: http://0.0.0.0:8000 | 按 Ctrl+C 退出\n");

            let serve = axum::serve(listener, app);
            let ctrl_c = tokio::signal::ctrl_c();
            tokio::select! {
                r = serve => {
                    if let Err(e) = r {
                        eprintln!("HTTP 服务错误: {}", e);
                    }
                }
                _ = ctrl_c => {
                    println!("\n🛑 收到 Ctrl+C，执行退出前撤单...");
                    let _ = cancel_all_orders(client.as_ref()).await;
                    println!("✅ 已退出");
                }
            }
        }
        "run" => {
            // 运行完整 daemon：auto_place + periodic_retry + spread_check + auto_close + json_sync
            let path = args.get(2).map(String::as_str).unwrap_or("strategy_tokens.json").to_string();
            let tokens = strategies::load_strategy_tokens(&path)
                .context("加载策略 token 失败")?;
            if tokens.is_empty() {
                anyhow::bail!("策略列表为空");
            }
            let tokens = std::sync::Arc::new(std::sync::RwLock::new(tokens));
            let pk = get_private_key()?;
            let signer = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let client = create_clob_client().await?;

            let funder_addr: Option<Address> = std::env::var("BROWSER_ADDRESS")
                .ok()
                .and_then(|s| s.parse().ok());

            println!("🚀 启动 daemon（auto_place + periodic_retry + spread_check + auto_close + monitor + json_sync）");
            place_order::run_auto_place_orders(&client, &signer, &tokens.read().unwrap()).await;

            let client2 = create_clob_client().await?;
            let signer2 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));

            tokio::spawn(async move {
                tasks::periodic_retry_task(&client2, &signer2).await;
            });

            if let Some(addr) = funder_addr {
                let client3 = create_clob_client().await?;
                let signer3 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                    .with_chain_id(Some(POLYGON));
                let tokens3 = std::sync::Arc::clone(&tokens);
                tokio::spawn(async move {
                    tasks::auto_close_positions_task(&client3, &signer3, tokens3, addr).await;
                });
            }

            let client4 = create_clob_client().await?;
            let signer4 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let tokens4 = std::sync::Arc::clone(&tokens);
            tokio::spawn(async move {
                tasks::spread_check_task(&client4, &signer4, tokens4).await;
            });

            let client5 = create_clob_client().await?;
            let signer5 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let tokens5 = std::sync::Arc::clone(&tokens);
            tokio::spawn(async move {
                monitor::monitor_defense_loop(&client5, &signer5, tokens5).await;
            });

            let client6 = create_clob_client().await?;
            let signer6 = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let tokens6 = std::sync::Arc::clone(&tokens);
            let path6 = path.clone();
            tokio::spawn(async move {
                tasks::json_sync_task(&client6, &signer6, tokens6, path6).await;
            });

            println!("✅ 后台任务已启动（auto_place + retry + spread_check + auto_close + monitor + json_sync），按 Ctrl+C 退出");
            tokio::signal::ctrl_c().await?;
        }
        "auto_place" => {
            let path = args.get(2).map(String::as_str).unwrap_or("strategy_tokens.json");
            let tokens = strategies::load_strategy_tokens(path)
                .context("加载策略 token 失败，请确保 strategy_tokens.json 存在")?;
            if tokens.is_empty() {
                anyhow::bail!("策略列表为空");
            }
            println!("📥 已加载 {} 个策略 token", tokens.len());
            let pk = get_private_key()?;
            let signer = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));
            let client = create_clob_client().await?;
            place_order::run_auto_place_orders(&client, &signer, &tokens).await;
        }
        "place" => {
            // poly_maker_rs place <token_id> <BUY|SELL> <price> <size> [neg_risk]
            let token_id = args.get(2).context("缺少 token_id")?;
            let side_str = args.get(3).context("缺少 BUY|SELL")?;
            let price: rust_decimal::Decimal = args.get(4).context("缺少 price")?.parse()?;
            let size: rust_decimal::Decimal = args.get(5).context("缺少 size")?.parse()?;
            let side = match side_str.to_uppercase().as_str() {
                "BUY" => Side::Buy,
                "SELL" => Side::Sell,
                _ => anyhow::bail!("side 必须是 BUY 或 SELL"),
            };

            let pk = get_private_key()?;
            let signer = alloy::signers::local::PrivateKeySigner::from_str(&pk)?
                .with_chain_id(Some(POLYGON));

            let client = create_clob_client().await?;
            place_limit_order(&client, &signer, token_id, side, price, size).await?;
        }
        _ => {
            println!("Polymarket Rust 客户端 (简化版，不含 Google 表格)");
            println!();
            println!("用法:");
            println!("  cancel_all                    一键撤单");
            println!("  cancel_asset <token_id>       撤销指定 token 的所有挂单");
            println!("  orderbook <token_id>          获取订单簿");
            println!("  orders                        获取当前挂单列表");
            println!("  positions <address>           获取持仓 (Data API)");
            println!("  auto_place [strategy.json]    自动挂单（默认 strategy_tokens.json）");
            println!("  serve [strategy.json]         服务器模式：HTTP API + daemon（替代 Python 主程序）");
            println!("  run [strategy.json]           daemon：auto_place + retry + spread_check + auto_close + monitor");
            println!("  place <token> BUY|SELL <price> <size>  挂限价单");
        }
    }

    Ok(())
}
