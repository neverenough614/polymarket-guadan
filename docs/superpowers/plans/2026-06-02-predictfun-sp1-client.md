# predict.fun 迁移 SP1：PredictFunClient 地基 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `PredictFunClient`（Polymarket `PolymarketClient` 的 BNB 对等物），能在 predict.fun 上鉴权、EIP-712 签名下单、查簿/市场/持仓/我的单、按 id 撤单，返回归一化数据；testnet smoke 脚本跑通完整下单→撤单闭环。

**Architecture:** 分层——`units.py`（纯换算）→ `normalize.py`（纯归一化）→ `rest_api.py`（注入式 HTTP 传输的 REST 层）→ `predictfun_client.py`（组合 SDK 签名 + REST + JWT 鉴权）。所有外部依赖（predict-sdk 的 OrderBuilder、HTTP 传输）通过构造器注入，使纯逻辑可在无网络/无 SDK 环境下单测。Polymarket 代码零改动，predict-sdk 作为可选依赖隔离。

**Tech Stack:** Python ≥3.10、predict-sdk≥0.0.16、eth-account（已有）、requests（已有）、pytest（已有）。

---

## 文件结构

| 文件 | 职责 | 新建/修改 |
|---|---|---|
| `pyproject.toml` | 加 `predictfun` 可选依赖组（predict-sdk），不动主依赖 | 修改 |
| `.env.example` | 记录 predict.fun 所需环境变量 | 新建/修改 |
| `config/bot_config.py` | 加 `PredictFunConfig` dataclass + 网络选择辅助 | 修改 |
| `predictfun_data/__init__.py` | 包初始化 | 新建 |
| `predictfun_data/units.py` | 价格·份额 ↔ wei(18) + tick 取整（纯函数） | 新建 |
| `predictfun_data/normalize.py` | 订单/簿/持仓归一化、撤单 id 分批（纯函数） | 新建 |
| `predictfun_data/rest_api.py` | REST 层 `PredictRest`（注入式 transport + 节流 + 401 重试） | 新建 |
| `predictfun_data/predictfun_client.py` | 主类 `PredictFunClient`（组合签名+REST+鉴权） | 新建 |
| `scripts/predictfun_smoke.py` | testnet 验收脚本（SP1 DoD） | 新建 |
| `tests/test_predictfun_units.py` | units 单测 | 新建 |
| `tests/test_predictfun_normalize.py` | normalize 单测 | 新建 |
| `tests/test_predictfun_rest.py` | REST 层单测（fake transport） | 新建 |
| `tests/test_predictfun_client.py` | client 单测（fake builder + fake rest） | 新建 |

**OpenAPI 待核对点**（实现时对照 `https://api.predict.fun/docs`，仅改对应归一化/body 构造单点）：鉴权端点路径与字段、`POST /v1/orders` body 字段、`outcomes[]` 中 token_id 字段名、tick_size 来源、my-orders/positions 响应结构、mainnet API key 请求头名、USDT 18 位确认。

---

## Task 1: 依赖隔离与环境变量样例

**Files:**
- Modify: `pyproject.toml`
- Create: `.env.example`

- [ ] **Step 1: 在 pyproject.toml 加 predictfun 可选依赖组（不动主依赖）**

修改 `pyproject.toml` 的 `[project.optional-dependencies]` 段为：

```toml
[project.optional-dependencies]
dev = [
    "black==24.4.2",
    "pytest==8.2.2",
]
predictfun = [
    "predict-sdk>=0.0.16",
]
```

> 说明：predict-sdk 需要 Python ≥3.10。Polymarket VPS 环境**不要**安装该组；predict.fun 进程用独立 venv（Python ≥3.10）执行 `pip install -e ".[predictfun]"`。主 `dependencies` 与 `requires-python` 保持不动，避免影响正在运行的 Polymarket。

- [ ] **Step 2: 创建 .env.example 记录 predict.fun 变量**

Create `.env.example`（若已存在则追加 predict.fun 段，不删原有行）：

```
# ===== predict.fun (SP1+) =====
# 平台开关：polymarket | predictfun
PLATFORM=polymarket
# 网络：testnet | mainnet
PREDICTFUN_NETWORK=testnet
# EOA 私钥（必需，0x 开头）
PREDICTFUN_PK=
# 仅 mainnet 必需；testnet 留空
PREDICTFUN_API_KEY=
# 可选：Predict 智能账户地址；留空=EOA 模式
PREDICTFUN_ACCOUNT=
# 价格最小变动单位（待 OpenAPI 确认，默认 0.01）
PREDICTFUN_TICK_SIZE=0.01
# 默认是否 yield-bearing 市场
PREDICTFUN_DEFAULT_YIELD_BEARING=false
```

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml .env.example
git commit -m "chore: predict.fun 可选依赖组与环境变量样例（不影响 Polymarket）"
```

---

## Task 2: PredictFunConfig 配置与网络选择

**Files:**
- Modify: `config/bot_config.py`
- Test: `tests/test_predictfun_client.py`（本任务先建该文件，仅放 config 测试）

- [ ] **Step 1: Write the failing test**

Create `tests/test_predictfun_client.py`:

```python
from config.bot_config import PredictFunConfig


