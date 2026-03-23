"""
reward_tracker.py - 每日流动性奖励追踪器

功能：
  1. 调用 Polymarket rewards API 获取当前累计奖励总额
  2. 与上一次记录对比，计算每日增量
  3. 保存到 reward_history.json 和 reward_history.csv
  4. 可选：推送 Telegram 通知

用法：
  # 手动运行一次（立即抓取并记录）
  python reward_tracker.py

  # 持续运行，每天北京时间 8:10 自动抓取
  python reward_tracker.py --daemon

  # 查看历史记录
  python reward_tracker.py --show

  # 指定自定义时间运行（24小时制）
  python reward_tracker.py --daemon --hour 8 --minute 10
"""

import os
import json
import time
import argparse
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.clob_types import RequestArgs
from py_clob_client.constants import POLYGON

load_dotenv()

# ======================================================
# ⚙️ 配置
# ======================================================
TZ_UTC8 = timezone(timedelta(hours=8))
HISTORY_JSON = "reward_history.json"
HISTORY_CSV  = "reward_history.csv"

# Telegram（复用 reward_monitor 的配置）
TELEGRAM_BOT_TOKEN = "8425179007:AAE_K7Z2_B6U4M3z5NnZ__sqjqRhfwe_iJ8"
TELEGRAM_CHAT_ID   = "-1003455972438"
ENABLE_TELEGRAM    = True


# ======================================================
# 🔑 初始化 Polymarket Client
# ======================================================
def init_client():
    """初始化 ClobClient 并获取 API 凭证"""
    key = os.getenv("PK")
    browser_address = os.getenv("BROWSER_ADDRESS")
    if not key or not browser_address:
        raise ValueError("请在 .env 中设置 PK 和 BROWSER_ADDRESS")

    from web3 import Web3
    funder = Web3.to_checksum_address(browser_address)

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=key,
        chain_id=POLYGON,
        funder=funder,
        signature_type=2,
    )
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds=creds)
    # 用 proxy wallet（实际下单地址）而非 browser address 查询奖励
    maker_address = client.get_address()
    return client, creds, maker_address


