
import time
import os
import sys
import pandas as pd
import ctypes
import threading
from datetime import datetime

# 引入项目模块
from data_updater.trading_utils import get_clob_client
from data_updater.google_utils import get_spreadsheet

# ================= ⚙️ 精准防御配置 =================

# 1. 表格名称
WATCHLIST_SHEET_NAME = "Monitor Watchlist"

# 2. 触发防御的阈值（相对跌幅 - 与上一轮比较）
THRESHOLD_FRONT_DEPTH_DROP = 0.30    # 前面的墙单轮塌陷 > 30% -> 撤单
THRESHOLD_SAME_DEPTH_DROP = 0.50     # 同档位单轮被吃 > 50% -> 撤单

# 3. 高水位线阈值（相对跌幅 - 与历史最高比较，防温水煮蛙）
THRESHOLD_FRONT_HIGH_WATER_DROP = 0.50  # 前墙相比历史最高跌 > 50% -> 撤单
THRESHOLD_SAME_HIGH_WATER_DROP = 0.60   # 同档位相比历史最高跌 > 60% -> 撤单

# 4. 第一档安全阈值
MIN_SAME_DEPTH_SAFE = 50.0           # 第一档同档位深度低于 $50 -> 撤单（太显眼）
MIN_FRONT_DEPTH_THRESHOLD = 30.0     # 前面的墙低于此值视为"已在第一档"

# 5. 绝对兜底阈值（无论什么情况，低于这个值就撤）
MIN_FRONT_DEPTH_ABSOLUTE = 15.0      # 前墙低于 $15 且曾经 > $60 -> 直接撤单
MIN_FRONT_DEPTH_ABSOLUTE_REF = 60.0  # 配合上面使用：高水位线必须曾超过此值才触发绝对兜底

# 6. 扫描频率
CHECK_INTERVAL = 1

# 7. 防御开关
ENABLE_AUTO_DEFENSE = True

# 8. 表格重载间隔（秒）
WATCHLIST_RELOAD_INTERVAL = 60

# ========================================================

try:
    import winsound
except ImportError:
    winsound = None


# 声音 + 弹窗 (非阻塞)
def panic_alert(title_msg, body_msg):
    if winsound:
        for _ in range(3):
            winsound.Beep(1500, 200)
    else:
        print('\a')

    try:
        threading.Thread(target=show_popup, args=(title_msg, body_msg)).start()
    except:
        pass


def show_popup(title, content):
    try:
        ctypes.windll.user32.MessageBoxW(0, content, title, 0x40000 | 0x10)
    except:
        pass


class MarketState:
    def __init__(self, question, token_type):
        self.question = question
        self.token_type = token_type

        # 您的挂单价格
        self.my_bid_price = None
        self.my_ask_price = None

        # 分层深度监控（上一轮值）
        self.last_bid_front_depth = 0
        self.last_bid_same_depth = 0
        self.last_ask_front_depth = 0
        self.last_ask_same_depth = 0

        # 🆕 高水位线（历史最高，只升不降，防温水煮蛙）
        self.bid_front_high_water = 0
        self.bid_same_high_water = 0
        self.ask_front_high_water = 0
        self.ask_same_high_water = 0

        self.first_run = True

    def reset_high_water(self):
        """撤单后重置高水位线，下次挂单重新计算"""
        self.bid_front_high_water = 0
        self.bid_same_high_water = 0
        self.ask_front_high_water = 0
        self.ask_same_high_water = 0


