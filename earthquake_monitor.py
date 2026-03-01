#!/usr/bin/env python3
"""
地震监控启动脚本 (支持代理) + Polymarket 自动下单
"""

# ============================================
# 地震监控配置
# ============================================

TELEGRAM_TOKEN = "8412520989:AAE2qlV7zxYpsAtuWPNYO9b5MsBTQa5xlt4"
TELEGRAM_CHAT_ID = "-1003316876299"
PROXY = "http://127.0.0.1:7890"
MIN_MAGNITUDE = 7.0
CHECK_INTERVAL = 1

# ============================================
# Polymarket 下单配置
# ============================================

POLYMARKET_CLI = r"C:\Windows\system32\polymarket-cli\target\release\polymarket.exe"
BOSS_TELEGRAM_TOKEN = "8527143243:AAENh0WVSNVsjLcGc-jnQVlymF_InVUDJlY"
BOSS_CHAT_ID = "607548102"
CONFIRM_TIMEOUT = 120  # Boss 确认等待时间（秒）

POLYMARKET_TARGETS = [
    {"name": "March 31", "token": "0xba00c81b61cad9725c6d4b8eb0f9127a46967cc26f036f2b9e98382fe305dbe7"},
    {"name": "April 30", "token": "0xc7b6e73781255ca76b11b13cce74c64d85804311cb0a43e6421855ace2c21321"},
    {"name": "May 31",   "token": "0xf68fe4427d16152afb6354966055d31136be966ade87a570674b8fd63518f3be"},
]

ORDER_PRICE = 0.95   # 主动吃单价格
ORDER_SIZE  = 105    # 每市场约 $100（100 / 0.95 ≈ 105 shares）

# ============================================

import requests
import subprocess
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import List, Optional, Set

CHINA_TZ = timezone(timedelta(hours=8))
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None


@dataclass
class Earthquake:
    id: str
    magnitude: float
    latitude: float
    longitude: float
    depth_km: float
    region: str
    time: datetime
    source: str
    url: Optional[str] = None
    tsunami: bool = False

    @property
    def distance_from_hk(self) -> float:
        from math import radians, sin, cos, sqrt, atan2
        HK_LAT, HK_LON = 22.3193, 114.1694
        R = 6371
        lat1, lon1 = radians(HK_LAT), radians(HK_LON)
        lat2, lon2 = radians(self.latitude), radians(self.longitude)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1-a))

    @property
    def distance_from_tokyo(self) -> float:
        from math import radians, sin, cos, sqrt, atan2
        TK_LAT, TK_LON = 35.6762, 139.6503
        R = 6371
        lat1, lon1 = radians(TK_LAT), radians(TK_LON)
        lat2, lon2 = radians(self.latitude), radians(self.longitude)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
        return R * 2 * atan2(sqrt(a), sqrt(1-a))

    @property
    def emoji(self) -> str:
        if self.magnitude >= 8: return "🔴🔴🔴"
        if self.magnitude >= 7: return "🔴🔴"
        if self.magnitude >= 6: return "🔴"
        if self.magnitude >= 5: return "🟠"
        return "🟡"

    def format_telegram(self) -> str:
        china_time = self.time.astimezone(CHINA_TZ)
        lines = [
            f"{self.emoji} *地震速报 M{self.magnitude:.1f}*",
            "",
            f"📍 *地区*: {self.region}",
            f"⏰ *时间*: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)",
            f"📐 *坐标*: {self.latitude:.2f}, {self.longitude:.2f}",
            f"📏 *深度*: {self.depth_km:.1f} km",
            f"🏙 *距香港*: {self.distance_from_hk:.0f} km",
            f"🗼 *距东京*: {self.distance_from_tokyo:.0f} km",
        ]
        if self.tsunami:
            lines.extend(["", "⚠️ *海啸警报*"])
        if self.url:
            lines.extend(["", f"🔗 [详情]({self.url})"])
        lines.extend(["", f"_数据源: {self.source}_"])
        return "\n".join(lines)

    def format_console(self) -> str:
        china_time = self.time.astimezone(CHINA_TZ)
        return (
            f"\n{'='*55}\n"
            f"{self.emoji} 地震速报 M{self.magnitude:.1f}\n"
            f"{'='*55}\n"
            f"   地区: {self.region}\n"
            f"   时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
            f"   坐标: ({self.latitude:.2f}, {self.longitude:.2f})\n"
            f"   深度: {self.depth_km:.1f} km\n"
            f"   距香港: {self.distance_from_hk:.0f} km\n"
            f"   距东京: {self.distance_from_tokyo:.0f} km\n"
            f"{'='*55}"
        )


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api = f"https://api.telegram.org/bot{token}"

    def send(self, text: str) -> bool:
        try:
            r = requests.post(
                f"{self.api}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "Markdown", "disable_web_page_preview": True},
                proxies=PROXIES, timeout=15
            )
            return r.status_code == 200
        except Exception as e:
            print(f"[Telegram错误] {e}")
            return False

    def test(self) -> bool:
        try:
            r = requests.get(f"{self.api}/getMe", proxies=PROXIES, timeout=15)
            if r.status_code == 200 and r.json().get("ok"):
                name = r.json()["result"].get("username", "Unknown")
                print(f"[Telegram] 连接成功: @{name}")
                return True
            return False
        except Exception as e:
            print(f"[Telegram] 连接错误: {e}")
            return False


