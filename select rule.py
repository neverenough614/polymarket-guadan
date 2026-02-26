import os
import pandas as pd

# ===== 1. 基本配置 =====
INPUT_FILE = "Polymarket Admin - Template.xlsx"  # 你的导出文件名
SHEET_NAME = "All Markets"                       # 或者 "Volatility Markets"
OUTPUT_SELECTED = "Selected_Markets_auto.xlsx"   # 输出文件名

# 整体放大/缩小仓位用的倍数（你可以按自己资金调）
CAPITAL_MULTIPLIER = 1.0

# ===== 2. 读数据 =====
print(f"读取文件: {INPUT_FILE} / 工作表: {SHEET_NAME}")
print("当前工作目录(CWD):", os.getcwd())

df = pd.read_excel(INPUT_FILE, sheet_name=SHEET_NAME)

# 把一些容易出 NaN/inf 的列处理一下
df = df.copy()
df["volatilty/reward"] = pd.to_numeric(df["volatilty/reward"], errors="coerce")
df["neg_risk"] = df["neg_risk"].fillna(False)

# ===== 3. 按规则筛选市场 =====
mask = (
    (df["rewards_daily_rate"] >= 20)
    & (df["gm_reward_per_100"] >= 3)
    & (df["bid_reward_per_100"] >= 2)
    & (df["ask_reward_per_100"] >= 2)
    & (df["volatility_sum"].between(30, 200))
    & (df["volatilty/reward"].fillna(9999) <= 0.15)
    & (df["spread"].between(0.01, 0.15))
    & (df["neg_risk"] == False)
)

filtered = df[mask].copy()
print(f"筛选后市场数量: {len(filtered)}")

if filtered.empty:
    print("没有任何市场满足条件，停止输出。")
    print("select rule.py finished")
    raise SystemExit(0)

# 如果你想按奖励排序（优先级从高到低）
filtered = filtered.sort_values(
    by=["gm_reward_per_100", "rewards_daily_rate"],
    ascending=[False, False]
)

# ===== 4. 按奖励强度划分层级，分配 param_type =====
def classify_param_type(row):
    gm = row["gm_reward_per_100"]
    daily = row["rewards_daily_rate"]

    # Tier 1: 极高奖励 + 高风险偏好
    if gm >= 5 and daily >= 50:
        return "very"  # 对应 Hyperparameters 里的 very
    # Tier 2: 高奖励
    elif gm >= 3 and daily >= 20:
        return "high"
    # Tier 3: 中等奖励
    else:
        return "mid"

filtered["param_type"] = filtered.apply(classify_param_type, axis=1)

# ===== 5. 根据 min_size 和 param_type 生成 trade_size / max_size =====
def compute_sizes(row):
    """
    根据 min_size + param_type + CAPITAL_MULTIPLIER 计算下单规模。
    你可以按自己风险偏好调整系数。
    """
    base = float(row["min_size"])
    ptype = row["param_type"]

    # 这里是相对 min_size 的倍率（你可以自己改）
    if ptype == "very":
        trade_mult = 6.0
        max_mult = 10.0
    elif ptype == "high":
        trade_mult = 4.0
        max_mult = 7.0
    else:  # "mid"
        trade_mult = 2.0
        max_mult = 4.0

    trade_size = base * trade_mult * CAPITAL_MULTIPLIER
    max_size = base * max_mult * CAPITAL_MULTIPLIER

    return pd.Series({
        "trade_size": trade_size,
        "max_size": max_size,
    })

sizes = filtered.apply(compute_sizes, axis=1)
filtered[["trade_size", "max_size"]] = sizes

# ===== 6. 导出到 Excel =====
output_path = os.path.join(os.getcwd(), OUTPUT_SELECTED)
filtered.to_excel(output_path, index=False)

print(f"已导出筛选结果到: {output_path}")
print("select rule.py finished")