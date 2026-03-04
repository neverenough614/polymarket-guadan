//! 策略 token 加载（对应 main.py load_strategy_markets）
//! Rust 版使用 JSON 文件代替 Google 表格

use std::fs;
use std::path::Path;

use anyhow::Result;
use crate::types::TokenInfo;

/// 关键词黑名单（大小写不敏感，命中则跳过第一档）
/// 对应 Python QUESTION_BLACKLIST_KEYWORDS
const QUESTION_BLACKLIST_KEYWORDS: &[&str] = &[
    // 军事打击类
    "strikes", "strike", "attack", "attacks", "bomb", "missile", "nuclear strike",
    // 地缘政治占领/封锁类
    "capture", "invade", "invasion", "Strait of Hormuz", "Iran", "aliens", "Iranian",
    // 政治演讲单日事件
    "State of the Union", "say \"", "tweets", "tweet",
];

/// 检查 question 是否命中黑名单关键词（大小写不敏感）
fn check_blacklisted(question: &str) -> (bool, Option<String>) {
    let question_lower = question.to_lowercase();
    for kw in QUESTION_BLACKLIST_KEYWORDS {
        if question_lower.contains(&kw.to_lowercase()) {
            return (true, Some(kw.to_string()));
        }
    }
    (false, None)
}

/// 从 strategy_tokens.json 加载策略 token
/// 格式示例:
/// ```json
/// [
///   {
///     "token_id": "15871154585880608648532107628464183779895785213830018178010423617714102767076",
///     "token_type": "YES",
///     "question": "Will X happen?",
///     "min_size": 100,
///     "neg_risk": false,
///     "max_spread": 0.03,
///     "volatility_sum": 0,
///     "source": "Normal LP"
///   }
/// ]
/// ```
pub fn load_strategy_tokens(path: impl AsRef<Path>) -> Result<Vec<TokenInfo>> {
    let content = fs::read_to_string(path)?;
    let mut tokens: Vec<TokenInfo> = serde_json::from_str(&content)?;

    // 应用黑名单关键词过滤：命中则标记 blacklisted=true（跳过第一档，从第二档开始）
    for token in &mut tokens {
        let (is_blacklisted, matched_kw) = check_blacklisted(&token.question);
        if is_blacklisted {
            token.blacklisted = true;
            if let Some(kw) = matched_kw {
                let q: String = token.question.chars().take(55).collect();
                println!("   ⚠️ [黑名单] 标记: {}... (命中: '{}') → 跳过第一档，从第二档开始", q, kw);
            }
        }
    }

    Ok(tokens)
}
