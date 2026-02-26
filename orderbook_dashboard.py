import threading
import asyncio
from typing import List, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import pandas as pd
import traceback

# 复用你项目里的模块
from poly_data.polymarket_client import PolymarketClient
from poly_data.data_utils import update_markets, update_positions, update_orders
from poly_data.websocket_handlers import connect_market_websocket
import poly_data.global_state as global_state

# ======================
# 1. FastAPI 应用
# ======================

app = FastAPI(title="Poly-Maker Orderbook Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_top_of_book(price_dict: Dict[float, float], depth: int, reverse: bool) -> List[Dict]:
    """
    从 bids/asks 的 dict 里，按价格排序取前 depth 档。
    reverse=True -> 从高到低（bids）
    reverse=False -> 从低到高（asks）
    """
    if not price_dict:
        return []

    items = []
    for p, s in price_dict.items():
        try:
            p_f = float(p)
            s_f = float(s)
        except (TypeError, ValueError):
            continue
        if s_f > 0:
            items.append((p_f, s_f))

    items.sort(key=lambda x: x[0], reverse=reverse)
    top = items[:depth]
    return [{"price": p, "size": s} for p, s in top]


# ======================
# 2. 只给 Dashboard 用的全量市场加载函数
# ======================

def _extract_tokens_from_market(m: Dict[str, Any]) -> List[str]:
    """
    尝试从一个 market 字典里提取所有相关 token id。
    不知道你市场结构的精确字段名，所以做了多种 fallback。
    """
    tokens = []

    # 常见字段 1：直接有 token1 / token2
    for key in ["token1", "token2"]:
        if key in m and m[key]:
            tokens.append(str(m[key]))

    # 常见字段 2：有 outcomes 或 tokens 列表
    for key in ["outcomes", "tokens", "assets"]:
        if key in m and isinstance(m[key], list):
            for t in m[key]:
                # 可能是字符串，也可能是 dict
                if isinstance(t, str):
                    tokens.append(t)
                elif isinstance(t, dict):
                    # 尝试从里面拿 tokenId / id
                    for tk in ["tokenId", "token_id", "id"]:
                        if tk in t and t[tk]:
                            tokens.append(str(t[tk]))

    # 去重
    return list({t for t in tokens if t})


def _markets_to_df(markets: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    把 PolymarketClient 返回的 markets list 转成 DataFrame，
    尽可能构造 question / answer1 / answer2 / token1 / token2 这些列。
    """
    if not markets:
        return pd.DataFrame()

    df = pd.DataFrame(markets)

    # question 列：如果没有，用 title 或 question 字段
    if "question" not in df.columns:
        if "title" in df.columns:
            df["question"] = df["title"]
        else:
            df["question"] = ""

    # answer1 / answer2：如果有 outcomes，用前两个；否则留空
    if "answer1" not in df.columns or "answer2" not in df.columns:
        answers1 = []
        answers2 = []
        for _, row in df.iterrows():
            a1 = ""
            a2 = ""
            if "outcomes" in row and isinstance(row["outcomes"], list) and len(row["outcomes"]) >= 2:
                # outcomes 可能是字符串列表
                if isinstance(row["outcomes"][0], str):
                    a1 = row["outcomes"][0]
                    a2 = row["outcomes"][1]
                # 也可能是 dict 列表
                elif isinstance(row["outcomes"][0], dict):
                    a1 = row["outcomes"][0].get("name", "")
                    a2 = row["outcomes"][1].get("name", "")
            answers1.append(a1)
            answers2.append(a2)
        if "answer1" not in df.columns:
            df["answer1"] = answers1
        if "answer2" not in df.columns:
            df["answer2"] = answers2

    # token1 / token2：优先用每行的 outcomes/tokens 中的前两个 token
    token1_list = []
    token2_list = []
    for _, row in df.iterrows():
        m = row.to_dict()
        ts = _extract_tokens_from_market(m)
        t1 = ts[0] if len(ts) > 0 else None
        t2 = ts[1] if len(ts) > 1 else None
        token1_list.append(t1)
        token2_list.append(t2)

    df["token1"] = token1_list
    df["token2"] = token2_list

    return df


def load_all_markets_for_dashboard():
    """
    直接通过 PolymarketClient 拉“全量市场”，
    在本文件里构造一个给 dashboard 用的 df 和 all_tokens。
    """
    try:
        if not getattr(global_state, "client", None):
            print("[Dashboard] global_state.client is None in load_all_markets_for_dashboard")
            global_state.df = pd.DataFrame()
            global_state.all_tokens = []
            return

        client = global_state.client
        print("[Dashboard] Fetching ALL markets for dashboard view via PolymarketClient...")

        # 先打印这个 client 拥有哪些方法，方便我们看应该用哪个
        print("[Dashboard] PolymarketClient available methods:")
        for name in dir(client):
            if not name.startswith("_"):
                attr = getattr(client, name)
                if callable(attr):
                    print("   -", name)

        # 尝试用几个常见的方法名（你项目里可能是其中一个）
        markets = None
        tried_methods = []

        for method_name in ["get_markets", "list_markets", "get_all_markets"]:
            if hasattr(client, method_name):
                tried_methods.append(method_name)
                try:
                    print(f"[Dashboard] Trying client.{method_name}() ...")
                    candidate = getattr(client, method_name)()
                    if candidate:
                        markets = candidate
                        print(f"[Dashboard] client.{method_name}() returned {len(candidate)} items.")
                        break
                    else:
                        print(f"[Dashboard] client.{method_name}() returned empty.")
                except Exception as e:
                    print(f"[Dashboard] ERROR calling client.{method_name}():", e)
                    traceback.print_exc()

        if markets is None:
            print("[Dashboard] ERROR: None of the tried methods produced markets.")
            print("[Dashboard] Tried methods:", tried_methods or "[]")
            global_state.df = pd.DataFrame()
            global_state.all_tokens = []
            return

        # 转成 DataFrame 并提取 token1/token2
        df = _markets_to_df(markets)

        if df.empty:
            print("[Dashboard] WARNING: _markets_to_df produced empty DataFrame.")
            global_state.df = pd.DataFrame()
            global_state.all_tokens = []
            return

        print("[Dashboard] final df shape for dashboard:", df.shape)
        print("[Dashboard] df columns:", list(df.columns))

        tokens = []
        if "token1" in df.columns:
            tokens.extend(df["token1"].dropna().astype(str).tolist())
        if "token2" in df.columns:
            tokens.extend(df["token2"].dropna().astype(str).tolist())

        if not tokens:
            print("[Dashboard] WARNING: token1/token2 columns exist but no tokens collected.")

        global_state.df = df
        global_state.all_tokens = sorted(set(tokens))

        print(
            f"[Dashboard] Loaded ALL markets for dashboard: df shape={df.shape}, "
            f"tokens={len(global_state.all_tokens)}"
        )

    except Exception as e:
        print("[Dashboard] ERROR in load_all_markets_for_dashboard:", e)
        traceback.print_exc()
        global_state.df = pd.DataFrame()
        global_state.all_tokens = []


# ======================
# 3. API 路由
# ======================

@app.get("/markets")
def list_markets():
    """
    返回所有可选的 asset（每个 answer 视为一个 asset），给前端下拉框用。
    """
    if not hasattr(global_state, "df") or global_state.df is None:
        raise HTTPException(status_code=500, detail="Markets not loaded yet")

    df = global_state.df
    print(f"[/markets] df shape = {getattr(df, 'shape', None)}")
    print(f"[/markets] df columns = {list(getattr(df, 'columns', []))}")

    markets = []
    for idx, row in df.iterrows():
        try:
            question = row.get("question", "")
            answer1 = row.get("answer1", "token1")
            answer2 = row.get("answer2", "token2")
            token1 = str(row.get("token1"))
            token2 = str(row.get("token2"))

            if token1 and token1 != "nan":
                markets.append(
                    {
                        "asset_id": token1,
                        "label": f"{question} - {answer1}",
                    }
                )
            if token2 and token2 != "nan":
                markets.append(
                    {
                        "asset_id": token2,
                        "label": f"{question} - {answer2}",
                    }
                )
        except Exception as e_row:
            print(f"[/markets] error while processing row {idx}: {e_row}")
            traceback.print_exc()
            continue

    # 去重
    seen = set()
    unique_markets = []
    for m in markets:
        key = m["asset_id"]
        if key not in seen:
            seen.add(key)
            unique_markets.append(m)

    print(f"[/markets] returning {len(unique_markets)} unique markets")
    return unique_markets


@app.get("/orderbook/{asset_id}")
def get_orderbook(asset_id: str, depth: int = 10):
    """
    返回指定 asset_id 的订单簿前 depth 档（bids / asks）。
    """
    if not hasattr(global_state, "all_data") or global_state.all_data is None:
        raise HTTPException(status_code=500, detail="Orderbook not loaded yet")

    print(f"[/orderbook] requested asset_id={asset_id}")
    ob = global_state.all_data.get(asset_id)
    if ob is None:
        print(f"[/orderbook] no entry in all_data for {asset_id}")
        raise HTTPException(status_code=404, detail=f"No orderbook for asset_id={asset_id}")

    bids = ob.get("bids", {})
    asks = ob.get("asks", {})
    print(f"[/orderbook] raw bids={len(bids)}, asks={len(asks)}")

    top_bids = _get_top_of_book(bids, depth=depth, reverse=True)
    top_asks = _get_top_of_book(asks, depth=depth, reverse=False)

    print(f"[/orderbook] top_bids={len(top_bids)}, top_asks={len(top_asks)}")

    return {
        "asset_id": asset_id,
        "bids": top_bids,
        "asks": top_asks,
    }


# ======================
# 4. WebSocket 抓盘口的后台线程
# ======================

def init_state_and_markets():
    """
    初始化 PolymarketClient / markets / positions / orders。
    先跑原来的 update_*，再单独为 dashboard 拉全量 markets。
    """
    try:
        print("[Dashboard] Initializing Polymarket client...")
        global_state.client = PolymarketClient()
    except Exception as e:
        print("[Dashboard] ERROR creating PolymarketClient:", e)
        traceback.print_exc()
        global_state.client = None
        global_state.df = pd.DataFrame()
        global_state.all_tokens = []
        return

    # 原项目里的更新逻辑（给策略 / Google 表用）
    try:
        update_markets()
        update_positions()
        update_orders()
    except Exception as e:
        print("[Dashboard] ERROR while update_markets/positions/orders:", e)
        traceback.print_exc()

    # 再单独为 dashboard 覆盖 df 和 all_tokens —— 这里就是 ALL MARKETS
    load_all_markets_for_dashboard()

    # 安全统计数量
    df = getattr(global_state, "df", None)
    if df is None:
        n_markets = 0
    else:
        try:
            n_markets = len(df)
        except Exception:
            n_markets = 0

    positions = getattr(global_state, "positions", None)
    n_positions = len(positions) if positions is not None else 0

    orders = getattr(global_state, "orders", None)
    n_orders = len(orders) if orders is not None else 0

    print("\n[Dashboard] Initial markets loaded (for dashboard):")
    print(f"  {n_markets} markets, {n_positions} positions, {n_orders} orders.")
    print(f"  all_tokens count: {len(getattr(global_state, 'all_tokens', []) or [])}")


async def market_ws_loop():
    """
    看板用的 WebSocket 循环：订阅 all_tokens 的盘口。
    """
    init_state_and_markets()

    while True:
        try:
            if not getattr(global_state, "all_tokens", None):
                print("[Dashboard] WARNING: all_tokens is empty, no markets to subscribe.")
                await asyncio.sleep(5)
                continue

            print("[Dashboard] Connecting market websocket for orderbook streaming...")
            await connect_market_websocket(global_state.all_tokens)
        except Exception as e:
            print("[Dashboard] Error in market_ws_loop:", e)
            traceback.print_exc()
        await asyncio.sleep(1)


def start_ws_thread():
    """
    在后台线程跑 asyncio 循环。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(market_ws_loop())


# ======================
# 5. 启动入口
# ======================

if __name__ == "__main__":
    # 启动 WebSocket 抓盘口线程
    ws_thread = threading.Thread(target=start_ws_thread, daemon=True)
    ws_thread.start()

    # 主线程跑 FastAPI
    uvicorn.run(app, host="0.0.0.0", port=8000)