# 获取您在该 token 的挂单价格
def get_my_order_prices(client, token_id):
    """
    返回: (最高买单价格, 最低卖单价格)
    如果没有挂单，返回 (None, None)
    """
    try:
        if hasattr(client, "get_orders"):
            try:
                orders = client.get_orders(open=True)
            except:
                orders = client.get_orders()
        else:
            orders = client.get_open_orders()

        # 筛选该 token 的订单
        my_orders = [o for o in orders if o.get('token_id') == token_id or o.get('asset_id') == token_id]

        if not my_orders:
            return None, None

        # 提取买单和卖单
        my_bids = [float(o['price']) for o in my_orders if o.get('side') == 'BUY']
        my_asks = [float(o['price']) for o in my_orders if o.get('side') == 'SELL']

        best_my_bid = max(my_bids) if my_bids else None
        best_my_ask = min(my_asks) if my_asks else None

        return best_my_bid, best_my_ask

    except Exception as e:
        print(f"⚠️ 获取挂单价格失败: {e}")
        return None, None


# 分层深度计算
def calculate_layered_depth(book, my_bid_price, my_ask_price):
    """
    返回: (bid_front, bid_same, ask_front, ask_same)

    bid_front: 价格 > my_bid_price 的买单深度（前面的墙）— 单位: 美金
    bid_same:  价格 == my_bid_price 的买单深度（同档位）— 单位: 美金
    ask_front: 价格 < my_ask_price 的卖单深度（前面的墙）— 单位: 美金
    ask_same:  价格 == my_ask_price 的卖单深度（同档位）— 单位: 美金
    """
    bid_front = 0
    bid_same = 0
    ask_front = 0
    ask_same = 0

    if not book.bids or not book.asks:
        return 0, 0, 0, 0

    # 买单分层
    if my_bid_price is not None:
        for bid in book.bids:
            price = float(bid.price)
            size = float(bid.size)
            depth = price * size

            if price > my_bid_price + 0.001:
                bid_front += depth
            elif abs(price - my_bid_price) < 0.001:
                bid_same += depth

    # 卖单分层
    if my_ask_price is not None:
        for ask in book.asks:
            price = float(ask.price)
            size = float(ask.size)
            depth = price * size

            if price < my_ask_price - 0.001:
                ask_front += depth
            elif abs(price - my_ask_price) < 0.001:
                ask_same += depth

    return bid_front, bid_same, ask_front, ask_same


# === [核心功能] 精准撤单 ===
def cancel_specific_token(client, token_id, question, token_type):
    print(f"\n🧨 正在对 [{question}] 执行精准撤单...")
    try:
        client.cancel_all(token_id=token_id)
        print(f"✅ 已成功撤销 {token_type} ({token_id[:10]}...) 的所有挂单。")
        return True
    except Exception as e:
        print(f"⚠️ 快速撤单失败 ({e})，尝试遍历撤单...")

        try:
            if hasattr(client, "get_orders"):
                try:
                    orders = client.get_orders(open=True)
                except:
                    orders = client.get_orders()
            else:
                orders = client.get_open_orders()

            target_orders = [o for o in orders if o.get('token_id') == token_id or o.get('asset_id') == token_id]

            if not target_orders:
                print(f"✅ 该 Token 下没有活跃挂单，无需撤销。")
                return True

            for o in target_orders:
                client.cancel(o['id'])
                print(f"   - 已撤销订单: {o['id']}")

            print(f"✅ 精准撤单完成。")
            return True

        except Exception as inner_e:
            print(f"❌ 撤单彻底失败: {inner_e}")
            return False


def load_watchlist():
    print(f"📥 正在读取表格: '{WATCHLIST_SHEET_NAME}' ...")
    try:
        sh = get_spreadsheet()
        wk = sh.worksheet(WATCHLIST_SHEET_NAME)
        df = pd.DataFrame(wk.get_all_records())
        watch_list = []
        if 'token1' not in df.columns:
            print(f"   ⚠️ 未找到 'token1' 列")
            return []

        for i, row in df.iterrows():
            q = str(row.get('question', 'Unknown')).strip()
            t1 = str(row.get('token1', '')).strip()
            if t1 and len(t1) > 10:
                watch_list.append({"id": t1, "type": "YES", "question": q})
            if 'token2' in df.columns:
                t2 = str(row.get('token2', '')).strip()
                if t2 and len(t2) > 10:
                    watch_list.append({"id": t2, "type": "NO ", "question": q})

        print(f"   ✅ 成功加载 {len(watch_list)} 个监控目标")
        return watch_list
    except Exception as e:
        print(f"❌ 读取表格失败: {e}")
        import traceback
        traceback.print_exc()
        return []


