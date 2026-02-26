
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

# 2. 触发防御的阈值
THRESHOLD_FRONT_DEPTH_DROP = 0.30    # 前面的墙塌陷 > 30% -> 撤单
THRESHOLD_SAME_DEPTH_DROP = 0.50     # 同档位被吃 > 50% -> 撤单

# 3. 第一档安全阈值
MIN_SAME_DEPTH_SAFE = 50.0           # 第一档同档位深度低于 $50 -> 撤单（太显眼）
MIN_FRONT_DEPTH_THRESHOLD = 30.0     # 前面的墙最低深度（低于此值视为"已在第一档"）

# 4. 扫描频率
CHECK_INTERVAL = 1

# 5. 防御开关
ENABLE_AUTO_DEFENSE = True 

# 6. 表格重载间隔（秒）
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
        
        # 🆕 分层深度监控
        self.last_bid_front_depth = 0    # 比您更好的买单深度
        self.last_bid_same_depth = 0     # 和您同价的买单深度
        self.last_ask_front_depth = 0    # 比您更好的卖单深度
        self.last_ask_same_depth = 0     # 和您同价的卖单深度
        
        self.first_run = True

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

# 🆕 激进型：分层深度计算
def calculate_layered_depth(book, my_bid_price, my_ask_price):
    """
    返回: (bid_front, bid_same, ask_front, ask_same)
    
    bid_front: 价格 > my_bid_price 的买单深度（前面的墙）
    bid_same:  价格 == my_bid_price 的买单深度（同档位）
    ask_front: 价格 < my_ask_price 的卖单深度（前面的墙）
    ask_same:  价格 == my_ask_price 的卖单深度（同档位）
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
            
            if price > my_bid_price + 0.001:  # 比您更好（浮点数容差）
                bid_front += depth
            elif abs(price - my_bid_price) < 0.001:  # 和您同价
                bid_same += depth
    
    # 卖单分层
    if my_ask_price is not None:
        for ask in book.asks:
            price = float(ask.price)
            size = float(ask.size)
            depth = price * size
            
            if price < my_ask_price - 0.001:  # 比您更好
                ask_front += depth
            elif abs(price - my_ask_price) < 0.001:  # 和您同价
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
                try: orders = client.get_orders(open=True)
                except: orders = client.get_orders()
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
            if t1 and len(t1) > 10: watch_list.append({"id": t1, "type": "YES", "question": q})
            if 'token2' in df.columns:
                t2 = str(row.get('token2', '')).strip()
                if t2 and len(t2) > 10: watch_list.append({"id": t2, "type": "NO ", "question": q})
        
        print(f"   ✅ 成功加载 {len(watch_list)} 个监控目标")
        return watch_list
    except Exception as e:
        print(f"❌ 读取表格失败: {e}")
        import traceback
        traceback.print_exc()
        return []

def monitor_targeted():
    print("=" * 70)
    print("🛡️  精准防御系统 v3.0 - 激进型（支持第一档挂单）")
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
    print(f"\n    🔥 激进型配置:")
    print(f"       - ✅ 允许挂第一档（高收益）")
    print(f"       - 前面的墙塌陷阈值: {THRESHOLD_FRONT_DEPTH_DROP*100:.0f}%")
    print(f"       - 同档位被吃阈值: {THRESHOLD_SAME_DEPTH_DROP*100:.0f}%")
    print(f"       - 第一档最小安全深度: ${MIN_SAME_DEPTH_SAFE:.0f}")
    print("-" * 70)
    
    last_reload_time = time.time()
    scan_count = 0

    while True:
        try:
            # 检查是否需要重载表格
            current_time = time.time()
            if current_time - last_reload_time >= WATCHLIST_RELOAD_INTERVAL:
                print(f"\n\n{'='*70}")
                print(f"🔄 [{datetime.now().strftime('%H:%M:%S')}] 正在重载监控列表...")
                print(f"{'='*70}")
                
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
                
                # 如果没有挂单，跳过监控
                if my_bid_price is None and my_ask_price is None:
                    state.first_run = True
                    continue
                
                # 更新 state 里的价格
                state.my_bid_price = my_bid_price
                state.my_ask_price = my_ask_price

                # 🆕 分层深度计算
                bid_front, bid_same, ask_front, ask_same = calculate_layered_depth(
                    book, my_bid_price, my_ask_price
                )

                trigger_reason = []
                triggered = False

                if not state.first_run:
                    # ========== 🔥 买单激进型监控逻辑 ==========
                    if my_bid_price is not None:
                        # 情况1: 前面有墙（不在第一档）
                        if bid_front > MIN_FRONT_DEPTH_THRESHOLD:
                            # 监控前面的墙是否塌陷
                            if (state.last_bid_front_depth > MIN_FRONT_DEPTH_THRESHOLD and 
                                bid_front < state.last_bid_front_depth * (1 - THRESHOLD_FRONT_DEPTH_DROP)):
                                
                                trigger_reason.append(
                                    f"🚨 买单前墙塌陷！\n"
                                    f"   您的买单: ${my_bid_price:.2f}\n"
                                    f"   前面的墙: ${state.last_bid_front_depth:.0f} → ${bid_front:.0f} "
                                    f"({((bid_front/state.last_bid_front_depth-1)*100):.0f}%)\n"
                                    f"   ⚠️ 前面的大单被吃掉了！"
                                )
                                triggered = True
                        
                        # 情况2: 在第一档（前面没墙或墙很薄）
                        else:
                            # 监控同档位深度
                            if bid_same < MIN_SAME_DEPTH_SAFE:
                                trigger_reason.append(
                                    f"🚨 第一档买单深度太薄！\n"
                                    f"   您的买单: ${my_bid_price:.2f}\n"
                                    f"   同档位深度: ${bid_same:.0f}\n"
                                    f"   ⚠️ 深度低于安全阈值 ${MIN_SAME_DEPTH_SAFE:.0f}，您太显眼！"
                                )
                                triggered = True
                            
                            elif (state.last_bid_same_depth > MIN_SAME_DEPTH_SAFE and 
                                  bid_same < state.last_bid_same_depth * (1 - THRESHOLD_SAME_DEPTH_DROP)):
                                
                                trigger_reason.append(
                                    f"🚨 第一档买单被大量吃掉！\n"
                                    f"   您的买单: ${my_bid_price:.2f}\n"
                                    f"   同档深度: ${state.last_bid_same_depth:.0f} → ${bid_same:.0f} "
                                    f"({((bid_same/state.last_bid_same_depth-1)*100):.0f}%)\n"
                                    f"   ⚠️ 同档位订单被吃掉一半，您即将暴露！"
                                )
                                triggered = True
                    
                    # ========== 🔥 卖单激进型监控逻辑 ==========
                    if my_ask_price is not None:
                        # 情况1: 前面有墙（不在第一档）
                        if ask_front > MIN_FRONT_DEPTH_THRESHOLD:
                            if (state.last_ask_front_depth > MIN_FRONT_DEPTH_THRESHOLD and 
                                ask_front < state.last_ask_front_depth * (1 - THRESHOLD_FRONT_DEPTH_DROP)):
                                
                                trigger_reason.append(
                                    f"🚨 卖单前墙塌陷！\n"
                                    f"   您的卖单: ${my_ask_price:.2f}\n"
                                    f"   前面的墙: ${state.last_ask_front_depth:.0f} → ${ask_front:.0f} "
                                    f"({((ask_front/state.last_ask_front_depth-1)*100):.0f}%)\n"
                                    f"   ⚠️ 前面的大单被吃掉了！"
                                )
                                triggered = True
                        
                        # 情况2: 在第一档
                        else:
                            if ask_same < MIN_SAME_DEPTH_SAFE:
                                trigger_reason.append(
                                    f"🚨 第一档卖单深度太薄！\n"
                                    f"   您的卖单: ${my_ask_price:.2f}\n"
                                    f"   同档位深度: ${ask_same:.0f}\n"
                                    f"   ⚠️ 深度低于安全阈值 ${MIN_SAME_DEPTH_SAFE:.0f}，您太显眼！"
                                )
                                triggered = True
                            
                            elif (state.last_ask_same_depth > MIN_SAME_DEPTH_SAFE and 
                                  ask_same < state.last_ask_same_depth * (1 - THRESHOLD_SAME_DEPTH_DROP)):
                                
                                trigger_reason.append(
                                    f"🚨 第一档卖单被大量吃掉！\n"
                                    f"   您的卖单: ${my_ask_price:.2f}\n"
                                    f"   同档深度: ${state.last_ask_same_depth:.0f} → ${ask_same:.0f} "
                                    f"({((ask_same/state.last_ask_same_depth-1)*100):.0f}%)\n"
                                    f"   ⚠️ 同档位订单被吃掉一半，您即将暴露！"
                                )
                                triggered = True

                # 更新状态
                state.last_bid_front_depth = bid_front
                state.last_bid_same_depth = bid_same
                state.last_ask_front_depth = ask_front
                state.last_ask_same_depth = ask_same
                state.first_run = False

                # === 触发精准防御 ===
                if triggered:
                    print(f"\n\n{'!'*20} ⚡ 检测到危险信号 ⚡ {'!'*20}")
                    print(f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"🎯 目标: {state.question}")
                    print(f"🆔 Token: {state.token_type}")
                    print(f"📋 威胁详情:")
                    for reason in trigger_reason:
                        print(f"{reason}")
                    
                    if ENABLE_AUTO_DEFENSE:
                        cancel_specific_token(client, token_id, state.question, state.token_type)
                        panic_alert(f"激进防御触发: {state.question[:10]}...", "\n".join(trigger_reason))
                        state.first_run = True 
                    else:
                        print("⚠️ 防御未开启，仅报警")
                        panic_alert("发现威胁 (未撤单)", "\n".join(trigger_reason))
                    
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
