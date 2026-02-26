#!/usr/bin/env python3
"""
地震监控启动脚本 (支持代理)
"""

# ============================================
# 👇 在这里填写你的配置 👇
# ============================================

# Telegram Bot Token
TELEGRAM_TOKEN = "8412520989:AAE2qlV7zxYpsAtuWPNYO9b5MsBTQa5xlt4"

# Telegram Chat ID
TELEGRAM_CHAT_ID = "-1003316876299"

# 代理设置 - 根据你的代理软件修改端口
# Clash 默认: 7890
# V2Ray 默认: 10809
# Shadowsocks 默认: 1080
PROXY = "http://127.0.0.1:7890"

# 如果不需要代理，把上面改成:
# PROXY = None

# 最小震级 (测试时可以设低一点，比如2.5，正式使用设5.0)
MIN_MAGNITUDE = 6

# 检查间隔 (秒)
CHECK_INTERVAL = 1

# ============================================
# 👆 配置结束 👆
# ============================================

import requests
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import List, Optional, Set

# 中国时区 UTC+8
CHINA_TZ = timezone(timedelta(hours=8))

# 设置代理
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
        # 转换为中国时间
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
        # 转换为中国时间
        china_time = self.time.astimezone(CHINA_TZ)
        return f"""
{'='*55}
{self.emoji} 地震速报 M{self.magnitude:.1f}
{'='*55}
   地区: {self.region}
   时间: {china_time.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)
   坐标: ({self.latitude:.2f}, {self.longitude:.2f})
   深度: {self.depth_km:.1f} km
   距香港: {self.distance_from_hk:.0f} km
   距东京: {self.distance_from_tokyo:.0f} km
{'='*55}"""


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.api = f"https://api.telegram.org/bot{token}"
    
    def send(self, text: str) -> bool:
        try:
            r = requests.post(
                f"{self.api}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True
                },
                proxies=PROXIES,
                timeout=15
            )
            if r.status_code == 200:
                return True
            else:
                print(f"[Telegram] 发送失败: {r.text}")
                return False
        except Exception as e:
            print(f"[Telegram错误] {e}")
            return False
    
    def test(self) -> bool:
        try:
            r = requests.get(f"{self.api}/getMe", proxies=PROXIES, timeout=15)
            if r.status_code == 200 and r.json().get("ok"):
                name = r.json()["result"].get("username", "Unknown")
                print(f"[Telegram] ✅ 连接成功: @{name}")
                return True
            print(f"[Telegram] ❌ 连接失败: {r.text}")
            return False
        except Exception as e:
            print(f"[Telegram] ❌ 连接错误: {e}")
            return False


def fetch_usgs() -> List[Earthquake]:
    """获取USGS数据"""
    # 使用 all_hour 获取更多数据
    url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"
    try:
        r = requests.get(url, timeout=15)
        print(f"[USGS] HTTP状态: {r.status_code}")
        data = r.json()
        
        features = data.get("features", [])
        print(f"[USGS] 原始数据: {len(features)} 条")
        
        earthquakes = []
        for f in features:
            props = f["properties"]
            coords = f["geometry"]["coordinates"]
            eq = Earthquake(
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
            )
            earthquakes.append(eq)
        return earthquakes
    except Exception as e:
        print(f"[USGS错误] {e}")
        return []


