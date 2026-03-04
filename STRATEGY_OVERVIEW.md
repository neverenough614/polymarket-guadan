## 策略与风控总览（当前版本）

> 本文件基于代码中的实际实现整理（`update_markets.py`、`main.py`），用于快速了解当前机器人在「筛选 → 挂单 → 防御 → 清仓」各环节的逻辑与关键参数。

---

## 一、市场筛选逻辑（`update_markets.py`）

### 1. 基础数据处理

- 使用 `get_all_markets / get_all_results / get_markets` 获取全市场信息与订单簿。
- 仅保留**有流动性奖励**的市场（`rewards_daily_rate > 0`）参与后续计算。
- 使用 `add_volatility_to_df(max_workers=15)` 计算 1h–30d 波动率：
  - 重点使用：`24_hour`、`7_day`、`14_day`，并定义：
    - `volatility_sum = 24_hour + 7_day + 14_day`
- 计算距离到期天数 `days_to_expiry`。
- `clean_and_prepare_data` 统一为数值型，并计算：
  - `rv_ratio = gm_reward_per_100 / (volatility_sum + 0.001)`
  - `mid_rv_ratio = mid_reward_per_100 / (volatility_sum + 0.001)`

### 2. Smart LP Strategy（宽口径总览）

- 目的：查漏补缺，宽口径地看所有「有奖励 + 合理点差」市场。
- 条件：
  - `0.005 <= spread <= 0.50`
  - `gm_reward_per_100 > 0.5`
- 排序：
  - 按 `rv_ratio` 降序。

### 3. Blue Ocean Strategy（蓝海，宽点差高息低波）

- 目的：寻找点差相对较宽但奖励高、波动相对可控的「蓝海」市场。
- 条件：
  - `0.06 < spread <= 0.10`
  - `gm_reward_per_100 > 1.5`
  - `volatility_sum < 30`
  - `days_to_expiry > 7` 或 `days_to_expiry == 0`（无到期/解析失败）
- 排序：
  - 按 `gm_reward_per_100` 降序。

### 4. Normal LP Strategy（正常稳健）

- 目的：**低爆点、低日波动、适中点差**，追求较稳健的 LP 奖励。
- 当前条件：
  - 点差：`0.01 <= spread <= 0.08`
  - 奖励：`mid_reward_per_100 >= 0.5`
  - 爆点：`burst_index <= 0.5`（如该列存在）
  - 日波动：`24_hour <= 25`（如该列存在）
  - 到期：`days_to_expiry > 7` 或 `days_to_expiry == 0`
- 排序：
  - 按 `mid_rv_ratio` 降序。

> 说明：点差条件相对蓝海更窄，配合 `burst_index` 与 `24_hour` 过滤高爆点/高波动市场，适合作为「主力稳健仓位」来源。

### 5. High Reward Aggressive（高奖励激进）

- 目的：在高奖励市场「刀口舔血」，允许更高波动，用防御和撤单保护。
- 条件：
  - 总奖励：`rewards_daily_rate >= 100`（每日奖励总额）
  - 奖励率：`gm_reward_per_100 >= 2.0`
  - 点差：`0.02 <= spread <= 0.12`
  - 到期：`days_to_expiry > 3` 或 `days_to_expiry == 0`
- 排序：
  - 按 `gm_reward_per_100` 降序。

> 当前版本对 `burst_index` / `24_hour` 不做硬过滤，而是依赖监控防御模块来保护尾部风险。

---

## 二、自动挂单逻辑（`main.py`）

### 1. 关键配置参数

- 策略来源表：
  - `STRATEGY_SHEET_NAME = "Normal LP Strategy"`
  - `AGGRESSIVE_SHEET_NAME = "High Reward Aggressive"`
- 深度阈值：
  - `DEPTH_THRESHOLD_TIER1 = 500.0`（第 1 档最小深度，USDC）
  - `DEPTH_THRESHOLD_TIER2 = 200.0`（第 2/3 档最小深度，USDC）
- 极端价格阈值：
  - `EXTREME_PRICE_THRESHOLD = 0.10`（YES < 10c 或 > 90c 视为极端价格）
- 重试与表格刷新：
  - `RETRY_INTERVAL = 300` 秒（深度不足重试间隔）
  - `SHEET_RELOAD_INTERVAL = 300` 秒（策略表重载间隔）
- 动态挂单量（分策略）：
  - Normal LP：
    - `NORMAL_SIZE_RATIO = 0.30`（占前三档总深度的 30%）
    - `NORMAL_MAX_ORDER_SIZE = 700.0`（最大 700 shares）
  - High Reward：
    - `AGGRESSIVE_SIZE_RATIO = 0.08`（占前三档总深度 8%）
    - `AGGRESSIVE_MAX_ORDER_SIZE = 300.0`
  - 默认值（兼容旧逻辑）：
    - `DYNAMIC_SIZE_RATIO = 0.10`
    - `MAX_ORDER_SIZE = 500.0`
