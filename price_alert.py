"""
price_alert.py - 全市场异常价格波动监控 + 网页端确认挂单

功能：
- 通过 WebSocket 订阅全市场实时成交价（last_trade_price）
- 当价格偏离短期均值超过 60% 时，记录到异常列表
- 网页端 http://localhost:8001 展示异常市场，点击按钮确认后才挂单

运行方式：
    python price_alert.py
    然后打开 http://localhost:8001
"""

import asyncio
import json
import time
import traceback
import threading
from collections import defaultdict, deque
from datetime import datetime
from typing import List, Dict, Optional

import websockets
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

# ======================================================
# ⚙️ 配置参数
# ======================================================
ALERT_THRESHOLD         = 0.60   # 偏离均值触发阈值（60%）
PRICE_HISTORY_SIZE      = 20     # 滚动价格窗口大小
MIN_TRADES_BEFORE_ALERT = 5      # 冷启动保护：至少N次成交后才检测
WS_CHUNK_SIZE           = 500    # 每个 WebSocket 连接订阅的 token 数量
WS_CONNECT_DELAY        = 2.0    # 每个连接建立后的延迟（秒）
COOLDOWN_SECONDS        = 120    # 同一市场两次警报的最小间隔（秒）
MIN_PRICE_DEVIATION     = 0.05   # 最小绝对偏差（避免极端价格市场噪音）
MAX_ALERTS_DISPLAY      = 50     # 网页最多显示的警报数量

# ======================================================
# 📊 全局状态
# ======================================================
price_history: dict = defaultdict(lambda: deque(maxlen=PRICE_HISTORY_SIZE))
token_to_question: dict = {}
token_to_slug: dict = {}
last_alert_time: dict = {}
alert_count = 0

# 警报列表（最新的在前）
alerts: List[Dict] = []
alerts_lock = threading.Lock()

