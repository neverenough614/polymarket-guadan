import time
import pandas as pd
from data_updater.trading_utils import get_clob_client

def calculate_total_exposure():
    """
    计算当前账户所有挂单的总金额 (Exposure)
    分为：
    1. 买单占用 (Locked USDC): 你的 Bid 挂单占用的资金
    2. 卖单价值 (Sell Value): 你的 Ask 挂单对应的当前名义价值
    """
    try:
        print("🔌 正在连接 Polymarket CLOB...")
        client = get_clob_client()
        
        print("📥 正在拉取所有活跃挂单...")
        
        # 获取挂单列表 (处理分页逻辑，确保挂单多的时候也能全抓到)
        all_orders = []
        cursor = ""
        while True:
            try:
                # 注意：不同版本的 client 参数可能不同，这里尝试通用写法
                # 如果你的 client 不支持 next_cursor 参数，通常它会自动返回全部或前100个
                resp = client.get_open_orders(next_cursor=cursor)
                
                # 兼容返回结构：可能是直接的 list，也可能是包含 'data' 的 dict
                if isinstance(resp, list):
                    orders = resp
                    next_cursor = None
                elif isinstance(resp, dict):
                    orders = resp.get('data', [])
                    next_cursor = resp.get('next_cursor')
                else:
                    orders = []
                    next_cursor = None
                
                all_orders.extend(orders)
                
                if not next_cursor or next_cursor == "null":
                    break
                cursor = next_cursor
            except TypeError:
                # 如果 get_open_orders 不接受 cursor 参数，说明是旧版 client，直接一次性获取
                all_orders = client.get_open_orders()
                break

        if not all_orders:
            print("\n✅ 当前没有活跃挂单 (No Open Orders).")
            return

        # === 开始计算 ===
        total_bid_exposure = 0.0  # 买单占用的 USDC
        total_ask_exposure = 0.0  # 卖单的名义价值
        bid_count = 0
        ask_count = 0
        
        print(f"\n📊 正在分析 {len(all_orders)} 个挂单...")
        print("-" * 40)
        print(f"{'Type':<6} | {'Price':<8} | {'Size':<10} | {'Value ($)':<10}")
        print("-" * 40)

        for order in all_orders:
            # 提取并转换数值
            side = order.get('side', '').upper()
            price = float(order.get('price', 0))
            # 剩余未成交的数量
            size = float(order.get('size', 0)) # 有些API是 'original_size' - 'size_matched'
            
            value = price * size
            
            # 打印详细列表 (前20个，避免刷屏)
            if bid_count + ask_count < 20:
                print(f"{side:<6} | {price:<8.2f} | {size:<10.1f} | {value:<10.2f}")

            if side == 'BUY':
                total_bid_exposure += value
                bid_count += 1
            elif side == 'SELL':
                total_ask_exposure += value
                ask_count += 1

        if len(all_orders) > 20:
            print(f"... (还有 {len(all_orders) - 20} 个挂单未显示)")

        print("-" * 40)
        print("\n💰 === 资金占用统计 (Exposure Summary) ===")
        print(f"🔹 活跃买单数 (Bids): {bid_count} 个")
        print(f"🔒 买单占用资金 (Locked USDC):  ${total_bid_exposure:,.2f}")
        print(f"   (这是你真正被挂单锁住的本金)")
        
        print(f"\n🔸 活跃卖单数 (Asks): {ask_count} 个")
        print(f"📄 卖单名义价值 (Sell Value):   ${total_ask_exposure:,.2f}")
        print(f"   (这是你正在出售的持仓当前价值)")
        
        print("\n🔥 总挂单名义价值 (Total Active): ${:,.2f}".format(total_bid_exposure + total_ask_exposure))
        print("===========================================")

    except Exception as e:
        print(f"❌ 计算失败: {e}")
        # import traceback
        # traceback.print_exc()

if __name__ == "__main__":
    calculate_total_exposure()