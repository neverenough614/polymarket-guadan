//! HTTP API（对应 main.py FastAPI Dashboard）
//! GET /markets, /orderbook/{asset_id}, /orders/log, POST /cancel_all

use std::str::FromStr;
use std::sync::{Arc, RwLock};

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use polymarket_client_sdk::clob::types::request::OrderBookSummaryRequest;
use polymarket_client_sdk::clob::Client;
use polymarket_client_sdk::types::U256;
use rust_decimal::prelude::ToPrimitive;
use serde::{Deserialize, Serialize};
use tower_http::cors::{Any, CorsLayer};

use crate::state::{PLACED_ORDERS_LOG, PENDING_RETRY_TOKENS};
use crate::types::{PlaceOrderResult, TokenInfo};

type AuthClient = Client<polymarket_client_sdk::auth::state::Authenticated<polymarket_client_sdk::auth::Normal>>;

/// 应用状态
#[derive(Clone)]
pub struct AppState {
    pub client: Arc<AuthClient>,
    pub strategy_tokens: Arc<RwLock<Vec<TokenInfo>>>,
}

/// GET /markets 返回格式
#[derive(Serialize)]
pub struct MarketItem {
    pub asset_id: String,
    pub label: String,
}

/// GET /orderbook/{asset_id} 返回格式
#[derive(Serialize)]
pub struct OrderbookResponse {
    pub asset_id: String,
    pub bids: Vec<PriceLevel>,
    pub asks: Vec<PriceLevel>,
}

#[derive(Serialize)]
pub struct PriceLevel {
    pub price: f64,
    pub size: f64,
}

/// GET /orders/log 返回格式
#[derive(Serialize)]
pub struct OrdersLogResponse {
    pub total: usize,
    pub placed_count: usize,
    pub pending_retry: usize,
    pub log: Vec<PlaceOrderResult>,
}

/// POST /cancel_all 返回格式
#[derive(Serialize)]
pub struct CancelAllResponse {
    pub status: String,
    pub message: String,
}

#[derive(Debug, Deserialize)]
pub struct OrderbookQuery {
    #[serde(default = "default_depth")]
    pub depth: usize,
}

fn default_depth() -> usize {
    10
}

/// GET /markets - 从 strategy_tokens 构建市场列表
async fn list_markets(State(state): State<AppState>) -> impl IntoResponse {
    let tokens = state.strategy_tokens.read().unwrap();
    let mut markets: Vec<MarketItem> = tokens
        .iter()
        .map(|t| MarketItem {
            asset_id: t.token_id.clone(),
            label: format!("{} - {}", t.question, t.token_type),
        })
        .collect();
    // 去重（按 asset_id）
    let mut seen = std::collections::HashSet::new();
    markets.retain(|m| seen.insert(m.asset_id.clone()));
    Json(markets)
}

/// GET /orderbook/{asset_id} - 实时拉取订单簿
async fn get_orderbook(
    State(state): State<AppState>,
    Path(asset_id): Path<String>,
    Query(q): Query<OrderbookQuery>,
) -> impl IntoResponse {
    let depth = q.depth.min(50).max(1);
    let tid: U256 = match U256::from_str(&asset_id) {
        Ok(t) => t,
        Err(_) => return (StatusCode::BAD_REQUEST, Json(OrderbookResponse { asset_id, bids: vec![], asks: vec![] })).into_response(),
    };

    let req = OrderBookSummaryRequest::builder().token_id(tid).build();
    let book = match state.client.order_book(&req).await {
        Ok(b) => b,
        Err(_) => {
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(OrderbookResponse { asset_id, bids: vec![], asks: vec![] }),
            )
                .into_response();
        }
    };

    let bids: Vec<PriceLevel> = book
        .bids
        .iter()
        .take(depth)
        .filter_map(|o| Some(PriceLevel { price: o.price.to_f64()?, size: o.size.to_f64()? }))
        .collect();
    let asks: Vec<PriceLevel> = book
        .asks
        .iter()
        .take(depth)
        .filter_map(|o| Some(PriceLevel { price: o.price.to_f64()?, size: o.size.to_f64()? }))
        .collect();

    (StatusCode::OK, Json(OrderbookResponse { asset_id, bids, asks })).into_response()
}

/// GET /orders/log - 挂单日志
async fn get_orders_log() -> impl IntoResponse {
    let log = PLACED_ORDERS_LOG.read().unwrap().clone();
    let pending = PENDING_RETRY_TOKENS.read().unwrap().len();
    let placed_count = log.iter().filter(|o| o.buy_status == "placed" || o.sell_status == "placed").count();
    let total = log.len();
    let last_50: Vec<_> = log.into_iter().rev().take(50).collect();
    Json(OrdersLogResponse {
        total,
        placed_count,
        pending_retry: pending,
        log: last_50,
    })
}

/// POST /cancel_all - 一键撤单
async fn api_cancel_all(State(state): State<AppState>) -> impl IntoResponse {
    match state.client.cancel_all_orders().await {
        Ok(_) => (
            StatusCode::OK,
            Json(CancelAllResponse {
                status: "ok".to_string(),
                message: "撤单指令已发送".to_string(),
            }),
        ),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(CancelAllResponse {
                status: "error".to_string(),
                message: format!("撤单失败: {}", e),
            }),
        ),
    }
}

/// 构建 API Router
pub fn api_router(state: AppState) -> Router {
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    Router::new()
        .route("/markets", get(list_markets))
        .route("/orderbook/:asset_id", get(get_orderbook))
        .route("/orders/log", get(get_orders_log))
        .route("/cancel_all", post(api_cancel_all))
        .layer(cors)
        .with_state(state)
}