# ========== 🆕 核心检测函数（修复版） ==========
def check_bid_threats(state, my_bid_price, bid_front, bid_same):
    """
    检测买单方向的所有威胁，返回 (triggered, reasons) 列表
    修复：跨分支检测 + 高水位线 + 绝对兜底
    """
    reasons = []
    triggered = False

    if my_bid_price is None:
        return False, []

    was_behind_wall = state.last_bid_front_depth > MIN_FRONT_DEPTH_THRESHOLD
    now_exposed = bid_front <= MIN_FRONT_DEPTH_THRESHOLD

    # =============================================
    # 🔥 检测层 1：跨分支检测（最关键的修复）
    #    上一轮前面有墙 → 这一轮前墙消失
    # =============================================
    if was_behind_wall and now_exposed:
        drop_pct = (1 - bid_front / state.last_bid_front_depth) * 100 if state.last_bid_front_depth > 0 else 100
        reasons.append(
            f"🚨 [跨分支] 买单前墙消失！\n"
            f"   您的买单: ${my_bid_price:.2f}\n"
            f"   前面的墙: ${state.last_bid_front_depth:.0f} → ${bid_front:.0f} (-{drop_pct:.0f}%)\n"
            f"   ⚠️ 前墙从有到无，您即将暴露在第一档！"
        )
        triggered = True

    # =============================================
    # 🔥 检测层 2：绝对兜底
    #    前墙低于绝对最低值，且历史上曾经很厚
    # =============================================
    if (bid_front < MIN_FRONT_DEPTH_ABSOLUTE and
            state.bid_front_high_water > MIN_FRONT_DEPTH_ABSOLUTE_REF):
        reasons.append(
            f"🚨 [绝对兜底] 买单前墙极度危险！\n"
            f"   您的买单: ${my_bid_price:.2f}\n"
            f"   前墙当前: ${bid_front:.0f} (历史最高: ${state.bid_front_high_water:.0f})\n"
            f"   ⚠️ 前墙低于绝对安全线 ${MIN_FRONT_DEPTH_ABSOLUTE:.0f}！"
        )
        triggered = True

    # =============================================
    # 🔥 检测层 3：高水位线检测（防温水煮蛙）
    #    前墙相比历史最高跌幅超过阈值
    # =============================================
    if (state.bid_front_high_water > MIN_FRONT_DEPTH_THRESHOLD and
            bid_front < state.bid_front_high_water * (1 - THRESHOLD_FRONT_HIGH_WATER_DROP)):
        reasons.append(
            f"🚨 [高水位] 买单前墙累计大幅下跌！\n"
            f"   您的买单: ${my_bid_price:.2f}\n"
            f"   前墙高水位: ${state.bid_front_high_water:.0f} → 当前: ${bid_front:.0f} "
            f"(-{((1 - bid_front / state.bid_front_high_water) * 100):.0f}%)\n"
            f"   ⚠️ 可能被分批吃掉（温水煮蛙）！"
        )
        triggered = True

    # =============================================
    # 检测层 4：原有逻辑（单轮检测，分情况讨论）
    # =============================================
    if bid_front > MIN_FRONT_DEPTH_THRESHOLD:
        # 情况 A：不在第一档，监控前墙单轮塌陷
        if (state.last_bid_front_depth > MIN_FRONT_DEPTH_THRESHOLD and
                bid_front < state.last_bid_front_depth * (1 - THRESHOLD_FRONT_DEPTH_DROP)):
            reasons.append(
                f"🚨 [单轮] 买单前墙塌陷！\n"
                f"   您的买单: ${my_bid_price:.2f}\n"
                f"   前面的墙: ${state.last_bid_front_depth:.0f} → ${bid_front:.0f} "
                f"({((bid_front / state.last_bid_front_depth - 1) * 100):.0f}%)\n"
                f"   ⚠️ 前面的大单被吃掉了！"
            )
            triggered = True
    else:
        # 情况 B：在第一档，监控同档位深度
        if bid_same < MIN_SAME_DEPTH_SAFE:
            reasons.append(
                f"🚨 [第一档] 买单深度太薄！\n"
                f"   您的买单: ${my_bid_price:.2f}\n"
                f"   同档位深度: ${bid_same:.0f}\n"
                f"   ⚠️ 深度低于安全阈值 ${MIN_SAME_DEPTH_SAFE:.0f}，您太显眼！"
            )
            triggered = True

        elif (state.last_bid_same_depth > MIN_SAME_DEPTH_SAFE and
              bid_same < state.last_bid_same_depth * (1 - THRESHOLD_SAME_DEPTH_DROP)):
            reasons.append(
                f"🚨 [第一档] 买单被大量吃掉！\n"
                f"   您的买单: ${my_bid_price:.2f}\n"
                f"   同档深度: ${state.last_bid_same_depth:.0f} → ${bid_same:.0f} "
                f"({((bid_same / state.last_bid_same_depth - 1) * 100):.0f}%)\n"
                f"   ⚠️ 同档位订单被吃掉一半，您即将暴露！"
            )
            triggered = True

        # 🆕 同档位高水位线检测
        if (state.bid_same_high_water > MIN_SAME_DEPTH_SAFE and
                bid_same < state.bid_same_high_water * (1 - THRESHOLD_SAME_HIGH_WATER_DROP)):
            reasons.append(
                f"🚨 [高水位] 第一档买单累计被吃！\n"
                f"   您的买单: ${my_bid_price:.2f}\n"
                f"   同档高水位: ${state.bid_same_high_water:.0f} → 当前: ${bid_same:.0f} "
                f"(-{((1 - bid_same / state.bid_same_high_water) * 100):.0f}%)\n"
                f"   ⚠️ 同档位被分批消耗！"
            )
            triggered = True

    return triggered, reasons


