# Penny-Up 自动建仓工具 — 设计 spec

日期：2026-06-08
状态：已与用户确认，待写实现计划

## 1. 目标

一个**独立、自包含**的命令行程序，针对用户指定的 Polymarket 市场，**单边 BUY 建仓**，始终把买单贴在「第一档队首」以求优先成交，同时**严格不超过价格上限**。建仓达到总目标量后自动停止。

非目标（明确不做）：做市/双边报价、卖单、策略表、评分、黑名单、奖励检查、热度防御、平仓、web 服务、监控面板。

## 2. 隔离要求（硬约束）

这个功能**绝不能影响**用户现在运行的主 bot 代码。

- **代码层**：全新独立文件夹 `penny_up_bot/`，**零 import 主 bot 任何模块**（不碰 `poly_data` / `execution` / `global_state` / `main.py` / `perform_trade` / `minimal_auto_order`）。只依赖已安装的 pip 包 `py_clob_client_v2` 及标准/通用库（`websockets`、`python-dotenv` 等）。
- **账户层**：工具使用**另一个 Polymarket 账户**（独立钱包/私钥），其 `.env` 填该号的 `PK` / `BROWSER_ADDRESS`，与主 bot 的 `.env` 完全分离。因为是不同钱包，订单分账户，账户层零干扰，无盘口抢单可能。
- **撤单纪律**：工具**只撤它自己记录的 order_id**，永不调用全局 `cancel_all()` / `cancel_all_asset()`。Ctrl+C 优雅退出也只撤自己挂过的单。

## 3. 核心行为规约（每个 token 独立运行）

记号：
- `tick` = 该市场最小价位（启动时按 token 查询，查不到默认 `0.01`）
- `best_comp` = **剔除我自己挂单后**的最高对手买价（competing best bid）
- `cap` = 用户为该 token 设定的硬上限买价
- `remaining` = `total_size − 已成交累计`

每次盘口变动后，对每个未完成 token 计算目标价并维护挂单：

| 盘口情况 | 动作 |
|---|---|
| 买盘没有对手（`best_comp` 不存在 / 只有我自己） | **撤掉自己的单，等待**（不主动开仓） |
| `best_comp + tick > cap`（对手已顶到/超过上限） | **撤单等待** |
| 否则 | 维护一张 BUY 单，价格 = `best_comp + tick`（**只比对手高一个 tick**） |

要点：
- **价格上下都跟**：目标价始终精确等于 `best_comp + tick`。对手撤单/降价 → 我跟着降；被人超 → 我升回去。
- **挂单量** = `remaining`（剩余未成交目标）。
- **完成条件**：已成交累计 ≥ `total_size`，或 `remaining` < 市场最小下单量 → 撤掉该 token 的单，标记 `done`。
- **硬上限**：构建订单那一层强制 `price ≤ cap`，任何会越界的价格直接不挂（fail-closed）。

### 核心纯函数（TDD 重点）

```
compute_target(best_comp: float | None, tick: float, cap: float) -> float | None
  - best_comp is None            -> None   （无对手，撤单等待）
  - best_comp + tick > cap       -> None   （顶到上限，撤单等待）
  - else                         -> round_to_tick(best_comp + tick)
```

边界用例：`best_comp + tick == cap` → 允许（正好等于上限）；`best_comp == cap` → None（已达上限）；`best_comp + tick` 略超 → None。

## 4. 模块结构（全部位于 `penny_up_bot/`）

```
penny_up_bot/
├── .env.example       # PK / BROWSER_ADDRESS（另一个号）/ DRY_RUN
├── README.md          # 用法
├── config.py          # TOKENS 配置 + 全局参数
├── resolver.py        # 市场标识 + YES/NO -> token_id（调 CLOB get_market / Gamma API）
├── client.py          # 封装 py_clob_client_v2 初始化 + 下单/撤单/查 tick/查持仓
├── book_state.py      # 每 token 的 bids/asks（SortedDict），由市场 ws 更新
├── position_state.py  # 每 token：当前挂单(id/price/size) + 已成交累计 + done 标志
├── quoting.py         # compute_target() 等纯函数（核心，TDD）
├── executor.py        # cancel+place 原子化，per-token 锁 + in-flight 防重挂 + 去抖
├── market_ws.py       # 订阅 assets，更新盘口，触发 requote
├── user_ws.py         # 订阅成交，扣减剩余量，满了停
├── reconcile.py       # REST 兜底：周期同步活跃单 + 持仓，纠正漂移
├── run.py             # asyncio 编排：启动两条 ws + reconcile + 优雅退出
└── tests/
    ├── test_quoting.py        # compute_target 全分支
    ├── test_best_comp.py      # 剔除自己算 best_comp
    └── test_fill_accounting.py# 成交记账/剩余量
```

依赖关系：`run` 编排一切；`market_ws`/`user_ws`/`reconcile` 写 `book_state`/`position_state`；`executor` 读两者 + 调 `client`；`quoting` 是无依赖纯函数；`resolver`/`client` 只依赖 pip 包。

