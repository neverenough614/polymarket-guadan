//! 全局状态（对应 main.py placed_orders_log, pending_retry_tokens）

use std::sync::RwLock;

use crate::types::{PlaceOrderResult, TokenInfo};

/// 挂单日志
pub static PLACED_ORDERS_LOG: RwLock<Vec<PlaceOrderResult>> = RwLock::new(Vec::new());

/// 待重试的 token（深度不足）
pub static PENDING_RETRY_TOKENS: RwLock<Vec<TokenInfo>> = RwLock::new(Vec::new());
