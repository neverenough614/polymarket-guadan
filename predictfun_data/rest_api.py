"""predict.fun REST 薄封装：URL 拼接、Bearer/API-key 注入、401 重鉴重试、节流、错误归一。

transport 为注入点：默认用 requests，测试注入 fake。所有端点路径集中于此。
待 OpenAPI 确认的路径标注 # VERIFY。
"""
import time
import urllib.parse
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
    """固定速率节流：保证请求间最小间隔 = 60/per_min 秒。"""
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
        return self._request("GET", f"/v1/auth/message?address={urllib.parse.quote(str(address))}")  # VERIFY

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
    return ("?" + urllib.parse.urlencode(items)) if items else ""