def fetch_usgs() -> List[Earthquake]:
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
    try:
        r = requests.get(url, timeout=15)
        print(f"[USGS] HTTP状态: {r.status_code}")
        features = r.json().get("features", [])
        print(f"[USGS] 原始数据: {len(features)} 条")
        earthquakes = []
        for f in features:
            props = f["properties"]
            coords = f["geometry"]["coordinates"]
            earthquakes.append(Earthquake(
                id=f["id"],
                magnitude=float(props.get("mag", 0) or 0),
                latitude=float(coords[1]),
                longitude=float(coords[0]),
                depth_km=float(coords[2]) if len(coords) > 2 else 0,
                region=props.get("place", "Unknown"),
                time=datetime.fromtimestamp(props["time"]/1000, tz=timezone.utc),
                source="USGS",
                url=props.get("url"),
                tsunami=bool(props.get("tsunami", 0))
            ))
        return earthquakes
    except Exception as e:
        print(f"[USGS错误] {e}")
        return []


def fetch_hko() -> List[Earthquake]:
    url = "https://data.weather.gov.hk/weatherAPI/opendata/earthquake.php?dataType=qem&lang=sc"
    try:
        r = requests.get(url, timeout=15)
        print(f"[HKO] HTTP状态: {r.status_code}")
        data = r.json()
        if isinstance(data, dict):
            data = [data] if data.get("mag") else []
        elif not isinstance(data, list):
            return []
        print(f"[HKO] 原始数据: {len(data)} 条")
        earthquakes = []
        for item in data:
            try:
                ptime = item.get("ptime", "")
                eq_time = datetime.fromisoformat(ptime.replace("Z", "+00:00")) if ptime else datetime.now(timezone.utc)
            except Exception:
                eq_time = datetime.now(timezone.utc)
            earthquakes.append(Earthquake(
                id=f"hko_{item.get('ptime')}_{item.get('lat')}_{item.get('lon')}",
                magnitude=float(item.get("mag", 0) or 0),
                latitude=float(item.get("lat", 0) or 0),
                longitude=float(item.get("lon", 0) or 0),
                depth_km=0,
                region=item.get("region", "未知"),
                time=eq_time,
                source="HKO",
                url="https://www.hko.gov.hk/tc/gts/equake/quake-info.htm"
            ))
        return earthquakes
    except Exception as e:
        print(f"[HKO错误] {e}")
        return []


# ============================================
# Polymarket 下单函数
# ============================================

def send_to_boss(text: str):
    """直接推送消息给 Boss"""
    url = f"https://api.telegram.org/bot{BOSS_TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": BOSS_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }, timeout=15)
    except Exception as e:
        print(f"[Boss推送错误] {e}")


