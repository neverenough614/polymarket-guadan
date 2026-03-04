# poly-maker-rs

main.py 的 Rust 迁移版，包含 **Polymarket 交互** 核心逻辑。策略 token 从 JSON 文件加载（代替 Google 表格）。

## 环境变量

- `PK` 或 `POLYMARKET_PRIVATE_KEY`：私钥
- `BROWSER_ADDRESS`：funder 地址（自动清仓任务需要，Data API 用此查持仓）
- `HTTP_PROXY` / `HTTPS_PROXY`：代理（如 Clash 7890 端口：`http://127.0.0.1:7890`）

## 策略配置

### 方式一：从 update_markets.py 自动同步（推荐）

运行 `update_markets.py` 后，会**自动**将 Normal LP 和 High Reward Aggressive 策略导出到 `poly_maker_rs/strategy_tokens.json`。Rust 版 `serve`/`run` 会**每 5 分钟**自动重载该文件（对应 Python 的 sheet_sync_task）：

```bash
# 在项目根目录执行
uv run python update_markets.py
# 运行后会输出: -> Exported N tokens to poly_maker_rs/strategy_tokens.json

# 然后启动 Rust 版
cd poly_maker_rs
cargo run --release -- serve
```

### 方式二：手动填写

复制 `strategy_tokens.example.json` 为 `strategy_tokens.json`，填入你的 token：

```json
[
  {
    "token_id": "15871154585880608648532107628464183779895785213830018178010423617714102767076",
    "token_type": "YES",
    "question": "Will X happen?",
    "min_size": 100,
    "neg_risk": false,
    "max_spread": 0.03,
    "volatility_sum": 0,
    "source": "Normal LP"
  }
]
```

## 编译

```bash
cd poly_maker_rs
cargo build --release
```

## 用法

```bash
# 一键撤单
cargo run -- cancel_all

# 撤销指定 token
cargo run -- cancel_asset <token_id>

# 获取订单簿
cargo run -- orderbook <token_id>

# 获取挂单
cargo run -- orders

# 获取持仓
cargo run -- positions <funder_address>

# 自动挂单（单次）
cargo run -- auto_place

# 服务器模式（替代 Python main.py）：HTTP API + daemon
cargo run -- serve
# 或指定策略文件：cargo run -- serve strategy_tokens.json

# daemon（仅后台任务，无 HTTP）
cargo run -- run

# 挂限价单
cargo run -- place <token_id> BUY 0.5 100
```

## 已迁移模块

| main.py | Rust | 状态 |
|---------|------|------|
| 订单簿分析 | `orderbook.rs` | ✅ |
| 动态挂单量 | `calculate_dynamic_size` | ✅ |
| 最优挂单价格 | `analyze_best_place_price_from_book` | ✅ |
| 极端价格检测 | `is_extreme_price_market` | ✅ |
| place_order_for_token | `place_order.rs` | ✅ |
| run_auto_place_orders | `place_order::run_auto_place_orders` | ✅ |
| periodic_retry_task | `tasks::periodic_retry_task` | ✅ |
| spread_check_task | `tasks::spread_check_task` | ✅ |
| check_and_rebalance_token | `tasks::check_and_rebalance_token` | ✅ |
| auto_close_positions_task | `tasks::auto_close_positions_task` | ✅ |
| 策略 token 加载 | `strategies::load_strategy_tokens`（JSON） | ✅ |
| monitor_defense_loop | `monitor::monitor_defense_loop` | ✅ |
| sheet_sync_task | `tasks::json_sync_task`（定期重载 strategy_tokens.json） | ✅ |
| FastAPI Dashboard | `api.rs`（axum HTTP API） | ✅ |
| update_markets (Google) | 由 update_markets.py 导出 JSON | ❌ 不迁移 |
| WebSocket market_ws | - | ❌ Python 已注释，不迁移 |

### HTTP API（`serve` 命令）

- `GET /markets` - 市场列表（来自 strategy_tokens）
- `GET /orderbook/{asset_id}?depth=10` - 订单簿（实时拉取）
- `GET /orders/log` - 挂单日志
- `POST /cancel_all` - 一键撤单

### 代理（VPN/Clash 等）

如需通过代理访问 Polymarket API，设置环境变量后运行即可：

```bash
# Windows PowerShell
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
cargo run --release -- serve

# 或在 .env 中加入
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

reqwest（HTTP 客户端）会自动读取上述变量。
