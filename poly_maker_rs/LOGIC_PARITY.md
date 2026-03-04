# Python 与 Rust 版逻辑对照

本文档核对 `main.py` 与 `poly_maker_rs` 的挂单/监控/清仓等逻辑是否一致。

## 1. 配置常量

| 配置项 | Python (main.py) | Rust (config.rs) | 一致 |
|--------|------------------|------------------|------|
| DEPTH_THRESHOLD_TIER1 | 1500.0 | 1500.0 | ✅ |
| DEPTH_THRESHOLD_TIER2 | 200.0 | 200.0 | ✅ |
| EXTREME_PRICE_THRESHOLD | 0.10 | 0.10 | ✅ |
| RETRY_INTERVAL / SECS | 300 | 300 | ✅ |
| NORMAL_SIZE_RATIO | 0.30 | 0.30 | ✅ |
| NORMAL_MAX_ORDER_SIZE | 700.0 | 700.0 | ✅ |
| AGGRESSIVE_* | 0.08 / 300 | 0.08 / 300 | ✅ |
| MAX_LEVEL_GAP | 0.02 | 0.02 | ✅ |
| POSITION_CHECK_INTERVAL | 3 | 3 | ✅ |
| MIN_POSITION_TO_CLOSE | 5.0 | 5.0 | ✅ |
| CLOSE_PRICE_OFFSET | 0.01 | 0.01 | ✅ |
| SPREAD_CHECK_INTERVAL | 60 | 60 | ✅ |
| THRESHOLD_FRONT_DEPTH_DROP | 0.20 | 0.20 | ✅ |
| THRESHOLD_SAME_DEPTH_DROP | 0.10 | 0.10 | ✅ |
| THRESHOLD_FRONT_HIGH_WATER_DROP | 0.50 | 0.50 | ✅ |
| THRESHOLD_SAME_HIGH_WATER_DROP | 0.50 | 0.50 | ✅ |
| MIN_SAME_DEPTH_SAFE | 200.0 | 200.0 | ✅ |
| MIN_FRONT_DEPTH_THRESHOLD | 100.0 | 100.0 | ✅ |
| MIN_FRONT_DEPTH_ABSOLUTE | 100.0 | 100.0 | ✅ |
| MIN_FRONT_DEPTH_ABSOLUTE_REF | 0.0 | 0.0 | ✅ |
| MONITOR_CHECK_INTERVAL | 2 | 2 | ✅ |
| IMBALANCE_* | 0.30, 5, 500 | 同 | ✅ |
| PLACE_ORDER_WORKERS | 8 | 8 | ✅ |

## 2. 订单簿与挂单分析

| 逻辑 | Python | Rust | 一致 |
|------|--------|------|------|
| best_bid / best_ask | bids 降序取首、asks 升序取首 | 显式 max(bids), min(asks) | ✅ |
| mid | (best_bid + best_ask) / 2 | 同 | ✅ |
| is_extreme_price_market | best_bid ≤ 0.10 或 ≥ 0.90 | 同 | ✅ |
| 第 1 档深度下限 | 先检查 ≥ 100 USDC | 同 | ✅ |
| 档位连续性 | 前三档相邻价差 ≤ MAX_LEVEL_GAP (0.02) | 同 | ✅ |
| 黑名单 skip_tier1 | 命中关键词 → 跳过第一档 | 同 | ✅ |
| 孤立厚墙检测 | 第1档/第2档深度比 > 5 → 跳过 | 同 | ✅ |
| 第一档占比 | 挂单价值 ≤ 该档深度 1/5 (20%) | 同 | ✅ |
| max_spread 范围 | 有则只选 [mid-ms, mid+ms] 内档位 | 同 | ✅ |
| calculate_dynamic_size | 前三档深度、size_ratio、波动率因子、min_size 下限 | 同 | ✅ |
| 波动率因子 | ≤10→1.0，否则 max(0.2, 1-(v-10)/60) | 同 | ✅ |

## 3. 单 token 挂单 (place_order_for_token)

| 步骤 | Python | Rust | 一致 |
|------|--------|------|------|
| base_min_size | max(100, raw_min_size) | 同 | ✅ |
| 策略 size_ratio/max_size | Normal LP / High Reward / 默认 | 同 | ✅ |
| order_size 为 None | 返回 depth_insufficient，不挂 | 同 | ✅ |
| 极端价格市场 | 必须买+卖都有档位才挂，否则整单跳过 | 同 | ✅ |
| 非极端 | 可只挂买或只挂卖 | 同 | ✅ |
| 执行顺序 | 先买后卖 | 同 | ✅ |
| 黑名单 skip_tier1 | blacklisted=True → 跳过第一档 | 同 | ✅ |

## 4. 批量挂单与重试

| 逻辑 | Python | Rust | 一致 |
|------|--------|------|------|
| 待重试条件 | 极端价格跳过 或 买+卖均为 depth_insufficient/extreme_skip | 同 | ✅ |
| 重试间隔 | RETRY_INTERVAL (300s) | RETRY_INTERVAL_SECS (300) | ✅ |
| 并发数 | PLACE_ORDER_WORKERS = 8 | PLACE_ORDER_WORKERS = 8 | ✅ |

## 5. 关键词黑名单