## 5. 配置 schema（方向用「市场 + YES/NO」自动解析）

```python
# config.py
TOKENS = [
    {
        # 二选一指定市场：condition_id 或 slug
        "condition_id": "0x...",        # 或
        "slug": "will-x-happen-by-2026",
        "outcome": "YES",               # "YES" 或 "NO"，工具解析成 token_id
        # 或者直接给 token_id（可选，给了就跳过解析）：
        # "token_id": "7291...4f3",
        "cap_price": 0.65,              # 硬上限买价
        "total_size": 1000.0,           # 总目标建仓量（shares）
        "neg_risk": False,
    },
]

# 全局参数（带默认值，可改）
REQUOTE_MIN_INTERVAL_MS = 300   # 每 token 最小重挂间隔，去抖
DEFAULT_TICK = 0.01             # 查不到 tick 时的默认值
RECONCILE_INTERVAL_S = 10       # REST 兜底周期
DRY_RUN = True                  # 默认只打印不下单（由 .env 覆盖）
```

`resolver.py`：用 `condition_id`（CLOB `get_market`）或 `slug`（Gamma API `/markets?slug=`）取市场，得到 `tokens: [{token_id, outcome}]`，把 `outcome`（YES/NO，大小写/Yes-No 归一）映射到 `token_id`。解析不出、市场找不到、或 outcome 不匹配 → **fail-fast 不启动**。

### 启动确认（含 DRY_RUN）

每个 token 启动时打印一行供肉眼核对方向：
```
市场: Will X happen by 2026?   买入方向: YES   token_id: 7291...4f3
上限价: 0.65   目标量: 1000 shares   tick: 0.01   DRY_RUN: true
```

## 6. 并发安全（针对历史「重复挂单 / 补位竞态」bug 重点防御）

- **每 token 一把 `asyncio.Lock`** + **in-flight 标志**：同一 token 任一时刻只允许一个「撤+挂」事务在途，杜绝出现两张活跃单。
- **去抖**：每 token 最小重挂间隔 `REQUOTE_MIN_INTERVAL_MS`（默认 300ms），盘口闪烁不狂刷，避免触发限频。
- **空操作短路**：仅当目标价 ≠ 当前挂单价（或当前无单而应有单 / 有单而应撤）时才动作。
- **撤+挂顺序**：先撤旧单确认成功，再挂新单；任何路径都保证同 token 至多一张 live 单。

## 7. 成交记账（双保险）

- **主**：user websocket 的 `order`(`size_matched`) / `trade` 事件实时累加该 token 成交量，扣减 `remaining`。
- **兜底**：`reconcile.py` 每 `RECONCILE_INTERVAL_S` 调 REST 拉持仓 + 活跃单，纠正 ws 丢包/漏算导致的漂移；也用于启动时接管/撤掉历史遗留单。
- `remaining ≤ 最小下单量` → 标记该 token `done`，撤单。

## 8. 安全 / 兜底

- `DRY_RUN`（默认 true）：只打印将要执行的「撤 / 挂」动作，不真下单。
- ws 断线自动重连（sleep 5s 重连模式）。
- 优雅退出（SIGINT/Ctrl+C）：只撤本工具记录的 order_id，然后退出。
- 所有 token `done` → 程序自动结束。
- 输入校验：`cap_price ∈ (0,1)`、`total_size > 0`、outcome ∈ {YES,NO}、市场可解析；任一不过 fail-fast。

## 9. 测试

TDD 重点放在纯逻辑：
- `test_quoting.py`：`compute_target` 全分支（无对手→None、顶到上限→None、正常→+1tick、`best_comp+tick==cap` 边界、tick=0.001 市场）。
- `test_best_comp.py`：从含我方挂单的盘口里剔除自己、算出对手最高买价（含「该价位只有我」→ 该价位不计）。
- `test_fill_accounting.py`：order/trade 事件累加、remaining 计算、done 判定。
- DRY_RUN 冒烟：跑一轮打印预期动作，不下单。

## 10. 已确认的关键决策（出处）

1. 单边 **BUY** 建仓（只买）。
2. **penny-up**：贴 `best_comp + tick`，只超对手一个价位。
3. 到顶（`best_comp + tick > cap`）→ **撤单等待**，不在高位挂。
4. 停止条件：**总目标量**，挂单量=剩余，满了停。
5. 盯盘：**WebSocket 实时**。
6. **多市场并行**。
7. 价格**上下都跟**（始终只超对手一个 tick，对手撤了也跟着降）。
8. 无对手时 → **暂不挂单等对手出现**。
9. 隔离：**新文件夹 `penny_up_bot/` + 另一个账户**，零 import 主 bot，只撤自己的单。
10. 方向：配置填**市场 + YES/NO**，工具自动解析 token_id（也兼容直接给 token_id）。

## 11. 默认参数（可改）

- 最小重挂间隔：300ms
- 默认 tick：0.01
- reconcile 周期：10s
- DRY_RUN 默认：true