# ======================================================
# 📊 获取奖励数据
# ======================================================
def fetch_total_earnings(client, creds, wallet_address: str) -> tuple:
    """
    调用 Polymarket rewards API，返回 (total_earnings, market_details)
    total_earnings: 所有市场累计奖励总和 (USDC)
    market_details: [{question, earnings, earning_percentage}, ...]
    """
    args = RequestArgs(method='GET', request_path='/rewards/user/markets')
    l2Headers = create_level_2_headers(client.signer, creds, args)

    url = "https://polymarket.com/api/rewards/markets"
    all_data = []
    cursor = ''

    # 分页获取所有数据
    for _ in range(20):  # 最多 20 页，防止死循环
        params = {
            "l2Headers": json.dumps(l2Headers),
            "orderBy": "earnings",
            "position": "DESC",
            "makerAddress": wallet_address,
            "authenticationType": "eoa",
            "nextCursor": cursor,
            "requestPath": "/rewards/user/markets",
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        result = resp.json()

        page_data = result.get("data", [])
        if not page_data:
            break
        all_data.extend(page_data)

        cursor = result.get("next_cursor", "")
        if not cursor:
            break

    # 解析 earnings
    market_details = []
    total = 0.0
    for item in all_data:
        question = item.get("question", "")
        earnings_list = item.get("earnings", [])
        earning = 0.0
        if earnings_list and isinstance(earnings_list, list):
            earning = float(earnings_list[0].get("earnings", 0))
        earning_pct = float(item.get("earning_percentage", 0))
        if earning > 0:
            total += earning
            market_details.append({
                "question": question,
                "earnings": round(earning, 6),
                "earning_percentage": round(earning_pct, 4),
            })

    market_details.sort(key=lambda x: x["earnings"], reverse=True)
    return round(total, 6), market_details


# ======================================================
# 💾 历史记录管理
# ======================================================
def load_history() -> list:
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return []


def save_history(history: list):
    with open(HISTORY_JSON, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    # 同时导出 CSV
    if history:
        df = pd.DataFrame(history)
        df.to_csv(HISTORY_CSV, index=False, encoding="utf-8-sig")


# ======================================================
# 📡 Telegram
# ======================================================
def send_telegram(message: str):
    if not ENABLE_TELEGRAM:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("   ✅ Telegram 推送成功")
        else:
            print(f"   ⚠️ Telegram 推送失败: {resp.status_code}")
    except Exception as e:
        print(f"   ❌ Telegram 异常: {e}")


# ======================================================
# 🔄 核心：抓取并记录
# ======================================================
def record_once():
    """执行一次抓取并记录"""
    now = datetime.now(tz=TZ_UTC8)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*55}")
    print(f"📊 [{time_str}] 正在抓取流动性奖励...")
    print(f"{'='*55}")

    # 初始化客户端
    client, creds, wallet = init_client()

    # 获取当前累计奖励
    total_earnings, market_details = fetch_total_earnings(client, creds, wallet)
    print(f"   💰 当前累计奖励: ${total_earnings:.4f} USDC")
    print(f"   📋 有奖励的市场数: {len(market_details)}")

    # 加载历史
    history = load_history()

    # 计算每日增量
    daily_earnings = 0.0
    if history:
        last_record = history[-1]
        last_total = last_record.get("total_earnings", 0)
        daily_earnings = round(total_earnings - last_total, 6)

        # 如果今天已经记录过，更新而不是追加
        if last_record.get("date") == date_str:
            print(f"   ℹ️ 今天已有记录，更新中...")
            # 重新计算：和前天比
            if len(history) >= 2:
                prev_total = history[-2].get("total_earnings", 0)
                daily_earnings = round(total_earnings - prev_total, 6)
            else:
                daily_earnings = total_earnings
            history[-1] = {
                "date": date_str,
                "recorded_at": time_str,
                "total_earnings": total_earnings,
                "daily_earnings": daily_earnings,
                "market_count": len(market_details),
                "top_markets": market_details[:5],
            }
        else:
            history.append({
                "date": date_str,
                "recorded_at": time_str,
                "total_earnings": total_earnings,
                "daily_earnings": daily_earnings,
                "market_count": len(market_details),
                "top_markets": market_details[:5],
            })
    else:
        # 第一次记录
        daily_earnings = total_earnings
        history.append({
            "date": date_str,
            "recorded_at": time_str,
            "total_earnings": total_earnings,
            "daily_earnings": daily_earnings,
            "market_count": len(market_details),
            "top_markets": market_details[:5],
        })

    # 保存
    save_history(history)

    # 打印结果
    print(f"\n   📅 日期: {date_str}")
    print(f"   💰 累计奖励: ${total_earnings:.4f}")
    print(f"   📈 今日奖励: ${daily_earnings:.4f}")
    print(f"   📋 市场数: {len(market_details)}")

    if market_details[:3]:
        print(f"\n   🏆 Top 3 收益市场:")
        for i, m in enumerate(market_details[:3], 1):
            print(f"      {i}. {m['question'][:45]}... ${m['earnings']:.4f}")

    print(f"\n   💾 已保存到 {HISTORY_JSON} 和 {HISTORY_CSV}")
    print(f"{'='*55}\n")

    # Telegram 通知
    msg = (
        f"📊 <b>每日流动性奖励报告</b>\n\n"
        f"📅 日期: {date_str}\n"
        f"💰 累计奖励: <b>${total_earnings:.4f}</b>\n"
        f"📈 今日奖励: <b>${daily_earnings:.4f}</b>\n"
        f"📋 有奖励市场: {len(market_details)} 个\n"
    )
    if market_details[:3]:
        msg += "\n🏆 Top 3:\n"
        for i, m in enumerate(market_details[:3], 1):
            msg += f"  {i}. {m['question'][:40]}... ${m['earnings']:.4f}\n"

    msg += f"\n⏰ {time_str} UTC+8"
    send_telegram(msg)

    return total_earnings, daily_earnings


# ======================================================
# 📋 显示历史记录
# ======================================================
def show_history():
    history = load_history()
    if not history:
        print("📭 暂无历史记录。先运行 python reward_tracker.py 抓取一次。")
        return

    print(f"\n{'='*65}")
    print(f"📊 流动性奖励历史记录 （共 {len(history)} 天）")
    print(f"{'='*65}")
    print(f"{'日期':<14} {'累计奖励':>14} {'当日奖励':>14} {'市场数':>8}")
    print(f"{'-'*14} {'-'*14} {'-'*14} {'-'*8}")

    total_daily = 0.0
    for record in history:
        date = record["date"]
        total = record["total_earnings"]
        daily = record["daily_earnings"]
        markets = record.get("market_count", 0)
        total_daily += daily
        print(f"{date:<14} ${total:>12.4f} ${daily:>12.4f} {markets:>8}")

    print(f"{'-'*14} {'-'*14} {'-'*14} {'-'*8}")
    avg_daily = total_daily / len(history) if history else 0
    print(f"{'合计':<14} {'':>14} ${total_daily:>12.4f}")
    print(f"{'日均':<14} {'':>14} ${avg_daily:>12.4f}")
    print(f"{'='*65}\n")


# ======================================================
# 🔄 守护进程模式
# ======================================================
def daemon_mode(target_hour=8, target_minute=10):
    """每天指定时间自动执行一次"""
    print(f"🔄 守护进程模式启动，每天 {target_hour:02d}:{target_minute:02d} (UTC+8) 自动抓取")
    print(f"   按 Ctrl+C 退出\n")

    # 启动时先执行一次
    print("📊 启动时先执行一次抓取...")
    try:
        record_once()
    except Exception as e:
        print(f"❌ 启动抓取失败: {e}")

    while True:
        try:
            now = datetime.now(tz=TZ_UTC8)
            # 计算下一次目标时间
            target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            if now >= target:
                # 今天的已过，等明天
                target += timedelta(days=1)

            wait_secs = (target - now).total_seconds()
            wait_hours = wait_secs / 3600

            print(f"⏰ 下次抓取: {target.strftime('%Y-%m-%d %H:%M:%S')} UTC+8 (等待 {wait_hours:.1f} 小时)")

            # 分段 sleep，便于 Ctrl+C 响应
            while wait_secs > 0:
                sleep_time = min(wait_secs, 60)
                time.sleep(sleep_time)
                wait_secs -= sleep_time

            # 执行抓取
            record_once()

        except KeyboardInterrupt:
            print("\n🛑 守护进程已停止")
            break
        except Exception as e:
            print(f"❌ 抓取异常: {e}")
            import traceback
            traceback.print_exc()
            print("⏳ 60秒后重试...")
            time.sleep(60)


# ======================================================
# 入口
# ======================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket 每日流动性奖励追踪器")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式，每天自动抓取")
    parser.add_argument("--show", action="store_true", help="显示历史记录")
    parser.add_argument("--hour", type=int, default=8, help="每天抓取时间（小时，默认8）")
    parser.add_argument("--minute", type=int, default=10, help="每天抓取时间（分钟，默认10）")
    args = parser.parse_args()

    if args.show:
        show_history()
    elif args.daemon:
        daemon_mode(args.hour, args.minute)
    else:
        record_once()
