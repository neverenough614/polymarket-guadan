import time
import traceback
from data_updater.trading_utils import get_clob_client

def cancel_everything():
    """
    一键撤销账户内所有挂单 (Cancel All Open Orders)
    """
    try:
        print("🔌 正在连接 Polymarket CLOB...")
        client = get_clob_client()
        
        # 获取当前有多少挂单（仅为了显示日志，不影响撤单速度）
        # 注意：如果挂单非常多，这一步可能会稍微拖慢一点点速度，
        # 如果追求极致速度，可以注释掉下面这几行 "Open Orders Check"
        try:
            open_orders = client.get_open_orders()
            count = len(open_orders) if open_orders else 0
            print(f"📋 当前检测到活跃挂单: {count} 个")
            
            if count == 0:
                print("✅ 账户内没有活跃挂单。")
                return
        except Exception as e:
            print(f"⚠️ 获取挂单列表失败 (不影响后续撤单): {e}")

        # === 核心撤单动作 ===
        print("🔥 正在执行【全部撤单】指令...")
        resp = client.cancel_all()
        
        print("✅ 撤单指令已发送成功!")
        print(f"   Response: {resp}")
        
    except Exception as e:
        print(f"❌ 撤单失败! 错误信息:")
        print(str(e))
        traceback.print_exc()

if __name__ == "__main__":
    # 为了防止手滑误点，增加一个简单的确认
    # 如果你想要真正的“一键无确认”，请把下面这3行注释掉
    confirm = input("⚠️ 警告：这将撤销你账户里【所有】市场的挂单！\n确定要继续吗? (输入 y 确认): ")
    if confirm.lower() != 'y':
        print("已取消操作。")
        exit()
        
    cancel_everything()