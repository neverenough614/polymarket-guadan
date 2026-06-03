"""PredictFunClient — Polymarket PolymarketClient 的 BNB 对等物。

依赖（OrderBuilder / PredictRest / signer）可注入，默认构建真实对象（懒导入 predict-sdk）。
对外方法面刻意贴近 PolymarketClient，便于 SP2 的 IExecutionBackend 包装最薄。
"""
import os
import time
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

from config.bot_config import PredictFunConfig

# 进程启动即加载 .env（与 poly_data/polymarket_client.py 行为一致），
# 使 PREDICTFUN_PK / _API_KEY / _ACCOUNT 等可从 .env 读取。
load_dotenv()
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
        self._authenticating: bool = False
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
        sig = acct.sign_message(signable).signature.hex()
        # 服务端验签要求 0x 前缀；hexbytes>=1 的 .hex() 不带前缀，这里兜底补上
        return sig if sig.startswith("0x") else "0x" + sig

    # ---- 鉴权 ----
    def authenticate(self) -> None:
        self._authenticating = True
        try:
            msg_resp = self.rest.get_auth_message()
            message = msg_resp.get("data", msg_resp).get("message")
            signature = self._signer(message)
            # signer 字段：EOA 模式=签名地址；智能账户模式=智能账户地址（self.address 已据此解析）
            tok_resp = self.rest.exchange_jwt(self.address, message, signature)
            data = tok_resp.get("data", tok_resp)
            self._jwt = data.get("token") or data.get("jwt")
            if not self._jwt:
                raise RuntimeError(f"authenticate: 响应中未找到 token，fields={list(data.keys())}")
            # 过期时间：缺省给 1 小时安全窗
            exp = data.get("expiresAt") or data.get("exp")
            self._jwt_exp = float(exp) if exp else time.time() + 3600
        finally:
            self._authenticating = False

    def _ensure_jwt(self) -> Optional[str]:
        if self._authenticating:
            return self._jwt
        if self._jwt is None or time.time() >= self._jwt_exp - 30:
            self.authenticate()
        return self._jwt

    # ---- 下单 ----
    def create_order(self, token_id, side, price, size, neg_risk=False,
                     is_yield_bearing=None, order_type="LIMIT", fee_rate_bps=0) -> Dict[str, Any]:
        yb = self._yield_default if is_yield_bearing is None else is_yield_bearing
        try:
            sdk_side, LimitHelperInput, BuildOrderInput = self._sdk_order_types(side)
            pps_wei = units.price_per_share_wei(price, self.cfg.tick_size)
            amounts = self._builder.get_limit_order_amounts(LimitHelperInput(
                side=sdk_side,
                price_per_share_wei=pps_wei,
                quantity_wei=units.shares_to_wei(size),
            ))
            order = self._builder.build_order(order_type, BuildOrderInput(
                side=sdk_side,
                token_id=str(token_id),
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=int(fee_rate_bps),  # 必须用市场要求的 feeRateBps（签进订单），否则被拒
            ))
            typed = self._builder.build_typed_data(order, is_neg_risk=neg_risk, is_yield_bearing=yb)
            signed = self._builder.sign_typed_data_order(typed)
            order_hash = self._builder.build_typed_data_hash(typed)  # SignedOrder.hash 为 None，需单独算
            strategy = "LIMIT" if str(order_type).upper() == "LIMIT" else "MARKET"
            body = self._order_body(signed, order_hash, pps_wei, strategy)
            resp = self.rest.create_order(body)
            data = resp.get("data", resp)
            return {"status": "live",
                    "order_id": str(data.get("id") or data.get("orderId") or data.get("orderHash") or ""),
                    "hash": data.get("hash") or data.get("orderHash"), "raw": resp}
        except Exception as e:  # 对齐 PolymarketClient：失败不抛裸异常
            return {"status": "error", "error": str(e)}

    @staticmethod
    def _sdk_order_types(side: str):
        """Return (sdk_side, LimitHelperInput, BuildOrderInput).

        Try to import from predict_sdk; if unavailable (test environment with injected
        fake builder), fall back to simple namespaces — the fake builder accepts anything.
        """
        side_str = str(side).upper()
        try:
            from predict_sdk import Side, BuildOrderInput, LimitHelperInput  # VERIFY: exact names
            sdk_side = Side.BUY if side_str == "BUY" else Side.SELL
            return sdk_side, LimitHelperInput, BuildOrderInput
        except ImportError:
            # predict_sdk not installed → use plain namespace objects (only valid with injected fakes)
            class _NS:
                def __init__(self, **kw):
                    self.__dict__.update(kw)
            sdk_side = "BUY" if side_str == "BUY" else "SELL"
            return sdk_side, _NS, _NS

    def _order_body(self, signed, order_hash, price_per_share_wei, strategy) -> Dict[str, Any]:
        # ContractOrder 字段名对齐 EIP-712 ORDER_STRUCTURE（camelCase）；uint256 用字符串，side/signatureType 用整数
        order_obj = {
            "salt": str(signed.salt),
            "maker": signed.maker,
            "signer": signed.signer,
            "taker": signed.taker,
            "tokenId": str(signed.token_id),
            "makerAmount": str(signed.maker_amount),
            "takerAmount": str(signed.taker_amount),
            "expiration": str(signed.expiration),
            "nonce": str(signed.nonce),
            "feeRateBps": str(signed.fee_rate_bps),
            "side": int(signed.side),
            "signatureType": int(signed.signature_type),
            "signature": signed.signature,
            "hash": order_hash,
        }
        return {"data": {"order": order_obj, "pricePerShare": str(price_per_share_wei), "strategy": strategy}}

    # ---- 查询（归一化）----
    def get_orderbook(self, market_id):
        resp = self.rest.get_orderbook(market_id)
        return normalize_orderbook(resp.get("data", resp))

    def get_markets(self, **filters) -> List[Dict[str, Any]]:
        resp = self.rest.get_markets(**filters)
        return resp.get("data", resp) or []

    def get_all_markets(self, status: str = "OPEN", page_size: int = 100,
                        max_pages: int = 300, **filters) -> List[Dict[str, Any]]:
        """游标分页拉取全部市场（predict.fun 用 `after`=cursor 翻页）。

        max_pages 是失控保护；cursor 为空/不再变化即自然停止。
        若耗尽 max_pages 但游标仍在（说明被截断）会打印告警，不静默吞掉剩余市场。
        """
        out: List[Dict[str, Any]] = []
        cursor = None
        seen_cursors = set()
        for _ in range(max(1, max_pages)):
            params = {"status": status, "first": page_size, **filters}
            if cursor:
                params["after"] = cursor
            resp = self.rest.get_markets(**params)
            if isinstance(resp, list):
                rows, cursor = resp, None
            elif isinstance(resp, dict):
                rows, cursor = (resp.get("data") or []), resp.get("cursor")
            else:
                rows, cursor = [], None
            if not rows:
                break
            out.extend(rows)
            if not cursor or cursor in seen_cursors:
                cursor = None       # 自然耗尽（游标空/重复）
                break
            seen_cursors.add(cursor)
        if cursor:  # 退出循环时游标仍在 → 触达 max_pages 上限，未覆盖全量
            print(f"⚠️ [get_all_markets] 达到 max_pages={max_pages} 上限仍有后续游标，"
                  f"已拉取 {len(out)} 个市场但可能未覆盖全部。如需更全请调高 max_pages。")
        return out

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
        removed, noop, errors = [], [], []
        clean = [str(i) for i in ids if str(i).strip()]
        for chunk in batch_ids(clean, 100):
            try:
                r = self.rest.remove_orders(chunk)
                removed.extend(r.get("removed", []))
                noop.extend(r.get("noop", []))
            except Exception as e:
                errors.append({"ids": chunk, "error": str(e)})
        return {"success": not errors, "removed": removed, "noop": noop, "errors": errors}

    # ---- 资金 / 授权 / 仓位 ----
    def get_usdt_balance(self) -> float:
        # 智能账户模式下资金在 self.address（PREDICTFUN_ACCOUNT）；EOA 模式即签名地址
        # balance_of 返回原始 wei(18 位)，转成人类可读的 USDT
        return units.from_wei(self._builder.balance_of("USDT", address=self.address))

    def set_approvals(self, is_yield_bearing=None) -> Any:
        yb = self._yield_default if is_yield_bearing is None else is_yield_bearing
        return self._builder.set_approvals(is_yield_bearing=yb)

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