- 档位连续性：
  - `MAX_LEVEL_GAP = 0.02`（任意相邻档位价差 > 2c 视为流动性不连续）

### 2. 策略表读取与 Token 解析

- 函数：`load_strategy_markets()`：
  - 从 Google Sheet 读取：
    - `Normal LP Strategy` → `source="Normal LP"`
    - `High Reward Aggressive` → `source="High Reward"`
  - 解析字段：
    - `token1` / `token2` → YES/NO token_id
    - `min_size`（最小挂单量）
    - `neg_risk`（是否为 neg risk 合约）
    - `max_spread`（允许的最大挂单偏离，中价 ± max_spread）
    - `volatility_sum`（用于后续动态挂单量折扣）
  - 对同一 token 去重，并合并更大的 `min_size`、更新 `max_spread`。

### 3. 挂单价格选择：`analyze_best_place_price_from_book`

- 输入：`book, side, max_spread, mid, order_size`
- 流程：
  1. 若第 1 档总深度 `< 100 USDC`，**整个市场直接跳过**。
  2. 检查前三档价格间距：
     - 任意相邻档价差 `> MAX_LEVEL_GAP(2c)` → 视为不连续，跳过。
  3. 遍历前三档：
     - 第 i 档深度：
       - 第 1 档：`depth >= DEPTH_THRESHOLD_TIER1 (=500 USDC)`
       - 第 2/3 档：`depth >= DEPTH_THRESHOLD_TIER2 (=200 USDC)`
     - 若深度不足 → 跳过该档。
     - 对第 1 档额外检查：**我的挂单价值 ≤ 该档深度的 1/3**
       - 若超过 → 跳过第 1 档，尝试第 2 档。
     - 若提供了 `max_spread` 和 `mid`：
       - 仅接受价格在 `[mid - max_spread, mid + max_spread]` 范围内。
  4. 返回第一档满足条件的 `(price, tier, depth)`，否则返回 `None`。

> 直观理解：只在深度足够、档位连续、挂单占比不过大的情况下，才允许挂在第 1 档（通常是买一/卖一）；否则优先跳到第 2/3 档。

### 4. 动态挂单量：`calculate_dynamic_size`

- 输入：`book, mid, min_size, volatility_sum, size_ratio, max_order_size`
- 逻辑：
  1. 分别计算买/卖前三档总深度（USDC）：
     - `top3_bid_depth`, `top3_ask_depth`
  2. 各方向目标挂单量：
     - `bid_target = top3_bid_depth * size_ratio / mid`
     - `ask_target = top3_ask_depth * size_ratio / mid`
  3. 取两者中较小或非零的那一个，避免某一边占比过大。
  4. 根据 `volatility_sum` 施加折扣：
     - `<= 10` → 因子 `1.0`
     - `> 10` → 线性衰减，最低到 `0.2`
  5. 若 `target_size < min_size` → `None`（深度不足，跳过该市场）。
  6. 否则返回 `round(min(target_size, max_order_size))`。

> 策略效果：深度越厚、波动越低，挂单量越大；反之减小挂单量，若小于最小奖励对应的 shares 则直接不挂。

### 5. 对单个 token 挂单：`place_order_for_token`

- 步骤：
  1. 读取 token 基本信息：`token_id / token_type / question / neg_risk / max_spread / volatility_sum / source`。
  2. 设定基础最小挂单量：
     - `base_min_size = max(100.0, raw_min_size)`。
  3. 调用 `get_orderbook_info` 获取一次订单簿和 `mid`。
  4. 根据 `source` 决定挂单量参数：
     - `Normal LP` → `NORMAL_SIZE_RATIO / NORMAL_MAX_ORDER_SIZE`
     - `High Reward` → `AGGRESSIVE_SIZE_RATIO / AGGRESSIVE_MAX_ORDER_SIZE`
  5. 调用 `calculate_dynamic_size` 得到 `order_size`：
     - 若为 `None` → 标记为深度不足，买卖均跳过。
  6. 调用 `is_extreme_price_market` 检测是否极端价格（YES < 10c 或 > 90c）。
  7. 分别调用 `analyze_best_place_price_from_book` 获取买档/卖档：
     - 若极端价格市场：要求买卖双向都满足条件，否则整个市场跳过。
  8. 最终根据得到的档位价格执行 `create_order`，记录挂单结果与档位。

---

## 三、监控防御逻辑（`monitor_defense_loop`）

### 1. 关键参数

