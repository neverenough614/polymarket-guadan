# predict.fun 迁移 — SP1：PredictFunClient 地基（设计文档）

- 日期：2026-06-02
- 状态：已通过 brainstorm，待 review → writing-plans
- 作者：neverenough614 + Claude
- 分支：feature/dynamic-market-heat（迁移工作建议另开 `feature/predictfun-migration`）

---

## 0. 背景与总目标

现有系统是一套 **Polymarket** 自动做市/刷奖励机器人：自动挂单、订单簿监控、防御撤单、自动清仓，核心文件是 `main.py`（挂单/监控/撤单引擎）与 `data_updater/find_markets.py`（市场发现，写 Google 表格）。目标是把这套**挂单 / 监控 / 撤单**体系迁移到 **predict.fun**（BNB 链）。

### 两平台关键差异（决定迁移不是 1:1 机械移植）

| 维度 | Polymarket（现状） | predict.fun（目标） |
|---|---|---|
| 链 / 抵押物 | Polygon / USDC（6 位小数）/ 份额计量 | BNB（chainId 56 主网 / 97 测试网）/ USDT（**18 位 wei**） |
| 订单提交 | `py_clob_client` 签名+提交一体 | predict-sdk **EIP-712 签名** + REST `POST /v1/orders` |
| 鉴权 | API key（create_or_derive_api_key） | 签名 message → 换 **JWT**；主网另需 API key，测试网不需要 |
| 订单簿 | 每个 outcome 一个 token_id 独立簿 | **每市场一本簿（Yes 口径）**，NO 侧由 `1 − Yes` swap+complement 推导；**无批量端点**，限速 240/min |
| 撤单 | `cancel_market_orders(asset_id)` / `cancel_all()` | **仅按 order id**：`POST /v1/orders/remove`（≤100/次，链下快撤） |
| 做市奖励 | 每市场实时 `max_spread`/`rewards_daily_rate`/Q-score | **无该 API**；改为积分/空投 farming（交易量+流动性+准确率+高不确定性 2x）。市场列表可读 `hasActiveRewards`/`spreadThreshold`/`shareThreshold`/`feeRateBps` |
| 仓位合并 | `poly_merger` Node 脚本 | SDK 原生 `merge_positions`/`redeem_positions` |

### 已确认的产品决策（brainstorm 结论）

1. **保留机制 + 换目标函数**：完整移植挂单/监控/撤单/风控/自动清仓机械层；把"该不该挂这个价位"的判断从 Polymarket 奖励数学换成 predict.fun 积分导向启发式。
2. **同一 repo + 配置开关**：新增 `PredictFunBackend` 实现现有 `IExecutionBackend`，用 `PLATFORM=predictfun` 切换。**Polymarket 路径一行不动**（它在 VPS 上继续跑）。
3. **testnet/mainnet 均支持，可切换**。
4. **predict.fun 用独立的 Google 表格**（不写 Polymarket 正在读的表）。
5. **钱包**：EOA 与 Predict 智能账户两种都在 spec 中预留，**默认 EOA**，可切。

### 总拆分（每个子项各自 spec → plan → 实现，互相解耦）

- **SP1 — PredictFunClient 地基**（本文档）：REST + JWT 鉴权 + SDK 签名下单 + testnet/mainnet 配置。
- SP2 — PredictFunBackend：`IExecutionBackend` 实现 + YES/NO 簿适配 + 撤单按 id。
- SP3 — 市场发现 + 独立表：`find_markets_predictfun.py` + 新表结构。
- SP4 — 目标函数替换：`strategy/objective.py` 抽象缝 + 积分启发式，接进 `main.py`。
- SP5 — 监控传输：WebSocket 订阅 + 防御/风控循环适配。

---

## 1. SP1 范围与边界

**做什么**：实现 `PolymarketClient` 的对等物 `PredictFunClient`，提供 BNB 上的原始能力——鉴权、签名、下单、查（簿/市场/持仓/我的单）、撤单、余额、授权——并把返回数据**归一化**为下游（main.py / 未来的 PredictFunBackend）已经在用的形状。

**不做什么（留给后续子项）**：
- YES/NO 订单簿的 swap+complement 推导 与 `IExecutionBackend` 包装 → SP2。
- 市场发现/写表 → SP3。
- 目标函数/挂单决策 → SP4。
- WebSocket 实时订阅 → SP5（SP1 仅提供 REST 取簿，够 smoke 验收）。