def place_orders() -> List[str]:
    """对三个市场各下一笔 YES 单"""
    results = []
    for target in POLYMARKET_TARGETS:
        cmd = [
            POLYMARKET_CLI,
            "clob", "--signature-type", "eoa",
            "create-order",
            "--token", target["token"],
            "--side", "buy",
            "--price", str(ORDER_PRICE),
            "--size", str(ORDER_SIZE)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = (result.stdout.strip() or result.stderr.strip())[:120]
            results.append(f"*{target['name']}*: {output}")
            print(f"[下单] {target['name']}: {output}")
        except Exception as e:
            results.append(f"*{target['name']}*: 错误 {e}")
            print(f"[下单错误] {target['name']}: {e}")
    return results


def wait_for_boss_confirm(eq: Earthquake) -> bool:
    """
    向 Boss 推送确认请求，轮询等待回复。
    回复「确认」执行，回复「取消」或超时则放弃。
    """
    msg = (
        f"*地震触发下单提醒*\n\n"
        f"{eq.emoji} M{eq.magnitude:.1f} {eq.region}\n"
        f"数据源: {eq.source}\n\n"
        f"准备对以下市场各买入 ~$100 YES (共$300):\n"
        f"- March 31\n"
        f"- April 30\n"
        f"- May 31\n\n"
        f"请在 {CONFIRM_TIMEOUT} 秒内回复「确认」执行，或「取消」放弃。"
    )
    send_to_boss(msg)
    print(f"[等待Boss确认] 已推送，等待最多 {CONFIRM_TIMEOUT} 秒...")

    url = f"https://api.telegram.org/bot{BOSS_TELEGRAM_TOKEN}/getUpdates"
    # 获取当前最新 update_id 作为基准，只处理新消息
    try:
        r = requests.get(url, params={"limit": 1}, timeout=10)
        updates = r.json().get("result", [])
        offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    deadline = time.time() + CONFIRM_TIMEOUT
    while time.time() < deadline:
        try:
            r = requests.get(url, params={"offset": offset, "timeout": 5}, timeout=15)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg_data = upd.get("message", {})
                if str(msg_data.get("chat", {}).get("id", "")) == BOSS_CHAT_ID:
                    text = msg_data.get("text", "").strip()
                    if "确认" in text:
                        print("[Boss] 已确认，开始下单")
                        return True
                    elif "取消" in text:
                        send_to_boss("已取消下单。")
                        print("[Boss] 已取消")
                        return False
        except Exception as e:
            print(f"[轮询错误] {e}")
        time.sleep(2)

    send_to_boss(f"超时 {CONFIRM_TIMEOUT} 秒未收到确认，已自动取消。")
    print("[超时] 自动取消")
    return False


# ============================================
# 主程序
# ============================================

def main():
    print("=" * 55)
    print("全球地震实时监控 + Polymarket 自动下单")
    print("=" * 55)
    print(f"   最小震级: M{MIN_MAGNITUDE}")
    print(f"   检查间隔: {CHECK_INTERVAL}秒")
    print(f"   代理: {PROXY or '未使用'}")
    print(f"   监控群组: {TELEGRAM_CHAT_ID}")
    print(f"   Boss推送: {BOSS_CHAT_ID}")
    print("=" * 55)

    bot = TelegramBot(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

    print("\n正在测试Telegram连接...")
    if bot.test():
        bot.send("*地震监控已启动*\n\nM7.0+ 将触发 Polymarket 下单确认请求。")
    else:
        print("\n⚠️ Telegram连接失败，监控仍会在控制台显示\n")

    send_to_boss("*地震监控已启动*\nM7.0+ 触发时将向您发送下单确认请求。")
    print("\n按 Ctrl+C 停止\n")

    notified: Set[str] = set()
    order_triggered: Set[str] = set()  # 防止同一地震重复触发

    try:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{now}] 正在检查...")

            usgs_data = fetch_usgs()
            hko_data = fetch_hko()
            all_eq = usgs_data + hko_data

            print(f"[汇总] USGS:{len(usgs_data)} + HKO:{len(hko_data)} = {len(all_eq)} 条")

            for eq in all_eq:
                # 群组推送通知
                if eq.id not in notified and eq.magnitude >= MIN_MAGNITUDE:
                    notified.add(eq.id)
                    print(eq.format_console())
                    bot.send(eq.format_telegram())

                # 触发下单流程（每个地震ID只触发一次）
                if eq.id not in order_triggered and eq.magnitude >= 7.0:
                    order_triggered.add(eq.id)
                    print(f"\n[下单触发] M{eq.magnitude:.1f} {eq.region}，向Boss请求确认...")
                    if wait_for_boss_confirm(eq):
                        results = place_orders()
                        summary = "\n".join(results)
                        send_to_boss(f"*下单完成！*\n\n{summary}")
                        bot.send(f"*Polymarket 下单已执行*\nM{eq.magnitude:.1f} {eq.region}\n\n{summary}")

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n监控已停止")
        bot.send("*地震监控已停止*")
        send_to_boss("*地震监控已停止*")


if __name__ == "__main__":
    main()