def check_ask_threats(state, my_ask_price, ask_front, ask_same):
    """
    检测卖单方向的所有威胁，返回 (triggered, reasons) 列表
    逻辑与买单完全对称
    """
    reasons = []
    triggered = False

    if my_ask_price is None:
        return False, []

    was_behind_wall = state.last_ask_front_depth > MIN_FRONT_DEPTH_THRESHOLD
    now_exposed = ask_front <= MIN_FRONT_DEPTH_THRESHOLD

    # 检测层 1：跨分支检测
    if was_behind_wall and now_exposed:
        drop_pct = (1 - ask_front / state.last_ask_front_depth) * 100 if state.last_ask_front_depth > 0 else 100
        reasons.append(
            f"🚨 [跨分支] 卖单前墙消失！\n"
            f"   您的卖单: ${my_ask_price:.2f}\n"
            f"   前面的墙: ${state.last_ask_front_depth:.0f} → ${ask_front:.0f} (-{drop_pct:.0f}%)\n"
            f"   ⚠️ 前墙从有到无，您即将暴露在第一档！"
        )
        triggered = True

    # 检测层 2：绝对兜底
    if (ask_front < MIN_FRONT_DEPTH_ABSOLUTE and
            state.ask_front_high_water > MIN_FRONT_DEPTH_ABSOLUTE_REF):
        reasons.append(
            f"🚨 [绝对兜底] 卖单前墙极度危险！\n"
            f"   您的卖单: ${my_ask_price:.2f}\n"
            f"   前墙当前: ${ask_front:.0f} (历史最高: ${state.ask_front_high_water:.0f})\n"
            f"   ⚠️ 前墙低于绝对安全线 ${MIN_FRONT_DEPTH_ABSOLUTE:.0f}！"
        )
        triggered = True

    # 检测层 3：高水位线检测（防温水煮蛙）
    if (state.ask_front_high_water > MIN_FRONT_DEPTH_THRESHOLD and
            ask_front < state.ask_front_high_water * (1 - THRESHOLD_FRONT_HIGH_WATER_DROP)):
        reasons.append(
            f"🚨 [高水位] 卖单前墙累计大幅下跌！\n"
            f"   您的卖单: ${my_ask_price:.2f}\n"
            f"   前墙高水位: ${state.ask_front_high_water:.0f} → 当前: ${ask_front:.0f} "
            f"(-{((1 - ask_front / state.ask_front_high_water) * 100):.0f}%)\n"
            f"   ⚠️ 可能被分批吃掉（温水煮蛙）！"
        )
        triggered = True

    # 检测层 4：原有逻辑
    if ask_front > MIN_FRONT_DEPTH_THRESHOLD:
        if (state.last_ask_front_depth > MIN_FRONT_DEPTH_THRESHOLD and
                ask_front < state.last_ask_front_depth * (1 - THRESHOLD_FRONT_DEPTH_DROP)):
            reasons.append(
                f"🚨 [单轮] 卖单前墙塌陷！\n"
                f"   您的卖单: ${my_ask_price:.2f}\n"
                f"   前面的墙: ${state.last_ask_front_depth:.0f} → ${ask_front:.0f} "
                f"({((ask_front / state.last_ask_front_depth - 1) * 100):.0f}%)\n"
                f"   ⚠️ 前面的大单被吃掉了！"
            )
            triggered = True
    else:
        if ask_same < MIN_SAME_DEPTH_SAFE:
            reasons.append(
                f"🚨 [第一档] 卖单深度太薄！\n"
                f"   您的卖单: ${my_ask_price:.2f}\n"
                f"   同档位深度: ${ask_same:.0f}\n"
                f"   ⚠️ 深度低于安全阈值 ${MIN_SAME_DEPTH_SAFE:.0f}，您太显眼！"
            )
            triggered = True

        elif (state.last_ask_same_depth > MIN_SAME_DEPTH_SAFE and
              ask_same < state.last_ask_same_depth * (1 - THRESHOLD_SAME_DEPTH_DROP)):
            reasons.append(
                f"🚨 [第一档] 卖单被大量吃掉！\n"
                f"   您的卖单: ${my_ask_price:.2f}\n"
                f"   同档深度: ${state.last_ask_same_depth:.0f} → ${ask_same:.0f} "
                f"({((ask_same / state.last_ask_same_depth - 1) * 100):.0f}%)\n"
                f"   ⚠️ 同档位订单被吃掉一半，您即将暴露！"
            )
            triggered = True

        # 同档位高水位线检测
        if (state.ask_same_high_water > MIN_SAME_DEPTH_SAFE and
                ask_same < state.ask_same_high_water * (1 - THRESHOLD_SAME_HIGH_WATER_DROP)):
            reasons.append(
                f"🚨 [高水位] 第一档卖单累计被吃！\n"
                f"   您的卖单: ${my_ask_price:.2f}\n"
                f"   同档高水位: ${state.ask_same_high_water:.0f} → 当前: ${ask_same:.0f} "
                f"(-{((1 - ask_same / state.ask_same_high_water) * 100):.0f}%)\n"
                f"   ⚠️ 同档位被分批消耗！"
            )
            triggered = True

    return triggered, reasons