**验收标准（Definition of Done）**：testnet 上 `scripts/predictfun_smoke.py` 能跑通：
`鉴权 → 取一个 OPEN 市场及其订单簿 → 挂一张远离 mid 的极小限价单 → 在"我的单"里查到它 → remove 撤掉它 → 确认已消失`。全程无裸异常，密钥缺失时启动即 fail fast。

---

## 2. 文件落点

```
predictfun_data/
  __init__.py
  predictfun_client.py     # 主类 PredictFunClient（对标 poly_data/polymarket_client.py）
  rest_api.py              # 纯 REST 层：auth / orders / markets / orderbook / positions
  units.py                 # 价格·份额 ↔ wei(18) + tick 取整 + 归一化辅助
config/
  bot_config.py            # 扩展：PLATFORM、PREDICTFUN_* 网络/URL/tick/默认 flag
scripts/
  predictfun_smoke.py      # testnet 验收脚本（SP1 的 DoD）
tests/
  test_predictfun_units.py # 单元测试：wei 换算 / tick 取整 / 归一化（纯函数，不打网络）
.env                       # PREDICTFUN_PK / PREDICTFUN_API_KEY / PREDICTFUN_ACCOUNT ...
```

依赖：`pip install predict-sdk`（≥0.0.16，需 Python ≥3.10）写入 `pyproject.toml`。`eth_account` 现有依赖已满足签名需要。

---

## 3. 配置与密钥

`config/bot_config.py` 新增（集中常量，无硬编码散落）：

```python
PLATFORM            = env("PLATFORM", "polymarket")        # polymarket | predictfun
PREDICTFUN_NETWORK  = env("PREDICTFUN_NETWORK", "testnet") # testnet | mainnet
PREDICTFUN_BASE_URL = {
    "testnet": "https://api-testnet.predict.fun",
    "mainnet": "https://api.predict.fun",
}
PREDICTFUN_CHAIN_ID = {"testnet": 97, "mainnet": 56}       # BNB_TESTNET / BNB_MAINNET
PREDICTFUN_TICK_SIZE        = float(env("PREDICTFUN_TICK_SIZE", "0.01"))  # 待 OpenAPI 确认，默认 0.01
PREDICTFUN_DEFAULT_YIELD_BEARING = env_bool("PREDICTFUN_DEFAULT_YIELD_BEARING", False)
PREDICTFUN_FEE_RATE_BPS_FALLBACK = int(env("PREDICTFUN_FEE_RATE_BPS_FALLBACK", "0"))  # 优先用市场返回的 feeRateBps
```

`.env`（密钥，永不入库）：

```
PREDICTFUN_PK=0x...            # EOA 私钥（必需）
PREDICTFUN_API_KEY=...         # 仅 mainnet 必需；testnet 留空
PREDICTFUN_ACCOUNT=0x...       # 可选：Predict 智能账户地址；留空=EOA 模式
```

**启动校验**：`PLATFORM=predictfun` 时，缺 `PREDICTFUN_PK`（或 mainnet 缺 `PREDICTFUN_API_KEY`）→ 立即抛错退出（fail fast，符合 security 规范：启动时校验必需密钥存在）。

---

## 4. 单位换算（units.py，纯函数 + 单测）

predict.fun 用 wei（18 位）；价格按 tick 取整后再换算。

```python
WEI = 10 ** 18

def price_to_tick(price: float, tick: float) -> float:
    """把价格量化到最近的 tick（如 0.01）。"""

def to_wei(amount: float) -> int:
    """份额 / USDT 金额 → wei（round 而非 floor，避免系统性少 1）。"""

def price_per_share_wei(price: float, tick: float) -> int:
    """价格(0~1) → 每股 wei。"""

def shares_to_wei(size: float) -> int:
    """份额 → quantity_wei。"""

def from_wei(x: int | str) -> float:
    """wei → float（解析 REST/SDK 返回）。"""
```

> 待验证：USDT 在 BSC 上确为 18 位小数；tick_size 的权威来源（市场返回字段 or 全局默认）。先用 0.01 默认 + 配置项兜底。

---