| 逻辑 | Python | Rust | 一致 |
|------|--------|------|------|
| 关键词列表 | QUESTION_BLACKLIST_KEYWORDS（军事/地缘/政治） | 同 | ✅ |
| 匹配方式 | 大小写不敏感 contains | 同 | ✅ |
| 命中行为 | 标记 blacklisted=True，跳过第一档 | 同 | ✅ |

## 6. 插队检测 (spread_check)

| 逻辑 | Python | Rust | 一致 |
|------|--------|------|------|
| 只处理有 max_spread 的 token | 是 | 是 | ✅ |
| 偏离判定 | 买/卖价不在 [mid-max_spread, mid+max_spread] | 同 | ✅ |
| 撤单后等待 | 0.5s | 500ms | ✅ |
| 重新挂单 | place_order_for_token | place_order_for_token | ✅ |

## 7. 监控防御 (monitor_defense_loop)

| 逻辑 | Python | Rust | 一致 |
|------|--------|------|------|
| 拉取我的订单 | get_all_my_orders_once → best_bid/ask, bid_ids/ask_ids | 同 | ✅ |
| 只对有挂单的 token 拉订单簿 | 是 | 是 | ✅ |
| 订单簿获取方式 | 并发多线程 | 批量 POST /books | ✅ 逻辑同，Rust 更快 |
| 极端价格孤单检测 | best_bid 极端且只有单边挂单→撤单 | 同 | ✅ |
| 偏斜检测 (check_book_imbalance) | 前 5 档、总深度≥500、占比<30% 撤对应边 | 同 | ✅ |
| 单边撤单 | 用缓存的 bid_ids/ask_ids 撤 | 同 | ✅ |
| calculate_layered_depth | bid_front/same, ask_front/same | 同 | ✅ |
| 同档排除自己 | bid_same_adj = bid_same - order_size*my_bid（且≥0） | 同 | ✅ |
| 高水位 | front/same 用 max 更新 | 同 | ✅ |
| last_* 存储 | last_bid_same_depth = bid_same（原始） | 同 | ✅ |
| 威胁检测入参 | 传 bid_same_adj / ask_same_adj | 同 | ✅ |
| check_bid_threats / check_ask_threats | 前墙消失、绝对兜底、高水位、单轮塌陷、同档薄/被吃 | 同 | ✅ |
| 防御后重挂 | 撤单→等 60s→place_order_for_token | 同 | ✅ |

## 8. 自动清仓 (auto_close_positions_task)

| 逻辑 | Python | Rust | 一致 |
|------|--------|------|------|
| 持仓来源 | get_all_positions() | Data API positions(user) | ✅ |
| 只清策略内 token | 是 | 是 | ✅ |
| 阈值 | size >= MIN_POSITION_TO_CLOSE (5) | 同 | ✅ |
| 清仓价 | max(0.01, round(best_bid - 0.01, 2)) | 同 | ✅ |
| 下单 | SELL limit @ close_price | place_single_order Sell | ✅ |

## 9. 策略来源

| 项目 | Python | Rust | 一致 |
|------|--------|------|------|
| 列表来源 | Google Sheet Normal LP + High Reward | strategy_tokens.json（同源导出） | ✅ |
| 重载 | sheet_sync_task 定期读表 | json_sync_task 定期读 JSON | ✅ |
| max_spread 无/0 | 导出为 null，挂单不限制范围 | 同 | ✅ |
| 黑名单过滤 | 加载时检查 QUESTION_BLACKLIST_KEYWORDS | 同 | ✅ |

## 10. 本次同步的变更（2026-03-03）

以下差异已从 Python main.py 最新版同步到 Rust：

| 变更 | 旧值 (Rust) | 新值 (与 Python 一致) | 文件 |
|------|-------------|----------------------|------|
| DEPTH_THRESHOLD_TIER1 | 200.0 | 1500.0 | config.rs |
| DEPTH_THRESHOLD_TIER2 | 100.0 | 200.0 | config.rs |
| POSITION_CHECK_INTERVAL_SECS | 5 | 3 | config.rs |
| THRESHOLD_FRONT_DEPTH_DROP | 0.30 | 0.20 | config.rs |
| THRESHOLD_SAME_DEPTH_DROP | 0.50 | 0.10 | config.rs |
| THRESHOLD_SAME_HIGH_WATER_DROP | 0.60 | 0.50 | config.rs |
| MIN_SAME_DEPTH_SAFE | 100.0 | 200.0 | config.rs |
| PLACE_ORDER_WORKERS | 5 | 8 | config.rs |
| 第一档占比检查 | 1/3 (33%) | 1/5 (20%) | orderbook.rs |
| 孤立厚墙检测 | 无 | tier1/tier2 > 5 跳过 | orderbook.rs |
| 黑名单 skip_tier1 | 无 | 命中关键词跳过第一档 | orderbook.rs, strategies.rs, place_order.rs, types.rs |
| 防御后重挂等待 | 30s | 60s | monitor.rs |

## 11. 已知差异（不影响逻辑一致性）

- **run_auto_place_orders 并发**：Python 用 8 线程并发，Rust 按 chunk 顺序执行（每 chunk 8 个并发），结果集与重试列表一致。
- **ORDERBOOK_TIMEOUT / MAX_CONCURRENT_WORKERS**：仅 Python 拉订单簿时使用；Rust 用批量 API，无单独超时配置。