- 深度/高水位相关：
  - `THRESHOLD_FRONT_DEPTH_DROP = 0.30`
  - `THRESHOLD_SAME_DEPTH_DROP = 0.50`
  - `THRESHOLD_FRONT_HIGH_WATER_DROP = 0.50`
  - `THRESHOLD_SAME_HIGH_WATER_DROP = 0.60`
  - `MIN_SAME_DEPTH_SAFE = 300.0`（同档安全深度，排除自己后）
  - `MIN_FRONT_DEPTH_THRESHOLD = 100.0`
  - `MIN_FRONT_DEPTH_ABSOLUTE = 100.0`
  - `MIN_FRONT_DEPTH_ABSOLUTE_REF = 0.0`
- 监控节奏：
  - `MONITOR_CHECK_INTERVAL = 2` 秒
- 是否启用自动防御、深度偏斜检测：
  - `ENABLE_AUTO_DEFENSE = True`
  - `ENABLE_IMBALANCE_DETECTION = True`

### 2. 整体流程

- `monitor_defense_loop(strategy_tokens)` 周期性执行：
  1. 使用 `get_all_my_orders_once` 一次性拉取所有挂单（含手动单），按 token_id 分组。
  2. 自动将有挂单但不在策略列表中的 token 作为 “MANUAL” 也纳入监控。
  3. 对所有有挂单的 token：
     - 并发获取订单簿 `get_all_order_books_concurrent`。
     - 结合我的挂单价格，计算：
       - 前墙深度 / 同档深度（买/卖）→ `calculate_layered_depth`。
     - 从同档深度中扣除自己的挂单价值，只看「别人的深度」。
  4. 针对每个 token，依次执行：
     - **极端价格孤单检测**：极端价格市场如果只挂了一边（买或卖），则撤掉孤立一边。
     - **买卖深度偏斜检测（imbalance）**：买卖深度高度不对称时，撤掉危险方向的单。
     - **前墙/同档深度跌幅检测**：包括单轮跌幅、高水位回撤、第一档被大量吃掉等多种触发条件。
  5. 若触发防御且 `ENABLE_AUTO_DEFENSE=True`：
     - 调用 `cancel_specific_token_monitor` 或 `cancel_one_side` 撤单；
     - 30 秒后尝试 `place_order_for_token` 重新挂单（若条件合适）。

> 效果：当别人的墙单塌陷、同档深度骤降或买卖深度严重不平衡时，优先撤单保护，避免在价格跳水/起飞时被“最后一刀”吃掉。

---

## 四、自动清仓逻辑（`auto_close_positions_task`）

### 1. 关键参数

- `POSITION_CHECK_INTERVAL = 5` 秒（检查持仓频率）
- `MIN_POSITION_TO_CLOSE = 5.0` shares（最小清仓触发阈值）
- `CLOSE_PRICE_OFFSET = 0.01`（当前：以 `best_bid - 1c` 价格清仓）

### 2. 当前流程

1. 每 `POSITION_CHECK_INTERVAL` 秒执行一次：
   - 从 `strategy_tokens` 构建 `token_map`，仅清仓策略内的 token。
   - 调用 `poly_client.get_all_positions()` 获取所有持仓。
2. 对于每个持仓：
   - 若 `size >= MIN_POSITION_TO_CLOSE` 且 `asset` 在 `token_map` 中：
     - 视为需要清仓的仓位（Normal LP + High Reward 策略产生的持仓）。
3. 对每个待清仓仓位：
   - 再次拉订单簿，获取 `best_bid`。
   - 以 `close_price = round(best_bid - CLOSE_PRICE_OFFSET, 2)` 挂出 SELL 限价单全部卖出。

> 特点：当前逻辑**偏激进**，会在发现持仓后迅速以「盘口买一减 1c」的方式砍仓，这也是你在实盘中经常看到「刚挂买一被吃，然后在 -1c/-2c 清掉」的根源之一。后续的任务会针对这一块做更精细、分情景的重构。

---

## 五、小结：风险收益视角下的目前状态

- **筛选层**已经引入了 `burst_index` 与 `24_hour` 控制 Normal LP 的爆点与日波动，高奖励策略则主要靠高奖励与防御。
- **挂单层**对档位与挂单量做了较多保护：
  - 深度阈值 + 档位连续性 + 第 1 档挂单占比上限；
  - 动态挂单量会在高波动市场自动缩小，甚至因深度不足直接跳过。
- **防御层**较为全面，重视：
  - 前墙塌陷、同档被吃、深度高水位回撤、买卖深度偏斜、极端价格孤单等异常。
- **清仓层**目前偏「止损保守但牺牲收益」，会在持仓刚出现时就以 `best_bid - 1c` 的价格清掉，这部分正是后续需要重点重构来提升整体风险收益比的关键模块之一。

本文件仅描述**当前实现**，后续所有策略与代码层优化（包括自动清仓重构、参数微调、结构重构等）都将以此为基线进行演化。