## 5. REST 层（rest_api.py）

薄封装，负责 URL 拼接、Bearer 注入、统一重试与限速节流、错误归一。不含业务语义。

```python
class PredictRest:
    def __init__(self, base_url, api_key=None, jwt_provider=None): ...
    # jwt_provider: 回调，返回当前有效 JWT；遇 401 触发上层重鉴后重试一次

    # 鉴权（确切路径待 OpenAPI 确认）
    def get_auth_message(self, address) -> dict
    def exchange_jwt(self, address, signature) -> dict        # → {token, expiresAt}

    # 订单
    def create_order(self, body: dict) -> dict                # POST /v1/orders
    def remove_orders(self, ids: list[str]) -> dict           # POST /v1/orders/remove
    def get_my_orders(self, **filters) -> dict                # GET（"get a list of your own orders"）
    def get_order_by_hash(self, h) -> dict

    # 行情 / 持仓
    def get_markets(self, **params) -> dict                   # GET /v1/markets
    def get_market(self, market_id) -> dict                   # GET /v1/markets/{id}
    def get_orderbook(self, market_id) -> dict                # GET /v1/markets/{id}/orderbook
    def get_positions(self, **params) -> dict                 # GET（自己的持仓）
```

**限速节流**：内置每分钟令牌（240/min 默认，可配），超额排队而非 429 风暴。**重试**：网络错误/5xx 指数退避；401 触发一次重鉴权后重试。所有响应先校验 `success` 字段/HTTP 状态，错误抛 `PredictApiError`（含 status + body 摘要），绝不静默吞错。

---

## 6. 主类 PredictFunClient（predictfun_client.py）

刻意贴近 `PolymarketClient` 的方法面，让 SP2 的 `PredictFunBackend` 包装最薄。

```python
class PredictFunClient:
    def __init__(self, network: str | None = None):
        # 1) 读 config（network / base_url / chain_id / tick / yield_bearing 默认）
        # 2) 读 .env（PK / API_KEY / ACCOUNT），缺失 fail fast
        # 3) builder = OrderBuilder.make(ChainId, pk[, OrderBuilderOptions(predict_account=...)])
        # 4) rest = PredictRest(base_url, api_key, jwt_provider=self._current_jwt)
        # 5) self.address = 签名者/智能账户地址
        # 6) authenticate()

    # ---- 鉴权 ----
    def authenticate(self) -> None                 # get_auth_message → 签名 → exchange_jwt → 缓存
    def _ensure_jwt(self) -> str                   # 过期/缺失则重鉴
    def _sign_message(self, message) -> str        # EOA: eth_account；smart account: builder.sign_predict_account_message

    # ---- 下单 ----
    def create_order(self, token_id, side, price, size,
                     neg_risk=False, is_yield_bearing=None, order_type="LIMIT") -> dict:
        # tick 取整 → get_limit_order_amounts(LimitHelperInput) → build_order
        # → build_typed_data(is_neg_risk, is_yield_bearing) → sign_typed_data_order
        # → rest.create_order(body) → 归一化返回 {status, order_id, hash, raw}
        # 失败返回 {"status":"error","error":...}（对齐 PolymarketClient.create_order，不抛裸异常）

    # ---- 查询（归一化输出）----
    def get_orderbook(self, market_id) -> dict     # 原始 Yes 口径 {bids:[[p,s]], asks:[[p,s]], market_id, ts}
    def get_markets(self, **filters) -> list[dict]
    def get_market(self, market_id) -> dict
    def get_open_orders(self, market_id=None) -> list[dict]
        # 归一化每单：{id, token_id, market_id, side(BUY/SELL), price(float), size(float),
        #             size_matched(float), status("LIVE"/...)}
    def get_positions(self) -> list[dict]

    # ---- 撤单 ----
    def remove_orders(self, ids: list[str]) -> dict  # 自动分批 ≤100，汇总 removed/noop

    # ---- 资金 / 授权 / 仓位 ----
    def get_usdt_balance(self) -> float              # builder.balance_of("USDT")
    def set_approvals(self) -> dict                  # 一次性 bootstrap（首跑授权）
    def merge_positions(self, condition_id, amount, is_neg_risk=False, is_yield_bearing=None)
    def redeem_positions(self, condition_id, index_set, is_neg_risk=False, is_yield_bearing=None, amount=None)
```