def test_predictfun_config_testnet_endpoint():
    c = PredictFunConfig(network="testnet")
    assert c.base_url == "https://api-testnet.predict.fun"
    assert c.chain_id == 97
    assert c.requires_api_key is False


def test_predictfun_config_mainnet_endpoint():
    c = PredictFunConfig(network="mainnet")
    assert c.base_url == "https://api.predict.fun"
    assert c.chain_id == 56
    assert c.requires_api_key is True


def test_predictfun_config_invalid_network_raises():
    import pytest
    with pytest.raises(ValueError):
        PredictFunConfig(network="devnet")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_predictfun_client.py -v`
Expected: FAIL（`ImportError: cannot import name 'PredictFunConfig'`）

- [ ] **Step 3: Write minimal implementation**

在 `config/bot_config.py` 的 `BotConfig` 定义**之前**插入：

```python
PREDICTFUN_ENDPOINTS = {
    "testnet": {"base_url": "https://api-testnet.predict.fun", "chain_id": 97, "requires_api_key": False},
    "mainnet": {"base_url": "https://api.predict.fun", "chain_id": 56, "requires_api_key": True},
}


@dataclass
class PredictFunConfig:
    """predict.fun 平台配置（BNB 链）。network 决定 URL/链/是否需 API key。"""
    network: str = "testnet"
    tick_size: float = 0.01
    default_yield_bearing: bool = False
    rate_limit_per_min: int = 240

    def __post_init__(self):
        if self.network not in PREDICTFUN_ENDPOINTS:
            raise ValueError(f"未知 network: {self.network}（应为 testnet|mainnet）")
        ep = PREDICTFUN_ENDPOINTS[self.network]
        self.base_url = ep["base_url"]
        self.chain_id = ep["chain_id"]
        self.requires_api_key = ep["requires_api_key"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_predictfun_client.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add config/bot_config.py tests/test_predictfun_client.py
git commit -m "feat: PredictFunConfig 网络/链/鉴权选择"
```

---

## Task 3: 单位换算 units.py

**Files:**
- Create: `predictfun_data/__init__.py`, `predictfun_data/units.py`
- Test: `tests/test_predictfun_units.py`

- [ ] **Step 1: Write the failing test**

Create `predictfun_data/__init__.py`（空文件）。
Create `tests/test_predictfun_units.py`:

```python
from predictfun_data.units import (
    price_to_tick, to_wei, price_per_share_wei, shares_to_wei, from_wei, WEI,
)


def test_wei_constant():
    assert WEI == 10 ** 18


def test_price_to_tick_rounds_to_nearest():
    assert price_to_tick(0.523, 0.01) == 0.52
    assert price_to_tick(0.525, 0.01) == 0.53
    assert price_to_tick(0.4999, 0.01) == 0.50


def test_price_to_tick_clamps_to_unit_interval():
    assert price_to_tick(1.5, 0.01) == 1.0
    assert price_to_tick(-0.2, 0.01) == 0.0


def test_to_wei_rounds_not_floors():
    assert to_wei(1.0) == 10 ** 18
    assert to_wei(0.5) == 5 * 10 ** 17
    # 浮点误差不应系统性少 1
    assert to_wei(0.1) == 10 ** 17


def test_price_per_share_wei_applies_tick():
    assert price_per_share_wei(0.523, 0.01) == 52 * 10 ** 16  # 0.52 * 1e18


def test_shares_to_wei():
    assert shares_to_wei(10) == 10 * 10 ** 18


def test_from_wei_accepts_int_and_str():
    assert from_wei(5 * 10 ** 17) == 0.5
    assert from_wei(str(10 ** 18)) == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_predictfun_units.py -v`
Expected: FAIL（`ModuleNotFoundError: predictfun_data.units`）

- [ ] **Step 3: Write minimal implementation**

Create `predictfun_data/units.py`:

```python
"""价格/份额 ↔ wei(18 位) 换算与 tick 取整（纯函数，无副作用）。"""

WEI = 10 ** 18


def price_to_tick(price: float, tick: float) -> float:
    """把价格量化到最近的 tick，并夹到 [0, 1]。"""
    if tick <= 0:
        raise ValueError("tick 必须 > 0")
    clamped = min(1.0, max(0.0, float(price)))
    steps = round(clamped / tick)
    return round(steps * tick, 10)


def to_wei(amount: float) -> int:
    """float 金额/份额 → wei，四舍五入（避免浮点系统性少 1）。"""
    return int(round(float(amount) * WEI))


def price_per_share_wei(price: float, tick: float) -> int:
    """价格(0~1) 先取 tick 再 → 每股 wei。"""
    return to_wei(price_to_tick(price, tick))


def shares_to_wei(size: float) -> int:
    """份额 → quantity_wei。"""
    return to_wei(size)


def from_wei(x) -> float:
    """wei(int 或 str) → float。"""
    return int(x) / WEI
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_predictfun_units.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add predictfun_data/__init__.py predictfun_data/units.py tests/test_predictfun_units.py
git commit -m "feat: predict.fun 单位换算 units.py"
```

---

## Task 4: 归一化 normalize.py

**Files:**
- Create: `predictfun_data/normalize.py`
- Test: `tests/test_predictfun_normalize.py`

归一化目标：让下游（main.py / SP2 backend）看到与 Polymarket 一致的形状——side 为 `BUY/SELL`、price/size 为 float、订单含 `status`、簿为 `.bids/.asks` 且元素有 `.price/.size`。

- [ ] **Step 1: Write the failing test**

Create `tests/test_predictfun_normalize.py`:

```python
from predictfun_data.normalize import (
    side_to_sdk, side_from_sdk, normalize_order, normalize_orderbook,
    batch_ids, BookLevel, NormalizedBook,
)


def test_side_round_trip():
    assert side_to_sdk("BUY") == 0
    assert side_to_sdk("sell") == 1
    assert side_from_sdk(0) == "BUY"
    assert side_from_sdk(1) == "SELL"


def test_normalize_order_maps_fields():
    raw = {
        "id": "123", "tokenId": "tok", "marketId": 7,
        "side": "Bid", "price": "0.52", "quantity": "100",
        "quantityMatched": "10", "status": "OPEN",
    }
    o = normalize_order(raw)
    assert o["id"] == "123"
    assert o["token_id"] == "tok"
    assert o["market_id"] == 7
    assert o["side"] == "BUY"
    assert o["price"] == 0.52
    assert o["size"] == 100.0
    assert o["size_matched"] == 10.0
    assert o["status"] == "LIVE"


def test_normalize_order_sell_and_nonlive_status():
    o = normalize_order({"id": "1", "side": "Ask", "price": "0.6",
                         "quantity": "5", "status": "CANCELLED"})
    assert o["side"] == "SELL"
    assert o["status"] == "CANCELLED"


def test_normalize_orderbook_yes_terms():
    raw = {"marketId": 7, "bids": [[0.49, 120], [0.48, 80]],
           "asks": [[0.51, 100], [0.52, 60]]}
    book = normalize_orderbook(raw)
    assert isinstance(book, NormalizedBook)
    assert book.market_id == 7
    assert book.bids[0].price == 0.49 and book.bids[0].size == 120.0
    assert book.asks[1].price == 0.52 and book.asks[1].size == 60.0


def test_batch_ids_chunks_by_100():
    ids = [str(i) for i in range(250)]
    chunks = batch_ids(ids, 100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_batch_ids_empty():
    assert batch_ids([], 100) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_predictfun_normalize.py -v`
Expected: FAIL（`ModuleNotFoundError: predictfun_data.normalize`）

- [ ] **Step 3: Write minimal implementation**

Create `predictfun_data/normalize.py`:

```python
"""把 predict.fun REST/SDK 原始数据归一化为下游已在用的形状（纯函数）。

待 OpenAPI 确认的字段名集中在本文件，是平台差异的唯一吸收点。
"""
from dataclasses import dataclass
from typing import Any, Dict, List

from .units import from_wei  # 预留：若金额以 wei 返回则用 from_wei


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class NormalizedBook:
    market_id: Any
    bids: List[BookLevel]
    asks: List[BookLevel]


_SIDE_TO_SDK = {"BUY": 0, "SELL": 1}
# predict.fun 簿/单中 "Bid"=买=BUY，"Ask"=卖=SELL
_RAW_SIDE_TO_CANON = {"BID": "BUY", "BUY": "BUY", "ASK": "SELL", "SELL": "SELL"}


def side_to_sdk(side: str) -> int:
    return _SIDE_TO_SDK[str(side).upper()]


def side_from_sdk(v: int) -> str:
    return "BUY" if int(v) == 0 else "SELL"


def _canon_side(raw_side: str) -> str:
    return _RAW_SIDE_TO_CANON[str(raw_side).upper()]


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_status(raw: str) -> str:
    """OPEN/LIVE → LIVE；其余原样大写。"""
    s = str(raw or "").upper()
    return "LIVE" if s in ("OPEN", "LIVE", "ACTIVE") else s


def normalize_order(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(raw.get("id") or raw.get("orderId") or raw.get("hash") or ""),
        "token_id": str(raw.get("tokenId") or raw.get("token_id") or raw.get("asset_id") or ""),
        "market_id": raw.get("marketId", raw.get("market_id")),
        "side": _canon_side(raw.get("side", "BUY")),
        "price": _f(raw.get("price")),
        "size": _f(raw.get("quantity", raw.get("size"))),
        "size_matched": _f(raw.get("quantityMatched", raw.get("size_matched"))),
        "status": normalize_status(raw.get("status")),
        "raw": raw,
    }


def normalize_orderbook(raw: Dict[str, Any]) -> NormalizedBook:
    def levels(rows):
        out = []
        for row in rows or []:
            # 形如 [price, size]
            p, s = row[0], row[1]
            out.append(BookLevel(_f(p), _f(s)))
        return out
    return NormalizedBook(
        market_id=raw.get("marketId", raw.get("market_id")),
        bids=levels(raw.get("bids")),
        asks=levels(raw.get("asks")),
    )


def batch_ids(ids: List[str], size: int) -> List[List[str]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_predictfun_normalize.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add predictfun_data/normalize.py tests/test_predictfun_normalize.py
git commit -m "feat: predict.fun 数据归一化 normalize.py"
```

---

## Task 5: REST 层 rest_api.py

**Files:**
- Create: `predictfun_data/rest_api.py`
- Test: `tests/test_predictfun_rest.py`

设计：`PredictRest` 接收注入式 `transport(method, url, headers, json_body) -> HttpResp(status_code, json())`，默认用 requests。`jwt_provider` 是返回当前 JWT 的回调；遇 401 调 `on_unauthorized()` 后重试一次。节流为可注入回调，测试用 no-op。

- [ ] **Step 1: Write the failing test**

Create `tests/test_predictfun_rest.py`:

```python
import pytest
from predictfun_data.rest_api import PredictRest, PredictApiError, HttpResp


class FakeTransport:
    def __init__(self, responses):
        # responses: list of (status_code, body_dict)
        self._responses = list(responses)
        self.calls = []  # (method, url, headers, json_body)

    def __call__(self, method, url, headers, json_body):
        self.calls.append((method, url, dict(headers or {}), json_body))
        status, body = self._responses.pop(0)
        return HttpResp(status, body)


def make_rest(transport, **kw):
    return PredictRest(
        base_url="https://api-testnet.predict.fun",
        api_key=None,
        jwt_provider=lambda: "JWT123",
        transport=transport,
        throttle=lambda: None,
        **kw,
    )


def test_get_injects_bearer_and_builds_url():
    t = FakeTransport([(200, {"success": True, "data": {"marketId": 7}})])
    rest = make_rest(t)
    out = rest.get_orderbook(7)
    assert out["data"]["marketId"] == 7
    method, url, headers, _ = t.calls[0]
    assert method == "GET"
    assert url == "https://api-testnet.predict.fun/v1/markets/7/orderbook"
    assert headers["Authorization"] == "Bearer JWT123"


def test_non_2xx_raises_predict_api_error():
    t = FakeTransport([(500, {"success": False, "error": "boom"})])
    rest = make_rest(t)
    with pytest.raises(PredictApiError) as ei:
        rest.get_market(7)
    assert ei.value.status == 500


def test_401_triggers_reauth_then_retries_once():
    t = FakeTransport([(401, {"error": "expired"}), (200, {"success": True, "data": []})])
    reauth_calls = []
    rest = make_rest(t, on_unauthorized=lambda: reauth_calls.append(1))
    out = rest.get_my_orders()
    assert out["success"] is True
    assert len(reauth_calls) == 1
    assert len(t.calls) == 2  # 第一次 401，重鉴后重试


def test_401_twice_raises():
    t = FakeTransport([(401, {}), (401, {})])
    rest = make_rest(t, on_unauthorized=lambda: None)
    with pytest.raises(PredictApiError):
        rest.get_my_orders()


def test_remove_orders_posts_ids_payload():
    t = FakeTransport([(200, {"success": True, "removed": ["1"], "noop": []})])
    rest = make_rest(t)
    out = rest.remove_orders(["1", "2"])
    method, url, _, body = t.calls[0]
    assert method == "POST"
    assert url == "https://api-testnet.predict.fun/v1/orders/remove"
    assert body == {"data": {"ids": ["1", "2"]}}
    assert out["removed"] == ["1"]


def test_mainnet_adds_api_key_header():
    t = FakeTransport([(200, {"success": True, "data": []})])
    rest = PredictRest(
        base_url="https://api.predict.fun", api_key="KEY",
        jwt_provider=lambda: "JWT", transport=t, throttle=lambda: None,
    )
    rest.get_markets()
    _, _, headers, _ = t.calls[0]
    assert headers["X-API-KEY"] == "KEY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_predictfun_rest.py -v`
Expected: FAIL（`ModuleNotFoundError: predictfun_data.rest_api`）

- [ ] **Step 3: Write minimal implementation**

Create `predictfun_data/rest_api.py`:

```python
"""predict.fun REST 薄封装：URL 拼接、Bearer/API-key 注入、401 重鉴重试、节流、错误归一。

transport 为注入点：默认用 requests，测试注入 fake。所有端点路径集中于此。
待 OpenAPI 确认的路径标注 # VERIFY。
"""
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class HttpResp:
    status_code: int
    _body: Dict[str, Any]

    def json(self) -> Dict[str, Any]:
        return self._body


class PredictApiError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"predict.fun API 错误 status={status} body={str(body)[:200]}")


def _requests_transport(method, url, headers, json_body) -> HttpResp:
    import requests
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=10)
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}
    return HttpResp(resp.status_code, body)


class _RateLimiter:
    """简单令牌桶：每分钟 n 次，超额则 sleep。"""
    def __init__(self, per_min: int):
        self._min_interval = 60.0 / max(1, per_min)
        self._last = 0.0

    def __call__(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


class PredictRest:
    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        jwt_provider: Optional[Callable[[], Optional[str]]] = None,
        on_unauthorized: Optional[Callable[[], None]] = None,
        transport: Optional[Callable] = None,
        throttle: Optional[Callable[[], None]] = None,
        rate_limit_per_min: int = 240,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._jwt_provider = jwt_provider or (lambda: None)
        self._on_unauthorized = on_unauthorized or (lambda: None)
        self._transport = transport or _requests_transport
        self._throttle = throttle or _RateLimiter(rate_limit_per_min)

    # ---- 核心请求 ----
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        jwt = self._jwt_provider()
        if jwt:
            h["Authorization"] = f"Bearer {jwt}"
        if self._api_key:
            h["X-API-KEY"] = self._api_key  # VERIFY header 名
        return h

    def _request(self, method: str, path: str, json_body=None, _retried=False) -> Dict[str, Any]:
        self._throttle()
        url = f"{self.base_url}{path}"
        resp = self._transport(method, url, self._headers(), json_body)
        if resp.status_code == 401 and not _retried:
            self._on_unauthorized()
            return self._request(method, path, json_body, _retried=True)
        if not (200 <= resp.status_code < 300):
            raise PredictApiError(resp.status_code, resp.json())
        return resp.json()

    # ---- 鉴权（路径 VERIFY）----
    def get_auth_message(self, address: str) -> Dict[str, Any]:
        return self._request("GET", f"/v1/auth/message?address={address}")  # VERIFY

    def exchange_jwt(self, address: str, signature: str) -> Dict[str, Any]:
        return self._request("POST", "/v1/auth/login", {"address": address, "signature": signature})  # VERIFY

    # ---- 订单 ----
    def create_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/v1/orders", body)

    def remove_orders(self, ids: List[str]) -> Dict[str, Any]:
        return self._request("POST", "/v1/orders/remove", {"data": {"ids": list(ids)}})

    def get_my_orders(self, **params) -> Dict[str, Any]:
        return self._request("GET", "/v1/orders" + _qs(params))  # VERIFY

    # ---- 行情 / 持仓 ----
    def get_markets(self, **params) -> Dict[str, Any]:
        return self._request("GET", "/v1/markets" + _qs(params))

    def get_market(self, market_id) -> Dict[str, Any]:
        return self._request("GET", f"/v1/markets/{market_id}")

    def get_orderbook(self, market_id) -> Dict[str, Any]:
        return self._request("GET", f"/v1/markets/{market_id}/orderbook")

    def get_positions(self, **params) -> Dict[str, Any]:
        return self._request("GET", "/v1/positions" + _qs(params))  # VERIFY


def _qs(params: Dict[str, Any]) -> str:
    items = [(k, v) for k, v in (params or {}).items() if v is not None]
    if not items:
        return ""
    return "?" + "&".join(f"{k}={v}" for k, v in items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_predictfun_rest.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add predictfun_data/rest_api.py tests/test_predictfun_rest.py
git commit -m "feat: predict.fun REST 层（注入式 transport + 401 重鉴 + 节流）"
```

---

## Task 6: 主类 PredictFunClient

**Files:**
- Create: `predictfun_data/predictfun_client.py`
- Test: `tests/test_predictfun_client.py`（追加）

设计：构造器接收可选注入 `builder`、`rest`、`signer`，默认 None 时才构建真实对象（懒导入 predict-sdk）。这样单测无需 predict-sdk/网络。`create_order` 走"算 amounts→build→签→POST→归一化"，失败返回 `{"status":"error"}`。

- [ ] **Step 1: Write the failing test（追加到 tests/test_predictfun_client.py）**

在 `tests/test_predictfun_client.py` 末尾追加：

```python
from predictfun_data.predictfun_client import PredictFunClient


class FakeAmounts:
    maker_amount = 52_000_000
    taker_amount = 100_000_000


class FakeSignedOrder:
    signature = "0xSIG"
    def to_dict(self):
        return {"maker": "0xabc", "makerAmount": "52000000", "takerAmount": "100000000"}


class FakeBuilder:
    def __init__(self):
        self.cancelled = []
    def get_limit_order_amounts(self, _inp):
        return FakeAmounts()
    def build_order(self, _type, _inp):
        return {"order": "obj"}
    def build_typed_data(self, _order, is_neg_risk, is_yield_bearing):
        return {"typed": "data", "is_neg_risk": is_neg_risk, "is_yield_bearing": is_yield_bearing}
    def sign_typed_data_order(self, _typed):
        return FakeSignedOrder()
    def balance_of(self, _sym):
        return 123.5


class FakeRest:
    def __init__(self):
        self.created = []
        self.removed = []
        self._orders = []
    def create_order(self, body):
        self.created.append(body)
        return {"success": True, "data": {"id": "ord1", "hash": "0xh"}}
    def remove_orders(self, ids):
        self.removed.append(list(ids))
        return {"success": True, "removed": list(ids), "noop": []}
    def get_my_orders(self, **kw):
        return {"success": True, "data": self._orders}
    def get_orderbook(self, mid):
        return {"success": True, "data": {"marketId": mid, "bids": [[0.49, 100]], "asks": [[0.51, 80]]}}


def make_client(**kw):
    return PredictFunClient(
        network="testnet",
        builder=FakeBuilder(),
        rest=FakeRest(),
        signer=lambda msg: "0xSIG",
        address="0xMe",
        skip_auth=True,
        **kw,
    )


def test_create_order_builds_signed_body_and_normalizes():
    c = make_client()
    out = c.create_order("tok", "BUY", 0.523, 100, neg_risk=False, is_yield_bearing=False)
    assert out["status"] == "live"
    assert out["order_id"] == "ord1"
    body = c.rest.created[0]
    # body 必含签名与 typed data（字段名 VERIFY，但结构存在）
    assert body["signature"] == "0xSIG"


def test_create_order_error_returns_error_status():
    c = make_client()
    def boom(body):
        raise RuntimeError("rejected")
    c.rest.create_order = boom
    out = c.create_order("tok", "BUY", 0.5, 100)
    assert out["status"] == "error"
    assert "rejected" in out["error"]


def test_get_open_orders_normalizes():
    c = make_client()
    c.rest._orders = [{"id": "1", "tokenId": "tok", "side": "Bid",
                       "price": "0.5", "quantity": "10", "status": "OPEN"}]
    orders = c.get_open_orders()
    assert orders[0]["side"] == "BUY"
    assert orders[0]["status"] == "LIVE"
    assert orders[0]["price"] == 0.5


def test_remove_orders_batches_over_100():
    c = make_client()
    ids = [str(i) for i in range(150)]
    c.remove_orders(ids)
    assert [len(b) for b in c.rest.removed] == [100, 50]


def test_get_usdt_balance_delegates_to_builder():
    c = make_client()
    assert c.get_usdt_balance() == 123.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_predictfun_client.py -v`
Expected: FAIL（`ModuleNotFoundError: predictfun_data.predictfun_client`）

- [ ] **Step 3: Write minimal implementation**

Create `predictfun_data/predictfun_client.py`:

```python
"""PredictFunClient — Polymarket PolymarketClient 的 BNB 对等物。

依赖（OrderBuilder / PredictRest / signer）可注入，默认构建真实对象（懒导入 predict-sdk）。
对外方法面刻意贴近 PolymarketClient，便于 SP2 的 IExecutionBackend 包装最薄。
"""
import os
import time
from typing import Any, Callable, Dict, List, Optional

from config.bot_config import PredictFunConfig
from .normalize import normalize_order, normalize_orderbook, batch_ids
from . import units


class PredictFunClient:
    def __init__(
        self,
        network: Optional[str] = None,
        builder: Any = None,
        rest: Any = None,
        signer: Optional[Callable[[Any], str]] = None,
        address: Optional[str] = None,
        skip_auth: bool = False,
    ):
        self.cfg = PredictFunConfig(network=network or os.getenv("PREDICTFUN_NETWORK", "testnet"))
        self._yield_default = self.cfg.default_yield_bearing

        # 依赖注入：测试传 fake；生产为 None 时构建真实对象
        self._builder = builder if builder is not None else self._build_real_builder()
        self.address = address or self._resolve_address()
        self._signer = signer or self._default_signer
        self.rest = rest if rest is not None else self._build_real_rest()

        self._jwt: Optional[str] = None
        self._jwt_exp: float = 0.0
        if not skip_auth:
            self.authenticate()

    # ---- 真实依赖构建（懒导入，测试路径不触发）----
    def _build_real_builder(self):
        from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions
        pk = os.getenv("PREDICTFUN_PK")
        if not pk:
            raise RuntimeError("缺少 PREDICTFUN_PK（fail fast）")
        if self.cfg.requires_api_key and not os.getenv("PREDICTFUN_API_KEY"):
            raise RuntimeError("mainnet 缺少 PREDICTFUN_API_KEY（fail fast）")
        chain = ChainId.BNB_MAINNET if self.cfg.network == "mainnet" else ChainId.BNB_TESTNET
        account = os.getenv("PREDICTFUN_ACCOUNT") or None
        if account:
            return OrderBuilder.make(chain, pk, OrderBuilderOptions(predict_account=account))
        return OrderBuilder.make(chain, pk)

    def _build_real_rest(self):
        from .rest_api import PredictRest
        return PredictRest(
            base_url=self.cfg.base_url,
            api_key=os.getenv("PREDICTFUN_API_KEY") if self.cfg.requires_api_key else None,
            jwt_provider=self._ensure_jwt,
            on_unauthorized=self.authenticate,
            rate_limit_per_min=self.cfg.rate_limit_per_min,
        )

    def _resolve_address(self) -> str:
        account = os.getenv("PREDICTFUN_ACCOUNT")
        if account:
            return account
        from eth_account import Account
        return Account.from_key(os.environ["PREDICTFUN_PK"]).address

    def _default_signer(self, message: Any) -> str:
        # EOA：eth_account 签名；smart account：builder.sign_predict_account_message
        if os.getenv("PREDICTFUN_ACCOUNT"):
            return self._builder.sign_predict_account_message(message)
        from eth_account import Account
        from eth_account.messages import encode_defunct
        acct = Account.from_key(os.environ["PREDICTFUN_PK"])
        signable = encode_defunct(text=message if isinstance(message, str) else str(message))
        return acct.sign_message(signable).signature.hex()

    # ---- 鉴权 ----
    def authenticate(self) -> None:
        msg_resp = self.rest.get_auth_message(self.address)
        message = msg_resp.get("data", msg_resp).get("message", msg_resp.get("message"))  # VERIFY
        signature = self._signer(message)
        tok_resp = self.rest.exchange_jwt(self.address, signature)
        data = tok_resp.get("data", tok_resp)
        self._jwt = data.get("token") or data.get("jwt")  # VERIFY
        # 过期时间：缺省给 1 小时安全窗
        exp = data.get("expiresAt") or data.get("exp")
        self._jwt_exp = float(exp) if exp else time.time() + 3600

    def _ensure_jwt(self) -> Optional[str]:
        if self._jwt is None or time.time() >= self._jwt_exp - 30:
            self.authenticate()
        return self._jwt

    # ---- 下单 ----
    def create_order(self, token_id, side, price, size, neg_risk=False,
                     is_yield_bearing=None, order_type="LIMIT") -> Dict[str, Any]:
        yb = self._yield_default if is_yield_bearing is None else is_yield_bearing
        try:
            from predict_sdk import Side, BuildOrderInput, LimitHelperInput
            sdk_side = Side.BUY if str(side).upper() == "BUY" else Side.SELL
            amounts = self._builder.get_limit_order_amounts(LimitHelperInput(
                side=sdk_side,
                price_per_share_wei=units.price_per_share_wei(price, self.cfg.tick_size),
                quantity_wei=units.shares_to_wei(size),
            ))
            order = self._builder.build_order(order_type, BuildOrderInput(
                side=sdk_side,
                token_id=str(token_id),
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=0,  # VERIFY: 优先用市场 feeRateBps
            ))
            typed = self._builder.build_typed_data(order, is_neg_risk=neg_risk, is_yield_bearing=yb)
            signed = self._builder.sign_typed_data_order(typed)
            body = self._order_body(signed, order_type, neg_risk, yb)
            resp = self.rest.create_order(body)
            data = resp.get("data", resp)
            return {"status": "live", "order_id": str(data.get("id") or data.get("hash") or ""),
                    "hash": data.get("hash"), "raw": resp}
        except ImportError:
            raise
        except Exception as e:  # 对齐 PolymarketClient：失败不抛裸异常
            return {"status": "error", "error": str(e)}

    def _order_body(self, signed, order_type, neg_risk, yield_bearing) -> Dict[str, Any]:
        # VERIFY: 确切 body 字段对照 OpenAPI；此处为单点修正位置
        order_obj = signed.to_dict() if hasattr(signed, "to_dict") else dict(getattr(signed, "__dict__", {}))
        return {
            "data": {
                "order": order_obj,
                "signature": signed.signature,
                "orderType": order_type,
                "isNegRisk": neg_risk,
                "isYieldBearing": yield_bearing,
            }
        }

    # ---- 查询（归一化）----
    def get_orderbook(self, market_id):
        resp = self.rest.get_orderbook(market_id)
        return normalize_orderbook(resp.get("data", resp))

    def get_markets(self, **filters) -> List[Dict[str, Any]]:
        resp = self.rest.get_markets(**filters)
        return resp.get("data", resp) or []

    def get_market(self, market_id) -> Dict[str, Any]:
        resp = self.rest.get_market(market_id)
        return resp.get("data", resp)

    def get_open_orders(self, market_id=None) -> List[Dict[str, Any]]:
        params = {"marketId": market_id} if market_id else {}
        resp = self.rest.get_my_orders(**params)
        rows = resp.get("data", resp) or []
        return [normalize_order(r) for r in rows]

    def get_positions(self) -> List[Dict[str, Any]]:
        resp = self.rest.get_positions()
        return resp.get("data", resp) or []

    # ---- 撤单 ----
    def remove_orders(self, ids: List[str]) -> Dict[str, Any]:
        removed, noop = [], []
        for chunk in batch_ids([str(i) for i in ids if i], 100):
            r = self.rest.remove_orders(chunk)
            removed.extend(r.get("removed", []))
            noop.extend(r.get("noop", []))
        return {"success": True, "removed": removed, "noop": noop}

    # ---- 资金 / 授权 / 仓位 ----
    def get_usdt_balance(self) -> float:
        return float(self._builder.balance_of("USDT"))

    def set_approvals(self) -> Any:
        return self._builder.set_approvals(is_yield_bearing=self._yield_default)

    def merge_positions(self, condition_id, amount, is_neg_risk=False, is_yield_bearing=None):
        yb = self._yield_default if is_yield_bearing is None else is_yield_bearing
        return self._builder.merge_positions(condition_id=condition_id, amount=amount,
                                             is_neg_risk=is_neg_risk, is_yield_bearing=yb)

    def redeem_positions(self, condition_id, index_set, is_neg_risk=False,
                         is_yield_bearing=None, amount=None):
        yb = self._yield_default if is_yield_bearing is None else is_yield_bearing
        kw = dict(condition_id=condition_id, index_set=index_set,
                  is_neg_risk=is_neg_risk, is_yield_bearing=yb)
        if amount is not None:
            kw["amount"] = amount
        return self._builder.redeem_positions(**kw)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_predictfun_client.py -v`
Expected: PASS（config 3 个 + client 5 个 = 8 passed）

- [ ] **Step 5: Commit**

```bash
git add predictfun_data/predictfun_client.py tests/test_predictfun_client.py
git commit -m "feat: PredictFunClient 主类（签名下单/查询/撤单/归一化，依赖注入可测）"
```

---

## Task 7: testnet 验收脚本 predictfun_smoke.py

**Files:**
- Create: `scripts/predictfun_smoke.py`

这是 SP1 的 DoD，手动在 testnet 跑（需 `pip install -e ".[predictfun]"` + 配好 `.env`）。

- [ ] **Step 1: 创建 smoke 脚本**

Create `scripts/predictfun_smoke.py`:

```python
"""predict.fun SP1 验收：testnet 跑通 鉴权→取市场+簿→挂极小单→查到→撤掉。

用法：
  PLATFORM=predictfun PREDICTFUN_NETWORK=testnet python scripts/predictfun_smoke.py
前置：pip install -e ".[predictfun]"；.env 配好 PREDICTFUN_PK。
"""
import sys
import time

from predictfun_data.predictfun_client import PredictFunClient


def main() -> int:
    c = PredictFunClient(network="testnet")
    print(f"[1] 鉴权 OK，address={c.address}")

    markets = c.get_markets(status="OPEN", first=5)
    if not markets:
        print("✗ 没取到 OPEN 市场"); return 1
    m = markets[0]
    market_id = m.get("id")
    print(f"[2] 取到市场 id={market_id} q={str(m.get('question'))[:50]}")

    book = c.get_orderbook(market_id)
    print(f"[3] 簿：bids={len(book.bids)} asks={len(book.asks)} "
          f"best_bid={book.bids[0].price if book.bids else None} "
          f"best_ask={book.asks[0].price if book.asks else None}")

    # 取 YES outcome 的 token_id（字段名以实际为准；VERIFY）
    outcomes = m.get("outcomes") or []
    token_id = (outcomes[0].get("tokenId") if outcomes else None)
    if not token_id:
        print("✗ 未找到 outcome token_id（对照 OpenAPI 调整）"); return 1

    # 挂一张远离 mid 的极小买单（避免成交）：价 0.01，量 5
    place = c.create_order(token_id, "BUY", 0.01, 5,
                           neg_risk=bool(m.get("isNegRisk")),
                           is_yield_bearing=bool(m.get("isYieldBearing")))
    print(f"[4] 下单结果：{place}")
    if place.get("status") != "live":
        print("✗ 下单失败"); return 1

    time.sleep(2)
    mine = c.get_open_orders(market_id=market_id)
    print(f"[5] 我的单：{[o['id'] for o in mine]}")
    ids = [o["id"] for o in mine] or [place["order_id"]]

    rm = c.remove_orders(ids)
    print(f"[6] 撤单：removed={rm['removed']} noop={rm['noop']}")

    time.sleep(2)
    left = c.get_open_orders(market_id=market_id)
    print(f"[7] 撤后剩余我的单：{[o['id'] for o in left]}")
    print("✓ SP1 smoke 通过" if not left else "⚠ 仍有残留单，检查撤单逻辑")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 静态自检（不打网络，确认可导入、无语法错）**

Run: `python -c "import ast; ast.parse(open('scripts/predictfun_smoke.py', encoding='utf-8').read()); print('ok')"`
Expected: 输出 `ok`

- [ ] **Step 3: Commit**

```bash
git add scripts/predictfun_smoke.py
git commit -m "feat: predict.fun SP1 testnet 验收脚本 smoke"
```

- [ ] **Step 4:（人工，需 testnet 资金）跑 smoke**

Run: `pip install -e ".[predictfun]"` 后配好 `.env`，再 `PLATFORM=predictfun python scripts/predictfun_smoke.py`
Expected: 打印 [1]~[7] 且末行 `✓ SP1 smoke 通过`。若某步字段名不符，按脚本内 `VERIFY` 注释对照 `https://api.predict.fun/docs` 修正 `normalize.py` / `rest_api.py` / `_order_body`。

---

## Task 8: 全量回归与收尾

- [ ] **Step 1: 跑全部新单测**

Run: `pytest tests/test_predictfun_units.py tests/test_predictfun_normalize.py tests/test_predictfun_rest.py tests/test_predictfun_client.py -v`
Expected: 全 PASS（8+6+6+8 = 28 passed）

- [ ] **Step 2: 确认未碰 Polymarket 测试**

Run: `pytest tests/test_order_idempotency.py tests/test_small_edge_strategy.py -q`
Expected: 与迁移前一致（无新失败）。

- [ ] **Step 3: Commit（如有收尾改动）**

```bash
git add -A
git commit -m "test: predict.fun SP1 全量单测回归" || echo "无改动"
```

---

## Self-Review 记录

- **Spec 覆盖**：①鉴权(Task6 authenticate/_ensure_jwt)②签名下单+wei(Task3+6 create_order)③查簿/市场/持仓/我的单(Task6, 归一化 Task4)④撤单按 id 分批(Task4 batch_ids + Task6 remove_orders)⑤testnet/mainnet 配置(Task2)⑥EOA/smart account 双签(Task6 _default_signer)⑦健壮性 401/节流/fail-fast(Task5/6)⑧独立隔离不动 Polymarket(Task1 可选依赖 + Task8 step2)⑨smoke 验收(Task7)。spec 第 10 节 VERIFY 项已在代码中以 `# VERIFY` 标注为单点修正位置。
- **占位符扫描**：无 TBD/TODO；所有步骤含完整代码与命令。VERIFY 注释是有意的实现期对照点，非占位。
- **类型一致性**：`normalize_order`/`normalize_orderbook`/`batch_ids`/`PredictFunConfig`/`PredictRest`/`PredictFunClient` 方法签名在各 task 间一致；side 规范统一 `BUY/SELL`；订单字段统一 `id/token_id/market_id/side/price/size/size_matched/status`。