def fetch_hko() -> List[Earthquake]:
    """获取香港天文台数据"""
    url = "https://data.weather.gov.hk/weatherAPI/opendata/earthquake.php?dataType=qem&lang=sc"
    try:
        r = requests.get(url, timeout=15)
        print(f"[HKO] HTTP状态: {r.status_code}")
        
        # 打印原始响应以便调试
        text = r.text[:200] if len(r.text) > 200 else r.text
        print(f"[HKO] 响应预览: {text}")
        
        data = r.json()
        
        # HKO 有时返回单个对象，有时返回列表
        if isinstance(data, dict):
            # 单条记录，包装成列表
            if data.get("mag"):  # 确保是有效的地震数据
                print(f"[HKO] 返回单条记录: M{data.get('mag')} {data.get('region')}")
                data = [data]
            else:
                print(f"[HKO] 返回空数据")
                return []
        elif not isinstance(data, list):
            print(f"[HKO] 返回类型异常: {type(data)}")
            return []
        
        print(f"[HKO] 原始数据: {len(data)} 条")
        
        earthquakes = []
        for item in data:
            try:
                ptime = item.get("ptime", "")
                if ptime:
                    eq_time = datetime.fromisoformat(ptime.replace("Z", "+00:00"))
                else:
                    eq_time = datetime.now(timezone.utc)
            except:
                eq_time = datetime.now(timezone.utc)
            
            eq = Earthquake(
                id=f"hko_{item.get('ptime')}_{item.get('lat')}_{item.get('lon')}",
                magnitude=float(item.get("mag", 0) or 0),
                latitude=float(item.get("lat", 0) or 0),
                longitude=float(item.get("lon", 0) or 0),
                depth_km=0,
                region=item.get("region", "未知"),
                time=eq_time,
                source="HKO",
                url="https://www.hko.gov.hk/tc/gts/equake/quake-info.htm"
            )
            earthquakes.append(eq)
        return earthquakes
    except Exception as e:
        print(f"[HKO错误] {e}")
        import traceback
        traceback.print_exc()
        return []


def main():
    print("=" * 55)
    print("🌏 全球地震实时监控")
    print("=" * 55)
    print(f"   最小震级: M{MIN_MAGNITUDE}")
    print(f"   检查间隔: {CHECK_INTERVAL}秒")
    print(f"   代理: {PROXY or '未使用'}")
    print(f"   Telegram: {TELEGRAM_CHAT_ID}")
    print("=" * 55)
    
    # 初始化Telegram
    bot = TelegramBot(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
    
    print("\n正在测试Telegram连接...")
    if bot.test():
        bot.send("🟢 *地震监控已启动*\n\n开始监控全球地震活动...")
    else:
        print("\n⚠️ Telegram连接失败!")
        print("   请检查:")
        print("   1. 代理端口是否正确:")
        print("      - Clash 默认: 7890")
        print("      - V2Ray 默认: 10809") 
        print("      - Shadowsocks: 1080")
        print("   2. 代理软件是否正在运行")
        print("   3. Token和ChatID是否正确")
        print("\n   监控仍会在控制台显示\n")
    
    print("\n按 Ctrl+C 停止\n")
    
    notified: Set[str] = set()
    
    try:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            print(f"\n{'='*55}")
            print(f"[{now}] 正在检查...")
            print(f"{'='*55}")
            
            # 获取数据
            usgs_data = fetch_usgs()
            hko_data = fetch_hko()
            all_eq = usgs_data + hko_data
            
            print(f"\n[汇总] 共获取 {len(all_eq)} 条数据 (USGS:{len(usgs_data)} + HKO:{len(hko_data)})")
            
            # 统计各震级数量
            if all_eq:
                mag_counts = {}
                for eq in all_eq:
                    level = int(eq.magnitude)
                    mag_counts[level] = mag_counts.get(level, 0) + 1
                print(f"[震级分布] {dict(sorted(mag_counts.items(), reverse=True))}")
            
            # 过滤并通知
            new_count = 0
            for eq in all_eq:
                if eq.id in notified:
                    continue
                if eq.magnitude < MIN_MAGNITUDE:
                    continue
                
                notified.add(eq.id)
                new_count += 1
                print(eq.format_console())
                bot.send(eq.format_telegram())
            
            if new_count == 0:
                print(f"\n   暂无新的 M{MIN_MAGNITUDE}+ 地震")
            else:
                print(f"\n   发现 {new_count} 条新地震!")
            
            print(f"\n下次检查: {CHECK_INTERVAL}秒后...")
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        print("\n\n监控已停止")
        bot.send("🔴 *地震监控已停止*")


if __name__ == "__main__":
    main()