**归一化约定**（贯穿全类）：对外 side 一律 `BUY`/`SELL`、price/size 一律 `float`、订单状态含 `LIVE`，使 `main.py` 既有逻辑（如 `has_matching_live_order`、`get_all_my_orders_grouped`）在 SP2 包装后无需改动。SDK 的 `Side.BUY=0 / SELL=1` 在 client 内部转换,不外泄。

---

## 7. 鉴权时序

```
__init__ → authenticate():
  msg   = rest.get_auth_message(address)          # 取待签 message / nonce
  sig   = _sign_message(msg)                       # EOA 或 smart account
  resp  = rest.exchange_jwt(address, sig)          # → {token, expiresAt}
  缓存 self._jwt, self._jwt_exp

每次 REST 调用 → jwt_provider → _ensure_jwt()：
  若 now >= exp - 安全余量 → 重新 authenticate()
  返回 self._jwt（注入 Authorization: Bearer）

REST 收到 401 → 触发一次 authenticate() 后重试该请求一次；再失败则抛 PredictApiError
```

mainnet 时所有请求额外带 `X-API-KEY: PREDICTFUN_API_KEY`（确切 header 名待 OpenAPI 确认）。

---

## 8. 健壮性与错误处理

- **fail fast**：必需密钥/网络配置缺失，启动即退出并打印清晰原因。
- **不静默吞错**：REST 错误抛 `PredictApiError`；唯一例外是 `create_order` 返回 `{"status":"error"}`（与 Polymarket 行为一致，便于热路径继续）。
- **JWT 自动刷新 + 401 重鉴**。
- **限速节流**（240/min 令牌桶）+ 5xx/网络指数退避重试。
- **外部数据先校验后用**：解析市场/簿/持仓前检查结构与类型，避免把错误响应当成"空"静默处理。

---

## 9. 测试

- **单元（不打网络）**：`tests/test_predictfun_units.py` 覆盖 `price_to_tick`/`to_wei`/`price_per_share_wei`/`from_wei`/订单归一化（含边界：tick 取整、0/1 价、超 100 单撤单分批）。
- **集成 / 验收（testnet，手动跑）**：`scripts/predictfun_smoke.py` 完成第 1 节 DoD 全链路；打印每步结果。该脚本即 SP1 完成标志。
- 遵循仓库 pytest 约定；纯函数优先，外部调用在 smoke 脚本里人工验证（避免对 testnet 的脆弱自动化依赖）。

---

## 10. 实现期需对照 OpenAPI（`https://api.predict.fun/docs`）确认的待办

1. 鉴权端点确切路径与字段（get message / exchange JWT）。
2. `POST /v1/orders` 请求 body 的确切字段（signedOrder 结构、signature、orderType、isNegRisk、isYieldBearing、slippage 等）。
3. `GET /v1/markets` 的 `outcomes[]` 中 **token_id 的字段名**（下单需要）。
4. `tick_size` 的权威来源（市场字段 or 全局默认）。
5. "我的单" 与 "持仓" 的响应结构（字段名 / 分页）。
6. mainnet API key 的请求头名称。
7. USDT 在 BSC 上的小数位（预期 18）。

> 这些不阻塞设计与文件骨架；在 SP1 实现时逐项对照 OpenAPI/SDK 源码落实，并据实修正归一化映射。

---

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| 无批量取簿 + 240/min 限速 | SP1 仅 REST 取簿够 smoke；高频监控在 SP5 用 WebSocket 解决 |
| 撤单仅按 id（无 cancel-by-market/all） | SP2 用"拉我的单→过滤→分批 remove"组合实现 `cancel_all_asset`/`cancel_all` |
| YES/NO 单簿模型 | SP2 在 backend 内做 swap+complement，对 main.py 透明 |
| OpenAPI 字段与文档/搜索结果有出入 | 第 10 节待办逐项核对；归一化层集中吸收差异 |
| 智能账户 vs EOA 签名差异 | `_sign_message` 内分支；默认 EOA，`PREDICTFUN_ACCOUNT` 非空切智能账户 |
| 误动正在跑的 Polymarket | 所有新增代码在 `PLATFORM=predictfun` 分支内；不改 poly_* 既有文件 |
