"""
reward_monitor.py - Polymarket LP 奖励赞助事件监控

原理：
  直接监控奖励合约 0xf7cD89BE08Af4D4D6B1522852ceD49FC10169f64 发出的
  RewardSponsored 事件（topic0 = 0xa0e1f8e6...），该事件包含完整的奖励参数。

已确认事件结构（来自真实交易分析）：
  topic[0] = 0xa0e1f8e6fb6dd49d885fabbf89adb64c0ef2b16b2786c92d6851742572fb1d14
  topic[1] = conditionId (bytes32)
  topic[2] = sponsor 地址 (address)
  data[0]  = amount (uint256, USDC.e 6位小数)
  data[1]  = startTime (uint256, unix timestamp)
  data[2]  = endTime (uint256, unix timestamp)
  data[3]  = maxSpread (uint256, 原始值)

已确认地址（Polygon 主网）：
  - 奖励合约: 0xf7cD89BE08Af4D4D6B1522852ceD49FC10169f64
  - Relay Hub: 0xD216153c06E857cD7f72665E0aF1d7D82172F494

运行方式：
    python reward_monitor.py
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta

# Google 表格工具
os.environ.setdefault("SPREADSHEET_URL", "https://docs.google.com/spreadsheets/d/1BwMq7kVN7wJXtOM73EZvWUF6D6x26wjIInrMBsAacY0/edit")

try:
    from data_updater.google_utils import get_spreadsheet
    GOOGLE_SHEETS_ENABLED = True
except ImportError:
    GOOGLE_SHEETS_ENABLED = False
    print("⚠️ 未找到 google_utils，Google 表格写入功能已禁用")

# ======================================================
# ⚙️ 配置
# ======================================================
POLYGONSCAN_API_KEY = "TVZ9YSFP9P4K6MBYI2RDPHUKQQNF9TMQGV"
TELEGRAM_BOT_TOKEN  = "8425179007:AAE_K7Z2_B6U4M3z5NnZ__sqjqRhfwe_iJ8"
TELEGRAM_CHAT_ID    = "-1003455972438"

# 奖励合约地址
REWARD_CONTRACT = "0xf7cD89BE08Af4D4D6B1522852ceD49FC10169f64"

# RewardSponsored 事件签名（已从链上交易确认，64 hex 字符）
REWARD_EVENT_TOPIC = "0xa0e1f8e6fb6dd49d885fabbf89adb64c0ef2b16b2786c92d6851742572fb1d14"

# 轮询间隔（秒）
POLL_INTERVAL = 30

# Etherscan V2 API（chainid=137 = Polygon 主网）
ETHERSCAN_API_URL = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID  = "137"

# CLOB API（精确查询市场信息）
CLOB_API_URL = "https://clob.polymarket.com/markets"

# 去重持久化文件（重启后不重复推送）
SEEN_KEYS_FILE = "reward_seen_keys.json"

# UTC+8 时区
TZ_UTC8 = timezone(timedelta(hours=8))

# 上次扫描的区块号
last_block = 0


# ======================================================
# 💾 去重持久化（重启后不重复推送）
# ======================================================
def load_seen_keys() -> set:
    """从文件加载已推送的事件唯一键"""
    if os.path.exists(SEEN_KEYS_FILE):
        try:
            with open(SEEN_KEYS_FILE, "r") as f:
                data = json.load(f)
                return set(data.get("keys", []))
        except:
            pass
    return set()


def save_seen_keys(seen: set):
    """持久化已推送的事件唯一键"""
    try:
        with open(SEEN_KEYS_FILE, "w") as f:
            json.dump({"keys": list(seen)}, f)
    except Exception as e:
        print(f"   ⚠️ 保存去重文件失败: {e}")


# ======================================================
# 📡 Telegram 推送
# ======================================================
def send_telegram(message: str):
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
            print(f"   ⚠️ Telegram 推送失败: {resp.status_code} {resp.text[:120]}")
    except Exception as e:
        print(f"   ❌ Telegram 推送异常: {e}")


# ======================================================
# 🔍 Etherscan V2 工具函数
# ======================================================
def get_latest_block() -> int:
    params = {
        "chainid": POLYGON_CHAIN_ID,
        "module":  "proxy",
        "action":  "eth_blockNumber",
        "apikey":  POLYGONSCAN_API_KEY,
    }
    try:
        resp = requests.get(ETHERSCAN_API_URL, params=params, timeout=10)
        result = resp.json().get("result", "0x0")
        return int(result, 16)
    except Exception as e:
        print(f"   ❌ 获取最新区块失败: {e}")
        return 0


def fetch_reward_events(from_block: int) -> list:
    """
    查询奖励合约发出的 RewardSponsored 事件。
    直接监控奖励合约地址 + 事件签名，无需额外过滤。
    """
    params = {
        "chainid":   POLYGON_CHAIN_ID,
        "module":    "logs",
        "action":    "getLogs",
        "address":   REWARD_CONTRACT,
        "fromBlock": str(from_block),
        "toBlock":   "latest",
        "topic0":    REWARD_EVENT_TOPIC,
        "apikey":    POLYGONSCAN_API_KEY,
    }
    try:
        resp = requests.get(ETHERSCAN_API_URL, params=params, timeout=15)
        data = resp.json()
        if data.get("status") == "1":
            return data.get("result", [])
        elif data.get("message") == "No records found":
            return []
        else:
            msg = data.get("message", "Unknown")
            res = str(data.get("result", ""))[:120]
            print(f"   ⚠️ API 返回: {msg} - {res}")
            return []
    except Exception as e:
        print(f"   ❌ 查询奖励事件失败: {e}")
        return []


# ======================================================
# 🔍 解析事件数据
# ======================================================
def parse_reward_event(log: dict) -> dict:
    """
    解析 RewardSponsored 事件的完整参数。

    事件结构（已从链上交易确认）：
      topic[1] = conditionId (bytes32)
      topic[2] = sponsor 地址 (address, 左填充)
      data     = abi.encode(amount, startTime, endTime, maxSpread)
                 每个参数 32 字节（64 hex 字符）
    """
    topics = log.get("topics", [])
    raw_data = log.get("data", "0x")

    # conditionId: topic[1] 就是 bytes32，直接使用
    condition_id = topics[1] if len(topics) > 1 else ""

    # sponsor 地址: topic[2] 左填充，取后 40 字符
    sponsor = ""
    if len(topics) > 2:
        sponsor = "0x" + topics[2][-40:]

    # 解析 data（去掉 0x 前缀，清理换行符等非 hex 字符，每 64 字符一个 uint256）
    data_hex = raw_data[2:] if raw_data.startswith("0x") else raw_data
    # 清理所有非 hex 字符（换行、空格等）
    import re
    data_hex = re.sub(r'[^0-9a-fA-F]', '', data_hex)
    # 补齐到 4 * 64 = 256 字符
    data_hex = data_hex.zfill(256)
    # 取前 256 字符
    data_hex = data_hex[:256]

    amount_raw = int(data_hex[0:64],   16)  # USDC.e 6位小数
    start_time = int(data_hex[64:128], 16)  # unix timestamp
    end_time   = int(data_hex[128:192],16)  # unix timestamp
    # data[3] 是 maxSpread，但实际上 maxSpread 是市场本身的参数，从 CLOB API 读取更准确
    # 这里只保留 amount/startTime/endTime

    # 计算衍生值
    amount_usdc   = amount_raw / 1e6
    duration_secs = end_time - start_time if end_time > start_time else 0
    duration_days = duration_secs / 86400
    hourly_rate   = amount_usdc / (duration_secs / 3600) if duration_secs > 0 else 0

    return {
        "condition_id": condition_id,
        "sponsor":      sponsor,
        "amount_usdc":  amount_usdc,
        "start_time":   start_time,
        "end_time":     end_time,
        "duration_days": duration_days,
        "hourly_rate":  hourly_rate,
    }


# ======================================================
# 🔍 查询市场信息（CLOB API 精确查询）
# ======================================================
def get_market_info(condition_id: str) -> dict:
    """
    通过 condition_id 精确查询 CLOB API 获取市场信息，包含 token1/token2 和 max_spread。
    """
    if not condition_id:
        return {}
    try:
        url = f"{CLOB_API_URL}/{condition_id}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            d = resp.json()
            if d and "error" not in d:
                # 提取 token1/token2
                tokens = d.get("tokens", [])
                token1 = tokens[0].get("token_id", "") if len(tokens) > 0 else ""
                token2 = tokens[1].get("token_id", "") if len(tokens) > 1 else ""
                # 提取 rewards 信息（max_spread 是市场本身的参数）
                rewards = d.get("rewards", {})
                max_spread = rewards.get("max_spread", None)
                min_size   = rewards.get("min_size", None)
                return {
                    "question":     d.get("question", ""),
                    "market_slug":  d.get("market_slug", ""),
                    "condition_id": condition_id,
                    "token1":       token1,
                    "token2":       token2,
                    "max_spread":   max_spread,
                    "min_size":     min_size,
                }
    except Exception as e:
        print(f"   ⚠️ CLOB API 查询失败: {e}")
    return {}


# ======================================================
# 📝 写入 Google 表格 New Rewards Alert
# ======================================================
# 最小赞助金额阈值（USDC）- 写入表格
MIN_REWARD_AMOUNT = 800.0
# 最小 Telegram 推送阈值（USDC）
MIN_TELEGRAM_AMOUNT = 800.0
# 奖励巡检间隔（秒）- 定期检查已写入表格的市场奖励是否仍有效
REWARD_CHECK_INTERVAL = 1800  # 30 分钟
# 奖励失效清除的最低金额阈值（低于此值则从表格中移除）
MIN_REWARD_TO_KEEP = 800.0
# 表格名称（独立工作表，与 New Rewards Alert 区分）
NEW_REWARDS_SHEET_NAME = "Chain Rewards Alert"


def write_to_new_rewards_sheet(parsed: dict, market_info: dict):
    """
    当赞助金额 >= MIN_REWARD_AMOUNT 时，将市场信息追加写入 Google 表格。
    同一 condition_id 不重复写入。
    """
    if not GOOGLE_SHEETS_ENABLED:
        return

    amount_usdc  = parsed.get("amount_usdc", 0)
    condition_id = parsed.get("condition_id", "")

    if amount_usdc < MIN_REWARD_AMOUNT:
        return  # 金额不足，跳过

    if not condition_id:
        return

    try:
        sh = get_spreadsheet()

        # 如果工作表不存在则自动创建
        try:
            wk = sh.worksheet(NEW_REWARDS_SHEET_NAME)
        except Exception:
            print(f"   📋 [表格] 工作表 '{NEW_REWARDS_SHEET_NAME}' 不存在，正在创建...")
            wk = sh.add_worksheet(title=NEW_REWARDS_SHEET_NAME, rows=1000, cols=15)
            # 写入表头
            headers_row = [
                "detected_at", "question", "amount_usdc", "duration_days",
                "hourly_rate", "max_spread_c", "condition_id",
                "token1", "token2", "market_slug"
            ]
            wk.append_row(headers_row, value_input_option="RAW")
            print(f"   ✅ [表格] 工作表已创建并写入表头")

        # 读取现有数据，检查是否已存在该 condition_id（去重）
        existing_data = wk.get_all_values()
        if existing_data:
            # 找到 condition_id 列的索引（第一行为表头）
            headers = existing_data[0] if existing_data else []
            try:
                cid_col_idx = headers.index("condition_id")
                existing_cids = {row[cid_col_idx] for row in existing_data[1:] if len(row) > cid_col_idx}
            except ValueError:
                existing_cids = set()

            if condition_id in existing_cids:
                print(f"   ℹ️ [表格] condition_id 已存在，跳过写入: {condition_id[:20]}...")
                return

        # 准备写入数据
        detected_at  = datetime.now(tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S")
        question     = market_info.get("question", "")
        market_slug  = market_info.get("market_slug", "")
        token1       = market_info.get("token1", "")
        token2       = market_info.get("token2", "")
        duration_days = round(parsed.get("duration_days", 0), 2)
        hourly_rate  = round(parsed.get("hourly_rate", 0), 4)
        # max_spread 从 CLOB API 读取（市场本身的参数）
        max_spread_c = market_info.get("max_spread", None)
        if max_spread_c is not None:
            max_spread_c = round(float(max_spread_c), 1)
        else:
            max_spread_c = ""

        # 如果表格是空的（没有表头），先写入表头
        if not existing_data or not existing_data[0]:
            headers_row = [
                "detected_at", "question", "amount_usdc", "duration_days",
                "hourly_rate", "max_spread_c", "condition_id",
                "token1", "token2", "market_slug"
            ]
            wk.append_row(headers_row, value_input_option="RAW")

        # 写入数据行
        new_row = [
            detected_at,
            question,
            round(amount_usdc, 2),
            duration_days,
            hourly_rate,
            max_spread_c,
            condition_id,
            token1,
            token2,
            market_slug,
        ]
        wk.append_row(new_row, value_input_option="RAW")
        print(f"   ✅ [表格] 已写入 New Rewards Alert: {question[:40]}... (${amount_usdc:.2f})")

    except Exception as e:
        print(f"   ❌ [表格] 写入失败: {e}")
        import traceback
        traceback.print_exc()


# ======================================================
# 📢 格式化并推送通知
# ======================================================
def format_and_send(log: dict, parsed: dict, market_info: dict, tx_hash: str):
    block_number = int(log.get("blockNumber", "0x0"), 16)
    log_index    = int(log.get("logIndex", "0x0"), 16)
    block_ts     = int(log.get("blockTimestamp", "0x0"), 16)

    question    = market_info.get("question", "（市场信息未知）")
    market_slug = market_info.get("market_slug", "")
    # max_spread 从 CLOB API 读取（市场本身的参数，不是链上解析的）
    max_spread  = market_info.get("max_spread", None)
    max_spread_str = f"±{max_spread}c" if max_spread is not None else "未知"

    # 时间格式化
    now_str = datetime.now(tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    event_str = ""
    if block_ts > 0:
        event_str = datetime.fromtimestamp(block_ts, tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S UTC+8")

    start_str = ""
    end_str   = ""
    try:
        if 0 < parsed["start_time"] < 9999999999:
            start_str = datetime.fromtimestamp(parsed["start_time"], tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except Exception:
        pass
    try:
        if 0 < parsed["end_time"] < 9999999999:
            end_str = datetime.fromtimestamp(parsed["end_time"], tz=TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    except Exception:
        pass

    tx_url      = f"https://polygonscan.com/tx/{tx_hash}"
    market_url  = f"https://polymarket.com/market/{market_slug}" if market_slug else ""
    sponsor     = parsed["sponsor"]
    sponsor_url = f"https://polygonscan.com/address/{sponsor}" if sponsor else ""

    # 构建推送消息
    msg = (
        f"🎯 <b>新 LP 奖励赞助: {parsed['amount_usdc']:.2f} USDC.e</b>\n\n"
        f"📌 市场: {question}\n"
    )
    if market_url:
        msg += f"🔗 <a href='{market_url}'>查看市场</a>\n"

    msg += (
        f"\n💰 赞助金额: <b>{parsed['amount_usdc']:.2f} USDC.e</b>\n"
        f"⏰ 事件时间: {event_str}\n"
        f"🕐 生效时间: {start_str}\n"
        f"🕑 结束时间: {end_str}\n"
        f"📅 生效周期: {parsed['duration_days']:.2f} 天\n"
        f"💵 每小时发放: {parsed['hourly_rate']:.2f} USDC.e/hr\n"
        f"📊 挂单范围: midpoint {max_spread_str}\n"
    )
    if sponsor_url:
        msg += f"👤 Sponsor: <a href='{sponsor_url}'>{sponsor[:10]}...{sponsor[-6:]}</a>\n"

    msg += (
        f"\n📋 <a href='{tx_url}'>查看交易</a>\n"
        f"📤 推送时间: {now_str}\n"
        f"🔢 区块: {block_number} | logIndex: {log_index}"
    )

    # 控制台输出
    print(f"\n{'='*60}")
    print(f"🎯 新 LP 奖励赞助!")
    print(f"   市场: {question[:55]}")
    print(f"   金额: {parsed['amount_usdc']:.2f} USDC.e")
    print(f"   周期: {parsed['duration_days']:.2f} 天")
    print(f"   每小时: {parsed['hourly_rate']:.2f} USDC.e/hr")
    print(f"   范围: midpoint {max_spread_str}")
    print(f"   TX:   {tx_hash}")
    print(f"{'='*60}")

    send_telegram(msg)


# ======================================================
# 🔍 定期巡检：检查已写入表格的市场奖励是否仍有效
# ======================================================
def check_rewards_still_valid():
    """
    遍历 Chain Rewards Alert 表格中的所有市场，
    调用 CLOB API 检查当前奖励是否仍有效且 >= MIN_REWARD_TO_KEEP。
    如果奖励已过期或金额不足，从表格中删除该行并推送 Telegram 通知。
    """
    if not GOOGLE_SHEETS_ENABLED:
        return

    print(f"\n{'='*60}")
    print(f"🔍 [奖励巡检] 开始检查已有市场的奖励有效性...")
    print(f"{'='*60}")

    try:
        sh = get_spreadsheet()
        try:
            wk = sh.worksheet(NEW_REWARDS_SHEET_NAME)
        except Exception:
            print(f"   ℹ️ 工作表 '{NEW_REWARDS_SHEET_NAME}' 不存在，跳过巡检")
            return

        all_data = wk.get_all_values()
        if not all_data or len(all_data) <= 1:
            print(f"   ℹ️ 表格为空或只有表头，跳过巡检")
            return

        headers = all_data[0]
        # 找到关键列的索引
        try:
            cid_col = headers.index("condition_id")
        except ValueError:
            print(f"   ⚠️ 表格中未找到 condition_id 列，跳过巡检")
            return

        try:
            question_col = headers.index("question")
        except ValueError:
            question_col = None

        try:
            amount_col = headers.index("amount_usdc")
        except ValueError:
            amount_col = None

        # 从最后一行开始检查（倒序删除，避免行号偏移）
        rows_to_delete = []
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())

        for row_idx in range(len(all_data) - 1, 0, -1):  # 跳过表头（第0行）
            row = all_data[row_idx]
            if len(row) <= cid_col:
                continue

            condition_id = row[cid_col].strip()
            if not condition_id:
                continue

            question_str = row[question_col].strip() if question_col is not None and len(row) > question_col else "未知"
            old_amount = 0
            if amount_col is not None and len(row) > amount_col:
                try:
                    old_amount = float(row[amount_col])
                except:
                    old_amount = 0

            # 调用 CLOB API 查询当前奖励状态
            try:
                url = f"{CLOB_API_URL}/{condition_id}"
                resp = requests.get(url, timeout=10)
                if resp.status_code != 200:
                    print(f"   ⚠️ CLOB API 返回 {resp.status_code}，跳过: {question_str[:40]}...")
                    continue

                market_data = resp.json()
                if not market_data or "error" in market_data:
                    # 市场不存在或已关闭
                    rows_to_delete.append({
                        "row_num": row_idx + 1,  # Google Sheets 行号从1开始
                        "question": question_str,
                        "reason": "市场已关闭或不存在",
                        "old_amount": old_amount,
                    })
                    continue

                rewards = market_data.get("rewards", {})
                if not rewards:
                    # 没有奖励信息
                    rows_to_delete.append({
                        "row_num": row_idx + 1,
                        "question": question_str,
                        "reason": "奖励已取消（无 rewards 信息）",
                        "old_amount": old_amount,
                    })
                    continue

                # 检查奖励是否过期
                reward_end_time = rewards.get("end_date", None)
                if reward_end_time:
                    try:
                        # end_date 可能是 ISO 格式字符串或 unix timestamp
                        if isinstance(reward_end_time, str):
                            end_dt = datetime.fromisoformat(reward_end_time.replace("Z", "+00:00"))
                            end_ts = int(end_dt.timestamp())
                        else:
                            end_ts = int(reward_end_time)

                        if end_ts < now_ts:
                            rows_to_delete.append({
                                "row_num": row_idx + 1,
                                "question": question_str,
                                "reason": f"奖励已过期（结束时间: {datetime.fromtimestamp(end_ts, tz=TZ_UTC8).strftime('%Y-%m-%d %H:%M')}）",
                                "old_amount": old_amount,
                            })
                            continue
                    except Exception:
                        pass  # 解析失败，不删除

                # 检查当前奖励金额是否足够
                # CLOB API 的 rewards 可能包含 rates 或其他字段表示当前奖励水平
                # 尝试多种字段名
                current_amount = None
                for key in ["amount", "total_amount", "reward_amount"]:
                    if key in rewards:
                        try:
                            current_amount = float(rewards[key])
                            break
                        except:
                            pass

                # 如果有 rates 字段（每日/每小时费率），计算剩余总奖励
                if current_amount is None and "rates" in rewards:
                    rates = rewards["rates"]
                    if isinstance(rates, list) and rates:
                        # 取第一个 rate
                        try:
                            rate_info = rates[0]
                            daily_rate = float(rate_info.get("daily_rate", 0))
                            if daily_rate > 0 and reward_end_time:
                                remaining_days = max(0, (end_ts - now_ts) / 86400)
                                current_amount = daily_rate * remaining_days
                        except:
                            pass

                if current_amount is not None and current_amount < MIN_REWARD_TO_KEEP:
                    rows_to_delete.append({
                        "row_num": row_idx + 1,
                        "question": question_str,
                        "reason": f"奖励金额不足（当前: ${current_amount:.2f} < ${MIN_REWARD_TO_KEEP:.0f}）",
                        "old_amount": old_amount,
                    })
                    continue

                # 检查 active 状态
                if rewards.get("active") is False or rewards.get("is_active") is False:
                    rows_to_delete.append({
                        "row_num": row_idx + 1,
                        "question": question_str,
                        "reason": "奖励已停用（active=false）",
                        "old_amount": old_amount,
                    })
                    continue

            except requests.exceptions.Timeout:
                print(f"   ⚠️ CLOB API 超时，跳过: {question_str[:40]}...")
                continue
            except Exception as e:
                print(f"   ⚠️ 检查失败: {question_str[:40]}... ({e})")
                continue

            # API 调用间隔，避免限流
            time.sleep(0.5)

        # 执行删除（倒序，避免行号偏移）
        if rows_to_delete:
            print(f"\n   🗑️ 发现 {len(rows_to_delete)} 个需要清除的市场：")
            # 按行号倒序排列（从大到小删除）
            rows_to_delete.sort(key=lambda x: x["row_num"], reverse=True)

            removed_questions = []
            for item in rows_to_delete:
                row_num = item["row_num"]
                question = item["question"]
                reason = item["reason"]
                old_amount = item["old_amount"]

                print(f"      ❌ 行{row_num}: {question[:45]}... → {reason}")
                try:
                    wk.delete_rows(row_num)
                    removed_questions.append(f"• {question[:50]}... (原${old_amount:.0f}) → {reason}")
                except Exception as e:
                    print(f"      ⚠️ 删除行{row_num}失败: {e}")

            # 推送 Telegram 通知
            if removed_questions:
                msg = (
                    f"🗑️ <b>奖励巡检：{len(removed_questions)} 个市场已清除</b>\n\n"
                    + "\n".join(removed_questions)
                    + f"\n\n⏰ {datetime.now(tz=TZ_UTC8).strftime('%Y-%m-%d %H:%M:%S UTC+8')}"
                )
                send_telegram(msg)
        else:
            print(f"   ✅ 所有市场奖励仍有效，无需清除")

        remaining = len(all_data) - 1 - len(rows_to_delete)
        print(f"\n   📋 巡检完成，剩余 {remaining} 个市场")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"   ❌ [奖励巡检] 运行时错误: {e}")
        import traceback
        traceback.print_exc()


# ======================================================
# 🔄 主监控循环
# ======================================================
def monitor_rewards():
    global last_block

    print("=" * 60)
    print("🎯 Polymarket LP 奖励赞助监控 v2")
    print("=" * 60)
    print(f"   奖励合约: {REWARD_CONTRACT}")
    print(f"   事件签名: {REWARD_EVENT_TOPIC[:20]}...")
    print(f"   轮询间隔: {POLL_INTERVAL}s")
    print("=" * 60)

    # 加载已推送的事件键（持久化去重）
    seen_keys = load_seen_keys()
    print(f"   已加载 {len(seen_keys)} 个历史事件键（去重）")

    print("\n🔍 获取当前区块号...")
    last_block = get_latest_block()
    if last_block == 0:
        print("❌ 无法获取区块号，请检查 API Key")
        return

    print(f"   ✅ 当前区块: {last_block}")
    print(f"\n🚀 开始监控（从区块 {last_block} 开始）...")
    print("   按 Ctrl+C 停止\n")

    send_telegram(
        f"🚀 <b>LP 奖励监控已启动 v2</b>\n\n"
        f"奖励合约: <code>{REWARD_CONTRACT}</code>\n"
        f"起始区块: {last_block}\n"
        f"轮询间隔: {POLL_INTERVAL}s\n"
        f"历史去重: {len(seen_keys)} 条\n"
        f"时间: {datetime.now(tz=TZ_UTC8).strftime('%Y-%m-%d %H:%M:%S UTC+8')}"
    )

    scan_count = 0
    last_reward_check = 0  # 上次奖励巡检时间（unix timestamp）

    while True:
        try:
            scan_count += 1
            ts_str = datetime.now(tz=TZ_UTC8).strftime("%H:%M:%S")

            # ── 定期奖励巡检（每 REWARD_CHECK_INTERVAL 秒）──────────
            now_epoch = time.time()
            if now_epoch - last_reward_check >= REWARD_CHECK_INTERVAL:
                try:
                    check_rewards_still_valid()
                except Exception as e:
                    print(f"\n❌ [奖励巡检] 异常: {e}")
                last_reward_check = now_epoch

            logs = fetch_reward_events(last_block)

            new_count = 0
            max_seen_block = last_block  # 记录本轮扫描到的最大区块号

            for log in logs:
                tx_hash   = log.get("transactionHash", "")
                log_index = log.get("logIndex", "0x0")
                key       = f"{tx_hash}_{log_index}"

                # 记录本轮最大区块（无论是否处理）
                blk = int(log.get("blockNumber", "0x0"), 16)
                if blk > max_seen_block:
                    max_seen_block = blk

                # 去重检查
                if key in seen_keys:
                    continue

                # 解析事件参数
                parsed = parse_reward_event(log)

                # 查询市场信息（CLOB API 精确查询）
                condition_id = parsed["condition_id"]
                print(f"\n[{ts_str}] 🎯 发现新奖励事件! TX: {tx_hash[:22]}...")
                print(f"   conditionId: {condition_id[:20]}...")

                market_info = get_market_info(condition_id)
                if market_info.get("question"):
                    print(f"   📌 市场: {market_info['question'][:55]}")
                else:
                    print(f"   ⚠️ 市场信息未找到（可能已结束）")

                # 推送通知（金额 >= 100 USDC 时才推送 Telegram）
                if parsed["amount_usdc"] >= MIN_TELEGRAM_AMOUNT:
                    format_and_send(log, parsed, market_info, tx_hash)
                else:
                    # 仅控制台输出，不推送 Telegram
                    print(f"\n{'='*60}")
                    print(f"💤 奖励赞助（金额 < ${MIN_TELEGRAM_AMOUNT:.0f}，不推送）")
                    print(f"   市场: {market_info.get('question', '未知')[:55]}")
                    print(f"   金额: {parsed['amount_usdc']:.2f} USDC.e")
                    print(f"{'='*60}")

                # 写入 Google 表格（金额 >= 100 USDC 时）
                write_to_new_rewards_sheet(parsed, market_info)

                # 记录已推送（内存 + 持久化）
                seen_keys.add(key)
                save_seen_keys(seen_keys)
                new_count += 1

            # 处理完所有事件后，推进到本轮扫描到的最大区块
            # 注意：不直接跳到最新区块，避免漏掉处理期间产生的新事件
            if max_seen_block > last_block:
                last_block = max_seen_block

            status = f"新事件: {new_count}" if new_count > 0 else "无新事件"
            print(f"\r[{ts_str}] 扫描#{scan_count} | 区块:{last_block} | {status}   ", end="", flush=True)

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n\n{'='*60}")
            print(f"🛑 已停止 | 共扫描 {scan_count} 次 | 累计发现 {len(seen_keys)} 个事件")
            print(f"{'='*60}")
            send_telegram(
                f"🛑 <b>LP 奖励监控已停止</b>\n\n"
                f"共扫描: {scan_count} 次\n"
                f"累计事件: {len(seen_keys)} 个\n"
                f"时间: {datetime.now(tz=TZ_UTC8).strftime('%Y-%m-%d %H:%M:%S UTC+8')}"
            )
            break

        except Exception as e:
            print(f"\n❌ 运行时错误: {e}")
            import traceback
            traceback.print_exc()
            print(f"⏳ {POLL_INTERVAL}s 后继续...")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    monitor_rewards()