def monitor_targeted():
    print("=" * 70)
    print("🛡️  精准防御系统 v4.0 - 修复版（跨分支+高水位+绝对兜底）")
    print("=" * 70)

    print("🔌 正在连接 Polymarket CLOB...")
    try:
        client = get_clob_client()
        print("✅ CLOB 连接成功")
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        import traceback
        traceback.print_exc()
        return

    # 初始加载
    targets = load_watchlist()
    if not targets:
        print("⚠️ 监控列表为空，等待下次重载...")

    market_states = {}
    for t in targets:
        market_states[t['id']] = MarketState(t['question'], t['type'])

    print(f"\n🛡️  精准防御系统已启动")
    print(f"    ⚙️  自动防御: {ENABLE_AUTO_DEFENSE}")
    print(f"    🎯 监控目标: {len(targets)} 个")
    print(f"    🔄 表格自动重载: {WATCHLIST_RELOAD_INTERVAL}秒")
    print(f"    ⏱️  扫描间隔: {CHECK_INTERVAL}秒")
    print(f"\n    🔥 防御配置:")
    print(f"       - 前墙单轮塌陷阈值: {THRESHOLD_FRONT_DEPTH_DROP * 100:.0f}%")
    print(f"       - 同档位单轮被吃阈值: {THRESHOLD_SAME_DEPTH_DROP * 100:.0f}%")
    print(f"       - 前墙高水位跌幅阈值: {THRESHOLD_FRONT_HIGH_WATER_DROP * 100:.0f}%")
    print(f"       - 同档高水位跌幅阈值: {THRESHOLD_SAME_HIGH_WATER_DROP * 100:.0f}%")
    print(f"       - 第一档最小安全深度: ${MIN_SAME_DEPTH_SAFE:.0f}")
    print(f"       - 前墙绝对最低值: ${MIN_FRONT_DEPTH_ABSOLUTE:.0f} (需高水位 > ${MIN_FRONT_DEPTH_ABSOLUTE_REF:.0f})")
    print(f"\n    🆕 v4.0 修复内容:")
    print(f"       - ✅ 跨分支检测：前墙从有到无时立即撤单")
    print(f"       - ✅ 高水位线：防止温水煮蛙式分批消耗")
    print(f"       - ✅ 绝对兜底：前墙低于绝对值时无条件撤单")
    print("-" * 70)

    last_reload_time = time.time()
    scan_count = 0

    while True:
        try:
            # 检查是否需要重载表格
            current_time = time.time()
            if current_time - last_reload_time >= WATCHLIST_RELOAD_INTERVAL:
                print(f"\n\n{'=' * 70}")
                print(f"🔄 [{datetime.now().strftime('%H:%M:%S')}] 正在重载监控列表...")
                print(f"{'=' * 70}")

                new_targets = load_watchlist()

                if new_targets:
                    old_ids = set(t['id'] for t in targets)
                    new_ids = set(t['id'] for t in new_targets)

                    added = new_ids - old_ids
                    removed = old_ids - new_ids

                    if added or removed:
                        print(f"   📊 变化检测:")
                        print(f"      ➕ 新增: {len(added)} 个")
                        print(f"      ➖ 移除: {len(removed)} 个")

                        if added:
                            for tid in added:
                                t_info = next((t for t in new_targets if t['id'] == tid), None)
                                if t_info:
                                    print(f"         + [{t_info['type']}] {t_info['question'][:40]}...")

                        if removed:
                            for tid in removed:
                                t_info = next((t for t in targets if t['id'] == tid), None)
                                if t_info:
                                    print(f"         - [{t_info['type']}] {t_info['question'][:40]}...")

                        targets = new_targets

                        for t in targets:
                            if t['id'] not in market_states:
                                market_states[t['id']] = MarketState(t['question'], t['type'])

                        market_states = {k: v for k, v in market_states.items() if k in new_ids}

                        print(f"   ✅ 当前监控: {len(targets)} 个目标")
                    else:
                        print(f"   ✅ 无变化 (仍为 {len(targets)} 个目标)")
                else:
                    print("   ⚠️ 重载失败或列表为空，保持原有配置")

                last_reload_time = current_time
                print("-" * 70)

            # 监控逻辑
            timestamp = datetime.now().strftime("%H:%M:%S")
            scan_count += 1

            time_until_reload = int(WATCHLIST_RELOAD_INTERVAL - (current_time - last_reload_time))
            print(f"\r[ {timestamp} ] 🎯 扫描 #{scan_count} | 下次重载: {time_until_reload}秒", end="", flush=True)

            for t in targets:
                token_id = t['id']
                state = market_states[token_id]

                # 获取盘口数据
                try:
                    book = client.get_order_book(token_id)
                except:
                    continue

                # 获取您的挂单价格
                my_bid_price, my_ask_price = get_my_order_prices(client, token_id)

                # 如果没有挂单，跳过监控并重置状态
                if my_bid_price is None and my_ask_price is None:
                    if not state.first_run:
                        state.first_run = True
                        state.reset_high_water()
                    continue

                # 更新 state 里的价格
                state.my_bid_price = my_bid_price
                state.my_ask_price = my_ask_price

                # 分层深度计算
                bid_front, bid_same, ask_front, ask_same = calculate_layered_depth(
                    book, my_bid_price, my_ask_price
                )

                # 🆕 更新高水位线（只升不降）
                state.bid_front_high_water = max(state.bid_front_high_water, bid_front)
                state.bid_same_high_water = max(state.bid_same_high_water, bid_same)
                state.ask_front_high_water = max(state.ask_front_high_water, ask_front)
                state.ask_same_high_water = max(state.ask_same_high_water, ask_same)

                trigger_reasons = []
                triggered = False

                if not state.first_run:
                    # 🆕 调用修复后的检测函数
                    bid_triggered, bid_reasons = check_bid_threats(state, my_bid_price, bid_front, bid_same)
                    ask_triggered, ask_reasons = check_ask_threats(state, my_ask_price, ask_front, ask_same)

                    if bid_triggered:
                        triggered = True
                        trigger_reasons.extend(bid_reasons)
                    if ask_triggered:
                        triggered = True
                        trigger_reasons.extend(ask_reasons)

                # 更新上一轮状态（在检测之后）
                state.last_bid_front_depth = bid_front
                state.last_bid_same_depth = bid_same
                state.last_ask_front_depth = ask_front
                state.last_ask_same_depth = ask_same
                state.first_run = False

                # === 触发精准防御 ===
                if triggered:
                    print(f"\n\n{'!' * 20} ⚡ 检测到危险信号 ⚡ {'!' * 20}")
                    print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"🎯 目标: {state.question}")
                    print(f"🆔 Token: {state.token_type}")
                    print(f"📋 威胁详情 ({len(trigger_reasons)} 条):")
                    for i, reason in enumerate(trigger_reasons, 1):
                        print(f"\n  [{i}] {reason}")

                    if ENABLE_AUTO_DEFENSE:
                        cancel_specific_token(client, token_id, state.question, state.token_type)
                        panic_alert(
                            f"防御触发: {state.question[:10]}...",
                            "\n".join(trigger_reasons)
                        )
                        # 撤单后重置状态，等下次挂单重新建立基准线
                        state.first_run = True
                        state.reset_high_water()
                    else:
                        print("⚠️ 防御未开启，仅报警")
                        panic_alert("发现威胁 (未撤单)", "\n".join(trigger_reasons))

                    print("!" * 70)

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n" + "=" * 70)
            print("🛑 用户手动停止")
            print(f"📊 本次运行统计:")
            print(f"   - 总扫描次数: {scan_count}")
            print(f"   - 监控目标数: {len(targets)}")
            print("=" * 70)
            break
        except Exception as e:
            print(f"\n❌ 运行时错误: {e}")
            import traceback
            traceback.print_exc()
            print("⏳ 5秒后继续...")
            time.sleep(5)


if __name__ == "__main__":
    monitor_targeted()