# ======================================================
# 🌐 FastAPI 网页服务
# ======================================================
app = FastAPI(title="Price Alert Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """网页端主页"""
    return HTMLResponse(content=DASHBOARD_HTML)


@app.get("/api/alerts")
def get_alerts():
    """获取警报列表"""
    with alerts_lock:
        return {"alerts": alerts[:MAX_ALERTS_DISPLAY], "total": alert_count}


@app.post("/api/place_order/{token_id}")
def place_order_confirm(token_id: str):
    """
    确认挂单接口（由网页端调用）
    返回市场信息，让前端展示确认框
    """
    question = token_to_question.get(token_id, "Unknown")
    slug = token_to_slug.get(token_id, "")
    
    # 找到对应的警报
    with alerts_lock:
        alert = next((a for a in alerts if a["token_id"] == token_id), None)
    
    if not alert:
        return {"status": "error", "message": "未找到该市场的警报记录"}
    
    return {
        "status": "ok",
        "token_id": token_id,
        "question": question,
        "current_price": alert["current_price"],
        "avg_price": alert["avg_price"],
        "deviation_pct": alert["deviation_pct"],
        "polymarket_url": f"https://polymarket.com/event/{slug}" if slug else "",
        "message": f"确认要在 {question[:50]} 挂单吗？\n当前价: {alert['current_price']:.4f}，均值: {alert['avg_price']:.4f}"
    }


@app.delete("/api/alerts/{token_id}")
def dismiss_alert(token_id: str):
    """忽略某个警报"""
    with alerts_lock:
        global alerts
        alerts = [a for a in alerts if a["token_id"] != token_id]
    return {"status": "ok"}


@app.delete("/api/alerts")
def clear_all_alerts():
    """清空所有警报"""
    with alerts_lock:
        alerts.clear()
    return {"status": "ok"}


# ======================================================
# 🖥️ 网页 HTML
# ======================================================
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚡ 价格异常监控</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', sans-serif; background: #0f0f1a; color: #e0e0e0; padding: 20px; }
        h1 { color: #ff6b6b; margin-bottom: 5px; font-size: 1.5em; }
        .subtitle { color: #888; font-size: 0.85em; margin-bottom: 20px; }
        .stats { display: flex; gap: 20px; margin-bottom: 20px; }
        .stat-box { background: #1a1a2e; border: 1px solid #333; border-radius: 8px; padding: 12px 20px; }
        .stat-box .num { font-size: 1.8em; font-weight: bold; color: #ff6b6b; }
        .stat-box .label { font-size: 0.75em; color: #888; }
        .controls { margin-bottom: 15px; display: flex; gap: 10px; align-items: center; }
        .btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.85em; font-weight: bold; }
        .btn-danger { background: #c0392b; color: white; }
        .btn-danger:hover { background: #e74c3c; }
        .btn-primary { background: #2980b9; color: white; }
        .btn-primary:hover { background: #3498db; }
        .btn-success { background: #27ae60; color: white; }
        .btn-success:hover { background: #2ecc71; }
        .btn-gray { background: #555; color: white; }
        .btn-gray:hover { background: #777; }
        .alert-list { display: flex; flex-direction: column; gap: 10px; }
        .alert-card { background: #1a1a2e; border: 1px solid #333; border-radius: 10px; padding: 15px; display: flex; align-items: center; gap: 15px; }
        .alert-card.up { border-left: 4px solid #e74c3c; }
        .alert-card.down { border-left: 4px solid #3498db; }
        .alert-card .direction { font-size: 1.5em; min-width: 30px; }
        .alert-card .info { flex: 1; }
        .alert-card .question { font-weight: bold; font-size: 0.95em; margin-bottom: 4px; }
        .alert-card .prices { font-size: 0.82em; color: #aaa; }
        .alert-card .deviation { font-size: 1.1em; font-weight: bold; min-width: 70px; text-align: right; }
        .alert-card.up .deviation { color: #e74c3c; }
        .alert-card.down .deviation { color: #3498db; }
        .alert-card .actions { display: flex; gap: 8px; }
        .alert-card .time { font-size: 0.75em; color: #666; margin-top: 3px; }
        .empty { text-align: center; color: #555; padding: 60px; font-size: 1.1em; }
        .refresh-indicator { font-size: 0.75em; color: #555; }
        .modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 1000; justify-content: center; align-items: center; }
        .modal-overlay.active { display: flex; }
        .modal { background: #1a1a2e; border: 1px solid #444; border-radius: 12px; padding: 25px; max-width: 450px; width: 90%; }
        .modal h3 { color: #ff6b6b; margin-bottom: 15px; }
        .modal .detail { background: #0f0f1a; border-radius: 8px; padding: 12px; margin-bottom: 15px; font-size: 0.85em; line-height: 1.8; }
        .modal .detail span { color: #aaa; }
        .modal .detail strong { color: #e0e0e0; }
        .modal-actions { display: flex; gap: 10px; justify-content: flex-end; }
    </style>
</head>
<body>
    <h1>⚡ 价格异常监控</h1>
    <p class="subtitle">实时监控全市场价格异常，捕捉套利机会 | 自动刷新: <span id="countdown">5</span>s</p>

    <div class="stats">
        <div class="stat-box">
            <div class="num" id="total-alerts">0</div>
            <div class="label">累计警报</div>
        </div>
        <div class="stat-box">
            <div class="num" id="current-alerts">0</div>
            <div class="label">待处理</div>
        </div>
    </div>

    <div class="controls">
        <button class="btn btn-danger" onclick="clearAll()">🗑️ 清空所有</button>
        <button class="btn btn-primary" onclick="fetchAlerts()">🔄 立即刷新</button>
        <span class="refresh-indicator" id="last-update"></span>
    </div>

    <div class="alert-list" id="alert-list">
        <div class="empty">⏳ 等待价格异常数据...</div>
    </div>

    <!-- 确认挂单弹窗 -->
    <div class="modal-overlay" id="modal">
        <div class="modal">
            <h3>📋 确认挂单</h3>
            <div class="detail" id="modal-detail"></div>
            <div class="modal-actions">
                <button class="btn btn-gray" onclick="closeModal()">取消</button>
                <a id="modal-link" href="#" target="_blank" class="btn btn-primary">🔗 查看市场</a>
                <button class="btn btn-success" id="modal-confirm" onclick="confirmOrder()">✅ 确认挂单</button>
            </div>
        </div>
    </div>

    <script>
        let currentTokenId = null;
        let countdown = 5;

        async function fetchAlerts() {
            try {
                const resp = await fetch('/api/alerts');
                const data = await resp.json();
                document.getElementById('total-alerts').textContent = data.total;
                document.getElementById('current-alerts').textContent = data.alerts.length;
                document.getElementById('last-update').textContent = '最后更新: ' + new Date().toLocaleTimeString();
                renderAlerts(data.alerts);
            } catch(e) {
                console.error(e);
            }
        }

        function renderAlerts(alerts) {
            const container = document.getElementById('alert-list');
            if (alerts.length === 0) {
                container.innerHTML = '<div class="empty">✅ 暂无异常价格警报</div>';
                return;
            }
            container.innerHTML = alerts.map(a => `
                <div class="alert-card ${a.direction === 'up' ? 'up' : 'down'}">
                    <div class="direction">${a.direction === 'up' ? '📈' : '📉'}</div>
                    <div class="info">
                        <div class="question">${a.question}</div>
                        <div class="prices">
                            当前: <strong>${(a.current_price * 100).toFixed(1)}%</strong> &nbsp;|&nbsp;
                            均值: ${(a.avg_price * 100).toFixed(1)}% &nbsp;|&nbsp;
                            Token: ${a.token_id.substring(0, 12)}...
                        </div>
                        <div class="time">⏰ ${a.timestamp}</div>
                    </div>
                    <div class="deviation">${a.deviation_pct.toFixed(0)}%</div>
                    <div class="actions">
                        <button class="btn btn-success" onclick="openPlaceOrder('${a.token_id}')">挂单</button>
                        <button class="btn btn-gray" onclick="dismissAlert('${a.token_id}')">忽略</button>
                    </div>
                </div>
            `).join('');
        }

        async function openPlaceOrder(tokenId) {
            currentTokenId = tokenId;
            try {
                const resp = await fetch('/api/place_order/' + tokenId, {method: 'POST'});
                const data = await resp.json();
                if (data.status === 'ok') {
                    document.getElementById('modal-detail').innerHTML = `
                        <span>市场:</span> <strong>${data.question}</strong><br>
                        <span>当前价格:</span> <strong>${(data.current_price * 100).toFixed(2)}%</strong><br>
                        <span>近期均值:</span> <strong>${(data.avg_price * 100).toFixed(2)}%</strong><br>
                        <span>偏离幅度:</span> <strong>${data.deviation_pct.toFixed(1)}%</strong>
                    `;
                    if (data.polymarket_url) {
                        document.getElementById('modal-link').href = data.polymarket_url;
                        document.getElementById('modal-link').style.display = 'inline-block';
                    } else {
                        document.getElementById('modal-link').style.display = 'none';
                    }
                    document.getElementById('modal').classList.add('active');
                }
            } catch(e) {
                alert('获取市场信息失败: ' + e);
            }
        }

        function closeModal() {
            document.getElementById('modal').classList.remove('active');
            currentTokenId = null;
        }

        function confirmOrder() {
            if (!currentTokenId) return;
            alert('✅ 请手动前往 Polymarket 对该市场挂单。\n\nToken ID: ' + currentTokenId);
            closeModal();
        }

        async function dismissAlert(tokenId) {
            await fetch('/api/alerts/' + tokenId, {method: 'DELETE'});
            fetchAlerts();
        }

        async function clearAll() {
            if (!confirm('确认清空所有警报？')) return;
            await fetch('/api/alerts', {method: 'DELETE'});
            fetchAlerts();
        }

        // 自动刷新
        function startCountdown() {
            countdown = 5;
            const timer = setInterval(() => {
                countdown--;
                document.getElementById('countdown').textContent = countdown;
                if (countdown <= 0) {
                    clearInterval(timer);
                    fetchAlerts();
                    startCountdown();
                }
            }, 1000);
        }

        fetchAlerts();
        startCountdown();
    </script>
</body>
</html>
"""


# ======================================================
# 🔔 警报记录
# ======================================================
def record_alert(token_id: str, current_price: float, avg_price: float, deviation: float):
    """记录价格异常到警报列表"""
    global alert_count

    now = time.time()
    if now - last_alert_time.get(token_id, 0) < COOLDOWN_SECONDS:
        return
    last_alert_time[token_id] = now
    alert_count += 1

    question = token_to_question.get(token_id, token_id[:20] + "...")
    direction = "up" if current_price > avg_price else "down"
    deviation_pct = deviation * 100
    timestamp = datetime.now().strftime('%H:%M:%S')

    alert = {
        "token_id": token_id,
        "question": question,
        "current_price": current_price,
        "avg_price": avg_price,
        "deviation_pct": deviation_pct,
        "direction": direction,
        "timestamp": timestamp,
    }

    with alerts_lock:
        # 如果已存在同一 token 的警报，更新它
        existing = next((i for i, a in enumerate(alerts) if a["token_id"] == token_id), None)
        if existing is not None:
            alerts[existing] = alert
        else:
            alerts.insert(0, alert)  # 最新的放最前面
        # 限制列表长度
        if len(alerts) > MAX_ALERTS_DISPLAY * 2:
            alerts[:] = alerts[:MAX_ALERTS_DISPLAY]

    print(f"[{timestamp}] ⚡ 价格异常 #{alert_count}: {question[:40]}... "
          f"{'📈' if direction == 'up' else '📉'} {current_price:.4f} (均值:{avg_price:.4f}, 偏离:{deviation_pct:.0f}%)")


# ======================================================
# 📈 价格检测
# ======================================================
def check_price_anomaly(token_id: str, new_price: float):
    history = price_history[token_id]
    history.append(new_price)

    if len(history) < MIN_TRADES_BEFORE_ALERT:
        return

    historical_prices = list(history)[:-1]
    if not historical_prices:
        return

    avg_price = sum(historical_prices) / len(historical_prices)
    if avg_price == 0:
        return

    deviation = abs(new_price - avg_price) / avg_price
    abs_deviation = abs(new_price - avg_price)

    if abs_deviation < MIN_PRICE_DEVIATION:
        return

    if deviation >= ALERT_THRESHOLD:
        record_alert(token_id, new_price, avg_price, deviation)


# ======================================================
# 📡 WebSocket 消息处理（只处理 last_trade_price）
# ======================================================
def process_ws_message(json_datas):
    if not isinstance(json_datas, list):
        json_datas = [json_datas]

    for msg in json_datas:
        event_type = msg.get('event_type')

        # 只处理成交价事件，忽略 book 和 price_change
        if event_type != 'last_trade_price':
            continue

        asset_id = msg.get('asset_id') or msg.get('market')
        if not asset_id:
            continue

        try:
            price = float(msg.get('price', 0))
            if price > 0:
                check_price_anomaly(asset_id, price)
        except (ValueError, TypeError):
            pass


# ======================================================
# 📡 单个 WebSocket 连接（带延迟启动）
# ======================================================
async def connect_ws_chunk(token_ids: list, chunk_index: int, start_delay: float):
    """订阅一批 token 的 WebSocket，带启动延迟"""
    # 错开连接时间，避免同时建立大量连接
    await asyncio.sleep(start_delay)

    uri = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                sub_msg = {"assets_ids": token_ids}
                await ws.send(json.dumps(sub_msg))
                print(f"   ✅ WS #{chunk_index} 已连接（{len(token_ids)} 个 token）")

                while True:
                    raw = await ws.recv()
                    try:
                        data = json.loads(raw)
                        process_ws_message(data)
                    except json.JSONDecodeError:
                        pass

        except websockets.ConnectionClosed:
            print(f"   ⚠️ WS #{chunk_index} 断开，5秒后重连...")
        except Exception as e:
            print(f"   ❌ WS #{chunk_index} 错误: {type(e).__name__}: {e}")

        await asyncio.sleep(5)


# ======================================================
# 🌐 获取全市场 Token 列表
# ======================================================
def fetch_all_tokens() -> list:
    print("🔍 正在获取全市场 token 列表...")
    all_tokens = []
    cursor = ""
    page = 0

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    while True:
        try:
            params = {"next_cursor": cursor} if cursor else {}
            resp = session.get(
                "https://clob.polymarket.com/sampling-markets",
                params=params,
                timeout=10
            )
            data = resp.json()
            markets = data.get('data', [])

            if not markets:
                break

            for m in markets:
                question = m.get('question', '')
                slug = m.get('market_slug', '')
                tokens = m.get('tokens', [])
                for t in tokens:
                    token_id = t.get('token_id', '')
                    if token_id:
                        all_tokens.append((token_id, question, slug))

            cursor = data.get('next_cursor')
            page += 1

            if page % 5 == 0:
                print(f"   已获取 {len(all_tokens)} 个 token（第 {page} 页）...")

            if not cursor:
                break

        except Exception as e:
            print(f"   ❌ 获取市场失败: {e}")
            break

    print(f"   ✅ 共获取 {len(all_tokens)} 个 token")
    return all_tokens


# ======================================================
# 🚀 后台监控任务
# ======================================================
async def run_monitor():
    global token_to_question, token_to_slug

    print("=" * 60)
    print("⚡ 全市场价格异常监控系统")
    print("=" * 60)
    print(f"⚙️  配置:")
    print(f"   - 偏离阈值: {ALERT_THRESHOLD*100:.0f}%")
    print(f"   - 最小绝对偏差: {MIN_PRICE_DEVIATION:.2f} ({MIN_PRICE_DEVIATION*100:.0f}¢)")
    print(f"   - 滚动窗口: {PRICE_HISTORY_SIZE} 次成交")
    print(f"   - 冷启动保护: {MIN_TRADES_BEFORE_ALERT} 次成交后开始检测")
    print(f"   - 警报冷却: {COOLDOWN_SECONDS} 秒")
    print(f"   - 每个WS连接: {WS_CHUNK_SIZE} 个 token，间隔 {WS_CONNECT_DELAY}s")
    print("=" * 60)

    # 获取全市场 token
    all_token_triples = fetch_all_tokens()
    if not all_token_triples:
        print("❌ 未获取到任何 token，退出")
        return

    all_token_ids = []
    for token_id, question, slug in all_token_triples:
        token_to_question[token_id] = question
        token_to_slug[token_id] = slug
        all_token_ids.append(token_id)

    all_token_ids = list(dict.fromkeys(all_token_ids))
    print(f"\n📊 共监控 {len(all_token_ids)} 个 token")

    # 分批建立 WebSocket 连接
    chunks = [
        all_token_ids[i:i + WS_CHUNK_SIZE]
        for i in range(0, len(all_token_ids), WS_CHUNK_SIZE)
    ]
    print(f"📡 建立 {len(chunks)} 个 WebSocket 连接（每个 {WS_CHUNK_SIZE} 个 token，间隔 {WS_CONNECT_DELAY}s）...")
    print(f"🌐 网页端: http://localhost:8001\n")

    # 状态打印
    async def status_printer():
        while True:
            await asyncio.sleep(60)
            monitored = len(price_history)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 已有价格记录: {monitored} 个 | 累计警报: {alert_count} 次 | 待处理: {len(alerts)} 个")

    tasks = [
        asyncio.create_task(connect_ws_chunk(chunk, i + 1, i * WS_CONNECT_DELAY))
        for i, chunk in enumerate(chunks)
    ]
    tasks.append(asyncio.create_task(status_printer()))

    await asyncio.gather(*tasks)


# ======================================================
# 🚀 主入口
# ======================================================
def start_monitor_thread():
    """在独立线程中运行 WebSocket 监控"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_monitor())


if __name__ == "__main__":
    # 启动后台监控线程
    monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
    monitor_thread.start()

    print("🌐 网页端启动中: http://localhost:8001")
    print("   按 Ctrl+C 停止\n")

    try:
        uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
    except KeyboardInterrupt:
        print("\n🛑 用户手动停止")
